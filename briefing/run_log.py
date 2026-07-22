"""
refresh_runs operational logger — one row per refresh attempt.

Records timings per stage, data quality, per-card AI audit (facts given,
validation attempts, fallback usage), token usage, and estimated cost.

FAIL-OPEN: every Supabase error is printed and swallowed. Logging must never
break a briefing — if the logbook is unreachable, the run proceeds unlogged.
"""

import os
import time
from datetime import datetime, timezone
from typing import Any

import requests


class RunLogger:
    def __init__(self, hotel_id: str, run_type: str = "full"):
        self.hotel_id = hotel_id
        self.run_type = run_type
        self.run_id: str | None = None
        self.timings: dict[str, int] = {}
        self.fields: dict[str, Any] = {}
        self._url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self._key = os.getenv("SUPABASE_SERVICE_KEY", "")

    @property
    def _enabled(self) -> bool:
        return bool(self._url and self._key)

    def _headers(self) -> dict:
        return {"apikey": self._key, "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json"}

    def start(self) -> None:
        if not self._enabled:
            return
        try:
            r = requests.post(
                f"{self._url}/rest/v1/refresh_runs",
                json={"hotel_id": self.hotel_id, "run_type": self.run_type,
                      "status": "running"},
                headers={**self._headers(), "Prefer": "return=representation"},
                timeout=10,
            )
            r.raise_for_status()
            self.run_id = r.json()[0]["id"]
        except Exception as exc:
            print(f"[run-log] start failed (continuing unlogged): {exc}")

    def stage(self, name: str) -> "_Stage":
        """Context manager timing one pipeline stage into timings.<name>_ms."""
        return _Stage(self, name)

    def record(self, **fields: Any) -> None:
        """Attach columns to be written at finish(). None values are dropped."""
        self.fields.update({k: v for k, v in fields.items() if v is not None})

    def finish(self, status: str, error_type: str | None = None,
               error_message: Any = None) -> None:
        summary = (f"[run-log] {self.run_type} run {str(self.hotel_id)[:8]}…: {status}"
                   + (f" ({error_type}: {str(error_message)[:120]})" if error_type else ""))
        print(summary)
        if not self._enabled or not self.run_id:
            return
        payload: dict[str, Any] = {
            "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "timings": self.timings,
            **self.fields,
        }
        if error_type:
            payload["error_type"] = error_type
        if error_message is not None:
            payload["error_message"] = str(error_message)[:2000]
        try:
            r = requests.patch(
                f"{self._url}/rest/v1/refresh_runs",
                params={"id": f"eq.{self.run_id}"},
                json=payload, headers=self._headers(), timeout=10,
            )
            r.raise_for_status()
        except Exception as exc:
            print(f"[run-log] finish failed (run {self.run_id} stays 'running'): {exc}")


class _Stage:
    def __init__(self, logger: RunLogger, name: str):
        self._logger = logger
        self._name = name

    def __enter__(self) -> "_Stage":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc) -> bool:
        self._logger.timings[f"{self._name}_ms"] = int((time.monotonic() - self._t0) * 1000)
        return False  # never swallow pipeline exceptions
