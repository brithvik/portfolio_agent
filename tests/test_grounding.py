"""Tests for the grounding check, including the real answer that got the tech total wrong.

    python -m pytest tests/test_grounding.py -v
"""
from grounding import check_grounding, contains_number, extract_numbers

# What the agent's tool returned on November 10 (before the tools pre-computed totals)
PORTFOLIO = {
    "as_of": "2026-11-10T20:55:07+00:00", "total_value": 99431.38, "cash": 12000.09, "cash_weight_pct": 12.07,
    "positions": [
        {"symbol": "NVDA", "market_value": 17564.71, "unrealized_pl_pct": -2.42, "portfolio_weight_pct": 17.67},
        {"symbol": "VOO", "market_value": 15043.97, "unrealized_pl_pct": 0.29, "portfolio_weight_pct": 15.13},
        {"symbol": "AAPL", "market_value": 13910.45, "unrealized_pl_pct": -0.64, "portfolio_weight_pct": 13.99},
        {"symbol": "MSFT", "market_value": 11812.80, "unrealized_pl_pct": -1.56, "portfolio_weight_pct": 11.88},
        {"symbol": "AMZN", "market_value": 9059.40, "unrealized_pl_pct": 0.66, "portfolio_weight_pct": 9.11},
        {"symbol": "GOOGL", "market_value": 7973.40, "unrealized_pl_pct": -0.33, "portfolio_weight_pct": 8.02},
        {"symbol": "JPM", "market_value": 4980.00, "unrealized_pl_pct": -0.40, "portfolio_weight_pct": 5.01},
        {"symbol": "KO", "market_value": 3989.00, "unrealized_pl_pct": -0.28, "portfolio_weight_pct": 4.01},
        {"symbol": "XOM", "market_value": 3097.56, "unrealized_pl_pct": 2.29, "portfolio_weight_pct": 3.12},
    ]}
NEWS = {"articles": [
    {"headline": "Morgan Stanley reiterates Overweight on Nvidia, $300 target"},
    {"headline": "China Renaissance upgrades Apple to Buy with a $425 price target"}]}

# The agent's actual briefing from that day, word for word
REAL_BRIEFING = """**Portfolio Briefing (November 10, 2026)**

**1. Total Value & Performance**
Total portfolio value is **$99,431.38** (with $12,000.09 in cash, 12.07%). *Note: Historical tracking data is currently limited to a single day ($99,431.38 on Nov 10), so 1-day and 7-day historical changes cannot be calculated at this time.*

**2. Biggest Movers (Unrealized P&L)**
* **Best:** Exxon Mobil (**XOM** +2.29%) and Amazon (**AMZN** +0.66%).
* **Worst:** NVIDIA (**NVDA** -2.42%) and Microsoft (**MSFT** -1.56%).

**3. Concentration**
* **Largest Positions:** NVIDIA is the largest holding at **17.67%** ($17,564.71), followed by VOO at 15.13% and Apple (AAPL) at 13.99%.
* **Technology Exposure:** Direct individual tech holdings (NVDA, AAPL, MSFT, AMZN, GOOGL) total **$62,316.85**, or **62.67%** of the portfolio (rising to nearly 78% if including tech-heavy VOO).

**4. Key News**
* **NVDA:** Morgan Stanley reiterated an Overweight rating ($300 target), but reports highlight ongoing Washington China-chip roadblocks and executive succession discussions.
* **AAPL:** China Renaissance upgraded Apple to Buy with a $425 price target.

**5. Things Worth Watching**
* **High Concentration Risk:** NVDA, AAPL, and MSFT make up nearly 47% of your total assets, leaving the portfolio sensitive to tech sector swings.
* **Cash Deployment:** With over 12% in cash ($12k), monitor whether you want to deploy capital or maintain a defensive buffer."""


def test_it_catches_every_wrong_number_in_the_real_briefing_and_nothing_else():
    result = check_grounding(REAL_BRIEFING, [PORTFOLIO, NEWS])
    flagged = {u["number"] for u in result["ungrounded"]}
    # The four numbers the model made up by doing its own arithmetic
    assert flagged == {"$62,316.85", "62.67%", "78%", "47%"}
    assert result["numbers_cited"] == 20 and result["numbers_grounded"] == 16   # 80% grounded


def test_numbers_the_data_supports_are_accepted():
    answer = "NVDA is 17.67% of the portfolio ($17,564.71) and is down 2.42%. Cash is $12,000.09."
    assert check_grounding(answer, PORTFOLIO)["ungrounded"] == []


def test_reasonable_rounding_is_accepted():
    answer = "NVDA is about 17.7% (roughly 18%) of the portfolio, worth $17.6K. Cash is about $12k."
    assert check_grounding(answer, PORTFOLIO)["ungrounded"] == []


def test_a_slightly_wrong_number_is_caught():
    assert check_grounding("NVDA is 18.67% of the portfolio.", PORTFOLIO)["ungrounded"][0]["number"] == "18.67%"
    assert check_grounding("Cash is $12,500.", PORTFOLIO)["ungrounded"][0]["number"] == "$12,500"


def test_numbers_from_news_text_count_as_sources():
    assert check_grounding("Morgan Stanley has a $300 target on Nvidia.", NEWS)["ungrounded"] == []
    assert check_grounding("Morgan Stanley has a $350 target on Nvidia.", NEWS)["ungrounded"] != []


def test_numbers_the_user_said_count_as_sources():
    result = check_grounding("I proposed selling $1,500 of NVDA.", PORTFOLIO,
                             question="Propose trimming NVDA by 1500 dollars.")
    assert result["ungrounded"] == []


def test_dates_times_list_markers_and_small_counts_are_not_claims():
    answer = ("As of 2026-11-10T20:55:07+00:00 (Nov 10, 2026 at 3:55 PM):\n1. Your top 3 holdings are large.\n"
              "2. You hold 9 positions over a 7-day window in the S&P 500 fund.")
    result = check_grounding(answer, PORTFOLIO)
    assert result["ungrounded"] == [] and result["numbers_cited"] == 0


def test_invented_prices_for_stocks_not_held_are_caught():
    result = check_grounding("Your Tesla position is worth $4,250.00.", PORTFOLIO)
    assert [u["number"] for u in result["ungrounded"]] == ["$4,250.00"]


def test_contains_number_respects_precision():
    assert contains_number("Tech is 43.5% of the portfolio.", 43.53)
    assert contains_number("Tech is about 44% of the portfolio.", 43.53)
    assert not contains_number("Tech is 45% of the portfolio.", 43.53)
    assert contains_number("Worth $17,842.85 today.", 17842.85)


def test_number_extraction_handles_common_formats():
    values = [(n.value * n.scale, n.kind) for n in extract_numbers("Up $1,234.50, or 2.5 percent, on 1.2M shares; -3%.")]
    assert values == [(1234.5, "dollars"), (2.5, "percent"), (1_200_000.0, "plain"), (3.0, "percent")]


def test_a_percentage_cannot_be_vouched_for_by_an_unrelated_number():
    # The real case: "47%" slipped through because some other figure in the data rounded to 47.
    data = {"positions": [{"symbol": "KO", "shares": 46.9, "price": 47.2, "portfolio_weight_pct": 4.01}]}
    assert [u["number"] for u in check_grounding("Tech is nearly 47% of your assets.", data)["ungrounded"]] == ["47%"]
    assert check_grounding("KO trades at about $47.", data)["ungrounded"] == []        # a real price
    assert check_grounding("KO is 4.01% of the portfolio.", data)["ungrounded"] == []  # a real weight
    assert [u["number"] for u in check_grounding("KO is worth $4.01.", data)["ungrounded"]] == ["$4.01"]


def test_dollar_and_percent_changes_are_kept_apart():
    history = {"change_over_period": {"dollars": 638.0, "percent": 0.64}}
    assert check_grounding("Up $638.00, or 0.64%, this week.", history)["ungrounded"] == []
    assert [u["number"] for u in check_grounding("Up 638% this week.", history)["ungrounded"]] == ["638%"]
