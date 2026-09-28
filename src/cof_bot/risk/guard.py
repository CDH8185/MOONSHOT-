"""Circuit breakers.

* Daily drawdown. Equity (cash plus positions marked at the best bid) is
  compared with the equity at the start of the trading day. At the limit the
  guard trips with ``flatten=True``: every position is sold and no entry is
  allowed until the next trading day.
* Consecutive losses, order failures and trades per day stop new entries for
  the rest of the day. Open positions keep their stops.
* Kill switch. If the kill switch file exists, every position is sold and no
  entry is allowed until the file is removed.

The trading day starts at midnight in ``limits.day_timezone`` (Pacific by
default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from cof_bot.risk.limits import RiskLimits


@dataclass
class DayState:
    day: str
    start_equity: Decimal
    realised_pnl: Decimal = Decimal(0)
    trades: int = 0
    consecutive_losses: int = 0
    order_failures: int = 0
    halted: str | None = None
    flatten: bool = False

    def to_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}


@dataclass
class GuardEvent:
    kind: str
    detail: dict = field(default_factory=dict)


class RiskGuard:
    def __init__(self, limits: RiskLimits, kill_switch_path: str | Path | None = None):
        self.limits = limits
        self.tz = ZoneInfo(limits.day_timezone)
        self.kill_switch_path = Path(kill_switch_path) if kill_switch_path else None
        self.state: DayState | None = None

    def day_key(self, now: float) -> str:
        return datetime.fromtimestamp(now, self.tz).strftime("%Y-%m-%d")

    def roll_day(self, now: float, equity: Decimal) -> GuardEvent | None:
        key = self.day_key(now)
        if self.state is None or self.state.day != key:
            previous = self.state
            self.state = DayState(day=key, start_equity=equity)
            return GuardEvent("new_day", {"day": key, "start_equity": str(equity),
                                          "previous": previous.to_dict() if previous else None})
        return None

    def restore(self, saved: dict | None, now: float) -> None:
        """Keep today's counters and halts across a restart."""
        if not saved or saved.get("day") != self.day_key(now):
            return
        self.state = DayState(
            day=saved["day"],
            start_equity=Decimal(saved["start_equity"]),
            realised_pnl=Decimal(saved.get("realised_pnl", "0")),
            trades=int(saved.get("trades", 0)),
            consecutive_losses=int(saved.get("consecutive_losses", 0)),
            order_failures=int(saved.get("order_failures", 0)),
            halted=saved.get("halted"),
            flatten=bool(saved.get("flatten", False)),
        )

    def kill_switch_on(self) -> bool:
        return bool(self.kill_switch_path and self.kill_switch_path.exists())

    def _trip(self, reason: str, flatten: bool) -> GuardEvent | None:
        s = self.state
        if s.halted and (s.flatten or not flatten):
            return None
        s.halted = reason
        s.flatten = s.flatten or flatten
        return GuardEvent("breaker", {"reason": reason, "flatten": s.flatten, "day": s.day})

    def drawdown(self, equity: Decimal) -> Decimal:
        s = self.state
        if s is None or s.start_equity <= 0:
            return Decimal(0)
        return (s.start_equity - equity) / s.start_equity

    def headroom_usd(self, equity: Decimal) -> Decimal:
        s = self.state
        if s is None:
            return Decimal(0)
        floor = s.start_equity * (1 - self.limits.max_daily_drawdown)
        return equity - floor

    def check(self, equity: Decimal) -> GuardEvent | None:
        if self.kill_switch_on():
            return self._trip("kill_switch", flatten=True)
        if self.drawdown(equity) >= self.limits.max_daily_drawdown:
            return self._trip("daily_drawdown", flatten=True)
        return None

    def can_enter(self, open_positions: int) -> str | None:
        """None when an entry is allowed, else the reason it is not."""
        s = self.state
        if s is None:
            return "trading day not started"
        if self.kill_switch_on():
            return "kill switch file present"
        if s.halted:
            return f"halted: {s.halted}"
        if s.trades >= self.limits.max_trades_per_day:
            return "max trades per day reached"
        if open_positions >= self.limits.max_open_positions:
            return "max open positions reached"
        return None

    def record_entry(self) -> None:
        self.state.trades += 1

    def record_exit(self, pnl: Decimal) -> GuardEvent | None:
        s = self.state
        s.realised_pnl += pnl
        s.consecutive_losses = s.consecutive_losses + 1 if pnl < 0 else 0
        if s.consecutive_losses >= self.limits.max_consecutive_losses:
            return self._trip("consecutive_losses", flatten=False)
        return None

    def record_order_failure(self) -> GuardEvent | None:
        s = self.state
        s.order_failures += 1
        if s.order_failures >= self.limits.max_order_failures:
            return self._trip("order_failures", flatten=False)
        return None
