"""The only code in the whole project that can place an order.

It wraps Alpaca in a small, plain interface, so the gateway's logic can be tested with a
stand-in broker, and so swapping brokers later means changing only this file.
"""
import os
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


class BrokerError(Exception):
    """The broker refused or failed a request."""


class OrderNotFound(BrokerError):
    pass


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float          # the smaller of cash and non-margin buying power: real spendable cash
    blocked: bool


@dataclass(frozen=True)
class OrderInfo:
    id: str
    client_order_id: str
    status: str
    filled_qty: float
    filled_avg_price: float | None


def _info(order):
    return OrderInfo(
        id=str(order.id), client_order_id=order.client_order_id,
        status=getattr(order.status, "value", str(order.status)),
        filled_qty=float(order.filled_qty or 0),
        filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
    )


class AlpacaBroker:
    def __init__(self):
        paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
        if not paper and os.environ.get("ALLOW_LIVE_TRADING") != "yes-i-understand-this-is-real-money":
            # A deliberate speed bump: real money needs a second, explicit setting
            raise RuntimeError("Refusing to start against a LIVE account. Set ALPACA_PAPER=true, or "
                               "set ALLOW_LIVE_TRADING=yes-i-understand-this-is-real-money.")
        self.paper = paper
        self.client = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"],
                                    paper=paper)

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except APIError as e:
            if getattr(e, "status_code", None) == 404:
                raise OrderNotFound(str(e)) from e
            raise BrokerError(str(e)) from e

    def account(self):
        a = self._call(self.client.get_account)
        cash = min(float(a.cash), float(a.non_marginable_buying_power or a.cash))
        return Account(equity=float(a.equity), cash=cash,
                       blocked=bool(a.trading_blocked or a.account_blocked))

    def positions(self):
        return {p.symbol: float(p.market_value) for p in self._call(self.client.get_all_positions)}

    def market_open(self):
        return bool(self._call(self.client.get_clock).is_open)

    def submit(self, symbol, side, notional, client_order_id):
        request = MarketOrderRequest(
            symbol=symbol, notional=round(float(notional), 2),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,   # the broker rejects a repeat of the same id
        )
        return _info(self._call(self.client.submit_order, request))

    def order(self, order_id):
        return _info(self._call(self.client.get_order_by_id, order_id))

    def order_by_client_id(self, client_order_id):
        return _info(self._call(self.client.get_order_by_client_id, client_order_id))
