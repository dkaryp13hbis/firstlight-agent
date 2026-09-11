"""Follow-up engine — advisor proposal §3, design settled with the user
2026-09-10: "one watchlist, two authors".

The WATCHLIST is the single follow-up mechanism. When the morning analyst
ships a month-performance card, the month is auto-added to the hotel's
watchlist as a hotel-level row (user_id NULL, source='firstlight').  Every
pipeline run (morning + the 5 intraday data-only refreshes) re-computes each
FirstLight item's gap and:

  - RESOLVES it after 2 consecutive runs-days at/above the recovery line
    (gap >= -2%): row deleted + a green closing line ships in FL Pulse
    ("watch_closures" on the ai_insights payload).
  - RETIRES it after 14 days without recovery: row deleted + one final
    honest note.  Re-flagging later creates a NEW episode (the analyst's
    novelty memory keeps the continuity wording).
  - Attaches follow-up meta to matching FL Pulse cards
    ("Flagged Mon · day 3 · gap 18% -> 11%").

Owner rows (source='user') are NEVER touched.  Caps: max 3 FirstLight items
per hotel.  Everything here is fail-open: any error logs and the briefing
publishes unaffected.  Schema-tolerant: until the watchlist columns exist
(docs/sql/2026-09-10_watch_followup.sql), every write fails quietly and the
feature is simply off.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta

log = logging.getLogger("followup")

FIRSTLIGHT_CAP = 3
RECOVERY_GAP = -2.0        # at/above this % vs same-time-last-year = recovered
CONFIRM_RUNS = 2           # consecutive DAYS at recovery before resolving
RETIRE_DAYS = 14           # stuck this long -> step back with a final note
ADD_GAP = -5.0             # a month this far behind earns auto-watching

_MON = ["jan", "feb", "mar", "apr", "may", "jun",
        "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


# ── pure helpers (unit-tested in test_followup.py) ──────────────────────────

def month_key_from_card_id(card_id: str | None) -> str | None:
    """'pace_oct_2026' -> '2026-10' (mirrors the app's monthKeyFromCardId)."""
    m = re.search(r"_(%s)_(\d{4})$" % "|".join(_MON), card_id or "", re.I)
    if not m:
        return None
    return f"{m.group(2)}-{_MON.index(m.group(1).lower()) + 1:02d}"


def month_gap(data: dict, key: str) -> float | None:
    """% gap vs same-time-last-year for a '2026-10' month key, from pace."""
    try:
        y, mnum = int(key[:4]), int(key[5:7])
    except (ValueError, IndexError):
        return None
    for p in data.get("pace") or []:
        if p.get("month_num") == mnum:
            stly = p.get("rn_stly") or 0
            if stly <= 0:
                return None
            return ((p.get("rn") or 0) - stly) / stly * 100.0
    return None


def month_title(key: str) -> str:
    try:
        return _MONTH_FULL[int(key[5:7]) - 1]
    except (ValueError, IndexError):
        return key


def decide(row: dict, gap: float | None, today: date) -> tuple[str, dict]:
    """One FirstLight row + today's gap -> (action, patch).
    action: 'resolve' | 'retire' | 'update' | 'skip'."""
    if gap is None:
        return "skip", {}
    flagged = row.get("flagged_date")
    age = (today - date.fromisoformat(flagged)).days if flagged else 0
    streak = int(row.get("resolve_streak") or 0)
    last_seen = row.get("last_gap_date")
    new_day = last_seen != str(today)          # streak counts DAYS, not runs
    if gap >= RECOVERY_GAP:
        streak = streak + 1 if new_day else max(streak, 1)
        if streak >= CONFIRM_RUNS:
            return "resolve", {}
    else:
        streak = 0
        if age >= RETIRE_DAYS:
            return "retire", {}
    return "update", {"last_gap": round(gap, 1), "resolve_streak": streak,
                      "last_gap_date": str(today)}


def closure_text(row: dict, gap: float | None, kind: str) -> str:
    title = month_title(row.get("key") or "")
    g = f"{abs(round(gap))}%" if gap is not None else "on track"
    if kind == "resolve":
        side = "ahead of" if (gap or 0) >= 0 else "behind"
        return (f"{title} has recovered — now {g} {side} last year. "
                f"No longer needs your attention.")
    first = row.get("first_gap")
    was = f" (was {abs(round(first))}% when flagged)" if first is not None else ""
    return (f"{title} is still {g} behind after two weeks{was}. "
            f"Stepping back — we'll flag it again if it worsens.")


# ── Supabase IO (service key; fail-open everywhere) ─────────────────────────

def _sb(method: str, path: str, params: dict | None = None, body=None):
    import requests
    url = os.getenv("SUPABASE_URL", "").rstrip("/") + "/rest/v1/" + path
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=representation"}
    r = requests.request(method, url, params=params or {}, json=body,
                         headers=h, timeout=10)
    if not r.ok:
        raise RuntimeError(f"{method} {path} {r.status_code}: {r.text[:160]}")
    return r.json() if r.text else []


_FL_SELECT = ("id,kind,key,label,source,flagged_date,first_gap,last_gap,"
              "last_gap_date,resolve_streak")


def update_followups(hotel_id: str, data: dict, ai: dict,
                     is_morning: bool) -> None:
    """Mutates ai in place (watch_closures + per-card follow_up) and keeps the
    hotel's FirstLight watchlist rows current.  Never raises."""
    today = date.today() - timedelta(days=0)   # run day; report_date is D-1
    from db import store as _store
    rows = _store.fl_watch_rows(hotel_id)
    if rows is None:
        try:
            rows = _sb("GET", "watchlist", {
                "hotel_id": f"eq.{hotel_id}", "source": "eq.firstlight",
                "select": _FL_SELECT,
            })
        except Exception as e:                  # both stores unreachable → off
            log.info(f"[followup] off: {e}")
            return

    closures: list[dict] = []
    open_by_key: dict[str, dict] = {}

    # 1 ── update / resolve / retire existing FirstLight rows
    for row in rows:
        if row.get("kind") != "month":
            continue
        gap = month_gap(data, row.get("key") or "")
        action, patch = decide(row, gap, today)
        try:
            if action in ("resolve", "retire"):
                _store.fl_watch_delete(row["id"])
                try:
                    _sb("DELETE", "watchlist", {"id": f"eq.{row['id']}"})
                except Exception:
                    pass   # echo
                closures.append({
                    "key": row.get("key"), "kind": action,
                    "title": month_title(row.get("key") or "").upper(),
                    "text": closure_text(row, gap, action),
                })
                log.info(f"[followup] {action}: {row.get('key')} gap={gap}")
            elif action == "update":
                _store.fl_watch_update(row["id"], patch)
                try:
                    _sb("PATCH", "watchlist", {"id": f"eq.{row['id']}"}, patch)
                except Exception:
                    pass   # echo
                open_by_key[row["key"]] = {**row, **patch}
            else:
                open_by_key[row["key"]] = row
        except Exception as e:
            log.warning(f"[followup] row {row.get('key')}: {e}")
            open_by_key[row["key"]] = row

    # 2 ── auto-add: month cards from THIS morning's analyst run
    if is_morning:
        try:
            existing = _store.fl_watch_keys(hotel_id)
            if existing is None:
                existing = _sb("GET", "watchlist", {
                    "hotel_id": f"eq.{hotel_id}", "kind": "eq.month",
                    "select": "key,source",
                })
            taken = {r.get("key") for r in existing}
            fl_count = sum(1 for r in existing if r.get("source") == "firstlight")
            for ins in (ai.get("insights") or []):
                key = month_key_from_card_id(ins.get("id"))
                if not key or key in taken or fl_count >= FIRSTLIGHT_CAP:
                    continue
                gap = month_gap(data, key)
                if gap is None or gap > ADD_GAP:
                    continue
                new_row = {
                    "hotel_id": hotel_id, "user_id": None,
                    "kind": "month", "key": key, "label": None,
                    "source": "firstlight", "flagged_date": str(today),
                    "first_gap": round(gap, 1), "last_gap": round(gap, 1),
                    "last_gap_date": str(today), "resolve_streak": 0,
                }
                _store.fl_watch_insert(new_row)
                try:
                    _sb("POST", "watchlist", None, new_row)
                except Exception:
                    pass   # echo
                taken.add(key)
                fl_count += 1
                open_by_key[key] = {"flagged_date": str(today),
                                    "first_gap": round(gap, 1),
                                    "last_gap": round(gap, 1)}
                log.info(f"[followup] auto-watch: {key} gap={gap:.1f}%")
        except Exception as e:
            log.warning(f"[followup] auto-add: {e}")

    # 3 ── attach follow-up meta to matching cards + ship closures
    for ins in (ai.get("insights") or []):
        key = month_key_from_card_id(ins.get("id"))
        row = open_by_key.get(key or "")
        if not row or not row.get("flagged_date"):
            continue
        day = (today - date.fromisoformat(row["flagged_date"])).days + 1
        if day <= 1:
            continue                            # first day needs no meta line
        ins["follow_up"] = {
            "flagged": row["flagged_date"], "day": day,
            "first_gap": row.get("first_gap"), "last_gap": row.get("last_gap"),
        }
    if closures:
        ai["watch_closures"] = (ai.get("watch_closures") or []) + closures
