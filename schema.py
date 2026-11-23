"""Database tables, shared by every component. Safe to run repeatedly; an advisory lock
makes sure only one component migrates at a time."""

STATEMENTS = [
    # One row per snapshot of the whole account
    """CREATE TABLE IF NOT EXISTS account_snapshots (
        taken_at     TIMESTAMPTZ PRIMARY KEY,
        equity       NUMERIC NOT NULL,
        cash         NUMERIC NOT NULL,
        buying_power NUMERIC NOT NULL
    )""",
    # Each holding at the moment of a snapshot
    """CREATE TABLE IF NOT EXISTS position_snapshots (
        taken_at        TIMESTAMPTZ NOT NULL REFERENCES account_snapshots(taken_at),
        symbol          TEXT NOT NULL,
        qty             NUMERIC NOT NULL,
        avg_entry_price NUMERIC,
        current_price   NUMERIC,
        market_value    NUMERIC NOT NULL,
        cost_basis      NUMERIC,
        unrealized_pl   NUMERIC,
        PRIMARY KEY (taken_at, symbol)
    )""",
    "CREATE INDEX IF NOT EXISTS position_snapshots_symbol_idx ON position_snapshots (symbol, taken_at)",
    # News about the stocks you hold. Treated as UNTRUSTED text: anyone can write a headline.
    """CREATE TABLE IF NOT EXISTS news (
        id         BIGINT PRIMARY KEY,
        headline   TEXT NOT NULL,
        summary    TEXT,
        source     TEXT,
        author     TEXT,
        url        TEXT,
        symbols    TEXT[] NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS news_symbols_idx ON news USING GIN (symbols)",
    "CREATE INDEX IF NOT EXISTS news_created_idx ON news (created_at DESC)",

    # What each holding is, so the tools (not the model) can total exposure by sector.
    # sector uses the standard GICS sectors; "group" is a looser label like "Mega-cap tech".
    """CREATE TABLE IF NOT EXISTS securities (
        symbol TEXT PRIMARY KEY,
        name   TEXT NOT NULL,
        kind   TEXT NOT NULL,      -- 'stock' or 'fund'
        sector TEXT NOT NULL,
        grp    TEXT
    )""",
    """INSERT INTO securities (symbol, name, kind, sector, grp) VALUES
        ('NVDA',  'NVIDIA',              'stock', 'Information Technology', 'Mega-cap tech'),
        ('AAPL',  'Apple',               'stock', 'Information Technology', 'Mega-cap tech'),
        ('MSFT',  'Microsoft',           'stock', 'Information Technology', 'Mega-cap tech'),
        ('GOOGL', 'Alphabet (Google)',   'stock', 'Communication Services', 'Mega-cap tech'),
        ('AMZN',  'Amazon',              'stock', 'Consumer Discretionary', 'Mega-cap tech'),
        ('VOO',   'Vanguard S&P 500 ETF','fund',  'Diversified index fund', NULL),
        ('JPM',   'JPMorgan Chase',      'stock', 'Financials',             NULL),
        ('KO',    'Coca-Cola',           'stock', 'Consumer Staples',       NULL),
        ('XOM',   'Exxon Mobil',         'stock', 'Energy',                 NULL)
       ON CONFLICT (symbol) DO NOTHING""",

    # The agent's audit log: every run, and every step inside it
    """CREATE TABLE IF NOT EXISTS agent_runs (
        id            BIGSERIAL PRIMARY KEY,
        kind          TEXT NOT NULL,            -- 'ask' or 'briefing'
        question      TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'running',   -- running, done, failed
        answer        TEXT,
        model         TEXT,
        steps         INTEGER NOT NULL DEFAULT 0,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        error         TEXT,
        started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at   TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS agent_steps (
        run_id      BIGINT NOT NULL REFERENCES agent_runs(id),
        step        INTEGER NOT NULL,
        kind        TEXT NOT NULL,              -- 'model' or 'tool'
        name        TEXT,                       -- model name or tool name
        args        JSONB,
        result      JSONB,
        duration_ms INTEGER,
        error       TEXT,
        at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS agent_steps_run_idx ON agent_steps (run_id, step)",

    # What the model drafted (with handles) and what the checks found, kept beside the final answer
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS answer_draft TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS checks JSONB",
    # ---------- The broker gateway ----------
    # A trade the agent proposed. Nothing here is an order until a human approves it.
    """CREATE TABLE IF NOT EXISTS trade_proposals (
        id               BIGSERIAL PRIMARY KEY,
        run_id           BIGINT REFERENCES agent_runs(id),  -- which agent run proposed it
        symbol           TEXT NOT NULL,
        side             TEXT NOT NULL,
        notional         NUMERIC NOT NULL,
        rationale        TEXT NOT NULL,
        -- pending -> executing -> submitted -> partially_filled -> filled
        -- or: rejected_by_policy, rejected (by the human), expired, canceled, failed
        status           TEXT NOT NULL DEFAULT 'pending',
        policy_checks    JSONB NOT NULL,
        read_untrusted   BOOLEAN NOT NULL DEFAULT false,  -- the run read news before proposing
        tools_used       JSONB NOT NULL DEFAULT '[]',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at       TIMESTAMPTZ NOT NULL,
        decided_at       TIMESTAMPTZ,
        decided_by       TEXT,
        decision_note    TEXT,
        client_order_id  TEXT UNIQUE,   -- sent to the broker so a retry can never double-order
        alpaca_order_id  TEXT,
        order_status     TEXT,
        filled_qty       NUMERIC,
        filled_avg_price NUMERIC,
        order_updated_at TIMESTAMPTZ,
        error            TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS trade_proposals_status_idx ON trade_proposals (status, created_at)",
    # Everything the gateway does, in order. Rows are only ever added.
    """CREATE TABLE IF NOT EXISTS gateway_events (
        id          BIGSERIAL PRIMARY KEY,
        proposal_id BIGINT REFERENCES trade_proposals(id),
        event       TEXT NOT NULL,
        actor       TEXT NOT NULL,       -- 'agent', 'approver', or 'gateway'
        detail      JSONB,
        at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS gateway_settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    "INSERT INTO gateway_settings (key, value) VALUES ('trading_enabled', 'true') ON CONFLICT DO NOTHING",
    # Results of the prompt-injection red-team suite (redteam.py), one row per full run
    """CREATE TABLE IF NOT EXISTS redteam_reports (
        id                  BIGSERIAL PRIMARY KEY,
        ran_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        model               TEXT NOT NULL,
        attacks             INTEGER NOT NULL,
        resisted            INTEGER NOT NULL,
        controls_ok         INTEGER NOT NULL,
        proposals_attempted INTEGER NOT NULL,
        proposals_blocked   INTEGER NOT NULL,
        errors              INTEGER NOT NULL,
        results             JSONB NOT NULL
    )""",
    # Results of the accuracy evaluation (evaluate.py), one row per full run
    """CREATE TABLE IF NOT EXISTS eval_reports (
        id               BIGSERIAL PRIMARY KEY,
        ran_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        model            TEXT NOT NULL,
        questions        INTEGER NOT NULL,
        passed           INTEGER NOT NULL,
        facts_total      INTEGER NOT NULL,
        facts_correct    INTEGER NOT NULL,
        numbers_cited    INTEGER NOT NULL,
        numbers_grounded INTEGER NOT NULL,
        avg_seconds      NUMERIC,
        total_tokens     INTEGER,
        results          JSONB NOT NULL
    )""",
    # ---------- Market data for the idea engine ----------
    # Which stocks were in the tradable universe on each day. Recorded daily and never
    # rewritten, so later analysis can't quietly forget the stocks that dropped out.
    """CREATE TABLE IF NOT EXISTS universe (
        as_of             DATE NOT NULL,
        symbol            TEXT NOT NULL,
        name              TEXT NOT NULL,
        exchange          TEXT NOT NULL,
        kind              TEXT NOT NULL,     -- 'stock' or 'fund' (funds are benchmarks, not ideas)
        tier              TEXT NOT NULL,     -- 'established' or 'emerging'
        price             NUMERIC NOT NULL,
        avg_dollar_volume NUMERIC NOT NULL,  -- average dollars traded per day, last 20 days
        PRIMARY KEY (as_of, symbol)
    )""",
    # Daily prices, adjusted for splits and dividends so returns are comparable over time
    """CREATE TABLE IF NOT EXISTS daily_bars (
        symbol TEXT NOT NULL,
        day    DATE NOT NULL,
        open   NUMERIC NOT NULL,
        high   NUMERIC NOT NULL,
        low    NUMERIC NOT NULL,
        close  NUMERIC NOT NULL,
        volume NUMERIC NOT NULL,
        PRIMARY KEY (symbol, day)
    )""",
    "CREATE INDEX IF NOT EXISTS daily_bars_day_idx ON daily_bars (day)",
    # Symbols whose full price history has been loaded (new arrivals get a one-time backfill)
    """CREATE TABLE IF NOT EXISTS bar_backfills (
        symbol TEXT PRIMARY KEY,
        done_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # Every stock's measured signals on every day, under a named version of the rules
    """CREATE TABLE IF NOT EXISTS screen_results (
        day           DATE NOT NULL,
        symbol        TEXT NOT NULL,
        rules_version TEXT NOT NULL,     -- e.g. 'screens-v1@3fa9c1d2' (name @ hash of the rules file)
        tier          TEXT NOT NULL,
        signals       JSONB NOT NULL,
        flags         TEXT[] NOT NULL,
        candidate     BOOLEAN NOT NULL,
        score         NUMERIC,
        rank          INTEGER,
        PRIMARY KEY (day, symbol, rules_version)
    )""",
    "CREATE INDEX IF NOT EXISTS screen_results_candidates_idx ON screen_results (day, rank) WHERE candidate",
    # When each recurring job last finished, so a run after the laptop slept knows how far back to look
    """CREATE TABLE IF NOT EXISTS job_state (
        key TEXT PRIMARY KEY,
        at  TIMESTAMPTZ NOT NULL
    )""",
    # News grouped into events: one thing that happened to one company, however many articles
    # were written about it. Counting events instead of articles stops a single press release,
    # rewritten twenty times, from looking like twenty pieces of evidence.
    """CREATE TABLE IF NOT EXISTS events (
        id               BIGSERIAL PRIMARY KEY,
        symbol           TEXT NOT NULL,
        first_article_id BIGINT NOT NULL,
        headline         TEXT NOT NULL,          -- the earliest headline (UNTRUSTED third-party text)
        first_seen       TIMESTAMPTZ NOT NULL,
        last_seen        TIMESTAMPTZ NOT NULL,
        article_ids      BIGINT[] NOT NULL,
        articles         INTEGER NOT NULL,
        authors          INTEGER NOT NULL,       -- distinct bylines: a weak sign of independent coverage
        sources          TEXT[] NOT NULL,
        UNIQUE (symbol, first_article_id)
    )""",
    "CREATE INDEX IF NOT EXISTS events_symbol_idx ON events (symbol, last_seen DESC)",
    # The classifier's label for each event, kept per prompt-and-model version
    """CREATE TABLE IF NOT EXISTS event_labels (
        event_id           BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        classifier_version TEXT NOT NULL,      -- e.g. 'events-v1@1a2b3c4d/gemini-3.5-flash-lite'
        type               TEXT NOT NULL,
        direction          TEXT NOT NULL,
        labeled_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (event_id, classifier_version)
    )""",
    # Each scoring of the classifier against the hand-labeled examples
    """CREATE TABLE IF NOT EXISTS classifier_reports (
        id                 BIGSERIAL PRIMARY KEY,
        ran_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        classifier_version TEXT NOT NULL,
        examples           INTEGER NOT NULL,
        type_correct       INTEGER NOT NULL,
        direction_correct  INTEGER NOT NULL,
        results            JSONB NOT NULL
    )""",
    # The ledger: every idea the engine produced, recorded once with what was known at that
    # moment. Rows are never updated or deleted; outcomes are computed from prices afterwards.
    """CREATE TABLE IF NOT EXISTS ideas (
        id           BIGSERIAL PRIMARY KEY,
        day          DATE NOT NULL,                  -- the market day whose data produced it
        decided_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        symbol       TEXT NOT NULL,
        kind         TEXT NOT NULL,                  -- 'shortlist', 'set_aside', or 'control'
        parent_id    BIGINT REFERENCES ideas(id),    -- for a control: the idea it is matched to
        rank         INTEGER,
        tier         TEXT NOT NULL,
        flags        TEXT[] NOT NULL DEFAULT '{}',
        evidence     JSONB NOT NULL DEFAULT '{}',    -- signals and labeled headlines at decision time
        rules        TEXT NOT NULL                   -- screen rules + ledger rules versions
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ideas_once_idx ON ideas (day, symbol, kind, coalesce(parent_id, 0))",
    "CREATE INDEX IF NOT EXISTS ideas_symbol_idx ON ideas (symbol, decided_at DESC)",
    # One message per day's new ideas, for the notifier to send
    """CREATE TABLE IF NOT EXISTS idea_digests (
        id         BIGSERIAL PRIMARY KEY,
        day        DATE NOT NULL UNIQUE,
        body       TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # Where the notifier got to, so a restart never re-sends (or skips) a message
    """CREATE TABLE IF NOT EXISTS notifier_state (
        key     TEXT PRIMARY KEY,       -- 'briefing' or 'proposal'
        last_id BIGINT NOT NULL
    )""",
    # A random id for this installation, used in order ids so they stay unique at the broker
    # even if this database is ever rebuilt and proposal numbers start again from 1.
    "INSERT INTO gateway_settings (key, value) VALUES ('install_id', substr(md5(random()::text), 1, 8)) ON CONFLICT DO NOTHING",
]


def ensure_schema(conn):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(8888)")
        for statement in STATEMENTS:
            conn.execute(statement)
