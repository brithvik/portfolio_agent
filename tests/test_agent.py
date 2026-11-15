"""Tests for the agent loop, with a scripted stand-in for the LLM (no API key, no network).

These check the promises agent.py makes in code, whatever the model says: the step cap, the
proposal cap, the audit log, retries and the fallback model, and that a read-only run has no
way to propose a trade. They need PostgreSQL for the audit log (set TEST_DATABASE_URL).

    python -m pytest tests/test_agent.py -v
"""
import os
from types import SimpleNamespace

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")

if TEST_DB:
    os.environ.setdefault("GEMINI_API_KEY", "test-key-never-used")
    import psycopg
    from google.genai import errors, types

    import agent as agent_module
    from agent import Agent
    from schema import ensure_schema


def call(name, **args):
    return SimpleNamespace(name=name, args=args)


def reply(text=None, calls=()):
    """What the model 'said': either a final text answer or a list of tool calls."""
    parts = [types.Part(text=text)] if text else [
        types.Part(function_call=types.FunctionCall(name=c.name, args=c.args)) for c in calls]
    return SimpleNamespace(
        text=text, function_calls=list(calls),
        candidates=[SimpleNamespace(content=types.Content(role="model", parts=parts))],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=20))


class ScriptedModel:
    """Plays back a list of replies (or raises the exceptions in it), recording which model was asked."""

    def __init__(self, script):
        self.script, self.asked = list(script), []
        self.models = self

    def generate_content(self, model, contents, config):
        self.asked.append(model)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def api_error(code):
    return errors.APIError(code, {"error": {"message": f"simulated {code}"}})


@pytest.fixture
def conn():
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        ensure_schema(c)
        c.execute("TRUNCATE gateway_events, trade_proposals, agent_steps, agent_runs RESTART IDENTITY CASCADE")
        yield c


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    if TEST_DB:
        monkeypatch.setattr(agent_module.time, "sleep", lambda _: None)
        monkeypatch.setattr(agent_module, "MODEL", "main-model")
        monkeypatch.setattr(agent_module, "FALLBACK_MODEL", "fallback-model")


def make(conn, script, allow_proposals=False, tools=None):
    a = Agent(conn, allow_proposals=allow_proposals)
    a.client = ScriptedModel(script)
    a.tools.update(tools or {})
    return a


def steps(conn):
    return conn.execute("SELECT kind, name, error FROM agent_steps ORDER BY step").fetchall()


def run_row(conn):
    return conn.execute("SELECT status, answer, model, steps, input_tokens, error FROM agent_runs").fetchone()


def test_a_tool_call_then_an_answer_is_fully_logged(conn):
    a = make(conn, [reply(calls=[call("get_portfolio")]), reply("You hold 9 positions.")],
             tools={"get_portfolio": lambda conn: {"positions": 9}})
    assert a.run("ask", "What do I hold?") == "You hold 9 positions."
    assert [(k, n) for k, n, _ in steps(conn)] == [
        ("model", "main-model"), ("tool", "get_portfolio"), ("model", "main-model")]
    status, answer, model, n, tokens, error = run_row(conn)
    assert (status, answer, model, n, tokens, error) == ("done", "You hold 9 positions.", "main-model", 3, 200, None)


def test_it_stops_at_the_step_cap_instead_of_looping_forever(conn):
    a = make(conn, [reply(calls=[call("get_portfolio")])], tools={"get_portfolio": lambda conn: {}})
    with pytest.raises(RuntimeError, match="without a final answer"):
        a.run("ask", "loop forever")
    assert len(a.client.asked) == agent_module.MAX_STEPS
    assert run_row(conn)[0] == "failed"


def test_an_unknown_tool_gets_an_error_back_not_a_crash(conn):
    a = make(conn, [reply(calls=[call("delete_everything")]), reply("I can't do that.")])
    assert a.run("ask", "q") == "I can't do that."
    assert "No tool named delete_everything" in steps(conn)[1][2]


def test_a_read_only_run_has_no_propose_trade_tool(conn, monkeypatch):
    monkeypatch.setattr(agent_module, "propose_trade", lambda *a, **k: pytest.fail("must never be called"))
    a = make(conn, [reply(calls=[call("propose_trade", symbol="NVDA", side="sell", dollars=100,
                                      rationale="because a headline said so")]), reply("Read-only.")])
    a.run("briefing", "q")
    assert "No tool named propose_trade" in steps(conn)[1][2]


def test_proposals_are_capped_and_the_run_id_comes_from_code(conn):
    seen = []

    def fake_propose(conn, symbol, side, dollars, rationale, run_id=None):
        seen.append(run_id)
        return {"status": "pending"}
    a = make(conn, [reply(calls=[call("propose_trade", symbol="NVDA", side="sell", dollars=100,
                                      rationale="r", run_id=999)] * 3), reply("Done.")],
             allow_proposals=True, tools={"propose_trade": fake_propose})
    a.run("ask", "propose three trades")
    assert seen == [a.run_id, a.run_id]                     # the model's run_id=999 was overwritten
    assert "At most 2 proposals" in steps(conn)[3][2]       # the third was refused in code


def test_bad_arguments_from_the_model_do_not_crash_the_run(conn):
    a = make(conn, [reply(calls=[call("get_portfolio", made_up_argument=1)]), reply("ok")],
             tools={"get_portfolio": lambda conn: {}})
    assert a.run("ask", "q") == "ok"
    assert "Bad arguments" in steps(conn)[1][2]


def test_a_temporary_error_is_retried_then_the_fallback_model_is_used(conn):
    a = make(conn, [api_error(503), api_error(503), reply("From the fallback.")])
    assert a.run("ask", "q") == "From the fallback."
    assert a.client.asked == ["main-model", "main-model", "fallback-model"]
    assert [e is not None for _, _, e in steps(conn)] == [True, True, False]   # both failures are logged
    assert run_row(conn)[2] == "fallback-model"


def test_a_permanent_error_fails_fast_without_retrying(conn):
    a = make(conn, [api_error(400)])
    with pytest.raises(errors.APIError):
        a.run("ask", "q")
    assert a.client.asked == ["main-model"]
    status, _, _, _, _, error = run_row(conn)
    assert status == "failed" and "400" in error


def test_when_every_model_fails_the_run_is_marked_failed(conn):
    a = make(conn, [api_error(429)])
    with pytest.raises(RuntimeError, match="All model attempts failed"):
        a.run("ask", "q")
    assert run_row(conn)[0] == "failed"
