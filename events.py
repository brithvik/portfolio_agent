"""Groups news articles into events: "one thing that happened to one company".

Why: the idea engine must count evidence, not headlines. A single press release gets
rewritten by many outlets within hours, and a naive count would treat that as many
confirmations. Here those articles become one event with a count of articles and bylines.

How (plain code, no model):
- Only focused articles count: ones tagged with at most 3 tickers. Roundups ("10 stocks
  moving today") say little about any one company.
- For each company, articles are read in time order. An article joins an existing event if
  it arrives within 36 hours of that event's latest article and its headline shares enough
  distinctive words with one of the event's headlines. Otherwise it starts a new event.

This is a heuristic, and it is deliberately simple so its mistakes are easy to understand.
Headlines are third-party text: they are stored and compared here, never obeyed.
"""
import re
from datetime import timedelta

WINDOW = timedelta(hours=36)
MIN_OVERLAP = 0.5            # share of the shorter headline's distinctive words that must match
MAX_TICKERS = 3
REBUILD_DAYS = 14            # events older than this are settled and left alone

_STOPWORDS = set("""a an the and or but of to in on for with by at from as is are was were be been it its this that
    after before over under into about amid than then up down new says said say will may could would shares stock
    stocks company inc corp co ltd plc today week year quarter report reports reported announces announced
    announce here why what how these those more most shares trading higher lower moving move moves""".split())


_NAME_FILLER = set("""inc corp corporation company ltd limited plc holdings holding group common stock class ordinary
    shares american depositary trust the and international global""".split())


def name_words(company_name):
    """The words of a company's own name. Two headlines sharing only these are not the same story."""
    return {w for w in re.findall(r"[a-z0-9&]+", (company_name or "").lower()) if len(w) > 2} - _NAME_FILLER


def distinctive_words(headline, symbol="", company=frozenset()):
    # Hyphens split words ("Equal-Weight" = "Equal Weight"); possessives and quote marks are
    # dropped ("Skydance's" = "Skydance", "Debut'" = "Debut")
    words = re.findall(r"[a-z0-9&]+", re.sub(r"['\u2019]s\b", "", headline.lower()))
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS and w != symbol.lower() and w not in company}


def same_story(a, b):
    """Do two sets of headline words describe the same story?"""
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= MIN_OVERLAP and len(a & b) >= 2


def group(articles, symbol, company_name=""):
    """Group one company's articles into events.

    articles: dicts with id, headline, created_at, author, source, oldest first.
    Returns a list of events, each a dict with its articles in order.
    """
    events, company = [], name_words(company_name)
    for article in articles:
        words = distinctive_words(article["headline"], symbol, company)
        home = None
        for event in reversed(events):                       # newest events first
            if article["created_at"] - event["last_seen"] > WINDOW:
                continue
            if any(same_story(words, other) for other in event["words"]):
                home = event
                break
        if home is None:
            home = {"symbol": symbol, "headline": article["headline"], "first_article_id": article["id"],
                    "first_seen": article["created_at"], "last_seen": article["created_at"],
                    "articles": [], "words": []}
            events.append(home)
        home["articles"].append(article)
        home["words"].append(words)
        home["last_seen"] = max(home["last_seen"], article["created_at"])
    return events


def build(conn, symbols=None, days=REBUILD_DAYS):
    """Rebuild the events of the last `days` days from the stored news. Returns how many events exist in that window.

    Safe to run repeatedly: an event is identified by its company and its first article, so
    rebuilding updates the same rows instead of creating new ones.
    """
    rows = conn.execute(
        """SELECT n.id, n.headline, n.created_at, n.author, n.source, s AS symbol
           FROM news n, unnest(n.symbols) AS s
           WHERE n.created_at > now() - make_interval(days => %s) AND cardinality(n.symbols) <= %s
             AND (%s::text[] IS NULL OR s = ANY(%s))
           ORDER BY s, n.created_at, n.id""",
        (days, MAX_TICKERS, symbols, symbols)).fetchall()
    by_symbol = {}
    for article_id, headline, created_at, author, source, symbol in rows:
        by_symbol.setdefault(symbol, []).append(
            {"id": article_id, "headline": headline, "created_at": created_at, "author": author, "source": source})

    names = dict(conn.execute("SELECT DISTINCT ON (symbol) symbol, name FROM universe ORDER BY symbol, as_of DESC"))
    total = 0
    with conn.transaction():
        for symbol, articles in by_symbol.items():
            for event in group(articles, symbol, names.get(symbol, "")):
                items = event["articles"]
                conn.execute(
                    """INSERT INTO events (symbol, first_article_id, headline, first_seen, last_seen,
                           article_ids, articles, authors, sources)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (symbol, first_article_id) DO UPDATE SET last_seen = EXCLUDED.last_seen,
                           article_ids = EXCLUDED.article_ids, articles = EXCLUDED.articles,
                           authors = EXCLUDED.authors, sources = EXCLUDED.sources""",
                    (symbol, event["first_article_id"], event["headline"][:500], event["first_seen"], event["last_seen"],
                     [a["id"] for a in items], len(items), len({a["author"] for a in items if a["author"]}),
                     sorted({a["source"] for a in items if a["source"]})))
                total += 1
        # An article that used to start its own event may now belong to an earlier one
        conn.execute(
            """DELETE FROM events e WHERE e.first_seen > now() - make_interval(days => %s)
                 AND EXISTS (SELECT 1 FROM events o WHERE o.symbol = e.symbol AND o.id <> e.id
                             AND e.first_article_id = ANY(o.article_ids) AND o.first_article_id <> e.first_article_id)""",
            (days,))
    return total


def recent(conn, symbols, days=90, per_symbol=3):
    """The most-covered recent events for some companies: {symbol: [(headline, articles, authors, last_seen), ...]}."""
    rows = conn.execute(
        """SELECT symbol, headline, articles, authors, last_seen FROM (
               SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY articles DESC, last_seen DESC) AS n
               FROM events WHERE symbol = ANY(%s) AND last_seen > now() - make_interval(days => %s)) ranked
           WHERE n <= %s ORDER BY symbol, n""",
        (list(symbols), days, per_symbol)).fetchall()
    out = {}
    for symbol, headline, articles, authors, last_seen in rows:
        out.setdefault(symbol, []).append((headline, articles, authors, last_seen))
    return out
