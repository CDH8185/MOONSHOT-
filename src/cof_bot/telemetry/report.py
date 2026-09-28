"""Reports built from the journal, for people and for tools.

``build_report`` returns one JSON-ready dict with:

* ``health``: the last status the bot wrote, its age, and a verdict;
* ``trades``: performance figures recomputed from ``trades.jsonl``;
* ``open_positions``: from the state file, with the stop of each;
* ``days``: which event files exist and how many records each holds;
* ``limits``: the risk limits in force at the last start and their fingerprint.

The layout is fixed by ``schema/report.schema.json`` (version 1.0), so a
Model Context Protocol server or any other program can read it without
parsing prose. ``format_text`` renders the same dict for a terminal.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from cof_bot.telemetry.journal import SCHEMA_VERSION, read_jsonl
from cof_bot.telemetry.metrics import summarise_trades

EVENTS_RE = re.compile(r"^events_(\d{4}-\d{2}-\d{2})\.jsonl$")
STALE_AFTER_S = 180.0  # status records are written every 60 s while running


def _load_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def health_verdict(health: dict | None, now: float) -> tuple[str, str]:
    """(verdict, reason). Verdicts: ok, stale, degraded, halted, unknown."""
    if not health:
        return "unknown", "no health.json; the bot has not written a status yet"
    age = now - float(health.get("written_at", 0) or 0)
    status = health.get("status") or {}
    if age > STALE_AFTER_S:
        return "stale", f"last status is {age:.0f} s old; the bot is not running or is stuck"
    trading = status.get("trading") or {}
    day = trading.get("day") or {}
    if day.get("halted"):
        return "halted", f"circuit breaker: {day['halted']}"
    ws_failures = int(status.get("ws_failures", 0) or 0)
    gaps = int(status.get("sequence_gaps", 0) or 0)
    if ws_failures or gaps or (status.get("server_errors") or []):
        return "degraded", f"ws_failures={ws_failures} sequence_gaps={gaps} server_errors={len(status.get('server_errors') or [])}"
    return "ok", f"last status {age:.0f} s ago"


def build_report(
    log_dir: str | Path,
    state_dir: str | Path,
    *,
    day: str | None = None,
    mode: str | None = None,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    log_dir, state_dir = Path(log_dir), Path(state_dir)
    health = _load_json(log_dir / "health.json")
    verdict, reason = health_verdict(health, now)
    status = (health or {}).get("status") or {}

    trades = read_jsonl(log_dir / "trades.jsonl")
    day_files = sorted(p for p in log_dir.glob("events_*.jsonl") if EVENTS_RE.match(p.name))
    days = []
    for p in day_files:
        recs = read_jsonl(p)
        days.append({"day": EVENTS_RE.match(p.name).group(1), "records": len(recs),
                     "signals": sum(1 for r in recs if r.get("type") == "signal"),
                     "entries": sum(1 for r in recs if r.get("type") == "entry"),
                     "exits": sum(1 for r in recs if r.get("type") == "exit"),
                     "errors": sum(1 for r in recs if r.get("type") == "error")})

    limits = None
    for p in reversed(day_files):
        for r in reversed(read_jsonl(p)):
            if r.get("type") == "risk_limits":
                limits = {"fingerprint": r.get("fingerprint"), "mode": r.get("mode"), "at_iso": r.get("at_iso"),
                          "limits": r.get("limits")}
                break
        if limits:
            break

    positions = {}
    for m in ([mode] if mode else ["shadow", "live"]):
        st = _load_json(state_dir / f"state_{m}.json")
        for pid, pos in ((st or {}).get("positions") or {}).items():
            positions[pid] = {"mode": m, **{k: pos.get(k) for k in
                              ("base_size", "entry_price", "stop_price", "high_water", "trailing", "opened_at")}}

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "health": {"verdict": verdict, "reason": reason,
                   "last_status_at_iso": (health or {}).get("written_at_iso"),
                   "run_id": (health or {}).get("run_id"),
                   "mode": status.get("mode") or (status.get("trading") or {}).get("mode"),
                   "equity_usd": (status.get("trading") or {}).get("equity_usd"),
                   "journal": (health or {}).get("journal")},
        "trades": summarise_trades(trades, day=day, mode=mode),
        "open_positions": positions,
        "days": days,
        "limits": limits,
    }


def format_text(report: dict) -> str:
    h, t = report["health"], report["trades"]
    lines = [
        f"Health         : {h['verdict']} ({h['reason']})",
        f"Last status    : {h.get('last_status_at_iso') or '-'}  mode={h.get('mode') or '-'}  equity={h.get('equity_usd') or '-'}",
        f"Trades         : {t['entries']} entries, {t['exits']} exits, {t['wins']} wins, {t['losses']} losses"
        + (f", win rate {t['win_rate']}" if t['win_rate'] else ""),
        f"Realised P&L   : {t['realised_pnl_usd']} USD  (fees {t['fees_usd']}, max drawdown {t['max_drawdown_usd']})",
    ]
    if t["profit_factor"]:
        lines.append(f"Profit factor  : {t['profit_factor']}  avg win {t['avg_win_usd']}  avg loss {t['avg_loss_usd']}  largest loss {t['largest_loss_usd']}")
    if t["exit_reasons"]:
        lines.append("Exit reasons   : " + ", ".join(f"{k}={v}" for k, v in sorted(t["exit_reasons"].items())))
    for pid, row in t["by_product"].items():
        lines.append(f"  {pid:<14} exits {row['exits']:>3}  wins {row['wins']:>3}  pnl {row['pnl_usd']:>10}")
    if report["open_positions"]:
        lines.append("Open positions :")
        for pid, p in report["open_positions"].items():
            lines.append(f"  {pid:<14} {p['mode']}  size {p['base_size']}  entry {p['entry_price']}  stop {p['stop_price']}"
                         f"  trailing={p['trailing']}")
    else:
        lines.append("Open positions : none")
    if report["limits"]:
        lines.append(f"Risk limits    : fingerprint {report['limits']['fingerprint']} ({report['limits']['mode']}, {report['limits']['at_iso']})")
    lines.append("Journal days   : " + (", ".join(f"{d['day']} ({d['records']} records)" for d in report["days"]) or "none"))
    return "\n".join(lines)
