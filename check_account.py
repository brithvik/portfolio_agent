"""Connect to your Alpaca paper account and print its balance and positions."""
import os

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

load_dotenv()

client = TradingClient(
    os.environ["ALPACA_API_KEY"],
    os.environ["ALPACA_SECRET_KEY"],
    paper=True,  # paper-api.alpaca.markets: simulated money only
)

account = client.get_account()
print(f"Account status:  {account.status}")
print(f"Portfolio value: ${float(account.equity):,.2f}")
print(f"Cash:            ${float(account.cash):,.2f}")
print(f"Buying power:    ${float(account.buying_power):,.2f}")

positions = client.get_all_positions()
print(f"\nPositions: {len(positions)}")
for p in positions:
    print(f"  {p.symbol:<6} {float(p.qty):>8g} shares  "
          f"${float(p.market_value):>10,.2f}  P/L ${float(p.unrealized_pl):>9,.2f}")