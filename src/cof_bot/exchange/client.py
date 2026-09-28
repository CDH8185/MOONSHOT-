"""Authenticated gateway over the official Coinbase Advanced API Python SDK.

Written against coinbase-advanced-py 1.8.4 (read from the installed source):

* ``RESTClient(api_key=..., api_secret=..., key_file=..., timeout=...)``.
  Credentials are passed explicitly. The SDK's own constructor defaults read
  COINBASE_API_KEY and COINBASE_API_SECRET at import time, which would miss
  values loaded from .env afterwards.
* ``get_api_key_permissions()`` returns can_view, can_trade, can_transfer.
* ``get_accounts(limit=, cursor=)`` pages with has_next and cursor.
* ``get_products(product_type="SPOT", get_all_products=True)`` for the
  authenticated, account scoped product list; ``get_public_products`` for the
  unauthenticated catalogue.

Every call goes through ``call_with_retry`` so rate limits, 5xx, timeouts and
dropped connections are retried with backoff and surface as typed errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

import requests
from coinbase.rest import RESTClient

from cof_bot.config import Settings
from cof_bot.errors import CredentialError, ExchangeError
from cof_bot.exchange.retry import RetryPolicy, Throttle, call_with_retry
from cof_bot.exchange.universe import Universe, build_universe

log = logging.getLogger(__name__)

ACCOUNTS_PAGE_LIMIT = 250
MAX_ACCOUNT_PAGES = 100


@dataclass(frozen=True)
class AccountBalance:
    currency: str
    available: Decimal
    hold: Decimal


@dataclass
class AccessReport:
    key: str
    can_view: bool
    can_trade: bool
    can_transfer: bool
    portfolio_type: str | None
    balances: list[AccountBalance] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def usd_available(self) -> Decimal:
        return sum((b.available for b in self.balances if b.currency == "USD"), Decimal("0"))


def _amount(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("value")
    else:
        value = getattr(value, "value", value)
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except Exception:  # noqa: BLE001, malformed amount counts as zero, never as funds
        return Decimal("0")


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class CoinbaseGateway:
    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[..., Any] = RESTClient,
        sleep: Callable[[float], None] | None = None,
    ):
        self.settings = settings
        self.policy = RetryPolicy(
            max_retries=settings.max_retries,
            backoff_base_s=settings.backoff_base_s,
            backoff_cap_s=settings.backoff_cap_s,
        )
        self.throttle = Throttle(settings.rest_max_rps)
        self._sleep = sleep
        self._client_factory = client_factory
        self._private = self._build_private_client() if settings.has_credentials else None
        self._public = client_factory(timeout=settings.request_timeout_s)

    def _build_private_client(self):
        s = self.settings
        try:
            if s.key_file:
                return self._client_factory(key_file=s.key_file, timeout=s.request_timeout_s)
            return self._client_factory(
                api_key=s.api_key, api_secret=s.api_secret, timeout=s.request_timeout_s
            )
        except Exception as exc:  # noqa: BLE001, SDK raises bare Exception on bad key files
            raise CredentialError(f"Could not load Coinbase credentials: {exc}") from exc

    @property
    def authenticated(self) -> bool:
        return self._private is not None

    def _call(self, fn, *args, **kwargs):
        extra = {"sleep": self._sleep} if self._sleep is not None else {}
        return call_with_retry(
            fn, *args, policy=self.policy, throttle=self.throttle, **extra, **kwargs
        )

    def _require_private(self):
        if self._private is None:
            raise CredentialError(
                "No Coinbase credentials configured. Set COINBASE_API_KEY and "
                "COINBASE_API_SECRET, or COINBASE_API_KEY_FILE."
            )
        return self._private

    def verify_access(self) -> AccessReport:
        """Confirm the key authenticates, holds safe permissions, and can read accounts."""
        client = self._require_private()
        try:
            perms = self._call(client.get_api_key_permissions, op_name="get_api_key_permissions")
        except ExchangeError as exc:
            # 401/403, or a local failure to sign the JWT (malformed secret),
            # is a credential problem. A network failure is not.
            network = isinstance(exc.__cause__, requests.exceptions.RequestException)
            if exc.status_code in (401, 403) or (exc.status_code is None and not network):
                raise CredentialError(f"Coinbase rejected the API key: {exc}") from exc
            raise

        report = AccessReport(
            key=self.settings.masked_key(),
            can_view=bool(_field(perms, "can_view")),
            can_trade=bool(_field(perms, "can_trade")),
            can_transfer=bool(_field(perms, "can_transfer")),
            portfolio_type=_field(perms, "portfolio_type"),
        )

        if report.can_transfer:
            raise CredentialError(
                "This API key holds the TRANSFER permission. Coinbase warns that a leaked "
                "key with transfer can move every asset out of the account. Create a new "
                "key with VIEW and TRADE only, and revoke this one."
            )
        if not report.can_view:
            raise CredentialError("This API key lacks the VIEW permission; the bot cannot read accounts.")
        if not report.can_trade:
            if self.settings.trading_mode == "live":
                raise CredentialError("COF_TRADING_MODE=live but the API key lacks the TRADE permission.")
            report.warnings.append("Key lacks TRADE permission. Shadow mode only.")

        report.balances = self._list_balances(client)
        return report

    def _list_balances(self, client) -> list[AccountBalance]:
        balances: list[AccountBalance] = []
        cursor = None
        for _ in range(MAX_ACCOUNT_PAGES):
            page = self._call(
                client.get_accounts, limit=ACCOUNTS_PAGE_LIMIT, cursor=cursor, op_name="get_accounts"
            )
            for account in _field(page, "accounts") or []:
                balances.append(
                    AccountBalance(
                        currency=str(_field(account, "currency") or "").upper(),
                        available=_amount(_field(account, "available_balance")),
                        hold=_amount(_field(account, "hold")),
                    )
                )
            cursor = _field(page, "cursor")
            if not _field(page, "has_next") or not cursor:
                return balances
        raise ExchangeError(f"get_accounts did not finish within {MAX_ACCOUNT_PAGES} pages")

    def fetch_universe(self) -> Universe:
        """Build the USD spot universe. Authoritative only when authenticated."""
        if self._private is not None:
            response = self._call(
                self._private.get_products,
                product_type="SPOT",
                get_all_products=True,
                op_name="get_products",
            )
            source, authoritative = "authenticated get_products", True
        else:
            response = self._call(
                self._public.get_public_products,
                product_type="SPOT",
                get_all_products=True,
                op_name="get_public_products",
            )
            source, authoritative = "public get_public_products", False

        products = _field(response, "products")
        if products is None:
            raise ExchangeError(f"{source} returned no 'products' field")
        universe = build_universe(products, self.settings, authoritative=authoritative, source=source)
        log.info(
            "Universe from %s: %d of %d products qualify (authoritative=%s)",
            source,
            len(universe.pairs),
            universe.total_products,
            authoritative,
        )
        return universe
