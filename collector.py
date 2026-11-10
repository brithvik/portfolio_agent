"""Collects your portfolio data into PostgreSQL. Runs once and exits; Kubernetes CronJobs run it.

  python collector.py snapshot   # positions + account value (skips when the market is closed)
  python collector.py news       # recent news about the stocks you hold

Read-only: it only ever reads from Alpaca. Trading goes through the broker gateway later.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from schema import ensure_schema

load_dotenv()

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
NEWS_LOOKBACK_HOURS = int(os.environ.get("NEWS_LOOKBACK_HOURS", "24"))


def connect_with_retry(url, attempts=10, delay=3):
    """Wait for the database instead of crashing if it's still starting up."""
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(url, autocommit=True)
        except psycopg.OperationalError:
            if attempt == attempts:
                raise
            print(f"Database not ready (attempt {attempt}/{attempts}), retrying in {delay}s...")
            time.sleep(delay)


def with_retries(call, attempts=4, delay=10):
    """Retry through temporary network problems. Never turns certificate checks off."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == attempts:
                raise SystemExit(f"Alpaca unreachable after {attempts} tries "
                                 f"({e.__class__.__name__}). Will try again next run.")
            print(f"Network problem reaching Alpaca (attempt {attempt}/{attempts}), "
                  f"retrying in {delay}s...")
            time.sleep(delay)


def num(value):
    return None if value is None else float(value)


def snapshot(conn, force=False):
    trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    clock = with_retries(trading.get_clock)
    if not clock.is_open and not force:
        print(f"Market closed; next open {clock.next_open}. Skipping snapshot.")
        return

    account = with_retries(trading.get_account)
    positions = with_retries(trading.get_all_positions)
    taken_at = datetime.now(timezone.utc).replace(microsecond=0)

    with conn.transaction():  # the account row and its positions save together, or not at all
        conn.execute(
            """INSERT INTO account_snapshots (taken_at, equity, cash, buying_power)
               VALUES (%s, %s, %s, %s) ON CONFLICT (taken_at) DO NOTHING""",
            (taken_at, num(account.equity), num(account.cash), num(account.buying_power)),
        )
        for p in positions:
            conn.execute(
                """INSERT INTO position_snapshots (taken_at, symbol, qty, avg_entry_price,
                       current_price, market_value, cost_basis, unrealized_pl)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (taken_at, symbol) DO NOTHING""",
                (taken_at, p.symbol, num(p.qty), num(p.avg_entry_price), num(p.current_price),
                 num(p.market_value), num(p.cost_basis), num(p.unrealized_pl)),
            )
    total = conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0]
    print(f"Snapshot {taken_at:%Y-%m-%d %H:%M} UTC: ${num(account.equity):,.2f} across "
          f"{len(positions)} positions. Snapshots stored: {total}")


def news(conn):
    trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    symbols = sorted(p.symbol for p in with_retries(trading.get_all_positions))
    if not symbols:
        print("No positions, so no news to fetch.")
        return

    client = NewsClient(API_KEY, SECRET_KEY)
    start = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    result = with_retries(lambda: client.get_news(NewsRequest(
        # The library pages internally, and limit is the TOTAL number of articles.
        # 500 is a safety cap so one run can't pull forever.
        symbols=",".join(symbols), start=start, limit=500)))
    articles = result.data.get("news", [])
    saved = 0
    for a in articles:
        saved += conn.execute(
            """INSERT INTO news (id, headline, summary, source, author, url, symbols, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (a.id, a.headline, a.summary, a.source, a.author, a.url,
             list(a.symbols or []), a.created_at),
        ).rowcount
    seen = len(articles)
    print(f"News: {seen} articles in the last {NEWS_LOOKBACK_HOURS}h for {len(symbols)} "
          f"holdings, {saved} new.")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("snapshot", "news"):
        raise SystemExit("Usage: python collector.py snapshot [--force] | news")
    with connect_with_retry(os.environ["DATABASE_URL"]) as conn:
        ensure_schema(conn)
        try:
            if command == "snapshot":
                snapshot(conn, force="--force" in sys.argv)
            else:
                news(conn)
        except APIError as e:
            raise SystemExit(f"Alpaca rejected the request: {e}")


if __name__ == "__main__":
    main()