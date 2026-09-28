"""Event journal: every record the bot emits, written to disk as JSON Lines.

Layout under the log directory (``logs/`` by default, git ignored):

* ``events_YYYY-MM-DD.jsonl``: every record, one JSON object per line, in a
  new file each trading day (Pacific by default). Nothing is ever rewritten.
* ``trades.jsonl``: only ``entry`` and ``exit`` records, appended for the
  life of the installation, so the trade history survives log rotation.
* ``health.json``: the latest ``status`` record plus the journal's own
  counters, replaced atomically, for an outside monitor to read.
* ``cof_bot.log``: the text log from Python ``logging``, rotated at 5 MB.

Every record is stamped before it is written:

* ``schema_version``: the version of ``schema/events.schema.json`` it obeys;
* ``seq``: a counter that rises by 1 per record within a run, so a gap in a
  consumer's copy is visible;
* ``run_id``: identifies the process that wrote it;
* ``at``: epoch seconds (already present on most records; added if not);
* ``at_iso``: the same instant in the trading time zone, for people.

Writes never raise into the trading loop: a disk error is counted, logged
once per run, and the loop continues, because a stop must still fire when
the disk is full.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
TRADE_TYPES = frozenset({"entry", "exit"})


class Journal:
    def __init__(
        self,
        log_dir: str | Path,
        *,
        timezone: str = "America/Los_Angeles",
        run_id: str | None = None,
        mode: str | None = None,
        echo: TextIO | None = None,
        clock=time.time,
    ):
        self.dir = Path(log_dir)
        self.tz = ZoneInfo(timezone)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.mode = mode
        self.echo = echo
        self.clock = clock
        self.seq = 0
        self.written = 0
        self.write_errors = 0
        self.last_status: dict | None = None
        self._lock = threading.Lock()
        self._events_day: str | None = None
        self._events: TextIO | None = None
        self._trades: TextIO | None = None
        self._error_logged = False

    # Paths --------------------------------------------------------------------

    def day_key(self, at: float) -> str:
        return datetime.fromtimestamp(at, self.tz).strftime("%Y-%m-%d")

    def events_path(self, day: str) -> Path:
        return self.dir / f"events_{day}.jsonl"

    @property
    def trades_path(self) -> Path:
        return self.dir / "trades.jsonl"

    @property
    def health_path(self) -> Path:
        return self.dir / "health.json"

    # Writing ------------------------------------------------------------------

    def stamp(self, record: dict) -> dict:
        at = record.get("at")
        if not isinstance(at, (int, float)):
            at = self.clock()
        self.seq += 1
        stamped = {
            "schema_version": SCHEMA_VERSION,
            "seq": self.seq,
            "run_id": self.run_id,
            "at": at,
            "at_iso": datetime.fromtimestamp(at, self.tz).isoformat(timespec="milliseconds"),
        }
        if self.mode is not None and "mode" not in record:
            stamped["mode"] = self.mode
        stamped.update(record)
        stamped["at"] = at
        return stamped

    def _open_events(self, day: str) -> TextIO:
        if self._events is None or self._events_day != day:
            if self._events is not None:
                self._events.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            self._events = self.events_path(day).open("a", encoding="utf-8")
            self._events_day = day
        return self._events

    def _open_trades(self) -> TextIO:
        if self._trades is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._trades = self.trades_path.open("a", encoding="utf-8")
        return self._trades

    def write(self, record: dict) -> dict:
        """Stamp, persist and echo one record. Returns the stamped record."""
        with self._lock:
            stamped = self.stamp(record)
            line = json.dumps(stamped, default=str) + "\n"
            try:
                f = self._open_events(self.day_key(stamped["at"]))
                f.write(line)
                f.flush()
                if stamped.get("type") in TRADE_TYPES:
                    t = self._open_trades()
                    t.write(line)
                    t.flush()
                    os.fsync(t.fileno())
                self.written += 1
            except OSError as exc:
                self.write_errors += 1
                if not self._error_logged:
                    self._error_logged = True
                    log.error("Journal write failed (%s); trading continues without the journal", exc)
            if stamped.get("type") == "status":
                self.last_status = stamped
                self._write_health(stamped)
        if self.echo is not None:
            self.echo.write(line)
            self.echo.flush()
        return stamped

    def _write_health(self, status: dict) -> None:
        health = {
            "written_at": status["at"],
            "written_at_iso": status["at_iso"],
            "run_id": self.run_id,
            "journal": {"records": self.written, "write_errors": self.write_errors, "seq": self.seq},
            "status": status,
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.health_path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(health, f, indent=2, default=str)
            os.replace(tmp, self.health_path)
        except OSError as exc:
            self.write_errors += 1
            log.debug("health.json write failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            for f in (self._events, self._trades):
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
            self._events = self._trades = None


def read_jsonl(path: str | Path) -> list[dict]:
    """Read a JSON Lines file, skipping any line that is not valid JSON.

    A half written last line (the process died mid write) is skipped rather
    than raised, so a report can always be produced.
    """
    out: list[dict] = []
    p = Path(path)
    if not p.is_file():
        return out
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out
