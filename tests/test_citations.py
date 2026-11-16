"""Tests for citations: the code, not the model, writes every figure in an answer.

    python -m pytest tests/test_citations.py -v
"""
import os

import pytest

from citations import Citations, format_value

PORTFOLIO = {
    "total_value": 100018.44, "cash": 12000.08, "minutes_old": 4,
    "totals": {"top_3_positions": {"symbols": ["NVDA", "AAPL", "MSFT"], "weight_pct": 60.67},
               "by_sector": [{"sector": "Information Technology", "weight_pct": 47.0}]},
    "positions": [{"symbol": "NVDA", "shares": 12.5, "unrealized_pl": -1234.5, "portfolio_weight_pct": 17.67}],
    "notes": ["Index funds hold hundreds of companies."],
}


def test_every_number_gets_a_handle_and_text_is_left_alone():
    c = Citations()
    tagged = c.tag("get_portfolio", PORTFOLIO)
    assert tagged["total_value"] == "$100,018.44 [F1]"
    assert tagged["totals"]["top_3_positions"] == {"symbols": ["NVDA", "AAPL", "MSFT"], "weight_pct": "60.67% [F4]"}
    assert tagged["notes"] == PORTFOLIO["notes"]
    assert PORTFOLIO["total_value"] == 100018.44          # the original is not modified
    assert len(c.facts) == 8


def test_each_handle_remembers_which_field_it_came_from():
    c = Citations()
    c.tag("get_portfolio", PORTFOLIO)
    paths = {f["path"] for f in c.facts.values()}
    assert "get_portfolio.totals.top_3_positions.weight_pct" in paths
    assert "get_portfolio.totals.by_sector[Information Technology].weight_pct" in paths
    assert "get_portfolio.positions[NVDA].portfolio_weight_pct" in paths


def test_figures_are_formatted_by_kind():
    assert format_value(60.67, "percent") == "60.67%" and format_value(47.0, "percent") == "47%"
    assert format_value(100018.44, "dollars") == "$100,018.44" and format_value(-1234.5, "dollars") == "-$1,234.50"
    assert format_value(9, "plain") == "9" and format_value(12.5, "plain") == "12.5"


def test_handles_in_an_answer_become_exact_figures():
    c = Citations()
    c.tag("get_portfolio", PORTFOLIO)
    text, used, unknown = c.render("Your top three are [F4] of [F1], and NVDA is down [F7].")
    assert text == "Your top three are 60.67% of $100,018.44, and NVDA is down -$1,234.50."
    assert used == ["F4", "F1", "F7"] and unknown == []
    assert c.paths(used)[0] == "get_portfolio.totals.top_3_positions.weight_pct"


def test_a_copied_value_next_to_its_handle_is_not_doubled():
    c = Citations()
    c.tag("get_portfolio", PORTFOLIO)
    assert c.render("The top three are 60.67% [F4].")[0] == "The top three are 60.67%."
    assert c.render("Total: $100,018.44 ([F1])")[0] == "Total: $100,018.44"


def test_an_invented_handle_is_reported_not_filled_in():
    c = Citations()
    c.tag("get_portfolio", PORTFOLIO)
    text, used, unknown = c.render("Tech is [F99] of the portfolio.")
    assert unknown == ["F99"] and "[F99]" in text and used == []


def test_handles_keep_counting_across_tool_calls():
    c = Citations()
    c.tag("get_portfolio", {"cash": 5.0})
    assert c.tag("get_portfolio_history", {"change_over_period": {"percent": 1.5}}) == {
        "change_over_period": {"percent": "1.5% [F2]"}}


def test_a_handle_passed_as_a_tool_argument_becomes_the_real_number():
    c = Citations()
    c.tag("get_portfolio", {"cash": 1500.0})
    assert c.resolve_argument("[F1]") == 1500.0
    assert c.resolve_argument("NVDA") == "NVDA" and c.resolve_argument("[F9]") == "[F9]" and c.resolve_argument(7) == 7


# ---------- inside the agent loop (needs PostgreSQL for the audit log) ----------
TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")

if TEST_DB:
    from test_agent import call, conn, fast, make, reply, run_row   # noqa: F401  (fixtures)


def checks(conn):
    return conn.execute("SELECT answer, answer_draft, checks FROM agent_runs").fetchone()


@needs_db
def test_the_final_answer_has_exact_figures_and_records_where_they_came_from(conn):
    a = make(conn, [reply(calls=[call("get_portfolio")]), reply("Your top three are [F4] of [F1].")],
             tools={"get_portfolio": lambda conn: PORTFOLIO})
    assert a.run("ask", "How concentrated am I?") == "Your top three are 60.67% of $100,018.44."
    answer, draft, c = checks(conn)
    assert draft == "Your top three are [F4] of [F1]."
    assert c["figures_from_handles"] == 2 and c["ungrounded"] == [] and c["repaired"] is False
    assert c["cited_fields"] == ["get_portfolio.totals.top_3_positions.weight_pct", "get_portfolio.total_value"]


@needs_db
def test_the_audit_log_keeps_the_real_tool_result_not_the_tagged_one(conn):
    a = make(conn, [reply(calls=[call("get_portfolio")]), reply("ok")], tools={"get_portfolio": lambda conn: PORTFOLIO})
    a.run("ask", "q")
    logged = conn.execute("SELECT result FROM agent_steps WHERE kind = 'tool'").fetchone()[0]
    assert logged["total_value"] == 100018.44


@needs_db
def test_a_made_up_number_gets_one_repair_attempt(conn):
    a = make(conn, [reply(calls=[call("get_portfolio")]),
                    reply("Your top three are 62.67% of the portfolio."),       # the real bug from the first briefing
                    reply("Your top three are [F4] of the portfolio.")],
             tools={"get_portfolio": lambda conn: PORTFOLIO})
    assert a.run("ask", "q") == "Your top three are 60.67% of the portfolio."
    assert len(a.client.asked) == 3
    assert checks(conn)[2]["repaired"] is True and checks(conn)[2]["ungrounded"] == []


@needs_db
def test_if_the_repair_fails_the_problem_is_recorded_not_hidden(conn):
    a = make(conn, [reply(calls=[call("get_portfolio")]), reply("Tech is [F99], about 62.67%.")],
             tools={"get_portfolio": lambda conn: PORTFOLIO})
    answer = a.run("ask", "q")
    assert "[figure unavailable]" in answer and "[F99]" not in answer
    c = checks(conn)[2]
    assert c["unknown_handles"] == ["F99"] and c["ungrounded"][0]["number"] == "62.67%" and c["repaired"] is True
    assert len(a.client.asked) == 3          # the tool call, the draft, one repair: no endless loop


@needs_db
def test_a_typed_number_that_is_in_the_data_is_still_accepted(conn):
    a = make(conn, [reply(calls=[call("get_portfolio")]), reply("Roughly 61% is in your top three.")],
             tools={"get_portfolio": lambda conn: PORTFOLIO})
    assert a.run("ask", "q") == "Roughly 61% is in your top three."
    assert len(a.client.asked) == 2 and checks(conn)[2]["repaired"] is False
