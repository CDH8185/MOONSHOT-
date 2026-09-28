from decimal import Decimal

import pytest
import requests

from cof_bot.config import load_settings
from cof_bot.errors import CredentialError, ExchangeError
from cof_bot.exchange.client import CoinbaseGateway
from conftest import make_http_error, spot_product

CREDS = {"COINBASE_API_KEY": "organizations/o/apiKeys/k12345", "COINBASE_API_SECRET": "secret", "COF_REST_MAX_RPS": "1000"}


class FakeClient:
    """Stands in for coinbase.rest.RESTClient; records how it was built and called."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.permissions = {"can_view": True, "can_trade": True, "can_transfer": False, "portfolio_type": "DEFAULT"}
        self.account_pages = [
            {
                "accounts": [
                    {"currency": "USD", "available_balance": {"value": "250.50", "currency": "USD"}, "hold": {"value": "0"}},
                    {"currency": "abc", "available_balance": {"value": "0"}, "hold": {"value": "1.5"}},
                ],
                "has_next": True,
                "cursor": "c1",
            },
            {"accounts": [{"currency": "USD", "available_balance": {"value": "0.25"}, "hold": None}], "has_next": False, "cursor": ""},
        ]
        self.products = [spot_product(), spot_product(product_id="BTC-USD", base_currency_id="BTC")]
        self.calls = []
        self.fail_with = {}

    def _maybe_fail(self, name):
        queue = self.fail_with.get(name)
        if queue:
            raise queue.pop(0)

    def get_api_key_permissions(self):
        self.calls.append(("perms",))
        self._maybe_fail("perms")
        return self.permissions

    def get_accounts(self, limit=None, cursor=None):
        self.calls.append(("accounts", limit, cursor))
        return self.account_pages.pop(0)

    def get_products(self, **kwargs):
        self.calls.append(("products", kwargs))
        return {"products": self.products}

    def get_public_products(self, **kwargs):
        self.calls.append(("public_products", kwargs))
        return {"products": self.products}


def gateway(env, fake=None):
    built = []

    def factory(**kwargs):
        private = "api_key" in kwargs or "key_file" in kwargs
        if private and fake is not None:
            client = fake
            client.init_kwargs = kwargs
        else:
            client = FakeClient(**kwargs)
        built.append(client)
        return client

    gw = CoinbaseGateway(load_settings(env), client_factory=factory, sleep=lambda s: None)
    return gw, built


def test_credentials_passed_explicitly_with_timeout():
    fake = FakeClient()
    gw, _ = gateway(CREDS, fake)
    assert gw.authenticated
    assert fake.init_kwargs == {"api_key": CREDS["COINBASE_API_KEY"], "api_secret": "secret", "timeout": 10}


def test_verify_access_happy_path_paginates_accounts():
    fake = FakeClient()
    gw, _ = gateway(CREDS, fake)
    report = gw.verify_access()
    assert report.can_trade and not report.can_transfer
    assert report.usd_available == Decimal("250.75")
    assert [c for c in fake.calls if c[0] == "accounts"] == [("accounts", 250, None), ("accounts", 250, "c1")]
    assert report.key == "...k12345"


def test_transfer_permission_refused():
    fake = FakeClient()
    fake.permissions["can_transfer"] = True
    gw, _ = gateway(CREDS, fake)
    with pytest.raises(CredentialError, match="TRANSFER"):
        gw.verify_access()


def test_missing_view_refused():
    fake = FakeClient()
    fake.permissions["can_view"] = False
    gw, _ = gateway(CREDS, fake)
    with pytest.raises(CredentialError, match="VIEW"):
        gw.verify_access()


def test_missing_trade_warns_in_shadow_and_fails_in_live():
    fake = FakeClient()
    fake.permissions["can_trade"] = False
    gw, _ = gateway(CREDS, fake)
    assert "Shadow mode only" in gw.verify_access().warnings[0]

    fake = FakeClient()
    fake.permissions["can_trade"] = False
    gw, _ = gateway({**CREDS, "COF_TRADING_MODE": "live"}, fake)
    with pytest.raises(CredentialError, match="TRADE"):
        gw.verify_access()


def test_401_is_credential_error():
    fake = FakeClient()
    fake.fail_with["perms"] = [make_http_error(401)]
    gw, _ = gateway(CREDS, fake)
    with pytest.raises(CredentialError):
        gw.verify_access()


def test_bad_secret_signing_failure_is_credential_error():
    fake = FakeClient()
    fake.fail_with["perms"] = [Exception("private key is neither PEM nor valid base64")]
    gw, _ = gateway(CREDS, fake)
    with pytest.raises(CredentialError):
        gw.verify_access()


def test_persistent_network_failure_is_not_blamed_on_credentials():
    fake = FakeClient()
    fake.fail_with["perms"] = [requests.exceptions.ConnectionError("down")] * 10
    gw, _ = gateway({**CREDS, "COF_MAX_RETRIES": "2"}, fake)
    with pytest.raises(ExchangeError) as info:
        gw.verify_access()
    assert not isinstance(info.value, CredentialError)
    assert len([c for c in fake.calls if c[0] == "perms"]) == 3


def test_transient_429_then_success():
    fake = FakeClient()
    fake.fail_with["perms"] = [make_http_error(429), make_http_error(503)]
    gw, _ = gateway(CREDS, fake)
    assert gw.verify_access().can_view


def test_verify_without_credentials_fails_clearly():
    gw, _ = gateway({})
    with pytest.raises(CredentialError, match="No Coinbase credentials"):
        gw.verify_access()


def test_universe_authenticated_is_authoritative():
    fake = FakeClient()
    gw, _ = gateway(CREDS, fake)
    u = gw.fetch_universe()
    assert u.authoritative and u.product_ids == ["ABC-USD"]
    assert ("products", {"product_type": "SPOT", "get_all_products": True}) in fake.calls


def test_universe_public_fallback_not_authoritative():
    gw, built = gateway({"COF_REST_MAX_RPS": "1000"})
    u = gw.fetch_universe()
    assert not u.authoritative
    assert u.source.startswith("public")
    assert built[0].calls[0][0] == "public_products"


def test_universe_missing_products_field_raises():
    fake = FakeClient()
    fake.get_products = lambda **kw: {}
    gw, _ = gateway(CREDS, fake)
    with pytest.raises(ExchangeError):
        gw.fetch_universe()


def test_bad_key_file_is_credential_error(tmp_path):
    bad = tmp_path / "key.json"
    bad.write_text("not json")
    with pytest.raises(CredentialError):
        CoinbaseGateway(load_settings({"COINBASE_API_KEY_FILE": str(bad)}))
