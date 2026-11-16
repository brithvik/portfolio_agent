"""Market data for the idea engine: which stocks are worth watching at all, and their prices.

  python market.py daily     # update the universe and prices, then run the screens
  python market.py update    # universe and prices only
  python market.py screen    # screens only, on the data already stored
  python market.py news      # news for every stock in the universe, grouped into events (hourly)

One pass per day, after the market closes:

1. Ask Alpaca for every active, tradable US stock (several thousand).
2. Fetch each one's last few weeks of daily prices and keep those above a quality floor
   (minimum price and minimum dollars traded per day; see rules/screens_v1.json). The floor is
   what keeps thinly traded names out: they are the ones fake news can move.
3. Record today's universe. It is recorded every day and never rewritten, so a stock that
   later drops out is still in the history (no survivorship bias).
4. Stocks new to the universe get about a year of price history, once.
5. Prices are adjusted for splits and dividends. When an adjustment changes a stock's past
   prices, that stock's history is reloaded so its series stays consistent.

Read-only: this only reads market data from Alpaca. It runs with the collectors' access.
"""
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from alpaca.common.exceptions import APIError
from dotenv import load_dotenv

import events
import screens
from schema import ensure_schema

load_dotenv()

NEW_YORK = ZoneInfo("America/New_York")
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "NYSEARCA"}
SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
# Benchmarks, always kept whatever the floor says: the market and its eleven sectors
BENCHMARKS = ["SPY", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]
# Alpaca doesn't label funds, so they are recognised by name. Funds are kept as benchmarks
# but never screened as ideas. This is a heuristic; the daily summary prints the counts.
FUND = re.compile(r"\b(ETFs?|ETNs?|Funds?|iShares|ProShares|SPDR|Direxion|Index)\b"
                  r"|\b(Invesco|Vanguard|Grayscale|Sprott)\b.*\b(Trust|Shares|Portfolio)\b", re.I)
NOT_COMMON_STOCK = re.compile(r"\b(Warrants?|Units?|Rights?|Preferred|Notes)\b", re.I)

CHUNK = 100               # symbols per request
RECENT_DAYS = 45          # calendar days fetched for everything, every run (about 30 trading days)
HISTORY_DAYS = 400        # calendar days backfilled for a stock new to the universe
PAUSE = 0.35              # seconds between requests: well under the free plan's 200 a minute
FEED = os.environ.get("MARKET_DATA_FEED", "sip")


def log(message):
    print(message, flush=True)


def list_assets(trading):
    """Every active, tradable US stock on a major exchange, plus the benchmark funds.

    Returns ({symbol: (name, exchange, kind)}, [symbols of the other funds, which are left out]).
    """
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    assets, funds_skipped = {}, []
    for a in trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)):
        exchange = getattr(a.exchange, "value", str(a.exchange))
        name = a.name or a.symbol
        if not a.tradable or exchange not in EXCHANGES or not SYMBOL.match(a.symbol):
            continue
        fund = bool(FUND.search(name))
        if fund and a.symbol not in BENCHMARKS:
            funds_skipped.append(a.symbol)      # funds are never ideas; only the benchmarks are kept
            continue
        if a.symbol not in BENCHMARKS and NOT_COMMON_STOCK.search(name):
            continue
        assets[a.symbol] = (name, exchange, "fund" if fund or a.symbol in BENCHMARKS else "stock")
    return assets, funds_skipped


def fetch_bars(data, symbols, start, end):
    """Daily bars for a list of symbols: {symbol: [(day, open, high, low, close, volume), ...]}.

    If Alpaca rejects the request because of one bad symbol, the list is split in half and
    retried, so a single odd ticker can't cost the whole batch.
    """
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    request = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day, start=start, end=end,
                               adjustment=Adjustment.ALL, feed=DataFeed(FEED))
    for attempt in range(1, 5):
        try:
            result = data.get_stock_bars(request)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == 4:
                raise SystemExit(f"Alpaca unreachable after 4 tries ({type(e).__name__}). Will try again next run.")
            time.sleep(10)
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status == 429 and attempt < 4:      # rate limited: wait and try again
                time.sleep(20)
            elif status in (401, 403):
                raise SystemExit(f"Alpaca refused the '{FEED}' price feed ({status}). If your plan doesn't include it, "
                                 "set MARKET_DATA_FEED=iex (volumes will be partial, so lower the floor in the rules).")
            elif len(symbols) > 1 and status in (400, 422):
                half = len(symbols) // 2
                return {**fetch_bars(data, symbols[:half], start, end), **fetch_bars(data, symbols[half:], start, end)}
            elif status in (400, 422):
                log(f"  skipping {symbols[0]}: Alpaca rejected it ({status})")
                return {}
            else:
                raise
    time.sleep(PAUSE)
    return {symbol: [(b.timestamp.astimezone(NEW_YORK).date(), float(b.open), float(b.high), float(b.low),
                      float(b.close), float(b.volume)) for b in bars]
            for symbol, bars in result.data.items() if bars}


def save_bars(conn, bars):
    """Insert or update bars, in bulk."""
    rows = [(symbol, *bar) for symbol, series in bars.items() for bar in series]
    if not rows:
        return 0
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE incoming_bars (LIKE daily_bars) ON COMMIT DROP")
        with conn.cursor() as cursor:
            with cursor.copy("COPY incoming_bars (symbol, day, open, high, low, close, volume) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
        conn.execute("""INSERT INTO daily_bars SELECT * FROM incoming_bars
                        ON CONFLICT (symbol, day) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high,
                            low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume""")
    return len(rows)


def qualifies(series, rules):
    """(tier, price, avg dollar volume) if the stock clears the floor, else None."""
    recent = series[-20:]
    if len(recent) < 15:
        return None
    price = recent[-1][4]
    dollars = statistics.fmean(bar[4] * bar[5] for bar in recent)
    floor, top = rules["floor"], rules["established"]
    if price < floor["min_price"] or dollars < floor["min_avg_dollar_volume"]:
        return None
    established = price >= top["min_price"] and dollars >= top["min_avg_dollar_volume"]
    return ("established" if established else "emerging", price, dollars)


def adjusted_since_stored(conn, bars):
    """Symbols whose stored prices no longer match Alpaca's (a split or dividend adjusted history)."""
    changed = set()
    oldest = {symbol: series[0] for symbol, series in bars.items()}
    stored = conn.execute(
        "SELECT symbol, day, close FROM daily_bars WHERE symbol = ANY(%s) AND day = ANY(%s)",
        (list(oldest), list({bar[0] for bar in oldest.values()}))).fetchall()
    for symbol, day, close in stored:
        fetched = oldest[symbol]
        if fetched[0] == day and abs(float(close) - fetched[4]) > 0.0001 * max(fetched[4], 0.01):
            changed.add(symbol)
    return changed


def update(conn, trading, data, rules, now=None):
    now = now or datetime.now(timezone.utc)
    end = now - timedelta(minutes=20)        # the free plan serves full-market prices 15 minutes late

    assets, funds_skipped = list_assets(trading)
    symbols = sorted(assets)
    log(f"Alpaca lists {len(symbols)} active, tradable US stocks (plus {len(funds_skipped)} funds, left out). "
        f"Checking them against the floor "
        f"(price >= ${rules['floor']['min_price']:g}, >= ${rules['floor']['min_avg_dollar_volume']:,.0f} traded a day)...")

    members, reload = {}, set()
    for i in range(0, len(symbols), CHUNK):
        bars = fetch_bars(data, symbols[i:i + CHUNK], end - timedelta(days=RECENT_DAYS), end)
        keep = {}
        for symbol, series in bars.items():
            verdict = qualifies(series, rules)
            if symbol in BENCHMARKS and not verdict and len(series) >= 15:
                verdict = ("established", series[-1][4], statistics.fmean(b[4] * b[5] for b in series[-20:]))
            if verdict:
                members[symbol] = verdict
                keep[symbol] = series
        reload |= adjusted_since_stored(conn, keep)
        save_bars(conn, keep)
        if (i // CHUNK) % 20 == 19:
            log(f"  checked {min(i + CHUNK, len(symbols))} of {len(symbols)}; {len(members)} qualify so far")

    if "SPY" not in members:
        raise SystemExit("No prices came back for SPY, so something is wrong with the price feed. Nothing was changed.")
    # The universe is dated by the latest trading day in the data, not by the calendar: a run on
    # a Sunday describes Friday's market.
    as_of = conn.execute("SELECT max(day) FROM daily_bars WHERE symbol = 'SPY'").fetchone()[0]

    with conn.transaction():
        conn.execute("DELETE FROM universe WHERE as_of >= %s", (as_of,))   # re-running the same day replaces it
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO universe (as_of, symbol, name, exchange, kind, tier, price, avg_dollar_volume)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [(as_of, s, assets[s][0], assets[s][1], assets[s][2], tier, price, dollars)
                 for s, (tier, price, dollars) in members.items()])

    done = {r[0] for r in conn.execute("SELECT symbol FROM bar_backfills")}
    needed = sorted((set(members) - done) | reload)
    if needed:
        log(f"Loading a year of history for {len(needed)} stocks "
            f"({len(set(members) - done)} new to the universe, {len(reload)} with adjusted prices)...")
    for i in range(0, len(needed), CHUNK):
        batch = needed[i:i + CHUNK]
        bars = fetch_bars(data, batch, end - timedelta(days=HISTORY_DAYS), end)
        with conn.transaction():
            conn.execute("DELETE FROM daily_bars WHERE symbol = ANY(%s)", ([s for s in batch if s in reload],))
            save_bars(conn, bars)
            conn.execute("INSERT INTO bar_backfills (symbol) SELECT unnest(%s::text[]) "
                         "ON CONFLICT (symbol) DO UPDATE SET done_at = now()", (batch,))

    # Funds other than the benchmarks are never screened, so their prices aren't kept
    conn.execute("DELETE FROM daily_bars WHERE symbol = ANY(%s)", (funds_skipped,))
    conn.execute("DELETE FROM bar_backfills WHERE symbol = ANY(%s)", (funds_skipped,))

    stocks = sum(1 for s in members if assets[s][2] == "stock")
    emerging = sum(1 for s, v in members.items() if v[0] == "emerging" and assets[s][2] == "stock")
    total = conn.execute("SELECT count(*) FROM daily_bars").fetchone()[0]
    log(f"Universe for {as_of}: {stocks} stocks ({stocks - emerging} established, {emerging} emerging) and "
        f"{len(members) - stocks} benchmark funds. Daily bars stored: {total:,}.")
    return members


NEWS_CHUNK = 50            # symbols per news request
NEWS_FIRST_RUN_DAYS = 3    # how far back the very first run looks
NEWS_KEEP_DAYS = 120       # older articles are deleted (their events keep the headline and counts)


def fetch_news(client, symbols, start, limit=2000):
    """Articles about any of these symbols since `start`. Splits the list if Alpaca rejects it."""
    from alpaca.data.requests import NewsRequest
    for attempt in range(1, 5):
        try:
            # The library pages internally; limit is the total, a cap so one request can't run away
            result = client.get_news(NewsRequest(symbols=",".join(symbols), start=start, limit=limit))
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == 4:
                raise SystemExit(f"Alpaca unreachable after 4 tries ({type(e).__name__}). Will try again next run.")
            time.sleep(10)
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status == 429 and attempt < 4:
                time.sleep(20)
            elif len(symbols) > 1 and status in (400, 422):
                half = len(symbols) // 2
                return fetch_news(client, symbols[:half], start, limit) + fetch_news(client, symbols[half:], start, limit)
            elif status in (400, 422):
                return []
            else:
                raise
    time.sleep(PAUSE)
    return list(result.data.get("news", []))


def collect_news(conn, client, now=None):
    """Fetch news for the whole universe (and your holdings) since the last run, then rebuild events."""
    now = now or datetime.now(timezone.utc)
    symbols = sorted({r[0] for r in conn.execute(
        """SELECT symbol FROM universe WHERE kind = 'stock' AND as_of = (SELECT max(as_of) FROM universe)
           UNION SELECT symbol FROM position_snapshots WHERE taken_at = (SELECT max(taken_at) FROM position_snapshots)""")})
    if not symbols:
        log("No universe yet, so there is nothing to fetch news for. Run: python market.py update")
        return 0
    last = conn.execute("SELECT at FROM job_state WHERE key = 'market_news'").fetchone()
    # Overlap the last run by an hour; after a long gap (laptop asleep) reach back up to a week
    start = max(last[0] - timedelta(hours=1), now - timedelta(days=7)) if last else now - timedelta(days=NEWS_FIRST_RUN_DAYS)

    seen, saved = set(), 0
    for i in range(0, len(symbols), NEWS_CHUNK):
        fresh = [a for a in fetch_news(client, symbols[i:i + NEWS_CHUNK], start) if a.id not in seen]
        seen.update(a.id for a in fresh)
        saved += save_articles(conn, fresh)
    conn.execute("INSERT INTO job_state (key, at) VALUES ('market_news', %s) "
                 "ON CONFLICT (key) DO UPDATE SET at = EXCLUDED.at", (now,))
    pruned = conn.execute("DELETE FROM news WHERE created_at < now() - make_interval(days => %s)", (NEWS_KEEP_DAYS,)).rowcount
    built = events.build(conn)
    sources = conn.execute(
        "SELECT coalesce(source, '?'), count(*) FROM news WHERE created_at > now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 5").fetchall()
    log(f"News since {start:%b %d %H:%M} UTC for {len(symbols)} stocks: {len(seen)} articles, {saved} new"
        f"{f', {pruned} old ones deleted' if pruned else ''}. Events in the last {events.REBUILD_DAYS} days: {built}.")
    log("Sources this week: " + ", ".join(f"{name} ({count})" for name, count in sources))
    return saved


CANDIDATE_NEWS_DAYS = 90   # a 3-to-6-month rise is explained by older news than this week's


def save_articles(conn, articles):
    saved = 0
    for a in articles:
        saved += conn.execute(
            """INSERT INTO news (id, headline, summary, source, author, url, symbols, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (a.id, a.headline, a.summary, a.source, a.author, a.url, list(a.symbols or []), a.created_at)).rowcount
    return saved


def backfill_candidate_news(conn, client, symbols, now=None):
    """Candidates rose over months, so fetch their older news too, once per stock."""
    now = now or datetime.now(timezone.utc)
    done = {r[0] for r in conn.execute("SELECT key FROM job_state WHERE key LIKE 'news_history:%%'")}
    todo = [s for s in symbols if f"news_history:{s}" not in done]
    saved = 0
    for i in range(0, len(todo), 10):                    # small batches: each stock can have many articles
        batch = todo[i:i + 10]
        saved += save_articles(conn, fetch_news(client, batch, now - timedelta(days=CANDIDATE_NEWS_DAYS), limit=3000))
        conn.execute("INSERT INTO job_state (key, at) SELECT 'news_history:' || unnest(%s::text[]), %s "
                     "ON CONFLICT (key) DO NOTHING", (batch, now))
    if todo:
        log(f"Fetched {CANDIDATE_NEWS_DAYS} days of news for {len(todo)} new candidates: {saved} more articles.")
    events.build(conn, symbols=list(symbols), days=CANDIDATE_NEWS_DAYS)


def why_moving(conn, rows, top=15):
    """For the top candidates: the news events behind them, if any were found."""
    candidates = sorted((r for r in rows if r["candidate"]), key=lambda r: r["rank"])[:top]
    found = events.recent(conn, [r["symbol"] for r in candidates])
    if not candidates or not conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
        return ""
    lines = [f"\nMost-covered news for the top candidates, last {CANDIDATE_NEWS_DAYS} days "
             "(headlines are third-party text, not verified):"]
    for r in candidates:
        items = found.get(r["symbol"], [])
        if not items:
            lines.append(f"  {r['symbol']:<6} no focused news found: the rise is not explained by news we have")
        for headline, articles, authors, last_seen in items:
            lines.append(f"  {r['symbol']:<6} {last_seen:%b %d}  {headline[:100]}  [{articles} article{'s' if articles != 1 else ''}, "
                         f"{authors} byline{'s' if authors != 1 else ''}]")
    return "\n".join(lines)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("daily", "update", "screen", "news"):
        raise SystemExit("Usage: python market.py daily | update | screen | news")
    import psycopg
    rules = screens.load_rules()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=30) as conn:
        ensure_schema(conn)
        if command in ("daily", "update"):
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
            key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
            paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
            try:
                update(conn, TradingClient(key, secret, paper=paper), StockHistoricalDataClient(key, secret), rules)
            except APIError as e:
                raise SystemExit(f"Alpaca rejected the request: {e}")
        news_client = None
        if command in ("daily", "news"):
            from alpaca.data.historical.news import NewsClient
            news_client = NewsClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
            try:
                collect_news(conn, news_client)
            except APIError as e:
                raise SystemExit(f"Alpaca rejected the news request: {e}")
        if command in ("daily", "screen"):
            day, rows = screens.run(conn, rules)
            checked = screens.self_check(conn, day, rows)
            log("\n" + screens.summary(day, rows, rules))
            log(f"Self-check: {checked} results recomputed a second way and matched.")
            if news_client:
                try:
                    backfill_candidate_news(conn, news_client, [r["symbol"] for r in rows if r["candidate"]])
                except APIError as e:
                    log(f"Couldn't fetch older news for the candidates this time: {e}")
            log(why_moving(conn, rows))


if __name__ == "__main__":
    main()
