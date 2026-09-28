"""Crash safe state file: open positions, shadow cash and today's risk counters.

Written atomically (temporary file, then rename) after every change, so a
restart never loses a position the bot is holding. The file lives under
data/, which is git ignored.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        if not self.path.is_file():
            return None
        with self.path.open(encoding="utf-8") as f:
            return json.load(f)

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
