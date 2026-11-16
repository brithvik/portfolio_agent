"""Screens: find stocks that are rising with sustained demand, using only prices and volume.

Everything here is plain arithmetic on daily bars. No model is involved, so the same inputs
always give the same answer, and every threshold lives in a versioned rules file
(rules/screens_v1.json) that was fixed before any results were collected.

What gets measured for each stock (all "excess" figures are versus SPY over the same days):

  excess_3m, excess_6m   return minus the market's return over 63 and 126 trading days
  persistence            share of the last 8 weeks in which it beat the market (sustained, not a spike)
  accumulation           dollars traded per day in the last 20 days versus the 120 days before
  below_high             how far the close is under its 52-week high
  ret_1m, above_50d      how fast it has run recently (used to flag "extended, hold off")
  volatility             yearly swing implied by the last 60 days

A stock is a CANDIDATE when it clears every "rising" threshold and is not "extended".
Candidates are ranked by their average percentile on four signals. A candidate is a stock
worth reading about, not a recommendation: the news layer and the human come after this.
"""
import hashlib
import json
import math
import os
import statistics
from pathlib import Path

from psycopg.types.json import Jsonb

RULES_PATH = Path(os.environ.get("SCREEN_RULES", Path(__file__).parent / "rules" / "screens_v1.json"))
BENCHMARK = "SPY"
LOOKBACK_DAYS = 400      # calendar days of bars to load: a little over 252 trading days
RANKED_ON = ("excess_6m", "excess_3m", "persistence", "accumulation")


def load_rules(path=RULES_PATH):
    """The rules plus a version string that changes if anyone edits the file."""
    raw = Path(path).read_bytes()
    rules = json.loads(raw)
    rules["rules_version"] = f"{rules['version']}@{hashlib.sha256(raw).hexdigest()[:8]}"
    return rules


def measure(days, closes, highs, volumes, bench, rules):
    """Signals for one stock. Lists are oldest first; bench maps day -> benchmark close.

    Returns None when there isn't enough history (or the benchmark is missing a day), which
    the caller records as 'too_new' instead of guessing.
    """
    n = len(closes)
    if n < rules["min_history_days"]:
        return None

    def ret(lookback):
        return closes[-1] / closes[-1 - lookback] - 1

    def bench_ret(start, end):   # indexes into days
        a, b = bench.get(days[start]), bench.get(days[end])
        return None if not a or not b else b / a - 1

    market_3m, market_6m = bench_ret(-64, -1), bench_ret(-127, -1)
    if market_3m is None or market_6m is None:
        return None

    weeks_ahead, weeks = 0, 0
    for w in range(8):                       # the last 8 blocks of 5 trading days
        end, start = -1 - 5 * w, -6 - 5 * w
        market = bench_ret(start, end)
        if market is None:
            continue
        weeks += 1
        weeks_ahead += closes[end] / closes[start] - 1 > market
    if weeks < 6:
        return None

    dollars = [c * v for c, v in zip(closes, volumes)]
    recent, before = dollars[-20:], dollars[-140:-20]
    daily = [closes[i] / closes[i - 1] - 1 for i in range(n - 60, n)]
    return {
        "close": round(closes[-1], 4),
        "excess_3m": round(ret(63) - market_3m, 4),
        "excess_6m": round(ret(126) - market_6m, 4),
        "persistence": round(weeks_ahead / weeks, 3),
        "accumulation": round(statistics.fmean(recent) / statistics.fmean(before), 3) if before and statistics.fmean(before) > 0 else None,
        "below_high": round(1 - closes[-1] / max(highs[-252:]), 4),
        "ret_1m": round(ret(21), 4),
        "above_50d": round(closes[-1] / statistics.fmean(closes[-50:]) - 1, 4),
        "volatility": round(statistics.pstdev(daily) * math.sqrt(252), 4),
        "history_days": n,
    }


def classify(signals, rules):
    """Flags for one stock's signals: 'rising', 'extended', or neither. Pure thresholds."""
    r, x = rules["rising"], rules["extended"]
    flags = []
    if (signals["excess_3m"] > r["min_excess_3m"] and signals["excess_6m"] > r["min_excess_6m"]
            and signals["persistence"] >= r["min_persistence"]
            and (signals["accumulation"] or 0) >= r["min_accumulation"]
            and signals["below_high"] <= r["max_below_high"]):
        flags.append("rising")
    if signals["ret_1m"] > x["ret_1m_over"] or signals["above_50d"] > x["above_50d_over"]:
        flags.append("extended")   # ran up too fast: worth knowing about, not worth chasing
    return flags


def rank(rows):
    """Score candidates by their average percentile (among all measured stocks) on four signals."""
    measured = [r for r in rows if r["signals"]]
    for key in RANKED_ON:
        ordered = sorted(measured, key=lambda r: r["signals"][key] or 0)
        for position, r in enumerate(ordered):
            r.setdefault("percentiles", []).append(position / max(len(ordered) - 1, 1))
    for r in measured:
        r["score"] = round(statistics.fmean(r.pop("percentiles")), 4)
    candidates = sorted((r for r in measured if r["candidate"]), key=lambda r: -r["score"])
    for position, r in enumerate(candidates, start=1):
        r["rank"] = position
    return rows


def run(conn, rules=None):
    """Measure every stock in today's universe and save the results. Returns (day, rows)."""
    rules = rules or load_rules()
    day = conn.execute("SELECT max(day) FROM daily_bars WHERE symbol = %s", (BENCHMARK,)).fetchone()[0]
    if day is None:
        raise RuntimeError(f"No {BENCHMARK} prices yet. Run: python market.py update")
    as_of = conn.execute("SELECT max(as_of) FROM universe WHERE as_of <= %s", (day,)).fetchone()[0]
    if as_of is None:
        raise RuntimeError("No universe yet. Run: python market.py update")
    members = dict(conn.execute(
        "SELECT symbol, tier FROM universe WHERE as_of = %s AND kind = 'stock'", (as_of,)).fetchall())
    bench = {d: float(c) for d, c in conn.execute(
        "SELECT day, close FROM daily_bars WHERE symbol = %s AND day > %s::date - %s", (BENCHMARK, day, LOOKBACK_DAYS))}

    rows, series, current = [], ([], [], [], []), None

    def finish(symbol):
        days, closes, highs, volumes = series
        # A stock that didn't trade on the latest day (halted, delisted) is not measured today
        signals = measure(days, closes, highs, volumes, bench, rules) if days and days[-1] == day else None
        flags = classify(signals, rules) if signals else ["too_new"]
        rows.append({"symbol": symbol, "tier": members[symbol], "signals": signals, "flags": flags,
                     "candidate": "rising" in flags and "extended" not in flags, "score": None, "rank": None})

    # One pass over the bars, a stock at a time, so memory stays small however big the universe is
    with conn.transaction(), conn.cursor(name="screen_bars") as cursor:
        cursor.execute(
            """SELECT symbol, day, close, high, volume FROM daily_bars
               WHERE day > %s::date - %s AND day <= %s AND symbol = ANY(%s) ORDER BY symbol, day""",
            (day, LOOKBACK_DAYS, day, list(members)))
        for symbol, d, close, high, volume in cursor:
            if symbol != current:
                if current is not None:
                    finish(current)
                current, series = symbol, ([], [], [], [])
            for values, value in zip(series, (d, float(close), float(high), float(volume))):
                values.append(value)
        if current is not None:
            finish(current)

    rank(rows)
    with conn.transaction():
        conn.execute("DELETE FROM screen_results WHERE day = %s AND rules_version = %s", (day, rules["rules_version"]))
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO screen_results (day, symbol, rules_version, tier, signals, flags, candidate, score, rank)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(day, r["symbol"], rules["rules_version"], r["tier"], Jsonb(r["signals"] or {}), r["flags"],
                  r["candidate"], r["score"], r["rank"]) for r in rows])
    return day, rows


def self_check(conn, day, rows, sample=5):
    """Recompute a few results a second, independent way (in SQL) and refuse to continue if they differ.

    The screens walk Python lists; this asks the database for the same closing prices by
    position and does the arithmetic again. Two routes to one number catch an off-by-one or a
    misaligned date that a single route would hide.
    """
    candidates = sorted((r for r in rows if r["candidate"]), key=lambda r: r["rank"])[:sample]
    for r in candidates:
        closes = dict(conn.execute(
            """WITH s AS (SELECT day, close, row_number() OVER (ORDER BY day DESC) AS n
                          FROM daily_bars WHERE symbol = %s AND day <= %s)
               SELECT s.n, s.close / b.close FROM s JOIN daily_bars b ON b.symbol = %s AND b.day = s.day
               WHERE s.n IN (1, 127)""", (r["symbol"], day, BENCHMARK)).fetchall())
        stock = conn.execute(
            """SELECT max(close) FILTER (WHERE n = 1) / max(close) FILTER (WHERE n = 127) - 1 FROM (
                   SELECT close, row_number() OVER (ORDER BY day DESC) AS n FROM daily_bars
                   WHERE symbol = %s AND day <= %s) s""", (r["symbol"], day)).fetchone()[0]
        # stock/benchmark ratios at both ends give the benchmark's return without reading it directly
        market = (1 + float(stock)) * float(closes[127]) / float(closes[1]) - 1
        expected = float(stock) - market
        if abs(expected - r["signals"]["excess_6m"]) > 0.0002:
            raise RuntimeError(f"Self-check failed for {r['symbol']}: the screens say {r['signals']['excess_6m']:+.4f} "
                               f"versus the market over 6 months, a second calculation says {expected:+.4f}.")
    return len(candidates)


def summary(day, rows, rules, top=15):
    measured = [r for r in rows if r["signals"]]
    candidates = sorted((r for r in rows if r["candidate"]), key=lambda r: r["rank"])
    extended = [r for r in measured if "extended" in r["flags"]]
    lines = [f"Screens for {day} ({rules['rules_version']}): {len(rows)} stocks, {len(measured)} with enough "
             f"history, {len(candidates)} rising candidates, {len(extended)} flagged as extended."]
    if candidates:
        lines.append(f"\n  {'#':>3} {'Symbol':<7}{'Tier':<12}{'vs mkt 6m':>10}{'vs mkt 3m':>10}{'Weeks ahead':>12}{'Volume x':>9}{'Below high':>11}")
        for r in candidates[:top]:
            s = r["signals"]
            lines.append(f"  {r['rank']:>3} {r['symbol']:<7}{r['tier']:<12}{s['excess_6m']:>+10.1%}{s['excess_3m']:>+10.1%}"
                         f"{s['persistence']:>12.0%}{s['accumulation']:>9.2f}{s['below_high']:>11.1%}")
    lines.append("\nThese are stocks worth reading about, not recommendations.")
    return "\n".join(lines)
