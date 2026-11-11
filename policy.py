"""The trading rules. Pure code, no AI: the agent cannot argue, persuade, or prompt its way
past anything in this file.

Every proposal is checked here twice: once when the agent proposes it, and again at the moment
a human approves it, because cash, positions, and the market can change in between.

Each rule returns a Check, so the approval screen can show exactly which rules passed and why.
"""
from dataclasses import asdict, dataclass, field
import os


@dataclass(frozen=True)
class Limits:
    max_trade_dollars: float = 2_000.0      # biggest single trade
    min_trade_dollars: float = 5.0          # smallest (avoids dust orders)
    max_daily_dollars: float = 5_000.0      # total traded per day, buys and sells together
    max_trades_per_day: int = 5
    max_position_pct: float = 25.0          # no single holding above this share of the portfolio
    min_cash_pct: float = 5.0               # buys can't push cash below this share
    max_pending: int = 3                  # caps how many proposals can wait for the human
    proposal_ttl_minutes: int = 120       # proposals expire, so stale ones can't be approved
    min_rationale_chars: int = 30

    @classmethod
    def from_env(cls):
        def get(name, cast):
            raw = os.environ.get(name.upper())
            return cast(raw) if raw not in (None, "") else getattr(cls, name)
        return cls(**{f: get(f, type(getattr(cls, f))) for f in cls.__dataclass_fields__})

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Proposal:
    symbol: str
    side: str            # 'buy' or 'sell'
    notional: float      # dollars
    rationale: str


@dataclass(frozen=True)
class State:
    """Everything the rules need to know, gathered live by the gateway (never from the agent)."""
    trading_enabled: bool
    allowed_symbols: frozenset
    equity: float
    cash: float                            # cash only; margin buying power is never used
    positions: dict = field(default_factory=dict)   # symbol -> market value in dollars
    trades_today: int = 0
    dollars_today: float = 0.0
    pending_count: int = 0
    pending_symbols: frozenset = frozenset()
    market_open: bool | None = None        # only checked at approval time
    account_blocked: bool = False


@dataclass(frozen=True)
class Check:
    rule: str
    passed: bool
    detail: str

    def as_dict(self):
        return asdict(self)


def money(x):
    return f"${x:,.2f}"


def evaluate(p: Proposal, s: State, limits: Limits, at_approval: bool = False):
    """Run every rule. Returns a list of Checks; the trade is allowed only if all pass."""
    checks = []

    def add(rule, passed, detail):
        checks.append(Check(rule, bool(passed), detail))

    add("kill_switch", s.trading_enabled,
        "Trading is enabled." if s.trading_enabled else "The kill switch is on: all trading is stopped.")
    add("account_ok", not s.account_blocked,
        "The brokerage account is in good standing." if not s.account_blocked
        else "The brokerage has blocked trading on this account.")

    side_ok = p.side in ("buy", "sell")
    add("valid_side", side_ok, f"Side is '{p.side}'." if side_ok else "Side must be 'buy' or 'sell'.")

    allowed = p.symbol in s.allowed_symbols
    add("symbol_allowed", allowed,
        f"{p.symbol} is on the approved list." if allowed
        else f"{p.symbol} is not on the approved list ({', '.join(sorted(s.allowed_symbols))}).")

    amount_ok = limits.min_trade_dollars <= p.notional <= limits.max_trade_dollars
    add("trade_size", amount_ok,
        f"{money(p.notional)} is within {money(limits.min_trade_dollars)}-{money(limits.max_trade_dollars)} per trade."
        if amount_ok else
        f"{money(p.notional)} is outside the allowed {money(limits.min_trade_dollars)}-{money(limits.max_trade_dollars)} per trade.")

    rationale_len = len(p.rationale.strip())
    add("rationale_given", rationale_len >= limits.min_rationale_chars,
        "A rationale was provided." if rationale_len >= limits.min_rationale_chars
        else f"The rationale is too short ({rationale_len} characters; at least {limits.min_rationale_chars} needed).")

    if not at_approval:  # these only matter while a proposal is being created
        add("pending_limit", s.pending_count < limits.max_pending,
            f"{s.pending_count} of {limits.max_pending} proposals are waiting for a decision."
            if s.pending_count < limits.max_pending else
            f"{limits.max_pending} proposals are already waiting. Decide those first.")
        duplicate = p.symbol in s.pending_symbols
        add("no_duplicate", not duplicate,
            f"No other proposal for {p.symbol} is waiting." if not duplicate
            else f"A proposal for {p.symbol} is already waiting for a decision.")

    add("daily_trade_count", s.trades_today < limits.max_trades_per_day,
        f"{s.trades_today} of {limits.max_trades_per_day} trades used today."
        if s.trades_today < limits.max_trades_per_day else
        f"The daily limit of {limits.max_trades_per_day} trades is used up.")

    daily_after = s.dollars_today + p.notional
    add("daily_dollars", daily_after <= limits.max_daily_dollars,
        f"Today's total would be {money(daily_after)} of {money(limits.max_daily_dollars)}."
        if daily_after <= limits.max_daily_dollars else
        f"Today's total would be {money(daily_after)}, over the {money(limits.max_daily_dollars)} daily limit.")

    held = float(s.positions.get(p.symbol, 0.0))
    if p.side == "buy":
        add("cash_only", p.notional <= s.cash,
            f"{money(p.notional)} is covered by {money(s.cash)} cash. No borrowed money is used."
            if p.notional <= s.cash else
            f"{money(p.notional)} is more than the {money(s.cash)} cash available. Margin is never used.")
        cash_after_pct = (s.cash - p.notional) / s.equity * 100 if s.equity > 0 else -1
        add("cash_buffer", cash_after_pct >= limits.min_cash_pct,
            f"Cash would be {cash_after_pct:.1f}% of the portfolio (minimum {limits.min_cash_pct:g}%)."
            if cash_after_pct >= limits.min_cash_pct else
            f"Cash would fall to {cash_after_pct:.1f}% of the portfolio, below the {limits.min_cash_pct:g}% minimum.")
        weight_after = (held + p.notional) / s.equity * 100 if s.equity > 0 else 101
        add("position_size", weight_after <= limits.max_position_pct,
            f"{p.symbol} would be {weight_after:.1f}% of the portfolio (maximum {limits.max_position_pct:g}%)."
            if weight_after <= limits.max_position_pct else
            f"{p.symbol} would be {weight_after:.1f}% of the portfolio, over the {limits.max_position_pct:g}% maximum.")
    elif p.side == "sell":
        add("no_short_selling", p.notional <= held,
            f"Selling {money(p.notional)} of the {money(held)} held."
            if p.notional <= held else
            f"Only {money(held)} of {p.symbol} is held, so {money(p.notional)} can't be sold. Short selling is never allowed.")

    if at_approval:
        add("market_open", s.market_open is True,
            "The market is open." if s.market_open else
            "The market is closed. Approve during market hours so the order fills at a live price.")
    return checks


def allowed(checks):
    return all(c.passed for c in checks)
