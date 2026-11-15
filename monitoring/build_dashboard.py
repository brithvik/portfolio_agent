"""Builds the Grafana dashboard (monitoring/dashboards/portfolio-agent.json) from code.

The dashboard is defined here, reviewed like any other code, and Terraform deploys the JSON.
Nobody edits it by clicking around in Grafana (the provisioning config forbids that).

    python monitoring/build_dashboard.py           # regenerate the JSON after editing this file
    python monitoring/build_dashboard.py --check   # CI: fail if the JSON is out of date
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).parent / "dashboards" / "portfolio-agent.json"
DS = {"type": "prometheus", "uid": "prometheus"}
GREEN, YELLOW, RED, BLUE = "green", "yellow", "red", "blue"
START_EQUITY = 100_000   # Alpaca paper accounts start here

_panels = []
_y = 0


def _thresholds(steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


def row(title):
    global _y
    _panels.append({"type": "row", "title": title, "collapsed": False, "panels": [],
                    "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y}})
    _y += 1


def panel(kind, title, targets, w, h, x, description="", unit=None, steps=None, mappings=None,
          decimals=None, options=None, minmax=None, no_value=None):
    defaults = {}
    if unit:
        defaults["unit"] = unit
    if steps:
        defaults["thresholds"] = _thresholds(steps)
        defaults["color"] = {"mode": "thresholds"}
    if mappings:
        defaults["mappings"] = mappings
    if decimals is not None:
        defaults["decimals"] = decimals
    if no_value:
        defaults["noValue"] = no_value
    if minmax:
        defaults["min"], defaults["max"] = minmax
    _panels.append({
        "type": kind, "title": title, "description": description, "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": _y},
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": expr, "legendFormat": legend}
                    for i, (expr, legend) in enumerate(targets)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": options or {},
    })


def stat(title, expr, x, w=4, **kw):
    panel("stat", title, [(expr, "")], w, 4, x,
          options={"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background",
                   "graphMode": "none", "textMode": "value"}, **kw)


def next_line(height):
    global _y
    _y += height


# ---------------------------------------------------------------- the dashboard
row("Is everything healthy?")
stat("Alerts firing", 'count(ALERTS{alertstate="firing"}) or vector(0)', 0,
     description="Alert rules are in k8s/monitoring.yaml. Open Prometheus to see which ones.",
     steps=[(None, GREEN), (1, RED)], decimals=0)
stat("Data is current",
     "clamp_max((portfolio_snapshot_age_seconds < bool 900) + (1 - market_hours), 1)", 4,
     mappings=[{"type": "value", "options": {"1": {"text": "Yes", "color": GREEN},
                                             "0": {"text": "STALE", "color": RED}}}],
     description="Snapshots run every 5 minutes during US market hours. Outside market hours there is "
                 "nothing to collect, so this stays green. Red means the collector stopped during the day.")
stat("Trading", "gateway_trading_enabled", 8,
     mappings=[{"type": "value", "options": {"1": {"text": "Enabled", "color": GREEN},
                                             "0": {"text": "KILL SWITCH ON", "color": RED}}}])
stat("Waiting for you", "trade_proposals_waiting", 12, decimals=0,
     description="Trade proposals waiting for a decision on the approval page.",
     steps=[(None, GREEN), (1, BLUE)])
stat("Live answers grounded (24h)",
     '(agent_answer_numbers_grounded{window="24h"} / agent_answer_numbers_cited{window="24h"})'
     ' and (agent_answer_numbers_cited{window="24h"} > 0)', 16,
     unit="percentunit", decimals=1, minmax=(0, 1), no_value="No answers yet",
     description="Every number in every real answer from the last 24 hours, checked against the data the "
                 "agent saw (grounding.py). The 7-day view is in Accuracy and safety below.",
     steps=[(None, RED), (0.9, YELLOW), (0.98, GREEN)])
stat("Red team resisted", "redteam_attacks_resisted / redteam_attacks", 20,
     unit="percentunit", decimals=0, minmax=(0, 1),
     description="Share of prompt-injection attacks resisted in the latest red-team run.",
     steps=[(None, RED), (1, GREEN)])
next_line(4)

row("Portfolio")
panel("timeseries", f"Profit / loss vs ${START_EQUITY:,.0f} start", [(f"portfolio_value_dollars - {START_EQUITY}", "profit / loss")], 16, 8, 0, unit="currencyUSD",
      description="Alpaca paper accounts start with $100,000. Plotting the difference keeps small moves visible.")
panel("timeseries", "Cash", [("portfolio_cash_dollars", "cash")], 8, 8, 16, unit="currencyUSD")
next_line(8)

row("Agent")
panel("timeseries", "Answer time (24h window)", [("agent_run_seconds", "p{{quantile}}")], 8, 8, 0, unit="s",
      description="Time from question to answer. Nearly all of it is spent waiting on the LLM.")
panel("timeseries", "LLM call latency (24h window)", [("agent_model_call_seconds", "p{{quantile}}")], 8, 8, 8, unit="s")
panel("timeseries", "Tokens per hour", [("sum by (kind) (increase(agent_tokens_total[1h]))", "{{kind}}")],
      8, 8, 16, unit="short")
next_line(8)
panel("bargauge", "Tool calls", [("sum by (tool) (agent_tool_calls_total)", "{{tool}}")], 8, 8, 0,
      options={"orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"]}})
panel("stat", "LLM calls by model and outcome",
      [('sum by (model, outcome) (agent_model_calls_total)', "{{model}} / {{outcome}}")], 8, 8, 8,
      description="Calls to the fallback model, or any error outcome, mean the main model was failing.",
      options={"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "none", "graphMode": "none",
               "textMode": "value_and_name", "orientation": "vertical"})
panel("bargauge", "Agent runs by outcome", [("sum by (kind, status) (agent_runs_total)", "{{kind}} {{status}}")],
      8, 8, 16, options={"orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"]}})
next_line(8)

row("Accuracy and safety")
panel("timeseries", "Evaluation suite (latest run)", [
    ("eval_questions_passed_ratio", "questions fully correct"),
    ("eval_facts_correct_ratio", "facts correct"),
    ("eval_numbers_grounded_ratio", "numbers grounded")], 12, 8, 0, unit="percentunit", minmax=(0, 1))
panel("timeseries", "Real answers with a number not in the data", [
    ('agent_answers_ungrounded{window="24h"}', "last 24h"),
    ('agent_answers_ungrounded{window="7d"}', "last 7 days")], 12, 8, 12, decimals=0,
      description="Find the run with: .\\scripts\\evaluate.ps1 -Run <id>")
next_line(8)
panel("bargauge", "Trade proposals by status", [("sum by (status) (trade_proposals_total)", "{{status}}")],
      12, 8, 0, options={"orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"]}})
panel("bargauge", "Which rules blocked proposals",
      [("sum by (rule) (trade_proposal_rule_failures_total)", "{{rule}}")], 12, 8, 12,
      description="Each blocked proposal counts once for every rule it broke.",
      options={"orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"]}})
next_line(8)

for i, p in enumerate(_panels, start=1):
    p["id"] = i

DASHBOARD = {
    "uid": "portfolio-agent", "title": "Portfolio agent", "tags": ["portfolio-agent"],
    "timezone": "browser", "schemaVersion": 39, "editable": False, "refresh": "1m",
    "time": {"from": "now-7d", "to": "now"}, "panels": _panels,
}


def main():
    text = json.dumps(DASHBOARD, indent=2) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            raise SystemExit(f"{OUT} is out of date. Run: python monitoring/build_dashboard.py")
        print("Dashboard JSON is up to date.")
        return
    OUT.write_text(text)
    print(f"Wrote {OUT} ({len(_panels)} panels)")


if __name__ == "__main__":
    main()
