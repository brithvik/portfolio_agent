"""Sends the things you'd want on your phone to a Discord channel:

  - each daily briefing, as soon as the agent finishes writing it
  - each new trade proposal that is waiting for your decision

(Alerts are sent separately by Alertmanager; see k8s/notifications.yaml.)

It is deliberately boring and locked down:
- It only READS the agent's tables. The single thing it writes is its own bookmark
  (notifier_state), so a restart never re-sends old messages or skips new ones.
- Its only way out of the cluster is the notify proxy, which tunnels to discord.com and
  nothing else. The agent cannot reach that proxy, so this is not a way for the agent to talk
  to the internet: the notifier decides what is sent, and only to your own webhook.
- Briefing text is written by an LLM, so it is treated as untrusted: it is length-limited and
  sent with mentions disabled (it can't ping @everyone).
- With no DISCORD_WEBHOOK_URL in .env, it idles and says so. Nothing else depends on it.
"""
import os
import time
from zoneinfo import ZoneInfo

import psycopg
import requests

from schema import ensure_schema

WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"
POLL_SECONDS = int(os.environ.get("NOTIFY_POLL_SECONDS", "30"))
MAX_CHARS = 1900            # Discord rejects messages over 2000 characters
BATCH = 5                   # at most this many messages of each kind per pass
NEW_YORK = ZoneInfo("America/New_York")

SOURCES = {
    "briefing": """SELECT id, answer, finished_at FROM agent_runs
                   WHERE kind = 'briefing' AND status = 'done' AND id > %s ORDER BY id LIMIT %s""",
    "proposal": """SELECT id, symbol, side, notional, rationale, expires_at, read_untrusted
                   FROM trade_proposals WHERE status = 'pending' AND id > %s ORDER BY id LIMIT %s""",
}
NEWEST = {"briefing": "SELECT coalesce(max(id), 0) FROM agent_runs",
          "proposal": "SELECT coalesce(max(id), 0) FROM trade_proposals"}


def log(message):
    print(f"[notifier] {message}", flush=True)


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_briefing(row):
    _, answer, finished_at = row
    when = finished_at.astimezone(NEW_YORK).strftime("%a %b %d, %I:%M %p ET") if finished_at else ""
    header = f"**Portfolio briefing** ({when})\n"
    return header + clip(answer, MAX_CHARS - len(header))


def format_proposal(row):
    proposal_id, symbol, side, notional, rationale, expires_at, read_untrusted = row
    lines = [
        f"**Trade proposal #{proposal_id} needs your decision**",
        f"{side.upper()} ${float(notional):,.2f} of {symbol}",
        f"Agent's reason: {clip(rationale, 600)}",
        f"Expires {expires_at.astimezone(NEW_YORK).strftime('%I:%M %p ET')}. "
        "Nothing happens unless you approve it: run scripts\\gateway.ps1.",
    ]
    if read_untrusted:
        lines.insert(3, "Caution: the agent read news (untrusted text) before proposing this.")
    return clip("\n".join(lines), MAX_CHARS)


FORMATTERS = {"briefing": format_briefing, "proposal": format_proposal}


def send_to_discord(webhook_url, content):
    """Post one message. Raises on any failure, so the caller keeps its bookmark and retries."""
    response = requests.post(
        webhook_url, timeout=15,
        json={"content": content, "allowed_mentions": {"parse": []}})   # no @everyone, no pings
    if response.status_code >= 300:
        raise RuntimeError(f"Discord answered {response.status_code}")


def run_once(conn, send):
    """Send everything new since the bookmarks. Returns how many messages went out.

    The bookmark moves only after a message is sent, one message at a time. If the process
    dies in between, the worst case is one repeated message; nothing is ever skipped.
    """
    sent = 0
    for key, query in SOURCES.items():
        row = conn.execute("SELECT last_id FROM notifier_state WHERE key = %s", (key,)).fetchone()
        if row is None:
            # First start: begin from "now" instead of flooding the channel with history
            newest = conn.execute(NEWEST[key]).fetchone()[0]
            conn.execute("INSERT INTO notifier_state (key, last_id) VALUES (%s, %s) "
                         "ON CONFLICT (key) DO NOTHING", (key, newest))
            log(f"{key}: starting after id {newest}")
            continue
        for item in conn.execute(query, (row[0], BATCH)).fetchall():
            send(FORMATTERS[key](item))
            conn.execute("UPDATE notifier_state SET last_id = %s WHERE key = %s", (item[0], key))
            log(f"sent {key} {item[0]}")
            sent += 1
    return sent


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log("DISCORD_WEBHOOK_URL is not set, so notifications are off. Add it to .env and run "
            "up.ps1 to turn them on.")
        while True:
            time.sleep(3600)
    if not webhook_url.startswith(WEBHOOK_PREFIX):
        # Refuse anything that isn't a Discord webhook, so a typo can't send briefings elsewhere
        raise SystemExit(f"DISCORD_WEBHOOK_URL must start with {WEBHOOK_PREFIX}")

    database_url = os.environ["DATABASE_URL"]
    log(f"watching for briefings and proposals every {POLL_SECONDS}s")
    schema_ready = False
    while True:
        try:
            with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
                if not schema_ready:
                    ensure_schema(conn)
                    schema_ready = True
                run_once(conn, lambda content: send_to_discord(webhook_url, content))
        except Exception as e:   # database restarting, Discord down, rate limited: try again next pass
            log(f"will retry: {type(e).__name__}: {str(e)[:200]}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
