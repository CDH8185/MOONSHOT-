import dataclasses
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from cof_bot.errors import ConfigError
from cof_bot.risk.guard import RiskGuard
from cof_bot.risk.limits import RiskLimits, load_risk_limits
from cof_bot.risk.sizing import round_down, size_entry
from cof_bot.trading.position import Position

L = RiskLimits()


def ts(y, m, d, h=12, tz="America/Los_Angeles"):
    return datetime(y, m, d, h, tzinfo=ZoneInfo(tz)).timestamp()


# Limits -------------------------------------------------------------------------


def test_defaults_and_fingerprint():
    limits = load_risk_limits({})
    assert limits == L
    assert limits.fingerprint() == RiskLimits().fingerprint()
    assert load_risk_limits({"COF_HARD_STOP": "0.07"}).fingerprint() != L.fingerprint()


def test_limits_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        L.hard_stop = D("0.5")


@pytest.mark.parametrize(
    "env",
    [
        {"COF_RISK_PER_TRADE": "0.5"},  # the typo the bounds exist for
        {"COF_HARD_STOP": "0"},
        {"COF_MAX_DAILY_DRAWDOWN": "0.2"},
        {"COF_MAX_POSITION": "x"},
        {"COF_MAX_OPEN_POSITIONS": "0"},
        {"COF_MAX_OPEN_POSITIONS": "2.5"},
        {"COF_MAX_PRICE_AGE_S": "120"},
        {"COF_DAY_TIMEZONE": "Mars/Olympus"},
        {"COF_TRAIL_DISTANCE": "0.2", "COF_TRAIL_ACTIVATION": "0.05", "COF_HARD_STOP": "0.05"},
        {"COF_RISK_PER_TRADE": "0.02", "COF_MAX_DAILY_DRAWDOWN": "0.01"},
    ],
)
def test_limits_refuse_bad_values(env):
    with pytest.raises(ConfigError):
        load_risk_limits(env)


# Sizing -------------------------------------------------------------------------

BASE = dict(
    limits=L,
    equity_usd=D("10000"),
    cash_usd=D("10000"),
    best_ask=D("1.00"),
    ask_depth_usd=D("1000000"),
    fee_rate=D("0.006"),
    drawdown_headroom_usd=D("300"),
    base_increment=D("0.1"),
    quote_increment=D("0.0001"),
    base_min_size=D("1"),
    quote_min_size=D("1"),
)


def test_size_bound_by_risk_budget():
    d = size_entry(**BASE)
    # loss per dollar = 0.06 stop + 0.0075 slippage + 2 x 0.006 fees = 0.0795
    # risk budget = 10000 x 0.005 / 0.0795 = 628.93 USD at limit price 1.0075
    assert d.ok and d.binding == "risk_budget"
    assert d.limit_price == D("1.0075")
    assert d.base_size == D("624.2")
    assert d.max_loss_usd <= D("50")


def test_size_bound_by_book_depth_and_cash():
    assert size_entry(**{**BASE, "ask_depth_usd": D("400")}).binding == "book_depth"
    d = size_entry(**{**BASE, "cash_usd": D("100")})
    assert d.binding == "cash" and d.notional_usd * (1 + D("0.006")) <= D("100")


def test_size_bound_by_daily_headroom():
    d = size_entry(**{**BASE, "drawdown_headroom_usd": D("10")})
    assert d.binding == "daily_headroom" and d.max_loss_usd <= D("10")
    assert not size_entry(**{**BASE, "drawdown_headroom_usd": D("-5")}).ok


def test_size_refused_below_product_minimum():
    d = size_entry(**{**BASE, "base_min_size": D("1000")})
    assert not d.ok and "below product minimum" in d.reason


def test_round_down():
    assert round_down(D("1.239"), D("0.01")) == D("1.23")
    assert round_down(D("5"), D("0")) == D("5")


# Infinity trailing ---------------------------------------------------------------


def pos(entry="1.00"):
    return Position.open(product_id="ABC-USD", base_currency="ABC", base_size=D("100"), entry_price=D(entry),
                         entry_fees_usd=D("0.6"), opened_at=0.0, limits=L)


def test_hard_stop():
    p = pos()
    assert p.stop_price == D("0.94")
    assert p.update(D("0.95"), L) is None
    assert p.update(D("0.94"), L) == "hard_stop"


def test_trailing_activates_ratchets_and_never_caps():
    p = pos()
    assert p.update(D("1.03"), L) is None and not p.trailing
    assert p.update(D("1.04"), L) is None and p.trailing
    assert p.stop_price == D("1.04") * D("0.95")
    # Keeps climbing: no take-profit cap, stop follows every new high.
    for price in ("1.5", "2.0", "5.0"):
        assert p.update(D(price), L) is None
    assert p.stop_price == D("4.75")
    # A pullback does not lower the stop.
    assert p.update(D("4.80"), L) is None and p.stop_price == D("4.75")
    assert p.update(D("4.75"), L) == "trailing_stop"


def test_first_trailing_stop_never_below_hard_stop():
    p = pos()
    p.update(D("1.04"), L)
    assert p.stop_price >= D("0.94")


def test_position_round_trip():
    p = pos()
    p.update(D("1.2"), L)
    assert Position.from_dict(p.to_dict()) == p


# Guard ---------------------------------------------------------------------------


def test_new_day_uses_pacific_time(tmp_path):
    g = RiskGuard(L, tmp_path / "KILL")
    # 23:30 Pacific on 2026-09-27 is already 2026-09-28 in UTC.
    assert g.roll_day(ts(2026, 9, 27, 23), D("1000")).detail["day"] == "2026-09-27"
    assert g.roll_day(ts(2026, 9, 27, 23) + 1800, D("990")) is None
    e = g.roll_day(ts(2026, 9, 28, 1), D("980"))
    assert e.kind == "new_day" and g.state.start_equity == D("980")


def test_daily_drawdown_trips_and_flattens(tmp_path):
    g = RiskGuard(L, tmp_path / "KILL")
    g.roll_day(ts(2026, 9, 27), D("1000"))
    assert g.check(D("971")) is None
    e = g.check(D("970"))
    assert e.detail == {"reason": "daily_drawdown", "flatten": True, "day": "2026-09-27"}
    assert g.can_enter(0) == "halted: daily_drawdown"
    assert g.check(D("960")) is None  # reported once
    g.roll_day(ts(2026, 9, 28), D("960"))
    assert g.can_enter(0) is None


def test_consecutive_losses_and_order_failures_halt_without_flatten(tmp_path):
    g = RiskGuard(L, tmp_path / "KILL")
    g.roll_day(ts(2026, 9, 27), D("1000"))
    g.record_exit(D("-1"))
    g.record_exit(D("2"))  # a win resets the count
    g.record_exit(D("-1"))
    g.record_exit(D("-1"))
    e = g.record_exit(D("-1"))
    assert e.detail["reason"] == "consecutive_losses" and not g.state.flatten
    g2 = RiskGuard(L, tmp_path / "KILL")
    g2.roll_day(ts(2026, 9, 27), D("1000"))
    for _ in range(2):
        assert g2.record_order_failure() is None
    assert g2.record_order_failure().detail["reason"] == "order_failures"


def test_trades_per_day_and_open_positions(tmp_path):
    g = RiskGuard(L, tmp_path / "KILL")
    g.roll_day(ts(2026, 9, 27), D("1000"))
    assert g.can_enter(3) == "max open positions reached"
    for _ in range(10):
        g.record_entry()
    assert g.can_enter(0) == "max trades per day reached"


def test_kill_switch(tmp_path):
    kill = tmp_path / "KILL"
    g = RiskGuard(L, kill)
    g.roll_day(ts(2026, 9, 27), D("1000"))
    kill.write_text("stop")
    assert g.can_enter(0) == "kill switch file present"
    assert g.check(D("1000")).detail["flatten"] is True


def test_restore_same_day_only(tmp_path):
    g = RiskGuard(L, tmp_path / "KILL")
    g.roll_day(ts(2026, 9, 27), D("1000"))
    g.record_entry()
    g.check(D("900"))
    saved = g.state.to_dict()
    g2 = RiskGuard(L, tmp_path / "KILL")
    g2.restore(saved, ts(2026, 9, 27, 20))
    assert g2.state.halted == "daily_drawdown" and g2.state.trades == 1
    g3 = RiskGuard(L, tmp_path / "KILL")
    g3.restore(saved, ts(2026, 9, 28))
    assert g3.state is None
