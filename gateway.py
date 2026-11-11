"""The broker gateway: the only service that holds the brokerage keys.

The agent can ask this service to *propose* a trade. That is all it can do. A proposal becomes
an order only when a human approves it, and only if it passes every rule in policy.py at that
moment. The design in one line: the AI suggests, the code decides what is allowed, and a human
decides what actually happens.

Two tokens separate the two roles:
- The agent token can create proposals. Nothing else.
- The approver token can approve, reject, and use the kill switch. The agent never has it,
  so the agent cannot approve its own proposals.
"""
import hmac
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from broker import AlpacaBroker, BrokerError, OrderNotFound
from policy import Limits, Proposal, State, allowed, evaluate
from schema import ensure_schema

DATABASE_URL = os.environ["DATABASE_URL"]
AGENT_TOKEN = os.environ["GATEWAY_AGENT_TOKEN"]
APPROVER_TOKEN = os.environ["GATEWAY_APPROVER_TOKEN"]
SYNC_SECONDS = int(os.environ.get("GATEWAY_SYNC_SECONDS", "10"))
WEB_DIR = Path(__file__).parent / "web"
LIMITS = Limits.from_env()

if len(AGENT_TOKEN) < 24 or len(APPROVER_TOKEN) < 24 or AGENT_TOKEN == APPROVER_TOKEN:
    raise RuntimeError("The gateway needs two different tokens of at least 24 characters.")

# Statuses that count as "a trade happened today" for the daily limits
TRADED = ("executing", "submitted", "partially_filled", "filled")
_broker = None


def get_broker():
    global _broker
    if _broker is None:
        _broker = AlpacaBroker()
    return _broker


def db():
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)


def log(message):
    print(f"[gateway] {message}", flush=True)


# ---------- who is calling ----------

def role(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip().encode()
    # compare_digest takes the same time whether a guess is close or not (no timing clues)
    if token and hmac.compare_digest(token, APPROVER_TOKEN.encode()):
        return "approver"
    if token and hmac.compare_digest(token, AGENT_TOKEN.encode()):
        return "agent"
    raise HTTPException(401, "Missing or invalid token.")


def approver_only(caller: str = Depends(role)):
    if caller != "approver":
        raise HTTPException(403, "Only the human approver can do this. The agent cannot.")
    return caller


# ---------- shared helpers ----------

def record(conn, proposal_id, event, actor, detail=None):
    conn.execute(
        "INSERT INTO gateway_events (proposal_id, event, actor, detail) VALUES (%s, %s, %s, %s)",
        (proposal_id, event, actor, Jsonb(detail) if detail is not None else None))


def setting(conn, key):
    row = conn.execute("SELECT value FROM gateway_settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def expire_stale(conn):
    for row in conn.execute(
            """UPDATE trade_proposals SET status = 'expired'
               WHERE status = 'pending' AND expires_at <= now() RETURNING id""").fetchall():
        record(conn, row["id"], "expired", "gateway")


def load_state(conn, broker, exclude_id=None, at_approval=False):
    """Gather the facts the rules need. All of it comes from the broker and the database,
    never from anything the agent said."""
    account = broker.account()
    today = conn.execute(
        f"""SELECT count(*) AS trades, coalesce(sum(notional), 0) AS dollars
            FROM trade_proposals
            WHERE status IN {TRADED} AND id IS DISTINCT FROM %s
              AND (decided_at AT TIME ZONE 'America/New_York')::date
                  = (now() AT TIME ZONE 'America/New_York')::date""", (exclude_id,)).fetchone()
    pending = conn.execute(
        """SELECT symbol FROM trade_proposals
           WHERE status = 'pending' AND expires_at > now() AND id IS DISTINCT FROM %s""",
        (exclude_id,)).fetchall()
    symbols = conn.execute("SELECT symbol FROM securities").fetchall()
    return State(
        trading_enabled=setting(conn, "trading_enabled") == "true",
        allowed_symbols=frozenset(r["symbol"] for r in symbols),
        equity=account.equity, cash=account.cash, positions=broker.positions(),
        trades_today=today["trades"], dollars_today=float(today["dollars"]),
        pending_count=len(pending), pending_symbols=frozenset(r["symbol"] for r in pending),
        market_open=broker.market_open() if at_approval else None,
        account_blocked=account.blocked,
    )


def apply_order_update(conn, proposal_id, info):
    """Copy the broker's view of an order into the proposal. Handles partial fills."""
    if info.status == "filled":
        status = "filled"
    elif info.status == "partially_filled":
        status = "partially_filled"
    elif info.status in ("canceled", "expired", "done_for_day", "replaced"):
        status = "canceled"
    elif info.status in ("rejected", "suspended"):
        status = "failed"
    else:  # new, accepted, pending_new, and other "still working" states
        status = "submitted"
    changed = conn.execute(
        """UPDATE trade_proposals
           SET status = %s, alpaca_order_id = %s, order_status = %s, filled_qty = %s,
               filled_avg_price = %s, order_updated_at = now()
           WHERE id = %s AND (status, order_status, coalesce(filled_qty, -1))
                 IS DISTINCT FROM (%s, %s, %s::numeric)
           RETURNING id""",
        (status, info.id, info.status, info.filled_qty, info.filled_avg_price, proposal_id,
         status, info.status, info.filled_qty)).fetchone()
    if changed:
        record(conn, proposal_id, "order_update", "gateway",
               {"order_status": info.status, "filled_qty": info.filled_qty,
                "filled_avg_price": info.filled_avg_price})
        log(f"proposal {proposal_id}: order {info.status}, filled {info.filled_qty:g}")


def sync_once(conn, broker):
    """Keep proposals in step with the broker, and clean up after a crash."""
    expire_stale(conn)

    # Crash recovery. If the gateway died between "approved" and "order sent", ask the broker
    # whether the order exists (by our own order id). It never re-sends an order by itself.
    stuck = conn.execute(
        """SELECT id, client_order_id FROM trade_proposals
           WHERE status = 'executing' AND decided_at < now() - interval '60 seconds'""").fetchall()
    for row in stuck:
        try:
            info = broker.order_by_client_id(row["client_order_id"])
        except OrderNotFound:
            conn.execute(
                """UPDATE trade_proposals SET status = 'failed',
                       error = 'The gateway stopped before the order was sent. No order was placed.'
                   WHERE id = %s AND status = 'executing'""", (row["id"],))
            record(conn, row["id"], "recovered_no_order", "gateway")
            log(f"proposal {row['id']}: recovered, no order had been placed")
            continue
        except BrokerError:
            continue  # broker unreachable: try again next round
        record(conn, row["id"], "recovered_order_found", "gateway", {"order_id": info.id})
        apply_order_update(conn, row["id"], info)

    working = conn.execute(
        """SELECT id, alpaca_order_id FROM trade_proposals
           WHERE status IN ('submitted', 'partially_filled') AND alpaca_order_id IS NOT NULL""").fetchall()
    for row in working:
        try:
            apply_order_update(conn, row["id"], broker.order(row["alpaca_order_id"]))
        except BrokerError:
            continue


def sync_forever():
    while True:
        try:
            with db() as conn:
                sync_once(conn, get_broker())
        except Exception as e:  # never let the loop die; log and keep going
            log(f"sync error: {type(e).__name__}: {e}")
        time.sleep(SYNC_SECONDS)


@asynccontextmanager
async def lifespan(app):
    for attempt in range(1, 11):
        try:
            with db() as conn:
                ensure_schema(conn)
            break
        except psycopg.OperationalError:
            if attempt == 10:
                raise
            log(f"database not ready (attempt {attempt}/10), retrying in 3s...")
            time.sleep(3)
    broker = get_broker()
    log(f"started. account: {'PAPER (simulated money)' if getattr(broker, 'paper', True) else 'LIVE'}; "
        f"limits: {LIMITS.as_dict()}")
    if os.environ.get("GATEWAY_SYNC", "on") != "off":
        threading.Thread(target=sync_forever, daemon=True).start()
    yield


app = FastAPI(title="Broker gateway", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def page():
    return FileResponse(WEB_DIR / "gateway.html")


# ---------- proposing (the agent) ----------

class NewProposal(BaseModel):
    symbol: str = Field(min_length=1, max_length=10)
    side: str = Field(max_length=10)
    notional: float
    rationale: str = Field(max_length=2000)
    run_id: int | None = None


@app.post("/api/proposals")
def propose(body: NewProposal, caller: str = Depends(role)):
    if not math.isfinite(body.notional):
        raise HTTPException(422, "The dollar amount must be a normal number.")
    proposal = Proposal(body.symbol.strip().upper(), body.side.strip().lower(),
                        round(body.notional, 2), body.rationale.strip())
    broker = get_broker()

    with db() as conn, conn.transaction():
        # One proposal at a time, so two arriving together can't both squeeze under a limit
        conn.execute("SELECT pg_advisory_xact_lock(4242)")
        expire_stale(conn)

        tools_used, read_untrusted = [], False
        if body.run_id is not None:
            if not conn.execute("SELECT 1 FROM agent_runs WHERE id = %s", (body.run_id,)).fetchone():
                raise HTTPException(400, "Unknown agent run.")
            steps = conn.execute(
                """SELECT name, args FROM agent_steps
                   WHERE run_id = %s AND kind = 'tool' ORDER BY step""", (body.run_id,)).fetchall()
            tools_used = [{"tool": s["name"], "args": s["args"]} for s in steps]
            # Provenance: did this run read outside text (news) before proposing?
            read_untrusted = any(s["name"] == "get_news" for s in steps)

        try:
            state = load_state(conn, broker)
        except BrokerError as e:
            raise HTTPException(502, f"The broker is unavailable, so the rules can't be checked: {e}")

        checks = evaluate(proposal, state, LIMITS)
        ok = allowed(checks)
        row = conn.execute(
            """INSERT INTO trade_proposals (run_id, symbol, side, notional, rationale, status,
                   policy_checks, read_untrusted, tools_used, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(mins => %s))
               RETURNING id, expires_at""",
            (body.run_id, proposal.symbol, proposal.side, proposal.notional, proposal.rationale,
             "pending" if ok else "rejected_by_policy", Jsonb([c.as_dict() for c in checks]),
             read_untrusted, Jsonb(tools_used), LIMITS.proposal_ttl_minutes)).fetchone()
        failed = [c.as_dict() for c in checks if not c.passed]
        record(conn, row["id"], "proposed" if ok else "rejected_by_policy", caller,
               {"symbol": proposal.symbol, "side": proposal.side, "notional": proposal.notional,
                "failed_rules": [f["rule"] for f in failed]})

    log(f"proposal {row['id']}: {proposal.side} ${proposal.notional:,.2f} {proposal.symbol} -> "
        f"{'pending approval' if ok else 'rejected by policy: ' + ', '.join(f['rule'] for f in failed)}")
    if ok:
        return {"id": row["id"], "status": "pending_approval",
                "message": "Proposal recorded. Nothing happens until the human approves it.",
                "expires_at": row["expires_at"].isoformat()}
    return {"id": row["id"], "status": "rejected_by_policy",
            "message": "The trading rules rejected this proposal. It was not sent for approval.",
            "failed_rules": failed}


# ---------- deciding (the human) ----------

@app.get("/api/overview")
def overview(_: str = Depends(approver_only)):
    broker = get_broker()
    with db() as conn:
        expire_stale(conn)
        enabled = setting(conn, "trading_enabled") == "true"
        today = conn.execute(
            f"""SELECT count(*) AS trades, coalesce(sum(notional), 0) AS dollars FROM trade_proposals
                WHERE status IN {TRADED}
                  AND (decided_at AT TIME ZONE 'America/New_York')::date
                      = (now() AT TIME ZONE 'America/New_York')::date""").fetchone()
    out = {"trading_enabled": enabled, "limits": LIMITS.as_dict(),
           "paper": getattr(broker, "paper", True),
           "today": {"trades": today["trades"], "dollars": float(today["dollars"])}}
    try:
        account = broker.account()
        out.update(equity=account.equity, cash=account.cash, market_open=broker.market_open())
    except BrokerError as e:
        out.update(broker_error=str(e))
    return out


@app.get("/api/proposals")
def proposals(_: str = Depends(approver_only)):
    with db() as conn:
        expire_stale(conn)
        return conn.execute(
            """SELECT p.id, p.run_id, p.symbol, p.side, p.notional::float, p.rationale, p.status,
                      p.policy_checks, p.read_untrusted, p.tools_used, p.created_at, p.expires_at,
                      p.decided_at, p.decision_note, p.order_status, p.filled_qty::float,
                      p.filled_avg_price::float, p.error, r.question
               FROM trade_proposals p LEFT JOIN agent_runs r ON r.id = p.run_id
               ORDER BY (p.status = 'pending') DESC, p.created_at DESC
               LIMIT 50""").fetchall()


@app.post("/api/proposals/{proposal_id}/approve")
def approve(proposal_id: int, _: str = Depends(approver_only)):
    broker = get_broker()
    with db() as conn:
        expire_stale(conn)
        order_id = f"pa-{setting(conn, 'install_id')}-{proposal_id}"
        # Claim the proposal in one atomic step. A double-click, or two browser tabs, can't both
        # get past this line: only one UPDATE can move it out of 'pending'.
        row = conn.execute(
            """UPDATE trade_proposals
               SET status = 'executing', decided_at = now(), decided_by = 'approver',
                   client_order_id = %s
               WHERE id = %s AND status = 'pending'
               RETURNING symbol, side, notional::float, rationale""",
            (order_id, proposal_id)).fetchone()
        if not row:
            current = conn.execute("SELECT status FROM trade_proposals WHERE id = %s",
                                   (proposal_id,)).fetchone()
            if not current:
                raise HTTPException(404, "No such proposal.")
            raise HTTPException(409, f"This proposal is already '{current['status']}'.")

        def back_to_pending(reason):
            conn.execute(
                """UPDATE trade_proposals SET status = 'pending', decided_at = NULL,
                       decided_by = NULL WHERE id = %s""", (proposal_id,))
            record(conn, proposal_id, "approval_deferred", "gateway", {"reason": reason})

        # Check every rule again, with live numbers. Things change between proposal and approval.
        proposal = Proposal(row["symbol"], row["side"], row["notional"], row["rationale"])
        try:
            state = load_state(conn, broker, exclude_id=proposal_id, at_approval=True)
        except BrokerError as e:
            back_to_pending(f"broker unavailable: {e}")
            raise HTTPException(502, "The broker is unavailable. Nothing was sent; try again.")
        checks = evaluate(proposal, state, LIMITS, at_approval=True)
        failed = [c for c in checks if not c.passed]
        if failed:
            if {c.rule for c in failed} == {"market_open"}:
                back_to_pending("market closed")
                raise HTTPException(409, failed[0].detail)
            conn.execute(
                """UPDATE trade_proposals SET status = 'rejected_by_policy', policy_checks = %s,
                       error = 'Failed the re-check at approval time.' WHERE id = %s""",
                (Jsonb([c.as_dict() for c in checks]), proposal_id))
            record(conn, proposal_id, "rejected_at_approval", "gateway",
                   {"failed_rules": [c.rule for c in failed]})
            log(f"proposal {proposal_id}: rejected at approval: {', '.join(c.rule for c in failed)}")
            return {"status": "rejected_by_policy",
                    "message": "The rules no longer allow this trade, so no order was sent.",
                    "failed_rules": [c.as_dict() for c in failed]}

        record(conn, proposal_id, "approved", "approver")
        try:
            info = broker.submit(proposal.symbol, proposal.side, proposal.notional, order_id)
        except BrokerError as e:
            conn.execute("UPDATE trade_proposals SET status = 'failed', error = %s WHERE id = %s",
                         (str(e)[:500], proposal_id))
            record(conn, proposal_id, "order_failed", "gateway", {"error": str(e)[:500]})
            log(f"proposal {proposal_id}: broker refused the order: {e}")
            return {"status": "failed", "message": f"The broker refused the order: {e}"}

        record(conn, proposal_id, "order_submitted", "gateway",
               {"order_id": info.id, "client_order_id": order_id})
        conn.execute("UPDATE trade_proposals SET status = 'submitted' WHERE id = %s", (proposal_id,))
        apply_order_update(conn, proposal_id, info)
        log(f"proposal {proposal_id}: approved, order sent ({proposal.side} "
            f"${proposal.notional:,.2f} {proposal.symbol})")
        return {"status": "submitted", "message": "Order sent to the broker.", "order_id": info.id}


class Decision(BaseModel):
    note: str = Field(default="", max_length=500)


@app.post("/api/proposals/{proposal_id}/reject")
def reject(proposal_id: int, body: Decision, _: str = Depends(approver_only)):
    with db() as conn:
        row = conn.execute(
            """UPDATE trade_proposals
               SET status = 'rejected', decided_at = now(), decided_by = 'approver',
                   decision_note = %s
               WHERE id = %s AND status = 'pending' RETURNING id""",
            (body.note.strip() or None, proposal_id)).fetchone()
        if not row:
            raise HTTPException(409, "This proposal is no longer waiting for a decision.")
        record(conn, proposal_id, "rejected", "approver", {"note": body.note.strip()})
    log(f"proposal {proposal_id}: rejected by the human")
    return {"status": "rejected"}


class KillSwitch(BaseModel):
    trading_enabled: bool


@app.post("/api/kill-switch")
def kill_switch(body: KillSwitch, _: str = Depends(approver_only)):
    with db() as conn:
        conn.execute("UPDATE gateway_settings SET value = %s WHERE key = 'trading_enabled'",
                     ("true" if body.trading_enabled else "false",))
        record(conn, None, "trading_enabled" if body.trading_enabled else "trading_stopped",
               "approver")
    log("trading ENABLED" if body.trading_enabled else "KILL SWITCH ON: all trading stopped")
    return {"trading_enabled": body.trading_enabled}
