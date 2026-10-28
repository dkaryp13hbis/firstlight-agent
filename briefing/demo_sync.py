"""
Demo mirror sync: clones the briefings of real hotels onto their anonymized
demo twins. Runs after the morning pipeline and after the 17:00 data-only run
(scheduled in api.py) — FAIL-OPEN: any error is logged and swallowed; the real
pipeline must never notice.

v2 (2026-10-28, demo switched to the Tor group twins — user: "lets switch the
demo to the tor hotel group, so we will have both multi and standalone
properties with full data"):
- twins are DATA, not code: every hotel row whose pms_config carries
  "demo_of": "<real hotel id>" and "replace": [[real text, demo text], ...]
  (docs/sql/pg/2026-10-28_demo_tor_twins.sql). Twins stay active=false so the
  scheduler never fetches them; db/store treats them as visible.
- Postgres in and out (the app reads PG first since STORAGE=pg; v1 wrote to
  Supabase only, which the app no longer reads).
- keeps the last HISTORY_DAYS report days, so the day strip, "since
  yesterday" and the watchlist trend work from the first sync. Days the twin
  already has are skipped, except the newest one (the 17:00 data-only run
  refreshes it).
"""
import json
import re
from datetime import date, timedelta

HISTORY_DAYS = 14
_COLS = ["report_date", "data", "ai_insights", "kpi_summary"]


def anonymize(text: str, pairs: list[tuple[str, str]]) -> str:
    """ONE pass, longest phrase first: 'City Hotel Thessaloniki' wins over
    'City Hotel', and a demo name that contains a real one ('Aurora City
    Hotel') is never rewritten again (chained str.replace did exactly that)."""
    table = {real: demo for real, demo in pairs if real}
    if not table:
        return text
    pattern = re.compile("|".join(re.escape(k) for k in sorted(table, key=len, reverse=True)))
    return pattern.sub(lambda m: table[m.group(0)], text)


def twin_row(src: dict, twin: dict) -> dict:
    """One source briefing row → the twin's row (ids and names swapped)."""
    pairs = list(twin["replace"]) + [(twin["source_id"], twin["id"])]
    body = {c: src.get(c) for c in _COLS if c != "report_date"}
    raw = anonymize(json.dumps(body, ensure_ascii=False, default=str), pairs)
    row = json.loads(raw)
    row["hotel_id"] = twin["id"]
    row["report_date"] = str(src["report_date"])[:10]
    return row


def sync_demo_briefings() -> None:
    from db import store
    twins = store.demo_twins()
    if not twins:
        return
    today = date.today()
    since = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    before = (today + timedelta(days=1)).isoformat()
    for twin in twins:
        try:
            rows = store.get_briefings_since(twin["source_id"], since, before, _COLS)
            if not rows:
                continue
            have = set(store.get_briefing_dates(twin["id"], HISTORY_DAYS + 7))
            newest = max(str(r["report_date"])[:10] for r in rows)
            n = 0
            for src in rows:
                rd = str(src["report_date"])[:10]
                if rd in have and rd != newest:
                    continue
                store.upsert_briefing(twin_row(src, twin))
                n += 1
            print(f"[demo-sync] {twin['source_id'][:8]}… -> {twin['id'][:8]}… "
                  f"{n} day(s) written, newest {newest}")
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            print(f"[demo-sync] {twin['id'][:8]}… failed (non-blocking): {exc}")
