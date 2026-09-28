"""An open position and its exit rule.

Infinity trailing. The stop starts at the hard stop below entry. When the best
bid first reaches entry x (1 + trail_activation), a trailing stop switches on
at the highest bid seen x (1 - trail_distance), and from then on follows every
new high. There is no fixed take-profit, so a winner is held for as long as it
keeps rising. The stop only ever moves up; the position is sold at market the
first time the best bid is at or below it.

The trigger uses the best bid, because that is the price a market sell meets.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cof_bot.risk.limits import RiskLimits


@dataclass
class Position:
    product_id: str
    base_currency: str
    base_size: Decimal
    entry_price: Decimal
    entry_fees_usd: Decimal
    opened_at: float
    stop_price: Decimal
    high_water: Decimal
    trailing: bool = False
    entry_order_id: str | None = None

    @classmethod
    def open(cls, *, product_id, base_currency, base_size, entry_price, entry_fees_usd, opened_at,
             limits: RiskLimits, entry_order_id=None) -> "Position":
        return cls(
            product_id=product_id,
            base_currency=base_currency,
            base_size=base_size,
            entry_price=entry_price,
            entry_fees_usd=entry_fees_usd,
            opened_at=opened_at,
            stop_price=entry_price * (1 - limits.hard_stop),
            high_water=entry_price,
            entry_order_id=entry_order_id,
        )

    @property
    def cost_usd(self) -> Decimal:
        return self.base_size * self.entry_price + self.entry_fees_usd

    def update(self, bid: Decimal, limits: RiskLimits) -> str | None:
        """Feed the current best bid. Returns an exit reason, or None to hold."""
        if bid > self.high_water:
            self.high_water = bid
        if not self.trailing and self.high_water >= self.entry_price * (1 + limits.trail_activation):
            self.trailing = True
        if self.trailing:
            trail = self.high_water * (1 - limits.trail_distance)
            if trail > self.stop_price:
                self.stop_price = trail
        if bid <= self.stop_price:
            return "trailing_stop" if self.trailing else "hard_stop"
        return None

    def to_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        dec = ("base_size", "entry_price", "entry_fees_usd", "stop_price", "high_water")
        return cls(**{k: Decimal(v) if k in dec else v for k, v in d.items()})
