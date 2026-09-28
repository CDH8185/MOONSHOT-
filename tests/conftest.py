import pytest
import requests


def make_http_error(status, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests.exceptions.HTTPError(f"{status} error", response=response)


def spot_product(**overrides):
    product = {
        "product_id": "ABC-USD",
        "product_type": "SPOT",
        "quote_currency_id": "USD",
        "base_currency_id": "ABC",
        "status": "online",
        "is_disabled": False,
        "trading_disabled": False,
        "view_only": False,
        "cancel_only": False,
        "limit_only": False,
        "post_only": False,
        "auction_mode": False,
        "base_increment": "0.01",
        "quote_increment": "0.0001",
        "base_min_size": "0.01",
        "quote_min_size": "1",
        "price": "0.5",
        "volume_24h": "1000000",
        "approximate_quote_24h_volume": "500000",
    }
    product.update(overrides)
    return product


@pytest.fixture
def no_sleep():
    calls = []
    return calls, calls.append
