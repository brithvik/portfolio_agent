"""Evaluation: measure how accurate the agent's answers are, with plain code as the judge.

For each test question, two things are checked:

  1. Facts:     did the answer state the right values? The expected values come from the exact
                tool results the agent received in that conversation, so the test is "did it
                report its data correctly", not "did the market move while we were grading".
  2. Grounding: is every number in the answer backed by that data? (see grounding.py)

Two trick questions check that it says "you don't hold that" and "I can't predict that"
instead of making something up.

    python evaluate.py                # run every question against the real model
    python evaluate.py --list
    python evaluate.py --question sector_exposure
    python evaluate.py --run 2        # re-check the numbers in any past conversation
"""
import argparse
import os
import re
import sys
import time

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

load_dotenv()

import agent as agent_module                               # noqa: E402
from grounding import check_grounding, contains_number     # noqa: E402
from schema import ensure_schema                           # noqa: E402

PAUSE_SECONDS = float(os.environ.get("EVAL_PAUSE_SECONDS", "6"))   # stay under free-tier rate limits
WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve"]


class Missing(Exception):
    """The agent never called the tool it needed, so it couldn't have known the answer."""


def portfolio(results):
    if "get_portfolio" not in results or "totals" not in results["get_portfolio"]:
        raise Missing("get_portfolio")
    return results["get_portfolio"]


def position(results, symbol):
    return next(p for p in portfolio(results)["positions"] if p["symbol"] == symbol)


def name_of(results, symbol):
    """A holding can be named by ticker or by company name."""
    return [symbol, position(results, symbol)["name"].split(" (")[0]]


def num(label, value):
    return {"type": "number", "label": label, "value": value}


def text(label, alternatives):
    return {"type": "text", "label": label, "any_of": alternatives}


def by_sector(results, sector):
    return next(s["weight_pct"] for s in portfolio(results)["totals"]["by_sector"] if s["sector"] == sector)


def largest_position(r):
    top = portfolio(r)["totals"]["largest_position"]
    return [text("names the largest holding", name_of(r, top["symbols"][0])),
            num("its share of the portfolio", top["weight_pct"])]


def cash_position(r):
    return [num("cash in dollars", portfolio(r)["cash"]),
            num("cash as a share of the portfolio", portfolio(r)["totals"]["cash_weight_pct"])]


def top_three(r):
    top = portfolio(r)["totals"]["top_3_positions"]
    return [num("combined share of the top 3", top["weight_pct"])] + [
        text(f"names {s}", name_of(r, s)) for s in top["symbols"]]


def sector_exposure(r):
    return [num("Information Technology share", by_sector(r, "Information Technology"))]


def group_exposure(r):
    group = portfolio(r)["totals"]["by_group"][0]
    return [num("mega-cap tech share", group["weight_pct"])]


def worst_performer(r):
    worst = portfolio(r)["totals"]["worst_performers_since_purchase"][0]
    return [text("names the worst performer", name_of(r, worst["symbol"])),
            num("its percentage loss", worst["unrealized_pl_pct"])]


def best_performer(r):
    best = portfolio(r)["totals"]["best_performers_since_purchase"][0]
    return [text("names the best performer", name_of(r, best["symbol"])),
            num("its percentage gain", best["unrealized_pl_pct"])]


def position_count(r):
    count = portfolio(r)["totals"]["number_of_positions"]
    return [{"type": "count", "label": "number of positions", "value": count}]


def index_funds(r):
    return [num("index fund share", portfolio(r)["totals"]["index_funds_weight_pct"])]


def total_value(r):
    return [num("total portfolio value", portfolio(r)["total_value"])]


def one_position(r):
    p = position(r, "AAPL")
    return [num("Apple position value", p["market_value"]),
            num("Apple gain or loss in dollars", p["unrealized_pl"])]


def value_history(r):
    if "get_portfolio_history" not in r:
        raise Missing("get_portfolio_history")
    change = r["get_portfolio_history"].get("change_over_period")
    if not change:
        return []   # not enough history yet: the only requirement is no made-up numbers
    return [num("change in dollars", change["dollars"]), num("change in percent", change["percent"])]


def not_held(r):
    return [text("says the stock isn't held", [
        "don't hold", "do not hold", "don't own", "do not own", "not hold", "not own", "no position",
        "not one of your", "not in your portfolio", "isn't in your portfolio", "is not in your portfolio",
        "not a holding", "not currently", "no tesla", "do not have", "don't have", "not part of"])]


def no_prediction(r):
    return [text("declines to predict a price", [
        "can't predict", "cannot predict", "can not predict", "unable to predict", "not able to predict",
        "don't predict", "do not predict", "won't predict", "can't forecast", "cannot forecast",
        "not predict", "no way to know", "can't know", "cannot know", "impossible to predict",
        "can't provide a prediction", "cannot provide a prediction", "not able to forecast",
        "unable to forecast", "can't tell you what", "cannot tell you what"])]


QUESTIONS = [
    ("largest_position", "What is my largest position, and what share of my portfolio is it?", largest_position),
    ("cash_position", "How much cash do I have, in dollars and as a share of the portfolio?", cash_position),
    ("top_three", "What are my top 3 positions, and what do they add up to as a share of the portfolio?", top_three),
    ("sector_exposure", "What percent of my portfolio is in the Information Technology sector?", sector_exposure),
    ("group_exposure", "What share of my portfolio is in mega-cap tech stocks?", group_exposure),
    ("worst_performer", "Which holding has done worst since I bought it, and by how much?", worst_performer),
    ("best_performer", "Which holding has done best since I bought it, and by how much?", best_performer),
    ("position_count", "How many positions do I hold?", position_count),
    ("index_funds", "How much of my portfolio is in index funds?", index_funds),
    ("total_value", "What is my total portfolio value right now?", total_value),
    ("one_position", "What is my Apple position worth, and what is my gain or loss on it in dollars?", one_position),
    ("value_history", "How has my total portfolio value changed over the past week?", value_history),
    ("not_held", "What is my Tesla position worth?", not_held),
    ("no_prediction", "What will Nvidia's stock price be one month from now?", no_prediction),
]


def check_fact(fact, answer):
    lower = answer.lower()
    if fact["type"] == "number":
        return contains_number(answer, fact["value"])
    if fact["type"] == "count":
        value = fact["value"]
        return contains_number(answer, value) or (value < len(WORDS) and re.search(rf"\b{WORDS[value]}\b", lower))
    return any(re.search(rf"(?<![a-z0-9]){re.escape(a.lower())}(?![a-z0-9])", lower) for a in fact["any_of"])


def tool_results(conn, run_id):
    """What the agent actually saw: every tool result from one conversation."""
    rows = conn.execute(
        """SELECT name, result FROM agent_steps
           WHERE run_id = %s AND kind = 'tool' AND result IS NOT NULL ORDER BY step""", (run_id,)).fetchall()
    return rows, {name: result for name, result in rows}   # the dict keeps each tool's latest result


def score(conn, run_id, question, answer, expect=None):
    rows, latest = tool_results(conn, run_id)
    grounding = check_grounding(answer, [r[1] for r in rows], question)
    facts, missing_tool = [], None
    if expect:
        try:
            facts = expect(latest)
        except Missing as m:
            missing_tool = str(m)
        except (KeyError, StopIteration, IndexError) as e:
            missing_tool = f"expected data was not in the tool results ({type(e).__name__})"
    checked = [{"label": f["label"], "ok": bool(check_fact(f, answer)),
                "expected": f.get("value", f.get("any_of", [""])[0])} for f in facts]
    passed = not missing_tool and all(c["ok"] for c in checked) and not grounding["ungrounded"]
    return {"passed": passed, "facts": checked, "missing_tool": missing_tool,
            "tools": [r[0] for r in rows], **grounding}


def print_result(result, indent="      "):
    if result.get("error"):
        print(f"{indent}ERROR: {result['error']}")
        return
    if result["missing_tool"]:
        print(f"{indent}never looked it up: {result['missing_tool']}")
    for fact in result["facts"]:
        print(f"{indent}{'ok   ' if fact['ok'] else 'WRONG'} {fact['label']}"
              + ("" if fact["ok"] else f" (expected {fact['expected']})"))
    for u in result["ungrounded"]:
        print(f"{indent}NOT IN THE DATA: {u['number']}  ...{u['context']}...")


def rescore_run(conn, run_id):
    run = conn.execute("SELECT question, answer, kind, status FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
    if not run or not run[1]:
        raise SystemExit(f"Run {run_id} has no answer to check.")
    result = score(conn, run_id, run[0], run[1])
    print(f"Run {run_id} ({run[2]}): {result['numbers_grounded']} of {result['numbers_cited']} numbers "
          f"are backed by the data ({result['grounded_rate']}%).")
    print_result(result, indent="  ")
    if not result["ungrounded"]:
        print("  Every number in this answer came from the data.")
    return 0 if not result["ungrounded"] else 1


def main():
    parser = argparse.ArgumentParser(description="Score the agent's accuracy.")
    parser.add_argument("--question", help="run only this question")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--run", type=int, help="re-check the numbers in a past conversation")
    parser.add_argument("--show-answers", action="store_true")
    args = parser.parse_args()
    if args.list:
        for name, question, _ in QUESTIONS:
            print(f"  {name:<18} {question}")
        return 0

    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    ensure_schema(conn)
    if args.run:
        return rescore_run(conn, args.run)

    questions = [q for q in QUESTIONS if not args.question or q[0] == args.question]
    if not questions:
        raise SystemExit(f"No question named {args.question}. Use --list.")
    print(f"Evaluation: {len(questions)} questions, model {agent_module.MODEL}. "
          f"Read-only: the agent cannot propose trades here.\n", flush=True)

    results = []
    for i, (name, question, expect) in enumerate(questions):
        if i:
            time.sleep(PAUSE_SECONDS)
        print(f"[{i + 1}/{len(questions)}] {name}: {question}", flush=True)
        agent = agent_module.Agent(conn, allow_proposals=False)
        started = time.time()
        try:
            answer = agent.run("eval", question)
        except Exception as e:
            results.append({"name": name, "passed": None, "error": f"{type(e).__name__}: {e}"[:300]})
            print_result(results[-1])
            continue
        seconds = round(time.time() - started, 1)
        tokens = conn.execute("SELECT input_tokens + output_tokens FROM agent_runs WHERE id = %s",
                              (agent.run_id,)).fetchone()[0]
        result = {"name": name, "run_id": agent.run_id, "seconds": seconds, "tokens": tokens,
                  "answer": answer, **score(conn, agent.run_id, question, answer, expect)}
        results.append(result)
        print(f"      -> {'pass' if result['passed'] else 'FAIL'} ({seconds}s, {tokens} tokens, "
              f"tools: {', '.join(result['tools']) or 'none'})", flush=True)
        print_result(result)
        if args.show_answers:
            print("      answer: " + answer.replace("\n", "\n              "), flush=True)

    scored = [r for r in results if r["passed"] is not None]
    errors = len(results) - len(scored)
    passed = sum(1 for r in scored if r["passed"])
    facts_total = sum(len(r["facts"]) for r in scored)
    facts_correct = sum(1 for r in scored for f in r["facts"] if f["ok"])
    cited = sum(r["numbers_cited"] for r in scored)
    grounded = sum(r["numbers_grounded"] for r in scored)
    avg_seconds = round(sum(r["seconds"] for r in scored) / len(scored), 1) if scored else None
    tokens = sum(r["tokens"] or 0 for r in scored)

    def pct(a, b):
        return f"{a / b * 100:.0f}%" if b else "n/a"

    print("\n" + "=" * 72)
    print(f"Questions fully correct:        {passed} of {len(scored)}  ({pct(passed, len(scored))})")
    print(f"Facts stated correctly:         {facts_correct} of {facts_total}  ({pct(facts_correct, facts_total)})")
    print(f"Numbers backed by the data:     {grounded} of {cited}  ({pct(grounded, cited)})")
    print(f"Average time per answer:        {avg_seconds}s")
    print(f"Tokens used:                    {tokens:,}")
    if errors:
        print(f"Runs that errored (not scored): {errors}  (often a rate limit; rerun with --question)")
    print("=" * 72)

    conn.execute(
        """INSERT INTO eval_reports (model, questions, passed, facts_total, facts_correct,
               numbers_cited, numbers_grounded, avg_seconds, total_tokens, results)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (agent_module.MODEL, len(scored), passed, facts_total, facts_correct, cited, grounded,
         avg_seconds, tokens, Jsonb(results)))
    print("Saved to the eval_reports table.")
    return 0 if scored and passed == len(scored) and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
