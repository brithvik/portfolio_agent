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
    # A random id for this installation, used in order ids so they stay unique at the broker
    # even if this database is ever rebuilt and proposal numbers start again from 1.
    "INSERT INTO gateway_settings (key, value) VALUES ('install_id', substr(md5(random()::text), 1, 8)) ON CONFLICT DO NOTHING",
]


def ensure_schema(conn):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(8888)")
        for statement in STATEMENTS:
            conn.execute(statement)
