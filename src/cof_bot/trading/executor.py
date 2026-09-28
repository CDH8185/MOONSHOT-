"""Order execution: shadow (simulated against the live order book) and live.

Both executors return the same ``Fill``, so the engine cannot tell them apart
and shadow results exercise exactly the code path live trading uses.

Entry is a limit IOC buy priced at the sizing module's limit price (best ask
plus the allowed slippage): whatever the book offers at or below that price
fills at once, the rest is cancelled by the exchange. Exit is a market sell,
because a stop must get out whatever the book looks like.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from cof_bot.errors import ExchangeError
from cof_bot.market.ws_feed import MarketState

log = logging.getLogger(__name__)

TERMINAL = frozenset({"FILLED", "CANCELLED", "EXPIRED", "FAILED"})


@dataclass(frozen=True)
class Fill:
    product_id: str
    side: str
    base_size: Decimal
    avg_price: Decimal
    fees_usd: Decimal
    client_order_id: str
    order_id: str | None
    status: str
    note: str | None = None

    @property
    def quote_usd(self) -> Decimal:
        return self.base_size * self.avg_price

    def to_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}


def new_client_order_id(prefix: str) -> str:
    return f"cof-{prefix}-{uuid.uuid4().hex[:20]}"


class ShadowExecutor:
    """Fills against the current order book. Never sends anything anywhere."""

    mode = "shadow"

    def __init__(self, state: MarketState, fee_rate: Decimal):
        self.state = state
        self.fee_rate = fee_rate

    def _levels(self, product_id: str, side: str) -> list[tuple[Decimal, Decimal]]:
        with self.state.lock:
            book = self.state.books.get(product_id)
            if book is not None and book.synced:
                src = book.asks if side == "BUY" else book.bids
                return sorted(src.items(), reverse=(side == "SELL"))
            t = self.state.tickers.get(product_id)
        price = None if t is None else (t.best_ask if side == "BUY" else t.best_bid) or t.price
        return [(price, Decimal("Infinity"))] if price else []

    def buy(self, product_id: str, base_size: Decimal, limit_price: Decimal) -> Fill:
        cid = new_client_order_id("buy")
        filled = cost = Decimal(0)
        for price, qty in self._levels(product_id, "BUY"):
            if price > limit_price or filled >= base_size:
                break
            take = min(qty, base_size - filled)
            filled += take
            cost += take * price
        if filled == 0:
            return Fill(product_id, "BUY", Decimal(0), Decimal(0), Decimal(0), cid, None, "CANCELLED",
                        "no ask at or below the limit price")
        return Fill(product_id, "BUY", filled, cost / filled, cost * self.fee_rate, cid, None, "FILLED",
                    None if filled == base_size else "partial fill")

    def sell(self, product_id: str, base_size: Decimal) -> Fill:
        cid = new_client_order_id("sell")
        filled = proceeds = Decimal(0)
        last = None
        for price, qty in self._levels(product_id, "SELL"):
            take = min(qty, base_size - filled)
            filled += take
            proceeds += take * price
            last = price
            if filled >= base_size:
                break
        note = None
        if filled < base_size and last is not None:
            # Visible depth ran out; assume the rest fills at the worst level seen.
            proceeds += (base_size - filled) * last
            filled = base_size
            note = "book depth exhausted; remainder priced at the last visible bid"
        if filled == 0:
            raise ExchangeError(f"shadow sell {product_id}: no price available")
        return Fill(product_id, "SELL", filled, proceeds / filled, proceeds * self.fee_rate, cid, None, "FILLED", note)


class LiveExecutor:
    """Places real orders through the gateway. Used only in live mode."""

    mode = "live"

    def __init__(self, gateway, fee_rate: Decimal, *, poll_s: float = 0.5, max_polls: int = 20,
                 sleep: Callable[[float], None] = time.sleep):
        self.gateway = gateway
        self.fee_rate = fee_rate
        self.poll_s = poll_s
        self.max_polls = max_polls
        self.sleep = sleep

    def _settle(self, order_id: str, cancel_if_open: bool) -> dict:
        order = self.gateway.get_order(order_id)
        for _ in range(self.max_polls):
            if order["status"] in TERMINAL:
                return order
            self.sleep(self.poll_s)
            order = self.gateway.get_order(order_id)
        if cancel_if_open:
            log.warning("Order %s still %s; cancelling", order_id, order["status"])
            try:
                self.gateway.cancel_order(order_id)
            except ExchangeError:
                log.exception("Cancel of %s failed", order_id)
            order = self.gateway.get_order(order_id)
        return order

    def _fill(self, product_id, side, cid, order_id, order) -> Fill:
        size = order["filled_size"]
        price = order["average_filled_price"]
        return Fill(product_id, side, size, price if size > 0 else Decimal(0), order["total_fees"], cid, order_id,
                    order["status"], None if order["status"] == "FILLED" else f"status {order['status']}")

    def buy(self, product_id: str, base_size: Decimal, limit_price: Decimal) -> Fill:
        cid = new_client_order_id("buy")
        order_id = self.gateway.place_limit_ioc_buy(cid, product_id, base_size, limit_price)
        return self._fill(product_id, "BUY", cid, order_id, self._settle(order_id, cancel_if_open=True))

    def sell(self, product_id: str, base_size: Decimal) -> Fill:
        cid = new_client_order_id("sell")
        order_id = self.gateway.place_market_sell(cid, product_id, base_size)
        fill = self._fill(product_id, "SELL", cid, order_id, self._settle(order_id, cancel_if_open=False))
        if fill.base_size <= 0:
            raise ExchangeError(f"market sell {product_id} ({order_id}) filled nothing: {fill.status}")
        return fill
