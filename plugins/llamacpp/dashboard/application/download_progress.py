"""Honest download-progress state transitions for quiet HF downloads."""
from __future__ import annotations
from typing import Any, MutableMapping

class DownloadProgressTracker:
    INDETERMINATE_PERCENT = 3
    def begin(self, job: MutableMapping[str, Any], detail: str) -> None:
        job.update({"phase": "downloading", "detail": detail, "percent": self.INDETERMINATE_PERCENT})
    def observe_percent(self, job: MutableMapping[str, Any], percent: int, index: int, total: int) -> None:
        bounded=max(0,min(100,int(percent))); job["percent"]=max(self.INDETERMINATE_PERCENT, round((index + bounded / 100) / max(1,total) * 100))
