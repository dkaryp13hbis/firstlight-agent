"""
Phase C storage layer — Railway Postgres twin of the backend's Supabase
writes (prepared 2026-09-04, DORMANT until STORAGE is set).

    STORAGE=supabase   (default) everything as today; this module no-ops
    STORAGE=dual       C1 window: every backend WRITE goes to BOTH stores
                       (reads stay on Supabase)
    STORAGE=pg         reads flip to Postgres too (C1 step 5)

Rules
- Fail-open: a Postgres error NEVER breaks a briefing. During `dual` a PG
  write failure is logged loudly (it must be seen in the dual-write
  verification) but the pipeline continues on Supabase.
- psycopg3 + pool (max 5). Lazy: nothing is imported or connected until the
  first call with STORAGE != supabase, so production is untouched until the
  env flip.
- `mirror_rows` bulk-copies APP-written tables (watchlist, feedback, ...)
  during the C1→C2 gap; the nightly verify job uses `counts()`.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

_POOL = None
_POOL_LOCK = threading.Lock()


def mode() -> str:
    return os.getenv("STORAGE", "supabase").lower()


def enabled() -> bool:
    """True when Postgres participates at all (dual or pg)."""
    return mode() in ("dual", "pg") and bool(os.getenv("DATABASE_URL"))


def read_from_pg() -> bool:
    return mode() == "pg" and bool(os.getenv("DATABASE_URL"))


def _pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                from psycopg_pool import ConnectionPool
                _POOL = ConnectionPool(os.environ["DATABASE_URL"],
                                       min_size=1, max_size=5,
                                       kwargs={"autocommit": True})
    return _POOL


def _jsonb(v: Any):
    from psycopg.types.json import Jsonb
    return Jsonb(v) if isinstance(v, (dict, list)) else v


def _exec(sql: str, params: tuple = (), fetch: bool = False):
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None


def _safe(label: str, fn):
    """Run a PG operation fail-open; loud log so dual-write gaps are visible."""
    if not enabled():
        return None
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        print(f"[store] PG {label} FAILED (pipeline continues on Supabase): {exc}")
        return None


# ── backend writes (dual-write targets) ──────────────────────────────────────

def upsert_briefing(payload: dict[str, Any]) -> None:
    """Twin of cloud_push's briefings upsert (on hotel_id+report_date)."""
    def go():
        cols = ["hotel_id", "report_date", "data", "ai_insights", "kpi_summary",
                "rendered_html", "source_run_id"]
        present = [c for c in cols if c in payload]
        sets = ", ".join(f"{c} = excluded.{c}" for c in present if c not in ("hotel_id", "report_date"))
        _exec(
            f"insert into briefings ({', '.join(present)}, generated_at) "
            f"values ({', '.join(['%s'] * len(present))}, now()) "
            f"on conflict (hotel_id, report_date) do update set {sets}, generated_at = now()",
            tuple(_jsonb(payload[c]) for c in present),
        )
    _safe("upsert_briefing", go)


def insert_run(row: dict[str, Any]) -> None:
    """Twin of RunLogger's insert — reuses the Supabase-assigned run id so the
    two stores stay joinable."""
    def go():
        cols = list(row.keys())
        _exec(
            f"insert into refresh_runs ({', '.join(cols)}) "
            f"values ({', '.join(['%s'] * len(cols))}) on conflict (id) do nothing",
            tuple(_jsonb(row[c]) for c in cols),
        )
    _safe("insert_run", go)


def update_run(run_id: str, fields: dict[str, Any]) -> None:
    def go():
        if not fields:
            return
        sets = ", ".join(f"{c} = %s" for c in fields)
        _exec(f"update refresh_runs set {sets} where id = %s",
              tuple(_jsonb(v) for v in fields.values()) + (run_id,))
    _safe("update_run", go)


def upsert_command(row: dict[str, Any]) -> None:
    """Mirror a refresh_commands row (the app writes them into Supabase; the
    poller mirrors on pickup so PG has the full history before C2)."""
    def go():
        cols = list(row.keys())
        sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        _exec(
            f"insert into refresh_commands ({', '.join(cols)}) "
            f"values ({', '.join(['%s'] * len(cols))}) "
            f"on conflict (id) do update set {sets}",
            tuple(_jsonb(row[c]) for c in cols),
        )
    _safe("upsert_command", go)


def claim_intraday(hotel_id: str, day: str, ntype: str) -> bool | None:
    """PG twin of the intraday claim. Returns True/False, or None when PG is
    not participating (caller keeps using the Supabase claim as authority)."""
    if not read_from_pg():
        # dual mode: mirror the claim but Supabase stays the authority
        _safe("mirror_intraday", lambda: _exec(
            "insert into intraday_log (hotel_id, day, type) values (%s, %s, %s) "
            "on conflict do nothing", (hotel_id, day, ntype)))
        return None
    def go():
        rows = _exec(
            "insert into intraday_log (hotel_id, day, type) values (%s, %s, %s) "
            "on conflict do nothing returning hotel_id", (hotel_id, day, ntype), fetch=True)
        return bool(rows)
    return _safe("claim_intraday", go)


# ── bulk mirror + verification (C1 step 4 nightly job) ───────────────────────

_MIRROR_CONFLICT = {
    "hotels": "id", "hotel_users": "hotel_id, user_id", "organizations": "id",
    "insight_feedback": "hotel_id, report_date, card_id, user_id",
    "hotel_prefs": "hotel_id", "push_subscriptions": "user_id, hotel_id",
    "watchlist": "user_id, hotel_id, kind, key", "usage_events": "id",
    "briefings": "hotel_id, report_date", "refresh_runs": "id",
    "refresh_commands": "id", "intraday_log": "hotel_id, day, type",
}


def mirror_rows(table: str, rows: list[dict[str, Any]]) -> int:
    """Upsert a batch of Supabase rows into PG (idempotent). Returns count."""
    if not rows or table not in _MIRROR_CONFLICT:
        return 0
    def go():
        n = 0
        for r in rows:
            cols = list(r.keys())
            sets = ", ".join(f"{c} = excluded.{c}" for c in cols)
            _exec(
                f"insert into {table} ({', '.join(cols)}) "
                f"values ({', '.join(['%s'] * len(cols))}) "
                f"on conflict ({_MIRROR_CONFLICT[table]}) do update set {sets}",
                tuple(_jsonb(r[c]) for c in cols),
            )
            n += 1
        return n
    return _safe(f"mirror:{table}", go) or 0


def counts() -> dict[str, int]:
    """Row counts per table — compared against Supabase in the nightly verify."""
    def go():
        out = {}
        for t in _MIRROR_CONFLICT:
            out[t] = _exec(f"select count(*) from {t}", fetch=True)[0][0]
        return out
    return _safe("counts", go) or {}


# ── reads (used only when STORAGE=pg — C1 step 5) ────────────────────────────

def get_latest_briefing(hotel_id: str, columns: list[str]) -> dict | None:
    def go():
        rows = _exec(
            f"select {', '.join(columns)} from briefings where hotel_id = %s "
            f"order by report_date desc limit 1", (hotel_id,), fetch=True)
        return dict(zip(columns, rows[0])) if rows else None
    return _safe("get_latest_briefing", go)


def get_briefings_since(hotel_id: str, since: str, before: str,
                        columns: list[str]) -> list[dict]:
    def go():
        rows = _exec(
            f"select {', '.join(columns)} from briefings where hotel_id = %s "
            f"and report_date >= %s and report_date < %s order by report_date",
            (hotel_id, since, before), fetch=True)
        return [dict(zip(columns, r)) for r in rows]
    return _safe("get_briefings_since", go) or []


def get_active_hotels(columns: list[str]) -> list[dict]:
    def go():
        rows = _exec(
            f"select {', '.join(columns)} from hotels where active order by name",
            fetch=True)
        return [dict(zip(columns, r)) for r in rows]
    return _safe("get_active_hotels", go) or []


def ping() -> bool:
    """Health probe for /health once PG participates."""
    return _safe("ping", lambda: _exec("select 1", fetch=True) is not None) or False
