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
                url = os.environ["DATABASE_URL"]
                # topology is not encryption (security review 2026-09-11):
                # require TLS even on the private network
                if "sslmode=" not in url:
                    url += ("&" if "?" in url else "?") + "sslmode=require"
                _POOL = ConnectionPool(url, min_size=1, max_size=5,
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

def _norm(v):
    """psycopg returns uuid.UUID / date / datetime OBJECTS where Supabase REST
    returned strings — callers compare ids and dates as strings, so normalize
    every scalar (2026-09-11 incident: manual run matched no hotels because
    UUID != str). jsonb stays dict/list — identical to Supabase's parse."""
    import uuid as _uuid
    from datetime import date as _date, datetime as _dt
    if isinstance(v, _uuid.UUID):
        return str(v)
    if isinstance(v, _dt):
        return v.isoformat()
    if isinstance(v, _date):
        return str(v)
    return v


def _row(columns: list[str], row: tuple) -> dict:
    return {c: _norm(v) for c, v in zip(columns, row)}


def get_latest_briefing(hotel_id: str, columns: list[str]) -> dict | None:
    def go():
        rows = _exec(
            f"select {', '.join(columns)} from briefings where hotel_id = %s "
            f"order by report_date desc limit 1", (hotel_id,), fetch=True)
        return _row(columns, rows[0]) if rows else None
    return _safe("get_latest_briefing", go)


def get_briefings_since(hotel_id: str, since: str, before: str,
                        columns: list[str]) -> list[dict]:
    def go():
        rows = _exec(
            f"select {', '.join(columns)} from briefings where hotel_id = %s "
            f"and report_date >= %s and report_date < %s order by report_date",
            (hotel_id, since, before), fetch=True)
        return [_row(columns, r) for r in rows]
    return _safe("get_briefings_since", go) or []


def get_active_hotels(columns: list[str]) -> list[dict]:
    def go():
        rows = _exec(
            f"select {', '.join(columns)} from hotels where active order by name",
            fetch=True)
        return [_row(columns, r) for r in rows]
    return _safe("get_active_hotels", go) or []


def get_briefing_on(hotel_id: str, report_date: str,
                    columns: list[str]) -> dict | None:
    """One briefing by exact report_date (pipeline's yesterday-reads)."""
    def go():
        rows = _exec(
            f"select {', '.join(columns)} from briefings where hotel_id = %s "
            f"and report_date = %s order by generated_at desc limit 1",
            (hotel_id, report_date), fetch=True)
        return _row(columns, rows[0]) if rows else None
    return _safe("get_briefing_on", go)


def get_pref_language(hotel_id: str) -> str | None:
    def go():
        rows = _exec("select language from hotel_prefs where hotel_id = %s",
                     (hotel_id,), fetch=True)
        return rows[0][0] if rows else None
    return _safe("get_pref_language", go)


def ping() -> bool:
    """Health probe for /health once PG participates."""
    return _safe("ping", lambda: _exec("select 1", fetch=True) is not None) or False

# ── superadmin portal (PG canonical, STORAGE=pg era) ─────────────────────────

def audit(admin_email: str, action: str, target_type: str | None = None,
          target_id: str | None = None, before=None, after=None,
          reason: str | None = None) -> None:
    """Append-only admin action log (ADMIN_PLAN §10). Fail-open."""
    def go():
        _exec(
            "insert into admin_audit (admin_email, action, target_type, "
            "target_id, before, after, reason) values (%s,%s,%s,%s,%s,%s,%s)",
            (admin_email, action, target_type, target_id,
             _jsonb(before), _jsonb(after), reason))
    _safe("audit", go)


_AUDIT_COLS = ["id", "at", "admin_email", "action", "target_type",
               "target_id", "before", "after", "reason"]


def audit_list(limit: int = 100, action: str | None = None,
               target_id: str | None = None) -> list[dict]:
    def go():
        q = f"select {', '.join(_AUDIT_COLS)} from admin_audit"
        conds, params = [], []
        if action:
            conds.append("action = %s"); params.append(action)
        if target_id:
            conds.append("target_id = %s"); params.append(target_id)
        if conds:
            q += " where " + " and ".join(conds)
        q += " order by at desc limit %s"; params.append(limit)
        return [_row(_AUDIT_COLS, r) for r in _exec(q, tuple(params), fetch=True)]
    return _safe("audit_list", go) or []


def hotel_runs(hotel_id: str, limit: int = 30) -> list[dict]:
    cols = ["started_at", "completed_at", "run_type", "status", "error_type",
            "attempt", "rows_fetched", "estimated_cost_usd", "fetch_path", "fallbacks"]
    def go():
        rows = _exec(
            "select started_at, completed_at, run_type, status, error_type, "
            "attempt, rows_fetched, estimated_cost_usd, fetch_path, "
            "(select count(*) from jsonb_array_elements(coalesce(cards_audit, '[]'::jsonb)) e "
            " where (e->>'fallback_used')::boolean) as fallbacks "
            "from refresh_runs where hotel_id = %s "
            "order by started_at desc limit %s", (hotel_id, limit), fetch=True)
        return [_row(cols, r) for r in rows]
    return _safe("hotel_runs", go) or []


def hotels_run_summary(days: int = 30) -> list[dict]:
    """Per hotel: run counts by status, cost, last run — one query."""
    cols = ["hotel_id", "runs", "ok", "degraded", "failed",
            "cost_usd", "last_run_at", "last_status"]
    def go():
        rows = _exec(
            "select hotel_id::text, count(*), "
            "count(*) filter (where status = 'success'), "
            "count(*) filter (where status = 'degraded'), "
            "count(*) filter (where status = 'failed'), "
            "coalesce(sum(estimated_cost_usd), 0), "
            "max(started_at), "
            "(array_agg(status order by started_at desc))[1] "
            "from refresh_runs where started_at > now() - make_interval(days => %s) "
            "group by hotel_id", (days,), fetch=True)
        return [_row(cols, r) for r in rows]
    return _safe("hotels_run_summary", go) or []


def runs_matrix(days: int = 7) -> list[dict]:
    """Day × run_type × status counts for the pipeline health view."""
    cols = ["day", "run_type", "status", "n"]
    def go():
        rows = _exec(
            "select to_char(started_at, 'YYYY-MM-DD'), run_type, status, count(*) "
            "from refresh_runs where started_at > now() - make_interval(days => %s) "
            "group by 1, 2, 3 order by 1 desc", (days,), fetch=True)
        return [_row(cols, r) for r in rows]
    return _safe("runs_matrix", go) or []


def ai_stats(days: int = 14) -> list[dict]:
    """Per card id: shipped count, fallback count — validator health."""
    cols = ["card_id", "n", "fallbacks"]
    def go():
        rows = _exec(
            "select e->>'card_id', count(*), "
            "count(*) filter (where (e->>'fallback_used')::boolean) "
            "from refresh_runs r, jsonb_array_elements(coalesce(r.cards_audit, '[]'::jsonb)) e "
            "where r.started_at > now() - make_interval(days => %s) "
            "and r.run_type = 'full' "
            "group by 1 order by 2 desc limit 20", (days,), fetch=True)
        return [_row(cols, r) for r in rows]
    return _safe("ai_stats", go) or []


def db_size_mb() -> float | None:
    def go():
        return round(_exec("select pg_database_size(current_database())",
                           fetch=True)[0][0] / 1e6, 1)
    return _safe("db_size", go)


def update_hotel_fields(hotel_id: str, fields: dict) -> None:
    """PG twin for hotel edits (pipeline reads hotels from PG now)."""
    def go():
        sets = ", ".join(f"{c} = %s" for c in fields)
        _exec(f"update hotels set {sets} where id = %s",
              tuple(_jsonb(v) for v in fields.values()) + (hotel_id,))
    _safe("update_hotel_fields", go)

def finance_daily(days: int = 30) -> list[dict]:
    """Per day: AI cost, tokens, rows processed (rows_fetched is jsonb —
    object of per-query counts on newer runs, bare number on legacy)."""
    cols = ["day", "cost_usd", "input_tokens", "output_tokens", "rows"]
    def go():
        rows = _exec(
            "select to_char(started_at, 'YYYY-MM-DD') as day, "
            "round(coalesce(sum(estimated_cost_usd), 0), 4), "
            "coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), "
            "coalesce(sum(case "
            "  when jsonb_typeof(rows_fetched) = 'object' then "
            "    (select sum(v::numeric) from jsonb_each_text(rows_fetched) as e(k, v)) "
            "  when jsonb_typeof(rows_fetched) = 'number' then rows_fetched::text::numeric "
            "  else 0 end), 0) "
            "from refresh_runs "
            "where started_at > now() - make_interval(days => %s) "
            "group by 1 order by 1", (days,), fetch=True)
        return [_row(cols, r) for r in rows]
    return _safe("finance_daily", go) or []

# ── company registry (portal §3/§4; PG canonical, migration 002) ─────────────

_ORG_COLS = ["id", "name", "legal_name", "vat_number", "country",
             "contact_name", "contact_phone", "group_id"]
_CON_COLS = ["status", "start_date", "monthly_eur", "annual_eur",
             "billing_anchor", "notes"]


def companies_list() -> list[dict]:
    cols = _ORG_COLS + ["group_name"] + _CON_COLS
    def go():
        rows = _exec(
            "select o.id, o.name, o.legal_name, o.vat_number, o.country, "
            "o.contact_name, o.contact_phone, o.group_id, g.name, "
            "c.status, c.start_date, "
            "c.monthly_eur, c.annual_eur, c.billing_anchor, c.notes "
            "from organizations o "
            "left join groups g on g.id = o.group_id "
            "left join contracts c on c.org_id = o.id "
            "order by g.name nulls last, o.name", fetch=True)
        return [_row(cols, r) for r in rows]
    return _safe("companies_list", go) or []


def groups_list() -> list[dict]:
    cols = ["id", "name", "companies"]
    def go():
        rows = _exec(
            "select g.id, g.name, count(o.id) from groups g "
            "left join organizations o on o.group_id = g.id "
            "where g.active group by g.id, g.name order by g.name", fetch=True)
        return [_row(cols, r) for r in rows]
    return _safe("groups_list", go) or []


def upsert_group(name: str, slug: str, group_id: str | None = None) -> str | None:
    def go():
        if group_id:
            _exec("update groups set name = %s where id = %s", (name, group_id))
            return group_id
        rows = _exec("insert into groups (name, slug) values (%s, %s) "
                     "on conflict (slug) do update set name = excluded.name "
                     "returning id", (name, slug), fetch=True)
        return str(rows[0][0])
    return _safe("upsert_group", go)


def org_hotel_map() -> dict[str, str]:
    """hotel_id -> org_id."""
    def go():
        rows = _exec("select id, org_id from hotels", fetch=True)
        return {str(r[0]): (str(r[1]) if r[1] else "") for r in rows}
    return _safe("org_hotel_map", go) or {}


def upsert_company(org: dict) -> str | None:
    """Insert or update an organization; returns its id."""
    def go():
        if org.get("id"):
            sets = ", ".join(f"{c} = %s" for c in _ORG_COLS if c != "id")
            _exec(f"update organizations set {sets} where id = %s",
                  tuple(org.get(c) for c in _ORG_COLS if c != "id") + (org["id"],))
            return org["id"]
        rows = _exec(
            "insert into organizations (name, legal_name, vat_number, country, "
            "contact_name, contact_phone, group_id, slug) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s) returning id",
            (org.get("name"), org.get("legal_name"), org.get("vat_number"),
             org.get("country") or "GR", org.get("contact_name"),
             org.get("contact_phone"), org.get("group_id"),
             org.get("slug")), fetch=True)
        return str(rows[0][0])
    try:
        if not enabled():
            return None
        return go()
    except Exception as exc:  # surface VAT-unique etc. to the endpoint
        raise RuntimeError(str(exc)) from exc


def upsert_contract(org_id: str, f: dict) -> None:
    def go():
        _exec(
            "insert into contracts (org_id, status, start_date, monthly_eur, "
            "annual_eur, billing_anchor, notes, updated_at) "
            "values (%s,%s,%s,%s,%s,%s,%s, now()) "
            "on conflict (org_id) do update set status = excluded.status, "
            "start_date = excluded.start_date, monthly_eur = excluded.monthly_eur, "
            "annual_eur = excluded.annual_eur, billing_anchor = excluded.billing_anchor, "
            "notes = excluded.notes, updated_at = now()",
            (org_id, f.get("status") or "trial", f.get("start_date"),
             f.get("monthly_eur"), f.get("annual_eur"),
             f.get("billing_anchor"), f.get("notes")))
    _safe("upsert_contract", go)


def set_hotel_org(hotel_id: str, org_id: str) -> None:
    _safe("set_hotel_org", lambda: _exec(
        "update hotels set org_id = %s where id = %s", (org_id, hotel_id)))
