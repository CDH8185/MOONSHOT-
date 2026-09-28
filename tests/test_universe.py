from decimal import Decimal

import pytest

from cof_bot.config import load_settings
from cof_bot.exchange.universe import build_universe, rejection_reason
from conftest import spot_product

S = load_settings({})


def test_qualifying_product_passes():
    assert rejection_reason(spot_product(), S) is None


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"product_type": "FUTURE"}, "not_spot"),
        ({"quote_currency_id": "USDC", "product_id": "ABC-USDC"}, "not_usd_quote"),
        ({"quote_currency_id": "EUR"}, "not_usd_quote"),
        ({"status": "delisted"}, "not_online"),
        ({"is_disabled": True}, "is_disabled"),
        ({"trading_disabled": True}, "trading_disabled"),
        ({"view_only": True}, "view_only"),
        ({"cancel_only": True}, "cancel_only"),
        ({"limit_only": True}, "limit_only"),
        ({"post_only": True}, "post_only"),
        ({"auction_mode": True}, "auction_mode"),
        ({"base_currency_id": "BTC"}, "excluded_base"),
        ({"base_currency_id": "eth"}, "excluded_base"),
        ({"base_currency_id": "USDT"}, "excluded_base"),
        ({"base_currency_id": ""}, "missing_base"),
        ({"base_increment": ""}, "missing_base_increment"),
        ({"approximate_quote_24h_volume": "99999.99"}, "below_min_volume"),
        ({"approximate_quote_24h_volume": None, "volume_24h": None}, "volume_unknown"),
    ],
)
def test_rejections(overrides, reason):
    assert rejection_reason(spot_product(**overrides), S) == reason


def test_volume_fallback_uses_base_volume_times_price():
    # 150,000 x 0.5 = 75,000, under the 100,000 floor.
    p = spot_product(approximate_quote_24h_volume=None, volume_24h="150000", price="0.5")
    assert rejection_reason(p, S) == "below_min_volume"
    # 300,000 x 0.5 = 150,000, over the floor.
    p = spot_product(approximate_quote_24h_volume=None, volume_24h="300000", price="0.5")
    assert rejection_reason(p, S) is None


def test_volume_floor_boundary_is_inclusive():
    assert rejection_reason(spot_product(approximate_quote_24h_volume="100000"), S) is None


def test_max_volume_ceiling():
    s = load_settings({"COF_MAX_QUOTE_VOLUME_24H": "400000"})
    assert rejection_reason(spot_product(), s) == "above_max_volume"


def test_floor_disabled_allows_unknown_volume():
    s = load_settings({"COF_MIN_QUOTE_VOLUME_24H": "0"})
    assert rejection_reason(spot_product(approximate_quote_24h_volume=None, volume_24h=None), s) is None


def test_build_universe_counts_sorts_and_dedupes():
    products = [
        spot_product(product_id="ZZZ-USD", base_currency_id="ZZZ"),
        spot_product(product_id="AAA-USD", base_currency_id="AAA"),
        spot_product(product_id="AAA-USD", base_currency_id="AAA"),
        spot_product(product_id="BTC-USD", base_currency_id="BTC"),
        spot_product(product_id="AAA-USDC", quote_currency_id="USDC"),
    ]
    u = build_universe(products, S, authoritative=True, source="test")
    assert u.product_ids == ["AAA-USD", "ZZZ-USD"]
    assert u.total_products == 5
    assert u.rejected == {"duplicate": 1, "excluded_base": 1, "not_usd_quote": 1}
    assert u.pairs[0].quote_min_size == Decimal("1")


def test_sdk_objects_supported():
    from coinbase.rest.types.product_types import Product

    u = build_universe([Product(**spot_product())], S, authoritative=False, source="test")
    assert u.product_ids == ["ABC-USD"]
    assert u.authoritative is False
