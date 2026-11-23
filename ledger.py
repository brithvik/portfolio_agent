"""The ledger: a permanent record of every idea, so the engine can be judged on what happened next.

  python ledger.py daily     # label today's news, record new ideas, print the review and the scoreboard
  python ledger.py record    # record new ideas from the labels already stored
  python ledger.py report    # the scoreboard only

What makes the record trustworthy:

- Written once. An idea is stored with what was known when it was made (rank, signals, flags,
  the labeled headlines) and is never edited or deleted. Outcomes are calculated from prices.
- Nothing is cherry-picked. Every one of the day's top candidates is recorded, including the
  ones the rules set aside, so we can also see whether setting them aside was right.
- Matched controls. Each idea gets random stocks drawn the same day from the same tier, with a
  fixed seed. "Did the ideas go up?" is easy to fool yourself with in a rising market; "did
  they beat random picks made the same day?" is not.
- Honest prices. An idea is priced from the next market open after it was recorded, never from
  the price before the news. A stock that stops trading keeps its last price, and the price
  job keeps following ledger stocks that fall out of the universe.
- One question decided in advance. The primary metric and the sample needed to answer it are
  in rules/ledger_v1.json, fixed before any outcome existed. Until there is enough data the
  scoreboard says so instead of showing a number.

No model and no brokerage access are needed for any of this: it reads and writes the database.
"""
import hashlib
import json
import os
import random
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

import classify
from schema import ensure_schema

RULES_PATH = Path(os.environ.get("LEDGER_RULES", Path(__file__).parent / "rules" / "ledger_v1.json"))
NEW_YORK = ZoneInfo("America/New_York")
KINDS = ("shortlist", "set_aside", "control")
# A candidate is recorded only once every news event from this many days back is labeled, so
# its flags rest on everything we had. (The results flags look back 45 days.)
COMPLETE_DAYS = 45


def log(message):
    print(message, flush=True)


def load_rules(path=RULES_PATH):
    raw = Path(path).read_bytes()
    rules = json.loads(raw)
    rules["rules_version"] = f"{rules['version']}@{hashlib.sha256(raw).hexdigest()[:8]}"
    return rules


def pick_controls(conn, day, idea_symbol, tier, exclude, count):
    """Random stocks from the same day and tier that are not ideas. The seed makes the draw repeatable."""
    as_of = conn.execute("SELECT max(as_of) FROM universe WHERE as_of <= %s", (day,)).fetchone()[0]
    pool = sorted(r[0] for r in conn.execute(
        "SELECT symbol FROM universe WHERE as_of = %s AND kind = 'stock' AND tier = %s", (as_of, tier)) if r[0] not in exclude)
    return random.Random(f"{day}:{idea_symbol}").sample(pool, min(count, len(pool)))


def unlabeled(conn, symbols, days=COMPLETE_DAYS):
    """{symbol: how many of its recent events have no label yet under the current prompt}."""
    return dict(conn.execute(
        """SELECT e.symbol, count(*) FROM events e
           WHERE e.symbol = ANY(%s) AND e.last_seen > now() - make_interval(days => %s)
             AND NOT EXISTS (SELECT 1 FROM event_labels l WHERE l.event_id = e.id
                             AND starts_with(l.classifier_version, %s))
           GROUP BY e.symbol""", (list(symbols), days, classify.prompt_version())).fetchall())


def record(conn, rules=None):
    """Record today's reviewed candidates that are not already in the ledger. Returns the new ideas."""
    rules = rules or load_rules()
    day, assessed = classify.assess(conn)
    if not assessed:
        log("Nothing to record: there are no screen results yet.")
        return []
    # A stock stays one idea for the cooldown period; it isn't re-recorded every day it keeps rising
    recent = {r[0] for r in conn.execute(
        "SELECT symbol FROM ideas WHERE kind <> 'control' AND decided_at > now() - make_interval(days => %s)",
        (rules["cooldown_days"],))}
    fresh = [a for a in assessed if a["symbol"] not in recent]
    # Hold back any candidate whose recent news isn't fully labeled (the model was busy, say).
    # An idea is frozen once recorded, so it waits for a later run rather than being recorded
    # on part of the evidence: a takeover headline still unlabeled would otherwise slip through.
    waiting = unlabeled(conn, [a["symbol"] for a in fresh])
    held = [a for a in fresh if waiting.get(a["symbol"])]
    fresh = [a for a in fresh if not waiting.get(a["symbol"])]
    not_controls = {a["symbol"] for a in assessed} | recent | {r[0] for r in conn.execute(
        "SELECT symbol FROM screen_results WHERE day = %s AND candidate", (day,))}

    new = []
    with conn.transaction():
        for a in fresh:
            evidence = {"signals": a["signals"], "events": [
                {"type": k, "direction": d, "date": seen.date().isoformat(), "headline": headline[:200], "articles": n}
                for k, d, seen, headline, n in a["events"][:8]]}
            stamp = f"{a['rules_version']} + {classify.FLAGS_VERSION} + {rules['rules_version']}"
            row = conn.execute(
                """INSERT INTO ideas (day, symbol, kind, rank, tier, flags, evidence, rules)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                (day, a["symbol"], a["status"], a["rank"], a["tier"], a["flags"], Jsonb(evidence), stamp)).fetchone()
            if not row:
                continue
            new.append(a)
            for control in pick_controls(conn, day, a["symbol"], a["tier"], not_controls, rules["controls_per_idea"]):
                conn.execute(
                    """INSERT INTO ideas (day, symbol, kind, parent_id, tier, rules)
                       VALUES (%s, %s, 'control', %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (day, control, row[0], a["tier"], stamp))
        if new:
            conn.execute("INSERT INTO idea_digests (day, body) VALUES (%s, %s) ON CONFLICT (day) DO NOTHING",
                         (day, digest(day, new)))
    shortlist = sum(a["status"] == "shortlist" for a in new)
    if held:
        log(f"Ledger: held back {len(held)} candidates until their recent news is labeled: "
            + ", ".join(f"{a['symbol']} ({waiting[a['symbol']]} unlabeled)" for a in held[:12])
            + (" ..." if len(held) > 12 else ""))
    log(f"Ledger: recorded {len(new)} new ideas for {day} ({shortlist} shortlist, {len(new) - shortlist} set aside), "
        f"each with up to {rules['controls_per_idea']} matched controls. Already in the ledger: {len(assessed) - len(fresh)}.")
    return new


def digest(day, new, top=5):
    """A short message about the day's new ideas, for Discord."""
    shortlist = [a for a in new if a["status"] == "shortlist"]
    aside = [a for a in new if a["status"] == "set_aside"]
    lines = [f"**Idea engine, {day:%b %d}**: {len(shortlist)} new on the shortlist" + (f", {len(aside)} set aside" if aside else "")]
    for a in shortlist[:top]:
        lines.append(f"#{a['rank']} **{a['symbol']}** ({a['tier']})" + (f" [{', '.join(a['flags'])}]" if a["flags"] else ""))
        if a["events"]:
            lines.append(f"   {a['events'][0][3][:110]}")
    if len(shortlist) > top:
        lines.append(f"...and {len(shortlist) - top} more.")
    for a in aside:
        lines.append(f"Set aside: {a['symbol']} ({', '.join(a['flags'])})")
    lines.append("A reading list from fixed rules, not advice. Headlines are third-party text.")
    return "\n".join(lines)


def outcomes(conn, rules):
    """Each idea's return minus the benchmark's, at every horizon that has finished.

    Returns a list of {"kind", "day", "symbol", "parent_id", "entry_day", "excess": {horizon: value}}.
    """
    ideas = conn.execute("SELECT id, day, decided_at, symbol, kind, parent_id FROM ideas ORDER BY id").fetchall()
    if not ideas:
        return []
    first = min(i[1] for i in ideas)
    calendar = [r[0] for r in conn.execute(
        "SELECT day FROM daily_bars WHERE symbol = %s AND day >= %s ORDER BY day", (classify_benchmark(), first))]
    bars = {}
    for symbol, day, open_, close in conn.execute(
            "SELECT symbol, day, open, close FROM daily_bars WHERE day >= %s AND symbol = ANY(%s) ORDER BY symbol, day",
            (first, list({i[3] for i in ideas} | {classify_benchmark()}))):
        bars.setdefault(symbol, {})[day] = (float(open_), float(close))
    market = bars.get(classify_benchmark(), {})

    out = []
    for idea_id, day, decided_at, symbol, kind, parent_id in ideas:
        decided = decided_at.astimezone(NEW_YORK).date()
        later = [d for d in calendar if d > max(decided, day)]      # the first open AFTER it was recorded
        entry_day = later[0] if later else None
        result = {"id": idea_id, "kind": kind, "day": day, "symbol": symbol, "parent_id": parent_id,
                  "entry_day": entry_day, "excess": {}}
        own = bars.get(symbol, {})
        if entry_day and entry_day in own and entry_day in market:
            start = calendar.index(entry_day)
            for horizon in rules["horizons_trading_days"]:
                if start + horizon - 1 >= len(calendar):
                    continue                                        # not finished yet
                exit_day = calendar[start + horizon - 1]
                last = max((d for d in own if d <= exit_day), default=None)   # a delisted stock keeps its last price
                stock = own[last][1] / own[entry_day][0] - 1
                bench = market[exit_day][1] / market[entry_day][0] - 1
                result["excess"][horizon] = stock - bench
        out.append(result)
    return out


def classify_benchmark():
    import screens
    return screens.BENCHMARK


def interval(pairs_by_day, draws=2000):
    """A 90% range for the average difference, resampling whole days (ideas from one day move together)."""
    days = sorted(pairs_by_day)
    rng = random.Random(7)
    means = []
    for _ in range(draws):
        sample = [x for d in rng.choices(days, k=len(days)) for x in pairs_by_day[d]]
        means.append(statistics.fmean(sample))
    means.sort()
    return means[int(0.05 * draws)], means[int(0.95 * draws)]


def report(conn, rules=None):
    rules = rules or load_rules()
    results = outcomes(conn, rules)
    if not results:
        return "The ledger is empty. Run: python ledger.py record"
    counts = {k: sum(r["kind"] == k for r in results) for k in KINDS}
    days = len({r["day"] for r in results})
    lines = [f"Scoreboard ({rules['rules_version']}): {counts['shortlist']} shortlist ideas, {counts['set_aside']} set aside, "
             f"{counts['control']} controls, recorded over {days} day{'s' if days != 1 else ''}.",
             f"Entry: {rules['entry']}", ""]
    lines.append(f"  {'Horizon':<10}{'Group':<11}{'Finished':>9}{'Avg vs S&P 500':>16}{'Beat it':>9}")
    for horizon in rules["horizons_trading_days"]:
        for kind in KINDS:
            values = [r["excess"][horizon] for r in results if r["kind"] == kind and horizon in r["excess"]]
            if values:
                lines.append(f"  {str(horizon) + ' days':<10}{kind:<11}{len(values):>9}{statistics.fmean(values):>+16.2%}"
                             f"{sum(v > 0 for v in values) / len(values):>9.0%}")
            else:
                lines.append(f"  {str(horizon) + ' days':<10}{kind:<11}{0:>9}{'not yet':>16}{'':>9}")

    # The one question fixed in advance: shortlist ideas versus their own matched controls
    horizon = rules["primary_horizon"]
    by_id = {r["id"]: r for r in results}
    controls = {}
    for r in results:
        if r["kind"] == "control" and horizon in r["excess"]:
            controls.setdefault(r["parent_id"], []).append(r["excess"][horizon])
    pairs = {}
    for idea_id, values in controls.items():
        idea = by_id[idea_id]
        if idea["kind"] == "shortlist" and horizon in idea["excess"]:
            pairs.setdefault(idea["day"], []).append(idea["excess"][horizon] - statistics.fmean(values))
    n = sum(len(v) for v in pairs.values())
    lines += ["", f"Primary question: {rules['primary_metric']}"]
    if n < rules["minimum_to_report_a_difference"] or len(pairs) < 5:
        lines.append(f"  Not enough data yet: {n} finished idea-and-control pairs over {len(pairs)} days. A difference is reported "
                     f"from {rules['minimum_to_report_a_difference']} pairs over at least 5 days, and about "
                     f"{rules['needed_to_detect_a_1pct_edge']} are needed to detect a 1% edge.")
    else:
        low, high = interval(pairs)
        average = statistics.fmean(x for v in pairs.values() for x in v)
        verdict = ("the range includes zero, so no edge is shown" if low <= 0 <= high
                   else "ideas beat their controls" if low > 0 else "ideas did worse than their controls")
        lines.append(f"  Ideas minus controls: {average:+.2%} on average over {n} pairs ({len(pairs)} days); "
                     f"90% range {low:+.2%} to {high:+.2%}: {verdict}.")
        if n < rules["needed_to_detect_a_1pct_edge"]:
            lines.append(f"  Still a small sample: about {rules['needed_to_detect_a_1pct_edge']} pairs are needed to detect a 1% edge.")
    return "\n".join(lines)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("daily", "record", "report"):
        raise SystemExit("Usage: python ledger.py daily | record | report")
    import psycopg
    rules = load_rules()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=30) as conn:
        ensure_schema(conn)
        if command == "daily":
            classify.run(conn, classify.GeminiLabeler())
            log("\n" + classify.review(conn))
        if command in ("daily", "record"):
            log("")
            record(conn, rules)
        log("\n" + report(conn, rules))


if __name__ == "__main__":
    main()
