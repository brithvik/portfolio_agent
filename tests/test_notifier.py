"""Tests for the notifier, against a real PostgreSQL database (set TEST_DATABASE_URL).

    python -m pytest tests/test_notifier.py -v
"""
import os

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")

if TEST_DB:
    import psycopg

    import notifier
    from schema import ensure_schema


@pytest.fixture
def conn():
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        ensure_schema(c)
        c.execute("""TRUNCATE notifier_state, gateway_events, trade_proposals, agent_steps, agent_runs
                     RESTART IDENTITY CASCADE""")
        yield c


def briefing(conn, answer="Total value is $100,018.44.", status="done", kind="briefing"):
    return conn.execute(
        """INSERT INTO agent_runs (kind, question, status, answer, finished_at)
           VALUES (%s, 'q', %s, %s, now()) RETURNING id""", (kind, status, answer)).fetchone()[0]


def proposal(conn, status="pending", untrusted=False, rationale="Reduce concentration in one holding."):
    return conn.execute(
        """INSERT INTO trade_proposals (symbol, side, notional, rationale, status, policy_checks,
               read_untrusted, expires_at)
           VALUES ('NVDA', 'sell', 1500, %s, %s, '[]', %s, now() + interval '2 hours') RETURNING id""",
        (rationale, status, untrusted)).fetchone()[0]


def started(conn):
    """A notifier that has already had its first pass (bookmarks set)."""
    assert notifier.run_once(conn, lambda _: pytest.fail("nothing should be sent on the first pass")) == 0


def test_first_start_does_not_resend_history(conn):
    briefing(conn)
    proposal(conn)
    started(conn)
    sent = []
    assert notifier.run_once(conn, sent.append) == 0 and sent == []


def test_new_briefings_and_proposals_are_sent_once(conn):
    started(conn)
    briefing(conn)
    proposal(conn)
    sent = []
    assert notifier.run_once(conn, sent.append) == 2
    assert "Portfolio briefing" in sent[0] and "$100,018.44" in sent[0]
    assert "Trade proposal #1" in sent[1] and "SELL $1,500.00 of NVDA" in sent[1]
    assert notifier.run_once(conn, sent.append) == 0 and len(sent) == 2   # never twice


def test_only_finished_briefings_and_pending_proposals(conn):
    started(conn)
    briefing(conn, status="failed", answer=None)
    briefing(conn, kind="ask")
    briefing(conn, kind="eval")
    proposal(conn, status="rejected_by_policy")
    sent = []
    assert notifier.run_once(conn, sent.append) == 0 and sent == []


def test_a_failed_send_is_retried_and_nothing_is_skipped(conn):
    started(conn)
    first, second = briefing(conn, "first"), briefing(conn, "second")

    def discord_is_down(_):
        raise RuntimeError("Discord answered 503")
    with pytest.raises(RuntimeError):
        notifier.run_once(conn, discord_is_down)

    sent = []
    notifier.run_once(conn, sent.append)
    assert len(sent) == 2 and "first" in sent[0] and "second" in sent[1]
    assert conn.execute("SELECT last_id FROM notifier_state WHERE key = 'briefing'").fetchone()[0] == second
    assert first < second


def test_a_failure_part_way_keeps_what_was_already_sent(conn):
    started(conn)
    briefing(conn, "first")
    briefing(conn, "second")
    sent = []

    def fails_on_the_second(content):
        if sent:
            raise RuntimeError("Discord answered 429")
        sent.append(content)
    with pytest.raises(RuntimeError):
        notifier.run_once(conn, fails_on_the_second)
    notifier.run_once(conn, sent.append)
    assert [("first" in s, "second" in s) for s in sent] == [(True, False), (False, True)]   # no repeat


def test_long_llm_text_is_cut_to_fit_discord(conn):
    started(conn)
    briefing(conn, "x" * 5000)
    proposal(conn, rationale="y" * 5000)
    sent = []
    notifier.run_once(conn, sent.append)
    assert all(len(s) <= 2000 for s in sent)


def test_proposals_that_read_news_carry_a_warning(conn):
    started(conn)
    proposal(conn, untrusted=True)
    sent = []
    notifier.run_once(conn, sent.append)
    assert "untrusted" in sent[0]


def test_messages_cannot_ping_anyone(monkeypatch):
    posted = {}

    class Reply:
        status_code = 204

    def fake_post(url, timeout, json):
        posted.update(url=url, json=json)
        return Reply()
    monkeypatch.setattr(notifier.requests, "post", fake_post)
    notifier.send_to_discord("https://discord.com/api/webhooks/1/abc", "@everyone buy now")
    assert posted["json"]["allowed_mentions"] == {"parse": []}


def test_a_discord_error_raises(monkeypatch):
    class Reply:
        status_code = 429
    monkeypatch.setattr(notifier.requests, "post", lambda *a, **k: Reply())
    with pytest.raises(RuntimeError):
        notifier.send_to_discord("https://discord.com/api/webhooks/1/abc", "hi")


def test_a_non_discord_webhook_is_refused(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://evil.example/collect")
    with pytest.raises(SystemExit):
        notifier.main()
