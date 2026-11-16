"""Tests for the screens (pure arithmetic) and the market-data update (with a stand-in for Alpaca).

The database tests need PostgreSQL (set TEST_DATABASE_URL); the arithmetic tests always run.

    python -m pytest tests/test_screens.py -v
"""
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import screens

TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")
RULES = screens.load_rules()
TODAY = date(2026, 11, 13)


def trading_days(n, end=TODAY):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def series(n=260, daily=0.0, start=100.0, volume=1_000_000, last_month_volume=None, last_month_daily=None):
    """n days of prices growing by `daily` each day. Returns (days, closes, highs, volumes)."""
    days, closes, volumes, price = trading_days(n), [], [], start
    for i in range(n):
        rate = last_month_daily if last_month_daily is not None and i >= n - 21 else daily
        price *= 1 + rate
        closes.append(price)
        volumes.append(last_month_volume if last_month_volume and i >= n - 20 else volume)
    return days, closes, [c * 1.01 for c in closes], volumes


def market(n=260, daily=0.0003):
    days, closes, _, _ = series(n, daily)
    return dict(zip(days, closes))


def test_the_rules_file_carries_a_version_that_changes_with_its_contents(tmp_path):
    assert RULES["rules_version"].startswith("screens-v1@")
    edited = tmp_path / "rules.json"
    edited.write_text(screens.RULES_PATH.read_text().replace("1.2", "1.1"))
    assert screens.load_rules(edited)["rules_version"] != RULES["rules_version"]


def test_a_steady_riser_on_growing_volume_is_a_candidate():
    s = screens.measure(*series(daily=0.002, last_month_volume=1_600_000), market(), RULES)
    assert s["excess_6m"] > 0.15 and s["excess_3m"] > 0.05
    assert s["persistence"] == 1.0 and s["accumulation"] > 1.5 and s["below_high"] < 0.02
    assert screens.classify(s, RULES) == ["rising"]


def test_a_stock_that_only_matches_the_market_is_not():
    s = screens.measure(*series(daily=0.0003, last_month_volume=1_600_000), market(), RULES)
    assert abs(s["excess_6m"]) < 0.001
    assert screens.classify(s, RULES) == []


def test_rising_without_growing_volume_is_not_sustained_demand():
    s = screens.measure(*series(daily=0.002), market(), RULES)
    assert s["accumulation"] < 1.2 and "rising" not in screens.classify(s, RULES)


def test_a_one_month_spike_is_flagged_extended_not_chased():
    s = screens.measure(*series(daily=0.001, last_month_daily=0.03, last_month_volume=5_000_000), market(), RULES)
    assert s["ret_1m"] > 0.40
    assert "extended" in screens.classify(s, RULES)


def test_a_faller_is_never_a_candidate():
    s = screens.measure(*series(daily=-0.002, last_month_volume=3_000_000), market(), RULES)
    assert s["excess_6m"] < 0 and screens.classify(s, RULES) == []


def test_too_little_history_is_not_measured():
    assert screens.measure(*series(n=100, daily=0.002), market(), RULES) is None


def test_excess_return_is_measured_against_the_market_over_the_same_days():
    days, closes, highs, volumes = series(daily=0.001)
    s = screens.measure(days, closes, highs, volumes, market(daily=0.001), RULES)
    assert abs(s["excess_3m"]) < 1e-9 and abs(s["excess_6m"]) < 1e-9
    assert s["persistence"] == 0.0     # matching the market is not beating it


# ---------- with a database and a stand-in for Alpaca ----------
if TEST_DB:
    import psycopg

    import market as market_module
    from schema import ensure_schema


class FakeAlpaca:
    """Answers like alpaca-py's trading and data clients, from a dict of price series."""

    def __init__(self, stocks, names=None):
        self.stocks, self.names, self.requests = stocks, names or {}, []

    def get_all_assets(self, _request):
        return [SimpleNamespace(symbol=s, name=self.names.get(s, f"{s} Inc. Common Stock"), tradable=True,
                                exchange=SimpleNamespace(value="NASDAQ")) for s in self.stocks]

    def get_stock_bars(self, request):
        self.requests.append(request)
        start = request.start.date()
        out = {}
        for symbol in request.symbol_or_symbols:
            days, closes, highs, volumes = self.stocks[symbol]
            out[symbol] = [SimpleNamespace(timestamp=datetime(d.year, d.month, d.day, 5, tzinfo=timezone.utc),
                                           open=c, high=h, low=c * 0.99, close=c, volume=v)
                           for d, c, h, v in zip(days, closes, highs, volumes) if d >= start]
        return SimpleNamespace(data=out)


NOW = datetime(2026, 11, 13, 23, 30, tzinfo=timezone.utc)   # Friday 6:30 PM in New York


def world():
    stocks = {
        "SPY": series(daily=0.0003, start=500, volume=50_000_000),
        "RISE": series(daily=0.002, start=50, volume=2_000_000, last_month_volume=3_500_000),
        "FLAT": series(daily=0.0003, start=80, volume=2_000_000),
        "PENNY": series(daily=0.004, start=0.5, volume=50_000_000),      # under the price floor
        "THIN": series(daily=0.004, start=40, volume=10_000),            # under the volume floor
        "NEWCO": series(n=40, daily=0.003, start=30, volume=400_000),    # a recent listing
        "QQQX": series(daily=0.002, start=90, volume=5_000_000, last_month_volume=9_000_000),
    }
    return FakeAlpaca(stocks, names={"SPY": "SPDR S&P 500 ETF Trust", "QQQX": "Example Growth ETF"})


@pytest.fixture
def conn(monkeypatch):
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL is not set")
    monkeypatch.setattr(market_module, "PAUSE", 0)
    monkeypatch.setattr(market_module, "BENCHMARKS", ["SPY"])
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        ensure_schema(c)
        c.execute("TRUNCATE screen_results, bar_backfills, daily_bars, universe")
        yield c


@needs_db
def test_the_floor_keeps_penny_and_thinly_traded_stocks_out(conn):
    fake = world()
    members = market_module.update(conn, fake, fake, RULES, now=NOW)
    assert set(members) == {"SPY", "RISE", "FLAT", "NEWCO"}          # QQQX is a fund: left out
    kinds = dict(conn.execute("SELECT symbol, kind FROM universe").fetchall())
    assert kinds == {"SPY": "fund", "RISE": "stock", "FLAT": "stock", "NEWCO": "stock"}
    tiers = dict(conn.execute("SELECT symbol, tier FROM universe").fetchall())
    assert tiers["RISE"] == "established" and tiers["NEWCO"] == "emerging"


@needs_db
def test_new_members_get_a_year_of_history_once(conn):
    fake = world()
    market_module.update(conn, fake, fake, RULES, now=NOW)
    assert conn.execute("SELECT count(*) FROM daily_bars WHERE symbol = 'RISE'").fetchone()[0] == 260
    assert conn.execute("SELECT count(*) FROM daily_bars WHERE symbol = 'PENNY'").fetchone()[0] == 0
    before = len(fake.requests)
    market_module.update(conn, fake, fake, RULES, now=NOW)
    assert len(fake.requests) - before == 1      # the second run only needs the recent window
    assert conn.execute("SELECT count(*) FROM universe").fetchone()[0] == 4   # same day: replaced, not doubled


@needs_db
def test_a_split_adjustment_reloads_that_stocks_history(conn):
    fake = world()
    market_module.update(conn, fake, fake, RULES, now=NOW)
    days, closes, highs, volumes = fake.stocks["RISE"]
    fake.stocks["RISE"] = (days, [c / 2 for c in closes], [h / 2 for h in highs], [v * 2 for v in volumes])  # 2-for-1
    market_module.update(conn, fake, fake, RULES, now=NOW)
    stored = [float(r[0]) for r in conn.execute("SELECT close FROM daily_bars WHERE symbol = 'RISE' ORDER BY day")]
    assert len(stored) == 260 and stored == pytest.approx([c / 2 for c in closes])   # no half-adjusted series


@needs_db
def test_the_screens_find_the_riser_and_never_rank_a_fund(conn):
    fake = world()
    market_module.update(conn, fake, fake, RULES, now=NOW)
    day, rows = screens.run(conn, RULES)
    by_symbol = {r["symbol"]: r for r in rows}
    assert day == TODAY and set(by_symbol) == {"RISE", "FLAT", "NEWCO"}          # funds are not screened
    assert by_symbol["RISE"]["candidate"] and by_symbol["RISE"]["rank"] == 1
    assert not by_symbol["FLAT"]["candidate"] and by_symbol["NEWCO"]["flags"] == ["too_new"]
    saved = conn.execute("SELECT symbol, rules_version FROM screen_results WHERE candidate").fetchall()
    assert saved == [("RISE", RULES["rules_version"])]
    screens.run(conn, RULES)                                                   # re-running replaces, not doubles
    assert conn.execute("SELECT count(*) FROM screen_results").fetchone()[0] == 3
    assert "RISE" in screens.summary(day, rows, RULES)


@needs_db
def test_a_weekend_run_describes_fridays_market_and_the_screens_still_work(conn):
    fake = world()
    sunday = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)            # Sunday 11 PM in New York
    market_module.update(conn, fake, fake, RULES, now=sunday)
    assert conn.execute("SELECT DISTINCT as_of FROM universe").fetchall() == [(TODAY,)]   # Friday
    day, rows = screens.run(conn, RULES)
    assert day == TODAY and any(r["candidate"] for r in rows)


@needs_db
def test_a_universe_dated_on_a_non_trading_day_is_cleaned_up(conn):
    fake = world()
    conn.execute("INSERT INTO universe VALUES ('2026-11-15', 'OLD', 'Old', 'NASDAQ', 'stock', 'emerging', 10, 6000000)")
    market_module.update(conn, fake, fake, RULES, now=NOW)
    assert conn.execute("SELECT count(*) FROM universe WHERE as_of > %s", (TODAY,)).fetchone()[0] == 0


@needs_db
def test_prices_of_funds_stored_by_an_earlier_version_are_removed(conn):
    fake = world()
    conn.execute("INSERT INTO daily_bars VALUES ('QQQX', '2026-11-12', 1, 1, 1, 1, 1)")
    conn.execute("INSERT INTO bar_backfills (symbol) VALUES ('QQQX')")
    market_module.update(conn, fake, fake, RULES, now=NOW)
    assert conn.execute("SELECT count(*) FROM daily_bars WHERE symbol = 'QQQX'").fetchone()[0] == 0


@needs_db
def test_the_self_check_agrees_with_the_screens_and_catches_a_wrong_number(conn):
    fake = world()
    market_module.update(conn, fake, fake, RULES, now=NOW)
    day, rows = screens.run(conn, RULES)
    assert screens.self_check(conn, day, rows) == 1
    next(r for r in rows if r["symbol"] == "RISE")["signals"]["excess_6m"] += 0.01      # a planted error
    with pytest.raises(RuntimeError, match="Self-check failed for RISE"):
        screens.self_check(conn, day, rows)
