"""
TEMPORARY Supabase echo — the last thread to Supabase while it is retired
(2026-10-28, user: "lets totally kill supabase, everything should go to
postgresql").

Postgres is the authority for everything. While the old project still exists
we keep sending it a best-effort copy of each write, so a rollback stays
possible for a few days. An echo NEVER raises, never blocks the caller and
never decides anything: writes are queued to ONE background worker (so a
run's open/close copies stay in order) and dropped if the queue is full.
Switch it off with SUPABASE_ECHO=0 (or by removing the Supabase env vars);
stage 2 deletes this module and every call to it.
"""
import os
import queue
import threading

import requests

_Q: "queue.Queue[tuple]" = queue.Queue(maxsize=2000)
_worker: threading.Thread | None = None
_lock = threading.Lock()


def enabled() -> bool:
    return (os.getenv("SUPABASE_ECHO", "1") != "0"
            and bool(os.getenv("SUPABASE_URL")) and bool(os.getenv("SUPABASE_SERVICE_KEY")))


def _send(method: str, table: str, params: dict | None, body, prefer: str) -> None:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not (url and key):
        return
    try:
        r = requests.request(
            method, f"{url}/rest/v1/{table}", params=params or {}, json=body,
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json", "Prefer": prefer},
            timeout=8)
        if r.status_code >= 400:
            print(f"[sb-echo] {method} {table} -> {r.status_code} (ignored)")
    except Exception as exc:  # noqa: BLE001 — an echo never breaks anything
        print(f"[sb-echo] {method} {table} failed (ignored): {exc}")


def _run() -> None:
    while True:
        item = _Q.get()
        try:
            _send(*item)
        finally:
            _Q.task_done()


def echo(method: str, table: str, params: dict | None = None, body=None,
         prefer: str = "return=minimal") -> None:
    """Queue a best-effort copy of one write to Supabase REST. Returns at once."""
    global _worker
    if not enabled():
        return
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="sb-echo", daemon=True)
            _worker.start()
    try:
        _Q.put_nowait((method, table, params, body, prefer))
    except queue.Full:
        print(f"[sb-echo] queue full — {method} {table} dropped (ignored)")


def flush(timeout: float = 30.0) -> None:
    """Wait until queued echoes are sent (tests, shutdown). Best-effort."""
    import time
    end = time.time() + timeout
    while _Q.unfinished_tasks and time.time() < end:
        time.sleep(0.05)
