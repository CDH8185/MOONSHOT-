"""Compliant asset universe: tradable USD spot pairs on Coinbase Advanced.

Compliance source. The authenticated List Products call
(``RESTClient.get_products``) answers for the account that owns the API key,
so for a United States account it returns what that account may trade. The
public call (``get_public_products``) is the global catalogue and is used only
as a read only fallback; a universe built from it is marked non authoritative
and must never feed live trading.

Filters, each recorded as a rejection reason so the result is auditable:
product_type SPOT, quote USD, status online, not disabled, not trading
disabled, not view only, not cancel only, not limit only, not post only, not
in auction mode (the strategy must be able to exit at market immediately),
base not excluded (Tier 1 or stablecoin), and 24 hour USD volume inside the
configured liquidity band.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from cof_bot.config import Settings


@dataclass(frozen=True)
class UsdPair:
    product_id: str
    base_currency: str
    base_name: str
    display_symbol: str
    base_increment: Decimal
    quote_increment: Decimal
    base_min_size: Decimal
    quote_min_size: Decimal
    price: Decimal | None
    quote_volume_24h: Decimal | None


@dataclass
class Universe:
    pairs: list[UsdPair]
    authoritative: bool
    source: str
    total_products: int
    rejected: Counter = field(default_factory=Counter)

    @property
    def product_ids(self) -> list[str]:
        return [p.product_id for p in self.pairs]


def _attr(product: Any, name: str, default: Any = None) -> Any:
    if isinstance(product, dict):
        return product.get(name, default)
    return getattr(product, name, default)


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _quote_volume(product: Any) -> Decimal | None:
    # approximate_quote_24h_volume is returned by the live API (observed
    # 2026-09-27) but is not a declared field in the SDK Product type, so it is
    # read defensively and a base volume times price fallback is used.
    direct = _dec(_attr(product, "approximate_quote_24h_volume"))
    if direct is not None:
        return direct
    base_vol = _dec(_attr(product, "volume_24h"))
    price = _dec(_attr(product, "price"))
    if base_vol is None or price is None:
        return None
    return base_vol * price


_BLOCKING_FLAGS = (
    "is_disabled",
    "trading_disabled",
    "view_only",
    "cancel_only",
    "limit_only",
    "post_only",
    "auction_mode",
)


def rejection_reason(product: Any, settings: Settings) -> str | None:
    """Return why a product is outside the universe, or None if it qualifies."""
    if _attr(product, "product_type") != "SPOT":
        return "not_spot"
    if _attr(product, "quote_currency_id") != "USD":
        return "not_usd_quote"
    if _attr(product, "status") != "online":
        return "not_online"
    for flag in _BLOCKING_FLAGS:
        if _attr(product, flag):
            return flag
    base = (_attr(product, "base_currency_id") or "").upper()
    if not base:
        return "missing_base"
    if base in settings.all_excluded_bases:
        return "excluded_base"
    for name in ("base_increment", "quote_increment", "base_min_size", "quote_min_size"):
        if _dec(_attr(product, name)) is None:
            return f"missing_{name}"
    volume = _quote_volume(product)
    if settings.min_quote_volume_24h > 0:
        if volume is None:
            return "volume_unknown"
        if volume < settings.min_quote_volume_24h:
            return "below_min_volume"
    if settings.max_quote_volume_24h is not None and volume is not None:
        if volume > settings.max_quote_volume_24h:
            return "above_max_volume"
    return None


def build_universe(
    products: Iterable[Any], settings: Settings, *, authoritative: bool, source: str
) -> Universe:
    products = list(products)
    pairs: list[UsdPair] = []
    rejected: Counter = Counter()
    seen: set[str] = set()
    for product in products:
        reason = rejection_reason(product, settings)
        product_id = _attr(product, "product_id")
        if reason is None and product_id in seen:
            reason = "duplicate"
        if reason is not None:
            rejected[reason] += 1
            continue
        seen.add(product_id)
        pairs.append(
            UsdPair(
                product_id=product_id,
                base_currency=_attr(product, "base_currency_id").upper(),
                base_name=str(_attr(product, "base_name") or ""),
                display_symbol=str(_attr(product, "base_display_symbol") or _attr(product, "base_currency_id")),
                base_increment=_dec(_attr(product, "base_increment")),
                quote_increment=_dec(_attr(product, "quote_increment")),
                base_min_size=_dec(_attr(product, "base_min_size")),
                quote_min_size=_dec(_attr(product, "quote_min_size")),
                price=_dec(_attr(product, "price")),
                quote_volume_24h=_quote_volume(product),
            )
        )
    pairs.sort(key=lambda p: p.product_id)
    return Universe(
        pairs=pairs,
        authoritative=authoritative,
        source=source,
        total_products=len(products),
        rejected=rejected,
    )
