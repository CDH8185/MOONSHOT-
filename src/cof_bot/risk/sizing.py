"""Fractional position sizing.

The size of a new position is the smallest of:

1. risk budget: equity x risk_per_trade, divided by the loss per dollar if the
   hard stop is hit (stop distance, plus the worst entry slippage allowed,
   plus the fee on the way in and on the way out);
2. equity x max_position;
3. cash available, less the entry fee;
4. max_book_share of the ask depth within 1% of the mid;
5. the daily drawdown headroom left, divided by the same loss per dollar, so
   one stopped-out trade can never take the day past its limit.

The result is rounded down to the product's base increment and refused if it
falls below the product's minimum base or quote size. All arithmetic is
Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from cof_bot.risk.limits import RiskLimits

BPS = Decimal("10000")


@dataclass(frozen=True)
class SizeDecision:
    base_size: Decimal
    limit_price: Decimal
    notional_usd: Decimal
    max_loss_usd: Decimal
    binding: str
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def round_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def size_entry(
    *,
    limits: RiskLimits,
    equity_usd: Decimal,
    cash_usd: Decimal,
    best_ask: Decimal,
    ask_depth_usd: Decimal,
    fee_rate: Decimal,
    drawdown_headroom_usd: Decimal,
    base_increment: Decimal,
    quote_increment: Decimal,
    base_min_size: Decimal,
    quote_min_size: Decimal,
) -> SizeDecision:
    slip = limits.max_entry_slippage_bps / BPS
    limit_price = round_down(best_ask * (1 + slip), quote_increment) if quote_increment > 0 else best_ask * (1 + slip)
    if limit_price <= 0 or equity_usd <= 0:
        return SizeDecision(Decimal(0), limit_price, Decimal(0), Decimal(0), "none", "no equity or price")

    loss_per_dollar = limits.hard_stop + slip + 2 * fee_rate
    caps = {
        "risk_budget": equity_usd * limits.risk_per_trade / loss_per_dollar,
        "max_position": equity_usd * limits.max_position,
        "cash": cash_usd / (1 + fee_rate),
        "book_depth": ask_depth_usd * limits.max_book_share,
        "daily_headroom": max(drawdown_headroom_usd, Decimal(0)) / loss_per_dollar,
    }
    binding = min(caps, key=caps.get)
    notional = caps[binding]
    base = round_down(notional / limit_price, base_increment)
    notional = base * limit_price
    max_loss = notional * loss_per_dollar

    reason = None
    if base <= 0 or base < base_min_size:
        reason = f"size {base} below product minimum {base_min_size} (bound by {binding})"
    elif notional < quote_min_size:
        reason = f"notional {notional:.2f} below product minimum {quote_min_size} (bound by {binding})"
    return SizeDecision(base, limit_price, notional, max_loss, binding, reason)
