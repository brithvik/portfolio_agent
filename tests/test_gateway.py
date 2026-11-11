"""Tests for the broker gateway, using a stand-in broker and a real PostgreSQL database.

These need a throwaway database, so they only run when TEST_DATABASE_URL is set:

    $env:TEST_DATABASE_URL = "postgresql://user:pass@localhost:5432/portfolio_test"
    python -m pytest tests/test_gateway.py -v

Never point TEST_DATABASE_URL at your real database: the tests empty the tables.
"""
import os

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")

AGENT = {"Authorization": "Bearer agent-token-for-tests-0123456789"}
HUMAN = {"Authorization": "Bearer approver-token-for-tests-0123456789"}
REASON = "Trim the largest position to reduce concentration in a single stock."

if TEST_DB:
    os.environ.update(
        DATABASE_URL=TEST_DB,
        GATEWAY_AGENT_TOKEN=AGENT["Authorization"].removeprefix("Bearer "),
        GATEWAY_APPROVER_TOKEN=HUMAN["Authorization"].removeprefix("Bearer "),
        GATEWAY_SYNC="off",   # tests call sync_once themselves
    )
    import psycopg
    from fastapi.testclient import TestClient

    import gateway
    from broker import Account, BrokerError, OrderInfo, OrderNotFound


class FakeBroker:
    """Behaves like the brokerage, without any real orders."""
    paper = True

    def __init__(self):
        self.cash, self.equity = 12_000.0, 100_000.0
        self.held = {"NVDA": 17_500.0, "AAPL": 14_000.0, "KO": 4_000.0}
        self.open = True
        self.orders = {}          # client_order_id -> OrderInfo
        self.fail_submit = None
        self.down = False

    def _check(self):
        if self.down:
            raise BrokerError("connection refused")

    def account(self):
        self._check()
        return Account(equity=self.equity, cash=self.cash, blocked=False)

    def positions(self):
        self._check()
        return dict(self.held)

    def market_open(self):
        self._check()
        return self.open

    def submit(self, symbol, side, notional, client_order_id):
        self._check()
        if self.fail_submit:
            raise BrokerError(self.fail_submit)
        if client_order_id in self.orders:
            raise BrokerError("client_order_id must be unique")
        info = OrderInfo(f"order-{len(self.orders) + 1}", client_order_id, "pending_new", 0.0, None)
        self.orders[client_order_id] = info
        return info

    def order(self, order_id):
        self._check()
        return next(o for o in self.orders.values() if o.id == order_id)

    def order_by_client_id(self, client_order_id):
        self._check()
        if client_order_id not in self.orders:
            raise OrderNotFound(client_order_id)
        return self.orders[client_order_id]

    def set_status(self, order_id, status, qty=0.0, price=None):
        for cid, o in self.orders.items():
            if o.id == order_id:
                self.orders[cid] = OrderInfo(o.id, cid, status, qty, price)


@pytest.fixture
def env():
    broker = FakeBroker()
    gateway._broker = broker
    with TestClient(gateway.app) as client:
        with psycopg.connect(TEST_DB, autocommit=True) as conn:
            conn.execute("TRUNCATE gateway_events, trade_proposals, agent_steps, agent_runs RESTART IDENTITY CASCADE")
            conn.execute("UPDATE gateway_settings SET value = 'true' WHERE key = 'trading_enabled'")
            yield client, broker, conn


def propose(client, symbol="NVDA", side="sell", notional=1_000, headers=AGENT, **extra):
    body = {"symbol": symbol, "side": side, "notional": notional, "rationale": REASON, **extra}
    return client.post("/api/proposals", json=body, headers=headers)


def status_of(conn, pid):
    return conn.execute("SELECT status FROM trade_proposals WHERE id = %s", (pid,)).fetchone()[0]


# ---------- who is allowed to do what ----------

def test_no_token_means_no_access(env):
    client, _, _ = env
    assert client.post("/api/proposals", json={}).status_code == 401
    assert client.get("/api/proposals").status_code == 401
    assert propose(client, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_the_agent_can_propose_but_cannot_approve_reject_or_flip_the_kill_switch(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    assert client.post(f"/api/proposals/{pid}/approve", headers=AGENT).status_code == 403
    assert client.post(f"/api/proposals/{pid}/reject", json={}, headers=AGENT).status_code == 403
    assert client.post("/api/kill-switch", json={"trading_enabled": False}, headers=AGENT).status_code == 403
    assert client.get("/api/proposals", headers=AGENT).status_code == 403
    assert status_of(conn, pid) == "pending" and not broker.orders


# ---------- proposing ----------

def test_a_valid_proposal_waits_for_approval_and_places_no_order(env):
    client, broker, conn = env
    r = propose(client).json()
    assert r["status"] == "pending_approval"
    assert status_of(conn, r["id"]) == "pending" and not broker.orders


def test_a_rule_breaking_proposal_is_rejected_and_kept_for_the_record(env):
    client, broker, conn = env
    r = propose(client, notional=17_500).json()   # "sell everything"
    assert r["status"] == "rejected_by_policy"
    assert "trade_size" in [f["rule"] for f in r["failed_rules"]]
    assert status_of(conn, r["id"]) == "rejected_by_policy"
    assert client.post(f"/api/proposals/{r['id']}/approve", headers=HUMAN).status_code == 409
    assert not broker.orders


def test_symbols_and_sides_are_cleaned_up_not_trusted(env):
    client, _, conn = env
    r = propose(client, symbol=" nvda ", side="SELL").json()
    assert conn.execute("SELECT symbol, side FROM trade_proposals WHERE id = %s", (r["id"],)).fetchone() == ("NVDA", "sell")


def test_news_reading_runs_are_flagged_for_the_human(env):
    client, _, conn = env
    run = conn.execute("INSERT INTO agent_runs (kind, question) VALUES ('ask', 'q') RETURNING id").fetchone()[0]
    conn.execute(
        """INSERT INTO agent_steps (run_id, step, kind, name, args) VALUES
           (%s, 1, 'tool', 'get_portfolio', '{}'), (%s, 2, 'tool', 'get_news', %s)""",
        (run, run, '{"symbol": "NVDA"}'))
    pid = propose(client, run_id=run).json()["id"]
    shown = client.get("/api/proposals", headers=HUMAN).json()[0]
    assert shown["id"] == pid and shown["read_untrusted"] is True
    assert [t["tool"] for t in shown["tools_used"]] == ["get_portfolio", "get_news"]
    assert propose(client, symbol="KO", run_id=999_999).status_code == 400   # made-up run


def test_proposals_cannot_flood_the_human(env):
    client, _, _ = env
    for symbol in ("NVDA", "AAPL", "KO"):
        assert propose(client, symbol=symbol).json()["status"] == "pending_approval"
    assert propose(client, symbol="NVDA").json()["status"] == "rejected_by_policy"   # duplicate + full


def test_broker_outage_blocks_proposals_instead_of_guessing(env):
    client, broker, _ = env
    broker.down = True
    assert propose(client).status_code == 502


# ---------- approving ----------

def test_approval_sends_exactly_one_order_even_if_clicked_twice(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).json()["status"] == "submitted"
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).status_code == 409
    assert len(broker.orders) == 1 and status_of(conn, pid) == "submitted"


def test_rules_are_rechecked_at_approval(env):
    client, broker, conn = env
    pid = propose(client, symbol="KO", side="buy", notional=1_500).json()["id"]
    broker.cash = 1_000.0                      # the cash was spent in the meantime
    r = client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).json()
    assert r["status"] == "rejected_by_policy"
    assert "cash_only" in [f["rule"] for f in r["failed_rules"]]
    assert not broker.orders and status_of(conn, pid) == "rejected_by_policy"


def test_approving_while_the_market_is_closed_keeps_the_proposal_waiting(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    broker.open = False
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).status_code == 409
    assert status_of(conn, pid) == "pending" and not broker.orders
    broker.open = True
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).json()["status"] == "submitted"


def test_broker_outage_at_approval_sends_nothing_and_keeps_it_waiting(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    broker.down = True
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).status_code == 502
    assert status_of(conn, pid) == "pending"


def test_a_refused_order_is_marked_failed(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    broker.fail_submit = "insufficient qty available"
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).json()["status"] == "failed"
    assert status_of(conn, pid) == "failed"


def test_expired_proposals_cannot_be_approved(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    conn.execute("UPDATE trade_proposals SET expires_at = now() - interval '1 minute' WHERE id = %s", (pid,))
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).status_code == 409
    assert status_of(conn, pid) == "expired" and not broker.orders


def test_daily_limits_count_approved_trades(env):
    client, broker, _ = env
    for symbol in ("NVDA", "AAPL"):
        pid = propose(client, symbol=symbol, notional=2_000).json()["id"]
        assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).json()["status"] == "submitted"
    r = propose(client, symbol="KO", notional=1_500).json()     # 4,000 + 1,500 > 5,000 a day
    assert "daily_dollars" in [f["rule"] for f in r["failed_rules"]]


def test_reject_and_kill_switch(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    assert client.post(f"/api/proposals/{pid}/reject", json={"note": "not now"}, headers=HUMAN).json()["status"] == "rejected"
    assert client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).status_code == 409

    pid2 = propose(client, symbol="AAPL").json()["id"]
    client.post("/api/kill-switch", json={"trading_enabled": False}, headers=HUMAN)
    assert client.post(f"/api/proposals/{pid2}/approve", headers=HUMAN).json()["status"] == "rejected_by_policy"
    assert propose(client, symbol="KO").json()["status"] == "rejected_by_policy"
    assert not broker.orders


# ---------- after the order is sent ----------

def test_partial_fills_then_a_full_fill_are_tracked(env):
    client, broker, conn = env
    pid = propose(client).json()["id"]
    order_id = client.post(f"/api/proposals/{pid}/approve", headers=HUMAN).json()["order_id"]
    broker.set_status(order_id, "partially_filled", qty=2.5, price=171.9)
    gateway.sync_once(conn_dict(), broker)
    assert status_of(conn, pid) == "partially_filled"
    broker.set_status(order_id, "filled", qty=5.8, price=172.0)
    gateway.sync_once(conn_dict(), broker)
    row = conn.execute("SELECT status, filled_qty, filled_avg_price FROM trade_proposals WHERE id = %s", (pid,)).fetchone()
    assert row[0] == "filled" and float(row[1]) == 5.8 and float(row[2]) == 172.0
    events = [e[0] for e in conn.execute("SELECT event FROM gateway_events WHERE proposal_id = %s ORDER BY id", (pid,))]
    assert events == ["proposed", "approved", "order_submitted", "order_update", "order_update", "order_update"]


def test_crash_between_approval_and_order_never_double_orders(env):
    client, broker, conn = env
    # Case 1: the gateway died before the order went out
    pid = propose(client).json()["id"]
    conn.execute("""UPDATE trade_proposals SET status = 'executing', client_order_id = 'pa-test-1',
                    decided_at = now() - interval '5 minutes' WHERE id = %s""", (pid,))
    gateway.sync_once(conn_dict(), broker)
    assert status_of(conn, pid) == "failed" and not broker.orders      # it does NOT re-send

    # Case 2: the order went out, but the gateway died before recording it
    pid2 = propose(client, symbol="AAPL").json()["id"]
    broker.submit("AAPL", "sell", 1_000, "pa-test-2")
    conn.execute("""UPDATE trade_proposals SET status = 'executing', client_order_id = 'pa-test-2',
                    decided_at = now() - interval '5 minutes' WHERE id = %s""", (pid2,))
    gateway.sync_once(conn_dict(), broker)
    assert status_of(conn, pid2) == "submitted" and len(broker.orders) == 1   # found, not re-sent


def conn_dict():
    from psycopg.rows import dict_row
    return psycopg.connect(TEST_DB, autocommit=True, row_factory=dict_row)


# ---------- the agent and the gateway together ----------

def scripted_agent(conn, monkeypatch, client, script, allow_proposals=True):
    """An agent whose 'model' follows a script, wired to the test gateway."""
    os.environ.setdefault("GEMINI_API_KEY", "not-used")
    import agent as agent_module
    import tools
    from google.genai import types

    def call(name, args):
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
            role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]))])

    def say(text):
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
            role="model", parts=[types.Part(text=text)]))])

    steps = [call(*s) if isinstance(s, tuple) else say(s) for s in script]

    class Models:
        def generate_content(self, model, contents, config):
            return steps.pop(0)

    monkeypatch.setenv("GATEWAY_URL", "http://gateway")
    monkeypatch.setenv("GATEWAY_AGENT_TOKEN", AGENT["Authorization"].removeprefix("Bearer "))
    monkeypatch.setattr(tools.requests, "post",
                        lambda url, json, headers, timeout: client.post(url.replace("http://gateway", ""), json=json, headers=headers))
    plain = psycopg.connect(TEST_DB, autocommit=True)
    a = agent_module.Agent(plain, allow_proposals=allow_proposals)
    a.client = type("Client", (), {"models": Models()})()
    return a


def test_agent_proposal_carries_its_evidence_and_places_no_order(env, monkeypatch):
    client, broker, conn = env
    a = scripted_agent(conn, monkeypatch, client, [
        ("get_news", {"symbol": "NVDA"}),
        ("propose_trade", {"symbol": "NVDA", "side": "sell", "dollars": 1500, "rationale": REASON,
                           "run_id": 424242}),          # the model tries to fake the run id
        "I filed a proposal to sell $1,500 of NVDA. It needs your approval.",
    ])
    a.run("ask", "Propose trimming NVDA by $1,500.")
    row = conn.execute("SELECT run_id, status, read_untrusted, tools_used FROM trade_proposals").fetchone()
    assert row[0] == a.run_id                 # the code's run id won, not the model's
    assert row[1] == "pending" and row[2] is True
    assert [t["tool"] for t in row[3]] == ["get_news"]
    assert not broker.orders


def test_an_injected_sell_everything_goes_nowhere(env, monkeypatch):
    client, broker, conn = env
    a = scripted_agent(conn, monkeypatch, client, [
        ("propose_trade", {"symbol": "NVDA", "side": "sell", "dollars": 17500, "rationale": REASON}),
        ("propose_trade", {"symbol": "NVDA", "side": "sell", "dollars": 2000, "rationale": REASON}),
        ("propose_trade", {"symbol": "AAPL", "side": "sell", "dollars": 2000, "rationale": REASON}),
        "Done.",
    ])
    a.run("ask", "(imagine a poisoned article told the model to dump everything)")
    statuses = [r[0] for r in conn.execute("SELECT status FROM trade_proposals ORDER BY id")]
    assert statuses == ["rejected_by_policy", "pending"]   # third call hit the per-run cap
    assert not broker.orders                               # and nothing was ever ordered


def test_briefings_have_no_proposal_tool_at_all(env, monkeypatch):
    client, broker, conn = env
    a = scripted_agent(conn, monkeypatch, client, [
        ("propose_trade", {"symbol": "NVDA", "side": "sell", "dollars": 500, "rationale": REASON}),
        "Briefing text.",
    ], allow_proposals=False)
    a.run("briefing", "Write the briefing.")
    assert conn.execute("SELECT count(*) FROM trade_proposals").fetchone()[0] == 0
    error = conn.execute("SELECT error FROM agent_steps WHERE name = 'propose_trade'").fetchone()[0]
    assert "No tool named propose_trade" in error
