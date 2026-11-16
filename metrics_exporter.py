"""Metrics exporter: turns the database into numbers Prometheus can watch.

Most of this system runs as short-lived Jobs (collectors, the agent), which finish before
Prometheus could ever scrape them. But everything they do is already recorded in PostgreSQL:
snapshots, agent runs and their steps, proposals, evaluation and red-team reports. So instead of
instrumenting every Job, this one small service reads those tables on each scrape and publishes
the results. The database stays the single source of truth.

The headline metric is LIVE grounding: every answer the agent gives in real use (not just in the
evaluation suite) is checked with the same code as evaluate.py, so a drop in accuracy shows up on
the dashboard the day it happens.

Read-only: every query runs in a read-only transaction, so a bug here can't change any data.

    python metrics_exporter.py        # serves http://localhost:9100/metrics
"""
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from grounding import check_grounding

load_dotenv()

PORT = int(os.environ.get("METRICS_PORT", "9100"))
NEW_YORK = ZoneInfo("America/New_York")
WINDOWS = {"24h": 86_400, "7d": 7 * 86_400}


def market_hours(now=None):
    """Regular US market hours (9:30 AM to 4 PM ET, weekdays). Ignores holidays: it's only used
    to decide whether a stale snapshot is worth an alert."""
    local = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60


class PortfolioCollector(Collector):
    def __init__(self, database_url):
        self.database_url = database_url
        self.grounding_cache = {}   # run id -> (finished_at, numbers cited, numbers grounded)

    # ---------- plumbing ----------
    def connect(self):
        # The read-only flag makes every transaction on this connection refuse writes
        return psycopg.connect(self.database_url, autocommit=True, connect_timeout=5,
                               options="-c default_transaction_read_only=on")

    def collect(self):
        started = time.time()
        sections = [self.portfolio, self.agent_runs, self.agent_steps, self.proposals,
                    self.reports, self.market_data, self.live_grounding]
        errors = GaugeMetricFamily("portfolio_exporter_section_errors",
                                   "1 if a section of this scrape failed (see the exporter log)",
                                   labels=["section"])
        up = GaugeMetricFamily("portfolio_exporter_database_up", "1 if the database answered")
        try:
            conn = self.connect()
        except psycopg.OperationalError as e:
            print(f"[metrics] database unreachable: {e}", flush=True)
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        with conn:
            for section in sections:
                try:
                    yield from section(conn)
                    errors.add_metric([section.__name__], 0)
                except psycopg.Error as e:
                    # A missing table (a feature not used yet) shouldn't hide the other metrics
                    print(f"[metrics] {section.__name__}: {type(e).__name__}: {e}".strip(), flush=True)
                    errors.add_metric([section.__name__], 1)
        yield up
        yield errors
        market = GaugeMetricFamily("market_hours", "1 during regular US market hours (weekdays, ET)")
        market.add_metric([], 1 if market_hours() else 0)
        yield market
        took = GaugeMetricFamily("portfolio_exporter_scrape_seconds", "How long this scrape took")
        took.add_metric([], time.time() - started)
        yield took

    # ---------- sections ----------
    def portfolio(self, conn):
        row = conn.execute(
            """SELECT taken_at, equity, cash, extract(epoch FROM now() - taken_at),
                      (SELECT count(*) FROM account_snapshots)
               FROM account_snapshots ORDER BY taken_at DESC LIMIT 1""").fetchone()
        if row:
            for name, help_text, value in (
                    ("portfolio_value_dollars", "Total account value at the latest snapshot", row[1]),
                    ("portfolio_cash_dollars", "Cash at the latest snapshot", row[2]),
                    ("portfolio_snapshot_age_seconds", "Seconds since the latest snapshot", row[3]),
                    ("portfolio_snapshots", "Snapshots stored", row[4])):
                g = GaugeMetricFamily(name, help_text)
                g.add_metric([], float(value))
                yield g
        news = conn.execute("SELECT count(*) FROM news WHERE fetched_at > now() - interval '24 hours'").fetchone()[0]
        g = GaugeMetricFamily("news_articles_fetched_24h", "News articles saved in the last 24 hours")
        g.add_metric([], news)
        yield g

    def agent_runs(self, conn):
        runs = CounterMetricFamily("agent_runs", "Agent runs by kind and outcome", labels=["kind", "status"])
        tokens = CounterMetricFamily("agent_tokens", "LLM tokens used", labels=["kind", "direction"])
        for kind, status, count in conn.execute(
                "SELECT kind, status, count(*) FROM agent_runs GROUP BY kind, status"):
            runs.add_metric([kind, status], count)
        # Tokens are totaled by kind alone: each label set may appear only once per metric
        for kind, tok_in, tok_out in conn.execute(
                """SELECT kind, coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0)
                   FROM agent_runs GROUP BY kind"""):
            tokens.add_metric([kind, "input"], tok_in)
            tokens.add_metric([kind, "output"], tok_out)
        yield runs
        yield tokens

        seconds = GaugeMetricFamily("agent_run_seconds", "Agent run duration over the last 24 hours",
                                    labels=["quantile"])
        p50, p95, n = conn.execute(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d), percentile_cont(0.95) WITHIN GROUP (ORDER BY d), count(*)
               FROM (SELECT extract(epoch FROM finished_at - started_at) AS d FROM agent_runs
                     WHERE status = 'done' AND finished_at > now() - interval '24 hours') s""").fetchone()
        if n:
            seconds.add_metric(["0.5"], float(p50))
            seconds.add_metric(["0.95"], float(p95))
        yield seconds

    def agent_steps(self, conn):
        tools = CounterMetricFamily("agent_tool_calls", "Tool calls by tool and outcome", labels=["tool", "outcome"])
        models = CounterMetricFamily("agent_model_calls", "Model calls by model and outcome", labels=["model", "outcome"])
        for kind, name, ok, count in conn.execute(
                """SELECT kind, name, error IS NULL, count(*) FROM agent_steps
                   WHERE kind IN ('tool', 'model') GROUP BY kind, name, error IS NULL"""):
            (tools if kind == "tool" else models).add_metric([name or "unknown", "ok" if ok else "error"], count)
        yield tools
        yield models

        latency = GaugeMetricFamily("agent_model_call_seconds", "Model call latency over the last 24 hours",
                                    labels=["quantile"])
        p50, p95, n = conn.execute(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms),
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), count(*)
               FROM agent_steps WHERE kind = 'model' AND error IS NULL
                 AND duration_ms IS NOT NULL AND at > now() - interval '24 hours'""").fetchone()
        if n:
            latency.add_metric(["0.5"], float(p50) / 1000)
            latency.add_metric(["0.95"], float(p95) / 1000)
        yield latency

    def proposals(self, conn):
        by_status = CounterMetricFamily("trade_proposals", "Trade proposals by status", labels=["status"])
        for status, count in conn.execute("SELECT status, count(*) FROM trade_proposals GROUP BY status"):
            by_status.add_metric([status], count)
        yield by_status

        rules = CounterMetricFamily("trade_proposal_rule_failures",
                                    "How often each trading rule blocked a proposal", labels=["rule"])
        for rule, count in conn.execute(
                """SELECT c->>'rule', count(*) FROM trade_proposals, jsonb_array_elements(policy_checks) c
                   WHERE status = 'rejected_by_policy' AND (c->>'passed')::boolean = false
                   GROUP BY c->>'rule'"""):
            rules.add_metric([rule], count)
        yield rules

        pending = conn.execute(
            "SELECT count(*) FROM trade_proposals WHERE status = 'pending' AND expires_at > now()").fetchone()[0]
        g = GaugeMetricFamily("trade_proposals_waiting", "Proposals waiting for a human decision")
        g.add_metric([], pending)
        yield g

        enabled = conn.execute(
            "SELECT value FROM gateway_settings WHERE key = 'trading_enabled'").fetchone()
        g = GaugeMetricFamily("gateway_trading_enabled", "0 when the kill switch is on")
        g.add_metric([], 1 if enabled and enabled[0] == "true" else 0)
        yield g

    def reports(self, conn):
        row = conn.execute(
            """SELECT extract(epoch FROM ran_at), questions, passed, facts_total, facts_correct,
                      numbers_cited, numbers_grounded
               FROM eval_reports ORDER BY id DESC LIMIT 1""").fetchone()
        if row:
            ran, questions, passed, facts, correct, cited, grounded = row
            for name, help_text, value in (
                    ("eval_last_run_timestamp_seconds", "When the evaluation suite last ran", ran),
                    ("eval_questions_passed_ratio", "Share of evaluation questions fully correct", passed / questions if questions else 0),
                    ("eval_facts_correct_ratio", "Share of expected facts stated correctly", correct / facts if facts else 0),
                    ("eval_numbers_grounded_ratio", "Share of numbers backed by the data", grounded / cited if cited else 1)):
                g = GaugeMetricFamily(name, help_text)
                g.add_metric([], float(value))
                yield g

        row = conn.execute(
            """SELECT extract(epoch FROM ran_at), attacks, resisted, proposals_attempted
               FROM redteam_reports ORDER BY id DESC LIMIT 1""").fetchone()
        if row:
            for name, help_text, value in (
                    ("redteam_last_run_timestamp_seconds", "When the red-team suite last ran", row[0]),
                    ("redteam_attacks", "Attacks in the latest red-team run", row[1]),
                    ("redteam_attacks_resisted", "Attacks the agent resisted in the latest run", row[2]),
                    ("redteam_unrequested_proposals", "Trade proposals attacks caused in the latest run", row[3])):
                g = GaugeMetricFamily(name, help_text)
                g.add_metric([], float(value))
                yield g

    def market_data(self, conn):
        """Is the idea engine's data fresh? Prices update each weekday evening, news hourly."""
        row = conn.execute(
            """SELECT extract(epoch FROM now() - ((max(day) + 1)::timestamp AT TIME ZONE 'America/New_York'))
               FROM daily_bars WHERE symbol = 'SPY'""").fetchone()
        gauges = [("market_prices_age_seconds", "Time since the end of the latest trading day we have prices for",
                   row[0] if row else None)]
        row = conn.execute("SELECT extract(epoch FROM now() - at) FROM job_state WHERE key = 'market_news'").fetchone()
        gauges.append(("market_news_age_seconds", "Time since the market news job last finished", row[0] if row else None))
        row = conn.execute(
            """SELECT count(*), count(*) FILTER (WHERE candidate) FROM screen_results
               WHERE day = (SELECT max(day) FROM screen_results)""").fetchone()
        gauges += [("screen_stocks", "Stocks measured by the latest screen run", row[0]),
                   ("screen_candidates", "Rising candidates in the latest screen run", row[1])]
        row = conn.execute(
            "SELECT examples, type_correct, direction_correct FROM classifier_reports ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[0]:
            gauges += [("classifier_type_accuracy_ratio", "Event types labeled correctly in the latest classifier test", row[1] / row[0]),
                       ("classifier_direction_accuracy_ratio", "Event directions labeled correctly in the latest classifier test", row[2] / row[0])]
        for name, help_text, value in gauges:
            if value is not None:
                g = GaugeMetricFamily(name, help_text)
                g.add_metric([], float(value))
                yield g

    def live_grounding(self, conn):
        """Check every real answer (questions and briefings, not evaluation runs) the same way the
        evaluation suite does. Each answer is checked once and remembered."""
        runs = conn.execute(
            """SELECT id, finished_at, question, answer FROM agent_runs
               WHERE status = 'done' AND kind IN ('ask', 'briefing') AND answer IS NOT NULL
                 AND finished_at > now() - interval '7 days'""").fetchall()
        live_ids = set()
        for run_id, finished_at, question, answer in runs:
            live_ids.add(run_id)
            if run_id not in self.grounding_cache:
                sources = [r[0] for r in conn.execute(
                    """SELECT result FROM agent_steps
                       WHERE run_id = %s AND kind = 'tool' AND result IS NOT NULL ORDER BY step""", (run_id,))]
                result = check_grounding(answer, sources, question)
                self.grounding_cache[run_id] = (finished_at, result["numbers_cited"], result["numbers_grounded"])
        for run_id in list(self.grounding_cache):   # forget runs older than the longest window
            if run_id not in live_ids:
                del self.grounding_cache[run_id]

        cited = GaugeMetricFamily("agent_answer_numbers_cited", "Numbers stated in real answers",
                                  labels=["window"])
        grounded = GaugeMetricFamily("agent_answer_numbers_grounded",
                                     "Numbers in real answers that are backed by the data", labels=["window"])
        bad = GaugeMetricFamily("agent_answers_ungrounded",
                                "Real answers containing at least one number not in the data", labels=["window"])
        answers = GaugeMetricFamily("agent_answers_checked", "Real answers checked", labels=["window"])
        now = datetime.now(timezone.utc)
        for window, seconds in WINDOWS.items():
            recent = [v for v in self.grounding_cache.values() if (now - v[0]).total_seconds() <= seconds]
            cited.add_metric([window], sum(v[1] for v in recent))
            grounded.add_metric([window], sum(v[2] for v in recent))
            bad.add_metric([window], sum(1 for v in recent if v[2] < v[1]))
            answers.add_metric([window], len(recent))
        yield from (cited, grounded, bad, answers)


def main():
    REGISTRY.register(PortfolioCollector(os.environ["DATABASE_URL"]))
    start_http_server(PORT)
    print(f"[metrics] serving http://0.0.0.0:{PORT}/metrics", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
