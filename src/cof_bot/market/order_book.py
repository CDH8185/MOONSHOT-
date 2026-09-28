"""Level 2 order book maintained from the ``l2_data`` channel.

Message format, observed live 2026-09-27 and matching Coinbase's level2
reference: an event of ``type`` "snapshot" or "update" with ``updates``, each
``{side: "bid"|"offer", price_level, new_quantity, event_time}``.
``new_quantity`` is the new size at that price, not a delta; "0" removes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

BPS = Decimal("10000")


@dataclass(frozen=True)
class BookStats:
    best_bid: Decimal
    best_ask: Decimal
    mid: Decimal
    spread_bps: Decimal
    bid_depth_usd: Decimal
    ask_depth_usd: Decimal

    @property
    def imbalance(self) -> Decimal:
        """(bid - ask) / (bid + ask) depth near the mid, in [-1, 1]."""
        total = self.bid_depth_usd + self.ask_depth_usd
        if total == 0:
            return Decimal("0")
        return (self.bid_depth_usd - self.ask_depth_usd) / total


class OrderBook:
    def __init__(self, product_id: str):
        self.product_id = product_id
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.synced = False

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.synced = False

    def apply(self, event_type: str, updates: Iterable[dict]) -> int:
        """Apply a snapshot or update. Returns the number of levels applied."""
        if event_type == "snapshot":
            self.bids.clear()
            self.asks.clear()
        elif not self.synced:
            # An update without a prior snapshot cannot produce a correct book.
            return 0
        applied = 0
        for u in updates:
            side = u.get("side")
            book = self.bids if side == "bid" else self.asks if side in ("offer", "ask") else None
            if book is None:
                continue
            try:
                price = Decimal(str(u["price_level"]))
                qty = Decimal(str(u["new_quantity"]))
            except (KeyError, InvalidOperation):
                continue
            if not price.is_finite() or not qty.is_finite() or price <= 0 or qty < 0:
                continue
            if qty == 0:
                book.pop(price, None)
            else:
                book[price] = qty
            applied += 1
        if event_type == "snapshot":
            self.synced = True
        return applied

    def stats(self, depth_bps: Decimal = Decimal("100")) -> BookStats | None:
        """Top of book and USD depth within ``depth_bps`` of the mid."""
        if not self.synced or not self.bids or not self.asks:
            return None
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        if best_ask <= best_bid:
            return None  # crossed book; treat as unusable until the next snapshot
        mid = (best_bid + best_ask) / 2
        band = mid * depth_bps / BPS
        bid_depth = sum((p * q for p, q in self.bids.items() if p >= mid - band), Decimal("0"))
        ask_depth = sum((p * q for p, q in self.asks.items() if p <= mid + band), Decimal("0"))
        return BookStats(
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread_bps=(best_ask - best_bid) / mid * BPS,
            bid_depth_usd=bid_depth,
            ask_depth_usd=ask_depth,
        )
