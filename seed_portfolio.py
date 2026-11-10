"""Buy a starter portfolio in the PAPER account, so the agent has something to analyze.

It's deliberately tech-heavy (about 70%), giving the agent a real concentration risk to find.
Safe to run twice: it stops if the account already has positions or open orders.
"""
import os

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from dotenv import load_dotenv

load_dotenv()

# Dollar amounts per stock (fractional shares, so exact amounts work)
PORTFOLIO = {
    "NVDA": 18_000,
    "AAPL": 14_000,
    "MSFT": 12_000,
    "AMZN": 9_000,
    "GOOGL": 8_000,
    "VOO": 15_000,   # S&P 500 index fund
    "JPM": 5_000,
    "KO": 4_000,
    "XOM": 3_000,
}

client = TradingClient(
    os.environ["ALPACA_API_KEY"],
    os.environ["ALPACA_SECRET_KEY"],
    paper=True,  # never point this script at a live account
)

if client.get_all_positions():
    raise SystemExit("This account already has positions. Nothing to do.")
if client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
    raise SystemExit("There are already open orders (they fill when the market opens). Nothing to do.")

for symbol, dollars in PORTFOLIO.items():
    order = client.submit_order(MarketOrderRequest(
        symbol=symbol,
        notional=dollars,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    ))
    print(f"Ordered ${dollars:>6,} of {symbol:<5} status: {order.status}")

print(f"\nTotal: ${sum(PORTFOLIO.values()):,}. Orders fill when the market is open "
      "(9:30 AM - 4:00 PM ET on weekdays).")