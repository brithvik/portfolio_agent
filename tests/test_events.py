"""Tests for grouping news into events, and for the market-wide news collection.

    python -m pytest tests/test_events.py -v
"""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import events

T0 = datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc)


def article(i, headline, hours=0, author="Staff", source="benzinga"):
    return {"id": i, "headline": headline, "created_at": T0 + timedelta(hours=hours), "author": author, "source": source}


def headlines(grouped):
    return [[a["headline"] for a in e["articles"]] for e in grouped]


def test_rewrites_of_one_story_become_one_event():
    grouped = events.group([
        article(1, "Acme Robotics To Be Acquired By Globex For $4.2 Billion", 0, "A. Lee"),
        article(2, "Globex Agrees To Acquire Acme Robotics In $4.2 Billion Deal", 1, "B. Cho"),
        article(3, "Acme Robotics Shares Jump After Globex Acquisition Announcement", 2, "A. Lee"),
    ], "ACME")
    assert len(grouped) == 1 and len(grouped[0]["articles"]) == 3
    assert grouped[0]["first_article_id"] == 1 and grouped[0]["headline"].startswith("Acme Robotics To Be Acquired")


def test_different_stories_about_one_company_stay_separate():
    grouped = events.group([
        article(1, "Acme Robotics Reports Record Third Quarter Revenue, Raises Guidance", 0),
        article(2, "Acme Robotics Faces Patent Lawsuit From Former Supplier", 3),
    ], "ACME")
    assert len(grouped) == 2


def test_the_same_kind_of_story_days_apart_is_a_new_event():
    grouped = events.group([
        article(1, "Analyst Upgrades Acme Robotics, Raises Price Target", 0),
        article(2, "Analyst Upgrades Acme Robotics, Raises Price Target", 24 * 5),
    ], "ACME")
    assert len(grouped) == 2


def test_the_company_name_alone_does_not_link_two_stories():
    a = events.distinctive_words("ACME Announces New Chief Financial Officer", "ACME")
    b = events.distinctive_words("ACME Recalls Warehouse Robots Over Battery Fault", "ACME")
    assert "acme" not in a and not events.same_story(a, b)


def test_two_reports_of_one_analyst_call_merge_despite_different_wording():
    # Real headlines from the first run: they were wrongly kept apart ("Equal-Weight" vs "Equal Weight")
    grouped = events.group([
        article(1, "Stephens Initiates Coverage of NCR Voyix with Equal Weight Rating", 0),
        article(2, "Stephens & Co. Reinstates Equal-Weight on NCR Voyix, Announces $12 Price Target", 1),
    ], "VYX", "NCR Voyix Corporation Common Stock")
    assert len(grouped) == 1


def test_quote_marks_and_possessives_do_not_hide_a_match():
    # Real: the same licence deal, kept apart because one headline ended with "Debut'"
    grouped = events.group([
        article(1, "JAKKS Pacific Secures Global Master Toy License for Skydance's 'K-Pop: The Debut'", 0),
        article(2, "Skydance, JAKKS Pacific Sign Master Toy Agreement For K-Pop: The Debut", 1),
    ], "JAKK", "JAKKS Pacific, Inc. Common Stock")
    assert len(grouped) == 1


def test_sharing_only_the_company_name_does_not_merge_stories():
    # Also real: one company, two unrelated stories on the same day
    grouped = events.group([
        article(1, "Donnelley Financial Solutions CEO Files Form 144 for Proposed Sale of Common Stock", 0),
        article(2, "Donnelley Financial Solutions Named #1 Technology Company on BPI's Most Loved Workplaces List", 2),
    ], "DFIN", "Donnelley Financial Solutions, Inc. Common Stock")
    assert len(grouped) == 2


def test_an_empty_or_generic_headline_never_matches():
    assert not events.same_story(set(), {"lawsuit", "patent"})
    assert not events.same_story(events.distinctive_words("Why Shares Are Moving Today"), {"lawsuit", "patent"})


# ---------- with a database ----------
TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL is not set")

if TEST_DB:
    import psycopg

    import market as market_module
    from schema import ensure_schema


@pytest.fixture
def conn(monkeypatch):
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL is not set")
    monkeypatch.setattr(market_module, "PAUSE", 0)
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        ensure_schema(c)
        c.execute("TRUNCATE events, job_state, news, screen_results, bar_backfills, daily_bars, universe, "
                  "position_snapshots, account_snapshots CASCADE")
        c.execute("""INSERT INTO universe VALUES
                     (current_date, 'ACME', 'Acme Robotics', 'NASDAQ', 'stock', 'emerging', 20, 9000000),
                     (current_date, 'GLBX', 'Globex', 'NYSE', 'stock', 'established', 90, 90000000),
                     (current_date, 'SPY', 'SPDR S&P 500 ETF Trust', 'ARCA', 'fund', 'established', 500, 9000000000)""")
        yield c


def story(i, headline, symbols, hours_ago, author="Staff"):
    return SimpleNamespace(id=i, headline=headline, summary="", source="benzinga", author=author, url="",
                           symbols=symbols, created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago))


class FakeNews:
    def __init__(self, stories):
        self.stories, self.requests = stories, []

    def get_news(self, request):
        self.requests.append(request)
        wanted = set(request.symbols.split(","))
        return SimpleNamespace(data={"news": [s for s in self.stories
                                              if wanted & set(s.symbols) and s.created_at >= request.start]})


STORIES = [
    story(1, "Acme Robotics To Be Acquired By Globex For $4.2 Billion", ["ACME", "GLBX"], 30, "A. Lee"),
    story(2, "Globex Agrees To Acquire Acme Robotics In $4.2 Billion Deal", ["ACME", "GLBX"], 29, "B. Cho"),
    story(3, "10 Industrial Stocks Moving In Thursday's Session", ["ACME", "GLBX", "AAA", "BBB", "CCC"], 28),
    story(4, "Acme Robotics Faces Patent Lawsuit From Former Supplier", ["ACME"], 5),
    story(5, "Something About A Stock Outside The Universe", ["ZZZZ"], 5),
]


@needs_db
def test_news_is_collected_for_the_universe_and_grouped_into_events(conn):
    fake = FakeNews(STORIES)
    assert market_module.collect_news(conn, fake) == 4          # story 5 is about a stock we don't follow
    acme = conn.execute("SELECT headline, articles, authors FROM events WHERE symbol = 'ACME' ORDER BY first_seen").fetchall()
    assert [(a, b) for _, a, b in acme] == [(2, 2), (1, 1)]      # the deal (2 articles, 2 bylines) and the lawsuit
    assert conn.execute("SELECT count(*) FROM events WHERE symbol = 'GLBX'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM events WHERE symbol IN ('AAA', 'SPY')").fetchone()[0] == 0   # roundup ignored


@needs_db
def test_running_again_adds_nothing_and_only_looks_back_to_the_last_run(conn):
    fake = FakeNews(STORIES)
    market_module.collect_news(conn, fake)
    before = conn.execute("SELECT count(*), max(id) FROM events").fetchone()
    assert market_module.collect_news(conn, fake) == 0
    assert conn.execute("SELECT count(*), max(id) FROM events").fetchone() == before
    assert datetime.now(timezone.utc) - fake.requests[-1].start < timedelta(hours=2)


@needs_db
def test_a_later_article_joins_the_existing_event(conn):
    fake = FakeNews(STORIES[:1])
    market_module.collect_news(conn, fake)
    fake.stories = STORIES[:2]
    conn.execute("UPDATE job_state SET at = at - interval '2 days'")     # as if the laptop had been asleep
    market_module.collect_news(conn, fake)
    assert conn.execute("SELECT articles FROM events WHERE symbol = 'ACME'").fetchall() == [(2,)]


@needs_db
def test_the_candidate_report_shows_the_news_or_says_there_is_none(conn):
    market_module.collect_news(conn, FakeNews(STORIES))
    rows = [{"symbol": "ACME", "candidate": True, "rank": 1}, {"symbol": "GLBX", "candidate": False, "rank": None},
            {"symbol": "QUIET", "candidate": True, "rank": 2}]
    report = market_module.why_moving(conn, rows)
    assert "Acquired By Globex" in report and "[2 articles, 2 bylines]" in report
    assert "QUIET  no focused news found" in report


@needs_db
def test_an_asset_manager_is_not_mistaken_for_a_fund():
    assert not market_module.FUND.search("Invesco Ltd Common Stock")
    assert market_module.FUND.search("Invesco QQQ Trust, Series 1") and market_module.FUND.search("Vanguard S&P 500 ETF")
    assert market_module.FUND.search("SPDR Gold Shares") and not market_module.FUND.search("Northern Trust Corporation")


@needs_db
def test_candidates_get_their_older_news_once(conn):
    old = story(9, "Acme Robotics Reports Record Second Quarter Revenue, Raises Guidance", ["ACME"], 24 * 60)
    fake = FakeNews(STORIES + [old])
    market_module.collect_news(conn, fake)                      # the normal run only reaches back 3 days
    assert conn.execute("SELECT count(*) FROM news WHERE id = 9").fetchone()[0] == 0
    market_module.backfill_candidate_news(conn, fake, ["ACME"])
    assert conn.execute("SELECT count(*) FROM events WHERE symbol = 'ACME'").fetchone()[0] == 3
    asked = len(fake.requests)
    market_module.backfill_candidate_news(conn, fake, ["ACME"])
    assert len(fake.requests) == asked                          # not fetched twice
    report = market_module.why_moving(conn, [{"symbol": "ACME", "candidate": True, "rank": 1}])
    assert "Record Second Quarter Revenue" in report
