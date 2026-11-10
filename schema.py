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
]


def ensure_schema(conn):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(8888)")
        for statement in STATEMENTS:
            conn.execute(statement)
