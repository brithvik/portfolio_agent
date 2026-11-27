"""Tests for the metrics exporter, against a real PostgreSQL database.

Like the gateway tests, these only run when TEST_DATABASE_URL is set (they run in CI):

    python -m pytest tests/test_metrics.py -v

Never point TEST_DATABASE_URL at your real database: the tests empty the tables.
"""
import os
from datetime import datetime, timezone

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")

if TEST_DB:
    import psycopg
    from psycopg.types.json import Jsonb

    from metrics_exporter import PortfolioCollector, market_hours
    from schema import ensure_schema
    from test_grounding import NEWS, PORTFOLIO, REAL_BRIEFING


def scrape(collector):
    """Run one scrape and flatten it to {(metric name, labels...): value}."""
    out = {}
    for family in collector.collect():
        for sample in family.samples:
            out[(sample.name, *sample.labels.values())] = sample.value
    return out


@pytest.fixture
def conn():
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        ensure_schema(c)
        c.execute("""TRUNCATE gateway_events, trade_proposals, agent_steps, agent_runs, news,
                     position_snapshots, account_snapshots, eval_reports, redteam_reports
                     RESTART IDENTITY CASCADE""")
        c.execute("UPDATE gateway_settings SET value = 'true' WHERE key = 'trading_enabled'")
        yield c


def add_run(conn, kind, answer, results, question="q", status="done"):
    run = conn.execute(
        """INSERT INTO agent_runs (kind, question, status, answer, model, input_tokens, output_tokens, finished_at)
           VALUES (%s, %s, %s, %s, 'test-model', 1000, 200, now()) RETURNING id""",
        (kind, question, status, answer)).fetchone()[0]
    conn.execute("INSERT INTO agent_steps (run_id, step, kind, name, duration_ms) VALUES (%s, 1, 'model', 'test-model', 2500)", (run,))
    for i, (tool, result) in enumerate(results, start=2):
        conn.execute("INSERT INTO agent_steps (run_id, step, kind, name, result) VALUES (%s, %s, 'tool', %s, %s)",
                     (run, i, tool, Jsonb(result)))
    return run


def test_an_empty_database_scrapes_cleanly(conn):
    m = scrape(PortfolioCollector(TEST_DB))
    assert m[("portfolio_exporter_database_up",)] == 1
    assert all(v == 0 for k, v in m.items() if k[0] == "portfolio_exporter_section_errors")
    assert m[("agent_answers_checked", "7d")] == 0
    assert m[("gateway_trading_enabled",)] == 1


def test_portfolio_and_freshness(conn):
    conn.execute("INSERT INTO account_snapshots VALUES (now() - interval '20 minutes', 100018.44, 12000.08, 290000)")
    m = scrape(PortfolioCollector(TEST_DB))
    assert m[("portfolio_value_dollars",)] == pytest.approx(100018.44)
    assert m[("portfolio_cash_dollars",)] == pytest.approx(12000.08)
    assert 1190 < m[("portfolio_snapshot_age_seconds",)] < 1300


def test_live_grounding_catches_the_real_wrong_briefing(conn):
    add_run(conn, "briefing", REAL_BRIEFING, [("get_portfolio", PORTFOLIO), ("get_news", NEWS)])
    add_run(conn, "ask", "NVDA is 17.67% of the portfolio.", [("get_portfolio", PORTFOLIO)])
    add_run(conn, "eval", "Made-up 99.99%", [("get_portfolio", PORTFOLIO)])   # evals are measured separately
    m = scrape(PortfolioCollector(TEST_DB))
    assert m[("agent_answers_checked", "24h")] == 2
    assert m[("agent_answers_ungrounded", "24h")] == 1          # only the briefing
    assert m[("agent_answer_numbers_cited", "24h")] == 21       # 20 in the briefing + 1
    assert m[("agent_answer_numbers_grounded", "24h")] == 17    # 16 + 1


def test_grounding_results_are_cached_between_scrapes(conn):
    add_run(conn, "ask", "NVDA is 17.67% of the portfolio.", [("get_portfolio", PORTFOLIO)])
    collector = PortfolioCollector(TEST_DB)
    scrape(collector)
    assert len(collector.grounding_cache) == 1
    conn.execute("DELETE FROM agent_steps")   # if it re-checked, the number would now look ungrounded
    assert scrape(collector)[("agent_answers_ungrounded", "24h")] == 0


def test_agent_activity_is_counted(conn):
    add_run(conn, "ask", "ok", [("get_portfolio", PORTFOLIO)])
    add_run(conn, "ask", None, [], status="failed")
    conn.execute("INSERT INTO agent_steps (run_id, step, kind, name, error) VALUES (1, 9, 'model', 'test-model', '504: timeout')")
    m = scrape(PortfolioCollector(TEST_DB))
    assert m[("agent_runs_total", "ask", "done")] == 1
    assert m[("agent_runs_total", "ask", "failed")] == 1
    assert m[("agent_tokens_total", "ask", "input")] == 2000
    assert m[("agent_tool_calls_total", "get_portfolio", "ok")] == 1
    assert m[("agent_model_calls_total", "test-model", "error")] == 1
    assert m[("agent_model_call_seconds", "0.5")] == pytest.approx(2.5)


def test_proposals_rules_and_kill_switch(conn):
    checks = [{"rule": "trade_size", "passed": False, "detail": ""}, {"rule": "daily_dollars", "passed": False, "detail": ""}]
    for status, policy in (("rejected_by_policy", checks), ("pending", []), ("filled", [])):
        conn.execute(
            """INSERT INTO trade_proposals (symbol, side, notional, rationale, status, policy_checks, expires_at)
               VALUES ('NVDA', 'sell', 1000, 'r', %s, %s, now() + interval '1 hour')""", (status, Jsonb(policy)))
    conn.execute("UPDATE gateway_settings SET value = 'false' WHERE key = 'trading_enabled'")
    m = scrape(PortfolioCollector(TEST_DB))
    assert m[("trade_proposals_total", "rejected_by_policy")] == 1
    assert m[("trade_proposal_rule_failures_total", "trade_size")] == 1
    assert m[("trade_proposal_rule_failures_total", "daily_dollars")] == 1
    assert m[("trade_proposals_waiting",)] == 1
    assert m[("gateway_trading_enabled",)] == 0


def test_latest_evaluation_and_red_team_reports(conn):
    conn.execute("""INSERT INTO eval_reports (model, questions, passed, facts_total, facts_correct, numbers_cited,
                    numbers_grounded, results) VALUES ('m', 14, 14, 23, 23, 33, 33, '[]'), ('m', 14, 12, 23, 21, 33, 30, '[]')""")
    conn.execute("""INSERT INTO redteam_reports (model, attacks, resisted, controls_ok, proposals_attempted,
                    proposals_blocked, errors, results) VALUES ('m', 10, 10, 2, 0, 0, 0, '[]')""")
    m = scrape(PortfolioCollector(TEST_DB))
    assert m[("eval_questions_passed_ratio",)] == pytest.approx(12 / 14)   # the newest report wins
    assert m[("eval_numbers_grounded_ratio",)] == pytest.approx(30 / 33)
    assert m[("redteam_attacks_resisted",)] == 10


def test_the_exporter_cannot_write(conn):
    with PortfolioCollector(TEST_DB).connect() as ro:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("DELETE FROM agent_runs")


def test_an_unreachable_database_reports_down_instead_of_crashing():
    m = scrape(PortfolioCollector("postgresql://nobody:x@127.0.0.1:1/none"))
    assert m == {("portfolio_exporter_database_up",): 0}


def test_market_hours():
    ny = lambda *a: datetime(*a, tzinfo=timezone.utc)   # noqa: E731  (times below are UTC)
    assert market_hours(ny(2026, 11, 13, 15, 0))        # Fri 10:00 AM ET
    assert not market_hours(ny(2026, 11, 13, 14, 0))    # Fri 9:00 AM ET, before the open
    assert not market_hours(ny(2026, 11, 13, 21, 30))   # Fri 4:30 PM ET, after the close
    assert not market_hours(ny(2026, 11, 14, 16, 0))    # Saturday


def test_market_data_freshness_and_classifier_accuracy(conn):
    conn.execute("TRUNCATE daily_bars, job_state, screen_results, classifier_reports")
    m = scrape(PortfolioCollector(TEST_DB))
    assert ("market_prices_age_seconds",) not in m and m[("screen_stocks",)] == 0     # nothing yet: no fake age
    conn.execute("INSERT INTO daily_bars VALUES ('SPY', current_date - 3, 1, 1, 1, 1, 1)")
    conn.execute("INSERT INTO job_state VALUES ('market_news', now() - interval '2 hours')")
    conn.execute("INSERT INTO screen_results VALUES (current_date, 'A', 'v', 'emerging', '{}', '{}', true, 1, 1), "
                 "(current_date, 'B', 'v', 'emerging', '{}', '{}', false, 0, NULL)")
    conn.execute("INSERT INTO classifier_reports (classifier_version, examples, type_correct, direction_correct, results) "
                 "VALUES ('v', 40, 38, 36, '[]')")
    m = scrape(PortfolioCollector(TEST_DB))
    # Age is counted from the end of that trading day in New York. Work out the expected value
    # the same way instead of assuming a range: "current_date" is UTC, so in the New York
    # evening the age is under 2 days (this test used to fail after 7 PM Eastern).
    expected = conn.execute("""SELECT extract(epoch FROM now() - ((current_date - 2)::timestamp
                                AT TIME ZONE 'America/New_York'))""").fetchone()[0]
    assert m[("market_prices_age_seconds",)] == pytest.approx(float(expected), abs=60)
    assert 7100 < m[("market_news_age_seconds",)] < 7300
    assert m[("screen_stocks",)] == 2 and m[("screen_candidates",)] == 1
    assert m[("classifier_type_accuracy_ratio",)] == pytest.approx(0.95)
