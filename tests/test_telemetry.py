import io
import json
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cof_bot.cli import main
from cof_bot.telemetry.journal import SCHEMA_VERSION, Journal, read_jsonl
from cof_bot.telemetry.metrics import RunMetrics, summarise_trades
from cof_bot.telemetry.report import build_report, format_text, health_verdict

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"
PT = ZoneInfo("America/Los_Angeles")


def ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=PT).timestamp()


def fill(side, size="10", price="1.0", fees="0.06"):
    return {"product_id": "ABC-USD", "side": side, "base_size": size, "avg_price": price, "fees_usd": fees,
            "client_order_id": "cof-x", "order_id": None, "status": "FILLED", "note": None}


def entry(at, pid="ABC-USD"):
    return {"type": "entry", "at": at, "mode": "shadow", "product_id": pid, "fill": fill("BUY"),
            "sizing": {"binding": "risk_budget", "limit_price": "1.0075", "notional_usd": "10.00",
                       "max_loss_usd": "0.80", "equity_usd": "1000.00"},
            "stop_price": "0.94", "signal_strength": 0.5, "headline_ids": ["h1"]}


def exit_(at, pnl, reason="trailing_stop", pid="ABC-USD", held=120.0):
    return {"type": "exit", "at": at, "mode": "shadow", "product_id": pid, "reason": reason, "fill": fill("SELL"),
            "entry_price": "1.0", "high_water": "1.2", "pnl_usd": pnl, "held_s": held, "remaining_base": "0"}


def status(at, **extra):
    return {"type": "status", "at": at, "ws_connects": 1, "ws_failures": 0, "messages": 100, "sequence_gaps": 0,
            "bad_messages": 0, "server_errors": [], "books_synced": 2, "sentiment_spikes": 0, "volume_surges": 0,
            "signals_total": 0, "metrics": RunMetrics(at - 10).to_dict(at),
            "trading": {"mode": "shadow", "equity_usd": "1000.00", "cash_usd": "1000.00", "open_positions": {},
                        "day": {"day": "2026-09-27", "trades": 0, "halted": None, "realised_pnl_usd": "0.00",
                                "drawdown_pct": "0.000"}, "limits_fingerprint": "abc"}, **extra}


# Journal ------------------------------------------------------------------------


def test_journal_stamps_splits_days_and_keeps_trades(tmp_path):
    echo = io.StringIO()
    j = Journal(tmp_path, mode="shadow", echo=echo, run_id="run1")
    j.write({"type": "headline", "at": ts(2026, 9, 27, 23), "source": "s", "id": "h", "title": "t",
             "compound": 0.1, "label": "positive", "products": []})
    j.write(entry(ts(2026, 9, 27, 23, ) + 1800))  # 23:30 Pacific: still 09-27 here, 09-28 in UTC
    j.write(exit_(ts(2026, 9, 28, 1), "3.50"))
    j.close()

    d27 = read_jsonl(tmp_path / "events_2026-09-27.jsonl")
    d28 = read_jsonl(tmp_path / "events_2026-09-28.jsonl")
    assert [r["type"] for r in d27] == ["headline", "entry"] and [r["type"] for r in d28] == ["exit"]
    assert [r["seq"] for r in d27 + d28] == [1, 2, 3]
    assert all(r["schema_version"] == SCHEMA_VERSION and r["run_id"] == "run1" and r["mode"] == "shadow" for r in d27)
    assert d27[1]["at_iso"].startswith("2026-09-27T23:30:00")
    trades = read_jsonl(tmp_path / "trades.jsonl")
    assert [r["type"] for r in trades] == ["entry", "exit"]
    assert len(echo.getvalue().splitlines()) == 3 and json.loads(echo.getvalue().splitlines()[0])["seq"] == 1


def test_journal_health_file_and_missing_at(tmp_path):
    j = Journal(tmp_path, clock=lambda: ts(2026, 9, 27))
    j.write({"type": "warning", "message": "no at field"})
    j.write(status(ts(2026, 9, 27)))
    health = json.loads((tmp_path / "health.json").read_text())
    assert health["status"]["type"] == "status" and health["journal"]["records"] == 2
    assert read_jsonl(tmp_path / "events_2026-09-27.jsonl")[0]["at"] == ts(2026, 9, 27)
    assert not (tmp_path / "trades.jsonl").exists()


def test_journal_survives_unwritable_dir(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    j = Journal(blocked / "logs", echo=io.StringIO())
    rec = j.write(entry(ts(2026, 9, 27)))
    assert rec["seq"] == 1 and j.write_errors == 1 and j.written == 0


def test_read_jsonl_skips_torn_last_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n[1,2]\n{"b": 2}\n{"c": ')
    assert read_jsonl(p) == [{"a": 1}, {"b": 2}]
    assert read_jsonl(tmp_path / "missing.jsonl") == []


# Metrics ------------------------------------------------------------------------


def test_run_metrics_counts_and_timing():
    m = RunMetrics(started_at=0.0)
    for r in [{"type": "headline"}, {"type": "signal"}, {"type": "entry"}, {"type": "exit"},
              {"type": "skip", "reason": "halted: daily_drawdown"}, {"type": "skip", "reason": "halted: kill"},
              {"type": "error"}, {"type": "breaker"}, {"type": "status"}]:
        m.count(r)
    m.timed_eval(0.002)
    m.timed_eval(0.004)
    d = m.to_dict(10.0)
    assert d["uptime_s"] == 10.0 and d["skips"] == 2 and d["skip_reasons"] == {"halted": 2}
    assert d["eval_ms_last"] == 4.0 and d["eval_ms_max"] == 4.0 and d["eval_ms_mean"] == 3.0
    assert (d["headlines"], d["signals"], d["entries"], d["exits"], d["errors"], d["breakers"]) == (1, 1, 1, 1, 1, 1)


def test_summarise_trades_figures():
    recs = [entry(1), exit_(2, "10.00"), entry(3), exit_(4, "-4.00", "hard_stop"), entry(5),
            exit_(6, "-6.00", "breaker:daily_drawdown", pid="XYZ-USD"), entry(7), exit_(8, "2.00")]
    for r in recs:
        r["at_iso"] = "2026-09-27T10:00:00"
    s = summarise_trades(recs)
    assert (s["entries"], s["exits"], s["wins"], s["losses"]) == (4, 4, 2, 2)
    assert s["realised_pnl_usd"] == "2.00" and s["gross_win_usd"] == "12.00" and s["gross_loss_usd"] == "10.00"
    assert s["profit_factor"] == "1.200" and s["win_rate"] == "0.5000"
    assert s["avg_win_usd"] == "6.00" and s["avg_loss_usd"] == "5.00" and s["largest_loss_usd"] == "-6.00"
    # cumulative: 10, 6, 0, 2 -> peak 10, trough 0
    assert s["max_drawdown_usd"] == "10.00"
    assert s["fees_usd"] == str(D("0.06") * 8)
    assert s["exit_reasons"] == {"trailing_stop": 2, "hard_stop": 1, "breaker": 1}
    assert s["by_product"]["XYZ-USD"] == {"exits": 1, "wins": 0, "pnl_usd": "-6.00"}
    assert summarise_trades(recs, day="2026-09-28")["exits"] == 0
    assert summarise_trades(recs, mode="live")["entries"] == 0
    empty = summarise_trades([])
    assert empty["win_rate"] is None and empty["profit_factor"] is None and empty["realised_pnl_usd"] == "0.00"


# Report -------------------------------------------------------------------------


def test_health_verdicts():
    now = 1000.0
    assert health_verdict(None, now)[0] == "unknown"
    assert health_verdict({"written_at": now - 500, "status": {}}, now)[0] == "stale"
    ok = {"written_at": now - 30, "status": status(now - 30)}
    assert health_verdict(ok, now)[0] == "ok"
    halted = {"written_at": now - 30, "status": status(now - 30)}
    halted["status"]["trading"]["day"]["halted"] = "daily_drawdown"
    assert health_verdict(halted, now) == ("halted", "circuit breaker: daily_drawdown")
    bad = {"written_at": now - 30, "status": status(now - 30, sequence_gaps=2)}
    assert health_verdict(bad, now)[0] == "degraded"


def test_build_report_and_text(tmp_path):
    logs, state = tmp_path / "logs", tmp_path / "data"
    j = Journal(logs, mode="shadow", run_id="r1")
    now = ts(2026, 9, 27, 14)
    j.write({"type": "risk_limits", "at": now - 100, "fingerprint": "f00d", "limits": {"hard_stop": "0.06"},
             "fee_rate": "0.012", "cash_usd": "1000"})
    j.write(entry(now - 90))
    j.write(exit_(now - 60, "5.00"))
    j.write(status(now - 10))
    j.close()
    state.mkdir()
    (state / "state_shadow.json").write_text(json.dumps({"mode": "shadow", "positions": {"XYZ-USD": {
        "base_size": "3", "entry_price": "2", "stop_price": "1.88", "high_water": "2", "trailing": False,
        "opened_at": now - 5}}}))

    report = build_report(logs, state, now=now)
    assert report["health"]["verdict"] == "ok" and report["health"]["equity_usd"] == "1000.00"
    assert report["trades"]["realised_pnl_usd"] == "5.00" and report["trades"]["wins"] == 1
    assert report["open_positions"]["XYZ-USD"]["stop_price"] == "1.88"
    assert report["days"] == [{"day": "2026-09-27", "records": 4, "signals": 0, "entries": 1, "exits": 1, "errors": 0}]
    assert report["limits"]["fingerprint"] == "f00d"
    text = format_text(report)
    assert "Health         : ok" in text and "XYZ-USD" in text and "fingerprint f00d" in text
    assert build_report(logs, state, day="2026-09-26", now=now)["trades"]["exits"] == 0
    assert format_text(build_report(tmp_path / "none", tmp_path / "none", now=now)).startswith("Health         : unknown")


def test_records_and_report_obey_published_schemas(tmp_path):
    """Validate against schema/*.json with a minimal draft 2020-12 checker.

    jsonschema is not a dependency, so this checks the parts the schemas
    use: required, type, enum, const, pattern, properties, additionalProperties,
    items, allOf with if/then, and $ref into $defs.
    """
    import re

    events = json.loads((SCHEMA_DIR / "events.schema.json").read_text())
    report_schema = json.loads((SCHEMA_DIR / "report.schema.json").read_text())

    TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool,
             "null": type(None)}

    def check(value, schema, root, path="$"):
        if "$ref" in schema:
            node = root
            for part in schema["$ref"].lstrip("#/").split("/"):
                node = node[part]
            check(value, node, root, path)
            return
        if "const" in schema:
            assert value == schema["const"], f"{path}: {value!r} != {schema['const']!r}"
        if "enum" in schema:
            assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            ok = any(isinstance(value, TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
                     for t in types)
            assert ok, f"{path}: {value!r} is not {types}"
        if "pattern" in schema and isinstance(value, str):
            assert re.match(schema["pattern"], value), f"{path}: {value!r} fails {schema['pattern']}"
        if isinstance(value, dict):
            for req in schema.get("required", []):
                assert req in value, f"{path}: missing {req}"
            props = schema.get("properties", {})
            for k, v in value.items():
                if k in props:
                    check(v, props[k], root, f"{path}.{k}")
                elif "additionalProperties" in schema and isinstance(schema["additionalProperties"], dict):
                    check(v, schema["additionalProperties"], root, f"{path}.{k}")
        if isinstance(value, list) and "items" in schema:
            for i, v in enumerate(value):
                check(v, schema["items"], root, f"{path}[{i}]")
        for sub in schema.get("allOf", []):
            if "if" in sub:
                try:
                    check(value, sub["if"], root, path)
                except AssertionError:
                    continue
                check(value, sub["then"], root, path)
            else:
                check(value, sub, root, path)

    logs, state = tmp_path / "logs", tmp_path / "data"
    j = Journal(logs, mode="shadow")
    now = ts(2026, 9, 27, 14)
    records = [
        {"type": "risk_limits", "at": now, "fingerprint": "f00d", "limits": {}, "fee_rate": "0.012", "cash_usd": "1000"},
        {"type": "headline", "at": now, "source": "s", "id": "h1", "title": "t", "compound": 0.4, "label": "positive",
         "products": ["ABC-USD"]},
        {"type": "signal", "at": now, "product_id": "ABC-USD", "mentions": 1, "mean_compound": 0.4,
         "headline_ids": ["h1"], "titles": ["t"], "sources": ["s"], "volume_ratio": 2.5, "window_usd": 9000.0,
         "buy_share": 0.6, "price_change_pct": 1.0, "lag_s": 10.0, "strength": 0.5, "spread_bps": None,
         "book_imbalance": 0.1},
        {"type": "new_day", "at": now, "day": "2026-09-27", "start_equity": "1000", "previous": None},
        entry(now), exit_(now + 1, "-0.50", "hard_stop"),
        {"type": "skip", "at": now, "product_id": "ABC-USD", "reason": "spread too wide"},
        {"type": "breaker", "at": now, "reason": "daily_drawdown", "flatten": True, "day": "2026-09-27"},
        {"type": "error", "at": now, "product_id": "ABC-USD", "action": "buy", "message": "503"},
        status(now + 2),
        {"type": "stopped", "at": now + 3, "trading": status(now)["trading"], "metrics": RunMetrics(now).to_dict(now + 3)},
    ]
    for r in records:
        check(j.write(r), events, events)
    j.close()
    check(build_report(logs, state, now=now + 4), report_schema, report_schema)


# CLI ----------------------------------------------------------------------------


def test_report_command_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COF_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("COF_STATE_DIR", str(tmp_path / "data"))
    assert main(["--env-file", "/nonexistent", "report", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == SCHEMA_VERSION and out["health"]["verdict"] == "unknown"
    assert main(["--env-file", "/nonexistent", "report"]) == 0
    assert capsys.readouterr().out.startswith("Health         : unknown")


def test_stream_runner_uses_journal(tmp_path):
    from cof_bot.config import load_stream_settings
    from cof_bot.exchange.universe import Universe, UsdPair
    from cof_bot.runtime import StreamRunner
    from test_stream import FakeCollector, FakeFeed

    pair = UsdPair("ABC-USD", "ABC", "Abc", "ABC", D("0.1"), D("0.0001"), D("1"), D("1"), D("1"), D("500000"))
    out = io.StringIO()
    j = Journal(tmp_path, mode="stream", echo=out, clock=lambda: ts(2026, 9, 27))
    runner = StreamRunner(Universe([pair], True, "test", 1), load_stream_settings({}), out=out,
                          clock=lambda: ts(2026, 9, 27), feed_factory=FakeFeed, collector=FakeCollector([]), journal=j)
    result = runner.evaluate()
    runner.status(result)
    line = json.loads(out.getvalue().splitlines()[-1])
    assert line["type"] == "status" and line["seq"] == 1 and line["metrics"]["uptime_s"] == 0.0
    assert runner.metrics.eval_count == 1
    assert (tmp_path / "health.json").exists() and read_jsonl(tmp_path / "events_2026-09-27.jsonl")[0]["mode"] == "stream"
