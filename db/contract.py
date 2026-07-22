"""
HotelDataSnapshot — the canonical data contract of FirstLight.

Every PMS adapter (Protel/SQL Server today; Opera, Fidelio, Pylon, Hotelizer
later) must produce a payload matching this contract. Everything downstream —
the analyst signals, the insight cards, the app — consumes ONLY this shape and
never knows which PMS produced it.

The payload is a plain JSON-serializable dict (it crosses HTTP boundaries), so
the contract is enforced at runtime by `build_data_quality()`, which returns a
`data_quality` block describing the snapshot:

    {
      "contract_version": "1.0",
      "missing_fields":  ["pickup_daily", "pace[].rn_stly", ...],
      "rows_fetched":    {"pace": 12, "otb_by_date": 90, ...},
      "legacy_mode":     false,   # True → signal fields absent, analyst uses legacy path
      "sanity":          {"yesterday_nonzero": true, ...},
      "complete":        true,    # False → do NOT publish this snapshot
      "freshness":       {"report_date": "...", "generated_at": "..."}
    }

Publication rule: `complete == False` payloads must never overwrite a
previously published briefing. `legacy_mode == True` payloads are publishable
but route the analyst to the legacy (non-signal) path.

── Contract fields ────────────────────────────────────────────────────────────

Required core (every adapter, every PMS):
  hotel_name          str
  report_date         str   (display string for the reported day = yesterday)
  yesterday           {revenue, revenueLY, roomNights, roomNightsLY, adr,
                       adrLY, occupancy (0..1), occupancyLY, arrivals,
                       departures, stayovers, inHouse}
  mtd                 {revenue, revenueLY, roomNights, roomNightsLY, adr,
                       adrLY, occupancy, occupancyLY, month_name}
  pickup              {last1d{roomNights,revenue}, last3d{...}, last7d{...},
                       cancellations1d, cancellations7d, ...}
  pace                [ {month, month_num, occ, stly, final, rn, rn_stly,
                         rn_final_ly, rev, rev_stly, rev_final, adr,
                         adr_stly, adr_final_ly, status} ]   (12 future months)
  topChannels         [ {name, rev, rev_stly, nights, ...} ]
  next7days           [ {date, dow, occ, rooms, adr, rev, arrivals} ]

Signal fields (power the v1.2 analyst; absent → legacy_mode):
  pickup_daily            [ {ref_date, stay_month, stay_year, net_rn, net_rev} ]
                          (last 14 days × future stay month)
  otb_by_date             [ {stay_date, rn_ty, rev_ty, rn_stly, rev_stly} ]
                          (next 90 days, STLY at same lead time)
  current_month_remaining {rn_remaining_otb_ty, rev_remaining_otb_ty,
                           rn_remaining_stly, rev_remaining_stly,
                           rn_remaining_final_ly, rev_remaining_final_ly}

Query windows (adapters MUST stay bounded — stateless aggregates, no full
history reloads): MTD; next 90 days by date; last 14 days pickup; 12 months
pace; current + last-year month remaining split.
"""

import calendar as _cal
from datetime import date as _date, timedelta as _timedelta
from typing import Any

CONTRACT_VERSION = "1.0"

# Top-level required fields and their expected container type
_REQUIRED_CORE: dict[str, type] = {
    "hotel_name":  str,
    "report_date": str,
    "yesterday":   dict,
    "mtd":         dict,
    "pickup":      dict,
    "pace":        list,
    "topChannels": list,
    "next7days":   list,
}

# Signal fields — absent/empty means the analyst must use the legacy path
_SIGNAL_FIELDS = ("pickup_daily", "otb_by_date", "current_month_remaining")

# Missing entries with these prefixes flag legacy_mode but never block
# publication — old-format payloads (pre-signal fetchers) stay publishable.
_SIGNAL_PREFIXES = _SIGNAL_FIELDS + ("pace[].rn_stly", "pace[].rn_final_ly")

_REQUIRED_YESTERDAY = ("revenue", "revenueLY", "roomNights", "roomNightsLY",
                       "adr", "adrLY", "occupancy", "occupancyLY")
_REQUIRED_MTD       = ("revenue", "revenueLY", "occupancy", "adr", "month_name")
_REQUIRED_PACE_ITEM = ("month_num", "occ", "stly", "final", "rn", "rn_stly",
                       "rn_final_ly", "rev", "rev_stly", "rev_final",
                       "adr", "adr_final_ly")
_REQUIRED_CM        = ("rn_remaining_otb_ty", "rev_remaining_otb_ty",
                       "rn_remaining_stly", "rev_remaining_stly",
                       "rn_remaining_final_ly", "rev_remaining_final_ly")


def _num(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_data_quality(data: dict[str, Any], total_rooms: int | None = None) -> dict[str, Any]:
    """Inspect a snapshot payload and return its data_quality block.

    Never raises — a broken payload yields complete=False, not an exception.
    """
    missing: list[str] = []
    sanity: dict[str, bool] = {}

    # ── Required core fields ──────────────────────────────────────────────
    for field, ftype in _REQUIRED_CORE.items():
        value = data.get(field)
        if value is None or not isinstance(value, ftype) or (ftype in (list, dict) and not value):
            missing.append(field)

    yd  = data.get("yesterday") or {}
    mtd = data.get("mtd") or {}
    for k in _REQUIRED_YESTERDAY:
        if k not in yd:
            missing.append(f"yesterday.{k}")
    for k in _REQUIRED_MTD:
        if k not in mtd:
            missing.append(f"mtd.{k}")

    pace = data.get("pace") or []
    if pace:
        first = pace[0] if isinstance(pace[0], dict) else {}
        for k in _REQUIRED_PACE_ITEM:
            if k not in first:
                missing.append(f"pace[].{k}")

    # ── Signal fields → legacy_mode ───────────────────────────────────────
    signal_missing = []
    for field in _SIGNAL_FIELDS:
        value = data.get(field)
        if not value:
            signal_missing.append(field)
            missing.append(field)
    cm = data.get("current_month_remaining") or {}
    if cm:
        for k in _REQUIRED_CM:
            if k not in cm:
                missing.append(f"current_month_remaining.{k}")
    legacy_mode = bool(signal_missing)

    # ── Row counts ────────────────────────────────────────────────────────
    rows_fetched = {
        field: len(data.get(field) or [])
        for field in ("pace", "pickup_daily", "otb_by_date", "topChannels", "next7days")
    }

    # ── Sanity checks ─────────────────────────────────────────────────────
    # Hard: a failure here means the snapshot must not be published.
    rev_yd = _num(yd.get("revenue"))
    rn_yd  = _num(yd.get("roomNights"))
    sanity["yesterday_nonzero"] = (rev_yd > 0 or rn_yd > 0)

    occ_values = [
        _num(yd.get("occupancy")), _num(yd.get("occupancyLY")),
        _num(mtd.get("occupancy")), _num(mtd.get("occupancyLY")),
    ] + [_num(p.get(k)) for p in pace if isinstance(p, dict) for k in ("occ", "stly", "final")]
    sanity["occupancy_bounds"] = all(0.0 <= v <= 1.05 for v in occ_values)

    sanity["no_negative_core"] = all(v >= 0 for v in (
        rev_yd, rn_yd, _num(mtd.get("revenue")), _num(mtd.get("roomNights")),
    ))

    # Remaining OTB must fit remaining capacity (spec v1.2 D1 sanity)
    if cm and total_rooms:
        today = _date.today()
        month_end = _date(today.year, today.month,
                          _cal.monthrange(today.year, today.month)[1])
        rem_days = max(1, (month_end - today).days + 1)
        sanity["remaining_fits_capacity"] = (
            _num(cm.get("rn_remaining_otb_ty")) <= total_rooms * rem_days
        )
    else:
        sanity["remaining_fits_capacity"] = True

    # Soft: logged but do not block publication.
    otb = data.get("otb_by_date") or []
    if otb and total_rooms:
        sanity["otb_within_capacity"] = all(
            _num(r.get("rn_ty")) <= total_rooms * 1.10 for r in otb if isinstance(r, dict)
        )
    else:
        sanity["otb_within_capacity"] = True

    pickup_daily = data.get("pickup_daily") or []
    if pickup_daily:
        cutoff = (_date.today() - _timedelta(days=15)).isoformat()
        sanity["pickup_window_bounded"] = all(
            str(r.get("ref_date", "")) >= cutoff for r in pickup_daily if isinstance(r, dict)
        )
    else:
        sanity["pickup_window_bounded"] = True

    # ── Verdict ───────────────────────────────────────────────────────────
    hard_checks = ("yesterday_nonzero", "occupancy_bounds", "no_negative_core",
                   "remaining_fits_capacity")
    core_missing = [m for m in missing
                    if not any(m.startswith(s) for s in _SIGNAL_PREFIXES)]
    complete = not core_missing and all(sanity[k] for k in hard_checks)

    return {
        "contract_version": CONTRACT_VERSION,
        "missing_fields":   missing,
        "rows_fetched":     rows_fetched,
        "legacy_mode":      legacy_mode,
        "sanity":           sanity,
        "complete":         complete,
        "freshness": {
            "report_date":  data.get("report_date", ""),
            "generated_at": data.get("generated_at", ""),
        },
    }


def attach_data_quality(data: dict[str, Any], total_rooms: int | None = None) -> dict[str, Any]:
    """Attach the data_quality block to a snapshot in place and return it."""
    data["data_quality"] = build_data_quality(data, total_rooms)
    return data["data_quality"]


def is_publishable(data: dict[str, Any], total_rooms: int | None = None) -> tuple[bool, str]:
    """Publication gate. Returns (ok, reason). Uses the payload's own
    data_quality if present (trusted from our fetcher), else computes one."""
    dq = data.get("data_quality") or build_data_quality(data, total_rooms)
    if dq.get("complete"):
        return True, "ok"
    failed = [k for k, v in (dq.get("sanity") or {}).items() if not v]
    core_missing = [m for m in (dq.get("missing_fields") or [])
                    if not any(m.startswith(s) for s in _SIGNAL_PREFIXES)]
    reasons = []
    if core_missing:
        reasons.append(f"missing: {', '.join(core_missing[:5])}")
    if failed:
        reasons.append(f"failed sanity: {', '.join(failed)}")
    return False, "; ".join(reasons) or "incomplete snapshot"
