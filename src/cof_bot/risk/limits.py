"""Immutable risk limits.

The limits are read once, at start, from the environment, checked against hard
bounds, and frozen. Nothing in the bot can change them while it runs: the
dataclass is frozen, the engine holds the only instance, and no code path
writes to it. A SHA-256 fingerprint of the values is printed at start and in
every status record, so a changed limit shows up as a changed fingerprint.

Every limit overrides every entry signal. A signal can only ever be refused
or made smaller by these limits, never enlarged.

Hard bounds exist so that a typo (0.5 for 0.005) cannot put half the account
at risk on one trade. Loosening a bound means editing this file.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cof_bot.errors import ConfigError


@dataclass(frozen=True)
class RiskLimits:
    # Fractional sizing: the loss taken if the hard stop is hit, fees and
    # slippage included, as a fraction of account equity.
    risk_per_trade: Decimal = Decimal("0.005")
    # Hard stop below the entry price.
    hard_stop: Decimal = Decimal("0.06")
    # Infinity trailing: once the best bid has risen this far above entry, a
    # trailing stop follows the highest bid seen, at this distance, with no
    # upper limit on how far it can climb. It only ever moves up.
    trail_activation: Decimal = Decimal("0.04")
    trail_distance: Decimal = Decimal("0.05")
    # One position may never exceed this fraction of equity.
    max_position: Decimal = Decimal("0.10")
    max_open_positions: int = 3
    # Loss from the day's starting equity (realised and unrealised) that
    # closes every position and stops new entries until the next day.
    max_daily_drawdown: Decimal = Decimal("0.03")
    max_trades_per_day: int = 10
    max_consecutive_losses: int = 3
    max_order_failures: int = 3
    # Entry is a limit IOC order priced this far above the best ask, so a thin
    # book can never fill the order at an unbounded price.
    max_entry_slippage_bps: Decimal = Decimal("75")
    max_spread_bps: Decimal = Decimal("150")
    # The order may take at most this share of the ask depth within 1% of mid.
    max_book_share: Decimal = Decimal("0.25")
    # Prices older than this are not used to open a position.
    max_price_age_s: float = 10.0
    # Trading day boundary for the daily limits.
    day_timezone: str = "America/Los_Angeles"

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), default=str, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


# (minimum, maximum) inclusive. A value outside refuses to start.
BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "risk_per_trade": (Decimal("0.0001"), Decimal("0.02")),
    "hard_stop": (Decimal("0.01"), Decimal("0.25")),
    "trail_activation": (Decimal("0.005"), Decimal("1")),
    "trail_distance": (Decimal("0.005"), Decimal("0.5")),
    "max_position": (Decimal("0.005"), Decimal("0.25")),
    "max_daily_drawdown": (Decimal("0.005"), Decimal("0.10")),
    "max_entry_slippage_bps": (Decimal("1"), Decimal("300")),
    "max_spread_bps": (Decimal("1"), Decimal("500")),
    "max_book_share": (Decimal("0.01"), Decimal("1")),
}
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "max_open_positions": (1, 10),
    "max_trades_per_day": (1, 100),
    "max_consecutive_losses": (1, 20),
    "max_order_failures": (1, 20),
}
ENV_NAMES = {
    "risk_per_trade": "COF_RISK_PER_TRADE",
    "hard_stop": "COF_HARD_STOP",
    "trail_activation": "COF_TRAIL_ACTIVATION",
    "trail_distance": "COF_TRAIL_DISTANCE",
    "max_position": "COF_MAX_POSITION",
    "max_open_positions": "COF_MAX_OPEN_POSITIONS",
    "max_daily_drawdown": "COF_MAX_DAILY_DRAWDOWN",
    "max_trades_per_day": "COF_MAX_TRADES_PER_DAY",
    "max_consecutive_losses": "COF_MAX_CONSECUTIVE_LOSSES",
    "max_order_failures": "COF_MAX_ORDER_FAILURES",
    "max_entry_slippage_bps": "COF_MAX_ENTRY_SLIPPAGE_BPS",
    "max_spread_bps": "COF_MAX_SPREAD_BPS",
    "max_book_share": "COF_MAX_BOOK_SHARE",
    "max_price_age_s": "COF_MAX_PRICE_AGE_S",
    "day_timezone": "COF_DAY_TIMEZONE",
}


def load_risk_limits(env: Mapping[str, str] | None = None) -> RiskLimits:
    if env is None:
        env = os.environ
    d = RiskLimits()
    values: dict = {}
    for name, (low, high) in BOUNDS.items():
        raw = (env.get(ENV_NAMES[name]) or "").strip()
        if not raw:
            values[name] = getattr(d, name)
            continue
        try:
            v = Decimal(raw)
        except InvalidOperation as exc:
            raise ConfigError(f"{ENV_NAMES[name]} must be a number, got {raw!r}") from exc
        if not v.is_finite() or not (low <= v <= high):
            raise ConfigError(f"{ENV_NAMES[name]} must be between {low} and {high}, got {raw}")
        values[name] = v
    for name, (low, high) in INT_BOUNDS.items():
        raw = (env.get(ENV_NAMES[name]) or "").strip()
        if not raw:
            values[name] = getattr(d, name)
            continue
        try:
            v = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{ENV_NAMES[name]} must be a whole number, got {raw!r}") from exc
        if not (low <= v <= high):
            raise ConfigError(f"{ENV_NAMES[name]} must be between {low} and {high}, got {raw}")
        values[name] = v

    raw_age = (env.get(ENV_NAMES["max_price_age_s"]) or "").strip()
    try:
        age = float(raw_age) if raw_age else d.max_price_age_s
    except ValueError as exc:
        raise ConfigError(f"COF_MAX_PRICE_AGE_S must be a number, got {raw_age!r}") from exc
    if not (1 <= age <= 60):
        raise ConfigError("COF_MAX_PRICE_AGE_S must be between 1 and 60.")
    values["max_price_age_s"] = age

    tz = (env.get(ENV_NAMES["day_timezone"]) or "").strip() or d.day_timezone
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"COF_DAY_TIMEZONE is not a known time zone: {tz!r}") from exc
    values["day_timezone"] = tz

    limits = RiskLimits(**values)
    if limits.trail_distance >= limits.trail_activation + limits.hard_stop:
        # The first trailing stop would sit below the hard stop and mean nothing.
        raise ConfigError("COF_TRAIL_DISTANCE must be smaller than COF_TRAIL_ACTIVATION + COF_HARD_STOP.")
    if limits.risk_per_trade > limits.max_daily_drawdown:
        raise ConfigError("COF_RISK_PER_TRADE may not exceed COF_MAX_DAILY_DRAWDOWN.")
    return limits
