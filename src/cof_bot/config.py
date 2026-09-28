"""Environment driven configuration.

Secrets are read from the process environment (optionally populated from a
git ignored .env file). Nothing secret is ever hardcoded, and the secret value
is excluded from repr() so it cannot leak into logs or tracebacks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from cof_bot.errors import ConfigError
from cof_bot.signals.correlator import CorrelatorSettings

# Stablecoins have no momentum to capture; they are never part of the universe,
# whatever COF_EXCLUDED_BASES says.
STABLECOIN_BASES = frozenset(
    {"USDT", "USDC", "DAI", "PYUSD", "EURC", "GUSD", "USDS", "FDUSD", "TUSD", "PAX", "USDP"}
)

TRADING_MODES = ("shadow", "live")


@dataclass(frozen=True)
class Settings:
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    key_file: str | None = None
    trading_mode: str = "shadow"
    request_timeout_s: int = 10
    max_retries: int = 5
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0
    rest_max_rps: float = 2.0
    excluded_bases: frozenset[str] = frozenset({"BTC", "ETH"})
    min_quote_volume_24h: Decimal = Decimal("100000")
    max_quote_volume_24h: Decimal | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.key_file) or bool(self.api_key and self.api_secret)

    @property
    def all_excluded_bases(self) -> frozenset[str]:
        return self.excluded_bases | STABLECOIN_BASES

    def masked_key(self) -> str:
        """Identify the key in logs without revealing it."""
        if self.key_file:
            return f"key_file:{Path(self.key_file).name}"
        if not self.api_key:
            return "<none>"
        return f"...{self.api_key[-6:]}"


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int(env, name, default, minimum):
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(env, name, default, minimum_exclusive):
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= minimum_exclusive:
        raise ConfigError(f"{name} must be > {minimum_exclusive}, got {value}")
    return value


def _decimal(env, name, default):
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{name} must be a finite number >= 0, got {raw!r}")
    return value


def load_settings(env: Mapping[str, str] | None = None, dotenv_path: str | None = None) -> Settings:
    """Build Settings from the environment.

    When ``env`` is None the real process environment is used, after loading a
    .env file if one exists. Real environment variables win over .env values.
    """
    if env is None:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=dotenv_path, override=False)
        env = os.environ

    api_key = _get(env, "COINBASE_API_KEY")
    api_secret = _get(env, "COINBASE_API_SECRET")
    key_file = _get(env, "COINBASE_API_KEY_FILE")

    if key_file and (api_key or api_secret):
        raise ConfigError(
            "Set either COINBASE_API_KEY_FILE or COINBASE_API_KEY and COINBASE_API_SECRET, not both."
        )
    if bool(api_key) != bool(api_secret):
        raise ConfigError("COINBASE_API_KEY and COINBASE_API_SECRET must be set together.")
    if key_file and not Path(key_file).is_file():
        raise ConfigError(f"COINBASE_API_KEY_FILE does not point to a file: {key_file}")

    mode = (_get(env, "COF_TRADING_MODE") or "shadow").lower()
    if mode not in TRADING_MODES:
        raise ConfigError(f"COF_TRADING_MODE must be one of {TRADING_MODES}, got {mode!r}")

    raw_excluded = _get(env, "COF_EXCLUDED_BASES")
    excluded = (
        frozenset(b.strip().upper() for b in raw_excluded.split(",") if b.strip())
        if raw_excluded is not None
        else Settings.excluded_bases
    )

    min_vol = _decimal(env, "COF_MIN_QUOTE_VOLUME_24H", Settings.min_quote_volume_24h)
    max_vol = _decimal(env, "COF_MAX_QUOTE_VOLUME_24H", None)
    if max_vol is not None and max_vol < min_vol:
        raise ConfigError("COF_MAX_QUOTE_VOLUME_24H must be >= COF_MIN_QUOTE_VOLUME_24H.")

    backoff_base = _float(env, "COF_BACKOFF_BASE_S", Settings.backoff_base_s, 0)
    backoff_cap = _float(env, "COF_BACKOFF_CAP_S", Settings.backoff_cap_s, 0)
    if backoff_cap < backoff_base:
        raise ConfigError("COF_BACKOFF_CAP_S must be >= COF_BACKOFF_BASE_S.")

    return Settings(
        api_key=api_key,
        api_secret=api_secret,
        key_file=key_file,
        trading_mode=mode,
        request_timeout_s=_int(env, "COF_REQUEST_TIMEOUT_S", Settings.request_timeout_s, 1),
        max_retries=_int(env, "COF_MAX_RETRIES", Settings.max_retries, 0),
        backoff_base_s=backoff_base,
        backoff_cap_s=backoff_cap,
        rest_max_rps=_float(env, "COF_REST_MAX_RPS", Settings.rest_max_rps, 0),
        excluded_bases=excluded,
        min_quote_volume_24h=min_vol,
        max_quote_volume_24h=max_vol,
    )


@dataclass(frozen=True)
class StreamSettings:
    """Phase 2 market data, news and signal settings."""

    ws_stale_after_s: float = 15.0
    ws_backoff_cap_s: float = 60.0
    l2_max_products: int = 25
    news_poll_s: float = 120.0
    news_max_age_s: float = 6 * 3600
    eval_interval_s: float = 5.0
    volume_window_s: int = 300
    volume_baseline_s: int = 3600
    correlator: CorrelatorSettings = field(default_factory=CorrelatorSettings)


def load_stream_settings(env: Mapping[str, str] | None = None) -> StreamSettings:
    if env is None:
        env = os.environ
    d = CorrelatorSettings()
    l2 = _int(env, "COF_L2_MAX_PRODUCTS", 25, 0)
    if l2 > 30:
        raise ConfigError("COF_L2_MAX_PRODUCTS must be <= 30 (Coinbase per-connection level2 limit, measured 2026-09-27)")
    window = _int(env, "COF_VOLUME_WINDOW_S", 300, 60)
    baseline = _int(env, "COF_VOLUME_BASELINE_S", 3600, 60)
    if window % 60 or baseline % 60 or baseline < window:
        raise ConfigError("COF_VOLUME_WINDOW_S and COF_VOLUME_BASELINE_S must be whole minutes, baseline >= window.")
    buy_share = _float(env, "COF_MIN_BUY_SHARE", d.min_buy_share, 0)
    if buy_share > 1:
        raise ConfigError("COF_MIN_BUY_SHARE must be <= 1.")
    mean = _float(env, "COF_MIN_MEAN_SENTIMENT", d.min_mean_compound, 0)
    veto_raw = _get(env, "COF_NEGATIVE_VETO")
    try:
        veto = float(veto_raw) if veto_raw is not None else d.negative_veto
    except ValueError as exc:
        raise ConfigError(f"COF_NEGATIVE_VETO must be a number, got {veto_raw!r}") from exc
    if not (-1 <= veto < 0) or mean > 1:
        raise ConfigError("COF_NEGATIVE_VETO must be in [-1, 0) and COF_MIN_MEAN_SENTIMENT in (0, 1].")
    return StreamSettings(
        ws_stale_after_s=_float(env, "COF_WS_STALE_AFTER_S", 15.0, 2),
        ws_backoff_cap_s=_float(env, "COF_WS_BACKOFF_CAP_S", 60.0, 0),
        l2_max_products=l2,
        news_poll_s=_float(env, "COF_NEWS_POLL_S", 120.0, 29),
        news_max_age_s=_float(env, "COF_NEWS_MAX_AGE_S", 6 * 3600, 0),
        eval_interval_s=_float(env, "COF_EVAL_INTERVAL_S", 5.0, 0),
        volume_window_s=window,
        volume_baseline_s=baseline,
        correlator=CorrelatorSettings(
            sentiment_window_s=_int(env, "COF_SENTIMENT_WINDOW_S", d.sentiment_window_s, 60),
            mention_baseline_s=_int(env, "COF_MENTION_BASELINE_S", d.mention_baseline_s, 0),
            min_mentions=_int(env, "COF_MIN_MENTIONS", d.min_mentions, 1),
            spike_multiple=_float(env, "COF_SPIKE_MULTIPLE", d.spike_multiple, 0),
            min_mean_compound=mean,
            negative_veto=veto,
            max_lag_s=_int(env, "COF_MAX_LAG_S", d.max_lag_s, 60),
            volume_ratio_min=_float(env, "COF_VOLUME_RATIO_MIN", d.volume_ratio_min, 1),
            min_window_usd=_float(env, "COF_MIN_WINDOW_USD", d.min_window_usd, 0),
            min_buy_share=buy_share,
            cooldown_s=_int(env, "COF_SIGNAL_COOLDOWN_S", d.cooldown_s, 0),
            hot_volume_ratio=_float(env, "COF_HOT_VOLUME_RATIO", d.hot_volume_ratio, 1),
        ),
    )
