"""Tests for the ledger: ideas are recorded once, with controls, and scored on what happened next.

    python -m pytest tests/test_ledger.py -v
"""
import os
from datetime import date, datetime, timedelta, timezone

import pytest

import ledger

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")
RULES = ledger.load_rules()
FRIDAY = date(2026, 11, 13)

if TEST_DB:
    import psycopg

    from schema import ensure_schema


def weekdays(start, n):
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def add_bars(conn, symbol, days, opens, closes):
    for day, open_, close in zip(days, opens, closes):
        conn.execute("INSERT INTO daily_bars VALUES (%s, %s, %s, %s, %s, %s, 1000000) ON CONFLICT DO NOTHING",
                     (symbol, day, open_, max(open_, close), min(open_, close), close))


@pytest.fixture
def conn():
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        ensure_schema(c)
        c.execute("TRUNCATE idea_digests, ideas, event_labels, events, news, screen_results, universe, daily_bars, "
                  "bar_backfills, notifier_state RESTART IDENTITY CASCADE")
        for i, (symbol, tier) in enumerate([("GOOD", "emerging"), ("DEAL", "emerging"), ("BIG", "established")], start=1):
            c.execute("INSERT INTO screen_results VALUES (%s, %s, 'screens-v1@test', %s, %s, '{rising}', true, 0.9, %s)",
                      (FRIDAY, symbol, tier, '{"excess_6m": 0.5}', i))
        for n in range(12):       # the pool that controls are drawn from
            for tier in ("emerging", "established"):
                c.execute("INSERT INTO universe VALUES (%s, %s, 'x', 'NASDAQ', 'stock', %s, 20, 9000000)",
                          (FRIDAY, f"{tier[:2].upper()}{n:02d}", tier))
        for symbol, tier in (("GOOD", "emerging"), ("DEAL", "emerging"), ("BIG", "established")):
            c.execute("INSERT INTO universe VALUES (%s, %s, 'x', 'NASDAQ', 'stock', %s, 20, 9000000)", (FRIDAY, symbol, tier))
        c.execute("""INSERT INTO events (symbol, first_article_id, headline, first_seen, last_seen, article_ids, articles, authors, sources)
                     VALUES ('DEAL', 1, 'Bigco Commences Tender Offer To Acquire DEAL Corp', now() - interval '3 days', now() - interval '3 days', '{1}', 1, 1, '{}'),
                            ('GOOD', 2, 'GOOD Corp Q3 EPS Beats Estimate', now() - interval '3 days', now() - interval '3 days', '{2}', 2, 1, '{}')""")
        c.execute("""INSERT INTO event_labels (event_id, classifier_version, type, direction)
                     VALUES (1, %(v)s, 'acquisition_target', 'positive'), (2, %(v)s, 'earnings_results', 'positive')""",
                  {"v": ledger.classify.prompt_version() + "test-model"})
        yield c


def test_every_reviewed_candidate_is_recorded_with_its_status_and_evidence(conn):
    new = ledger.record(conn, RULES)
    assert {(a["symbol"], a["status"]) for a in new} == {("GOOD", "shortlist"), ("DEAL", "set_aside"), ("BIG", "shortlist")}
    kind, flags, evidence, rules = conn.execute("SELECT kind, flags, evidence, rules FROM ideas WHERE symbol = 'GOOD'").fetchone()
    assert kind == "shortlist" and flags == ["strong_results"]
    assert evidence["signals"] == {"excess_6m": 0.5} and evidence["events"][0]["headline"] == "GOOD Corp Q3 EPS Beats Estimate"
    assert "screens-v1@test" in rules and RULES["rules_version"] in rules
    assert conn.execute("SELECT kind FROM ideas WHERE symbol = 'DEAL'").fetchone()[0] == "set_aside"   # recorded, not hidden


def test_each_idea_gets_matched_controls_from_the_same_tier_never_another_idea(conn):
    ledger.record(conn, RULES)
    rows = conn.execute("""SELECT p.symbol, p.tier, c.symbol, c.tier FROM ideas c JOIN ideas p ON p.id = c.parent_id
                           WHERE c.kind = 'control'""").fetchall()
    assert len(rows) == 9 and all(parent_tier == control_tier for _, parent_tier, _, control_tier in rows)
    assert not {c for _, _, c, _ in rows} & {"GOOD", "DEAL", "BIG"}


def test_the_control_draw_is_repeatable(conn):
    first = ledger.pick_controls(conn, FRIDAY, "GOOD", "emerging", {"GOOD"}, 3)
    assert first == ledger.pick_controls(conn, FRIDAY, "GOOD", "emerging", {"GOOD"}, 3)
    assert first != ledger.pick_controls(conn, FRIDAY, "DEAL", "emerging", {"GOOD"}, 3)


def test_recording_again_adds_nothing_and_a_stock_is_one_idea_during_its_cooldown(conn):
    ledger.record(conn, RULES)
    before = conn.execute("SELECT count(*) FROM ideas").fetchone()[0]
    assert ledger.record(conn, RULES) == []
    conn.execute("INSERT INTO screen_results SELECT day + 1, symbol, rules_version, tier, signals, flags, candidate, score, rank FROM screen_results")
    assert ledger.record(conn, RULES) == []                     # still rising the next day: not a new idea
    assert conn.execute("SELECT count(*) FROM ideas").fetchone()[0] == before


def test_the_days_digest_is_written_once_and_the_notifier_sends_it(conn):
    import notifier
    notifier.run_once(conn, lambda _: pytest.fail("nothing to send on the first pass"))
    ledger.record(conn, RULES)
    body = conn.execute("SELECT body FROM idea_digests").fetchone()[0]
    assert "2 new on the shortlist, 1 set aside" in body and "**GOOD**" in body and "Set aside: DEAL (deal_pending)" in body
    assert "not advice" in body
    sent = []
    assert notifier.run_once(conn, sent.append) == 1 and sent[0] == body
    assert notifier.run_once(conn, sent.append) == 0


def test_an_idea_is_priced_from_the_next_open_after_it_was_recorded(conn):
    days = weekdays(FRIDAY, 8)                                   # Fri 13, Mon 16, Tue 17, ...
    add_bars(conn, "SPY", days, [100] * 8, [100] * 8)
    add_bars(conn, "GOOD", days, [50, 60, 60, 60, 60, 60, 60, 60], [50, 60, 60, 60, 60, 66, 66, 66])   # jumped before entry
    ledger.record(conn, RULES)
    conn.execute("UPDATE ideas SET decided_at = %s", (datetime(2026, 11, 14, 0, 30, tzinfo=timezone.utc),))   # Friday 7:30 PM NY
    good = next(r for r in ledger.outcomes(conn, RULES) if r["symbol"] == "GOOD")
    assert good["entry_day"] == date(2026, 11, 16)               # Monday's open, not Friday's close
    assert good["excess"] == {5: pytest.approx(0.10)}            # 60 -> 66; the jump from 50 to 60 is not counted


def test_an_idea_recorded_late_is_priced_later(conn):
    days = weekdays(FRIDAY, 8)
    add_bars(conn, "SPY", days, [100] * 8, [100] * 8)
    add_bars(conn, "GOOD", days, [50] * 8, [50] * 8)
    ledger.record(conn, RULES)
    conn.execute("UPDATE ideas SET decided_at = %s", (datetime(2026, 11, 16, 15, 54, tzinfo=timezone.utc),))  # Monday 10:54 AM NY
    assert next(r for r in ledger.outcomes(conn, RULES) if r["symbol"] == "GOOD")["entry_day"] == date(2026, 11, 17)


def test_returns_are_measured_against_the_market_and_unfinished_horizons_are_left_out(conn):
    days = weekdays(FRIDAY, 8)
    add_bars(conn, "SPY", days, [100] * 8, [100, 100, 100, 100, 100, 104, 104, 104])
    add_bars(conn, "GOOD", days, [50] * 8, [50, 50, 50, 50, 50, 55, 55, 55])
    ledger.record(conn, RULES)
    conn.execute("UPDATE ideas SET decided_at = %s", (datetime(2026, 11, 14, 0, 30, tzinfo=timezone.utc),))
    good = next(r for r in ledger.outcomes(conn, RULES) if r["symbol"] == "GOOD")
    assert good["excess"] == {5: pytest.approx(0.10 - 0.04)}     # +10% while the market did +4%; 20 and 60 days not finished


def test_a_stock_that_stops_trading_keeps_its_last_price(conn):
    days = weekdays(FRIDAY, 8)
    add_bars(conn, "SPY", days, [100] * 8, [100] * 8)
    add_bars(conn, "DEAL", days[:3], [36, 36, 36], [36, 36, 30])      # no bars after Tuesday
    ledger.record(conn, RULES)
    conn.execute("UPDATE ideas SET decided_at = %s", (datetime(2026, 11, 14, 0, 30, tzinfo=timezone.utc),))
    deal = next(r for r in ledger.outcomes(conn, RULES) if r["symbol"] == "DEAL")
    assert deal["excess"][5] == pytest.approx(30 / 36 - 1)       # counted at its last price, not dropped


def test_the_scoreboard_refuses_to_claim_an_edge_from_too_little_data(conn):
    days = weekdays(FRIDAY, 8)
    for symbol in ["SPY", "GOOD", "DEAL", "BIG"] + [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM universe")]:
        add_bars(conn, symbol, days, [100] * 8, [100] * 8)
    ledger.record(conn, RULES)
    text = ledger.report(conn, RULES)
    assert "2 shortlist ideas, 1 set aside, 9 controls" in text
    assert "Not enough data yet" in text and "400" in text and "90% range" not in text


def test_with_enough_days_the_scoreboard_gives_a_range_and_a_plain_verdict(conn):
    # 8 days of ideas that all beat their controls by exactly 2 points
    conn.execute("TRUNCATE ideas CASCADE")
    starts = weekdays(date(2026, 6, 1), 8)
    calendar = weekdays(date(2026, 6, 1), 40)
    add_bars(conn, "SPY", calendar, [100] * 40, [100] * 40)
    for n in range(5):
        add_bars(conn, f"WIN{n}", calendar, [100] * 40, [103] * 40)
        add_bars(conn, f"CTL{n}", calendar, [100] * 40, [101] * 40)
    for day in starts:
        evening = datetime(day.year, day.month, day.day, 23, tzinfo=timezone.utc)
        for n in range(5):
            idea = conn.execute(
                "INSERT INTO ideas (day, decided_at, symbol, kind, tier, rules) VALUES (%s, %s, %s, 'shortlist', 'emerging', 'r') RETURNING id",
                (day, evening, f"WIN{n}")).fetchone()[0]
            conn.execute("INSERT INTO ideas (day, decided_at, symbol, kind, parent_id, tier, rules) VALUES (%s, %s, %s, 'control', %s, 'emerging', 'r')",
                         (day, evening, f"CTL{n}", idea))
    text = ledger.report(conn, RULES)
    assert "Ideas minus controls: +2.00% on average over 40 pairs (8 days)" in text
    assert "ideas beat their controls" in text and "Still a small sample" in text


def test_ledger_stocks_keep_getting_prices_after_leaving_the_universe(conn, monkeypatch):
    import market as market_module
    from test_screens import NOW, RULES as SCREEN_RULES, FakeAlpaca, series
    monkeypatch.setattr(market_module, "PAUSE", 0)
    monkeypatch.setattr(market_module, "BENCHMARKS", ["SPY"])
    conn.execute("TRUNCATE daily_bars, universe, bar_backfills")
    conn.execute("INSERT INTO ideas (day, symbol, kind, tier, rules) VALUES (%s, 'FELL', 'shortlist', 'emerging', 'r')", (FRIDAY,))
    fake = FakeAlpaca({"SPY": series(daily=0.0003, start=500, volume=50_000_000),
                       "FELL": series(daily=-0.01, start=40, volume=20_000),          # now far below the floor
                       "THIN": series(daily=0.0, start=40, volume=20_000)},
                      names={"SPY": "SPDR S&P 500 ETF Trust"})
    members = market_module.update(conn, fake, fake, SCREEN_RULES, now=NOW)
    assert "FELL" not in members                                 # not in the universe...
    assert conn.execute("SELECT count(*) FROM daily_bars WHERE symbol = 'FELL'").fetchone()[0] > 0   # ...but still priced
    assert conn.execute("SELECT count(*) FROM daily_bars WHERE symbol = 'THIN'").fetchone()[0] == 0


def test_a_candidate_with_unlabeled_recent_news_is_held_back_not_recorded(conn):
    # A takeover headline the model hasn't labeled yet (it was too busy): DEAL must not be
    # frozen into the ledger as an ordinary shortlist idea.
    conn.execute("""INSERT INTO events (symbol, first_article_id, headline, first_seen, last_seen, article_ids, articles, authors, sources)
                    VALUES ('BIG', 3, 'Megacorp Agrees To Acquire BIG Inc', now() - interval '1 day', now() - interval '1 day', '{3}', 1, 0, '{}')""")
    new = ledger.record(conn, RULES)
    assert {a["symbol"] for a in new} == {"GOOD", "DEAL"}
    assert conn.execute("SELECT count(*) FROM ideas WHERE symbol = 'BIG'").fetchone()[0] == 0
    # once it is labeled, the next run records it, with the label taken into account
    conn.execute("""INSERT INTO event_labels (event_id, classifier_version, type, direction)
                    SELECT id, %s, 'acquisition_target', 'positive' FROM events WHERE first_article_id = 3""",
                 (ledger.classify.prompt_version() + "test-model",))
    new = ledger.record(conn, RULES)
    assert [(a["symbol"], a["status"]) for a in new] == [("BIG", "set_aside")]


def test_old_unlabeled_news_does_not_hold_a_candidate_back(conn):
    conn.execute("""INSERT INTO events (symbol, first_article_id, headline, first_seen, last_seen, article_ids, articles, authors, sources)
                    VALUES ('BIG', 4, 'Something From Long Ago', now() - interval '80 days', now() - interval '80 days', '{4}', 1, 0, '{}')""")
    assert "BIG" in {a["symbol"] for a in ledger.record(conn, RULES)}
