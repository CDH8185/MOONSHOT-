"""Metrics derived from journal records.

``RunMetrics`` accumulates counters while the bot runs and is included in
every ``status`` record. ``summarise_trades`` rebuilds performance figures
from the trade ledger at any later time, so the numbers a report shows are
always recomputed from the record on disk, never carried in memory only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class RunMetrics:
    started_at: float
    headlines: int = 0
    signals: int = 0
    entries: int = 0
    exits: int = 0
    skips: int = 0
    errors: int = 0
    breakers: int = 0
    skip_reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    eval_ms_max: float = 0.0
    eval_ms_last: float = 0.0
    eval_count: int = 0
    eval_ms_total: float = 0.0

    def count(self, record: dict) -> None:
        t = record.get("type")
        if t == "headline":
            self.headlines += 1
        elif t == "signal":
            self.signals += 1
        elif t == "entry":
            self.entries += 1
        elif t == "exit":
            self.exits += 1
        elif t == "skip":
            self.skips += 1
            reason = str(record.get("reason", "?")).split(":")[0]
            self.skip_reasons[reason] += 1
        elif t == "error":
            self.errors += 1
        elif t == "breaker":
            self.breakers += 1

    def timed_eval(self, elapsed_s: float) -> None:
        ms = elapsed_s * 1000
        self.eval_count += 1
        self.eval_ms_total += ms
        self.eval_ms_last = round(ms, 2)
        self.eval_ms_max = round(max(self.eval_ms_max, ms), 2)

    def to_dict(self, now: float) -> dict:
        return {
            "uptime_s": round(now - self.started_at, 1),
            "headlines": self.headlines,
            "signals": self.signals,
            "entries": self.entries,
            "exits": self.exits,
            "skips": self.skips,
            "skip_reasons": dict(self.skip_reasons),
            "errors": self.errors,
            "breakers": self.breakers,
            "eval_ms_last": self.eval_ms_last,
            "eval_ms_max": self.eval_ms_max,
            "eval_ms_mean": round(self.eval_ms_total / self.eval_count, 2) if self.eval_count else 0.0,
        }


def _d(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001, a malformed figure counts as zero in a summary
        return Decimal(0)


def summarise_trades(records: list[dict], *, day: str | None = None, mode: str | None = None) -> dict:
    """Performance figures from ``entry`` and ``exit`` records.

    ``day`` limits the summary to exits whose ``at_iso`` starts with that
    date; ``mode`` to shadow or live. Figures are strings of Decimals, so
    they round trip through JSON without floating point drift.
    """
    exits = [r for r in records if r.get("type") == "exit"]
    entries = [r for r in records if r.get("type") == "entry"]
    if mode:
        exits = [r for r in exits if r.get("mode") == mode]
        entries = [r for r in entries if r.get("mode") == mode]
    if day:
        exits = [r for r in exits if str(r.get("at_iso", "")).startswith(day)]
        entries = [r for r in entries if str(r.get("at_iso", "")).startswith(day)]

    pnls = [_d(r.get("pnl_usd")) for r in exits]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls, Decimal(0))
    gross_win = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))

    # Max drawdown of the cumulative realised P&L, in USD.
    peak = running = Decimal(0)
    max_dd = Decimal(0)
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    by_product: dict[str, dict] = {}
    for r in exits:
        pid = str(r.get("product_id"))
        row = by_product.setdefault(pid, {"exits": 0, "pnl_usd": Decimal(0), "wins": 0})
        p = _d(r.get("pnl_usd"))
        row["exits"] += 1
        row["pnl_usd"] += p
        row["wins"] += int(p > 0)
    by_reason: dict[str, int] = defaultdict(int)
    for r in exits:
        by_reason[str(r.get("reason", "?")).split(":")[0]] += 1
    held = [float(r.get("held_s", 0) or 0) for r in exits]
    fees = sum((_d((r.get("fill") or {}).get("fees_usd")) for r in exits + entries), Decimal(0))

    return {
        "day": day,
        "mode": mode,
        "entries": len(entries),
        "exits": len(exits),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": str(round(Decimal(len(wins)) / len(pnls), 4)) if pnls else None,
        "realised_pnl_usd": str(round(total, 2)),
        "gross_win_usd": str(round(gross_win, 2)),
        "gross_loss_usd": str(round(gross_loss, 2)),
        "profit_factor": str(round(gross_win / gross_loss, 3)) if gross_loss else None,
        "avg_win_usd": str(round(gross_win / len(wins), 2)) if wins else None,
        "avg_loss_usd": str(round(gross_loss / len(losses), 2)) if losses else None,
        "largest_loss_usd": str(round(min(losses), 2)) if losses else None,
        "max_drawdown_usd": str(round(max_dd, 2)),
        "fees_usd": str(round(fees, 2)),
        "avg_held_s": round(sum(held) / len(held), 1) if held else None,
        "exit_reasons": dict(by_reason),
        "by_product": {
            pid: {"exits": v["exits"], "wins": v["wins"], "pnl_usd": str(round(v["pnl_usd"], 2))}
            for pid, v in sorted(by_product.items())
        },
    }
