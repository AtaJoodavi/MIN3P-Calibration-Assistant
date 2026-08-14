from __future__ import annotations

"""Optional V14 event writer used by the optimiser; pipeline audit remains authoritative."""

from pathlib import Path
from typing import Any

from modules.v14_utils import append_excel_atomic, json_safe, now


class V14HistoryManager:
    VERSION = "V14"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config
        self.event_file = Path(paths.results_dir) / "v14_optimizer_events.xlsx"

    def append_event(self, payload: dict[str, Any]) -> None:
        append_excel_atomic(
            self.event_file,
            [{"timestamp": now(), "optimizer_version": self.VERSION, **json_safe(payload)}],
        )

    def snapshot(self) -> dict[str, Any]:
        return {"event_file": str(self.event_file), "optimizer_version": self.VERSION}
