"""
Protel / SQL Server adapter — fetch implementation.

fetch_snapshot(conn, hotel_ctx) runs the Protel query pack and returns a
HotelDataSnapshot payload (see db/contract.py). hotel_ctx identifies the
property per call — no global config — so one process can serve many hotels:

    hotel_ctx = {
        "hotel_name":   "Pome Hotel",
        "total_rooms":  167,
        "pms_hotel_id": 1,     # mpehotel in BiData
    }

The queries produce result rows; `db/adapters/assemble.py` turns them into
the payload (shared with every other adapter). All derived values (ADR,
occupancy %, variance) are calculated there.
"""

from typing import Any

import pyodbc

from db.adapters.protel_mssql import queries as Q
from db.adapters.assemble import assemble_snapshot

CONNECTOR = "mssql"


def _rows(cursor: pyodbc.Cursor) -> list[dict]:
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _first(cursor: pyodbc.Cursor) -> dict:
    rows = _rows(cursor)
    return rows[0] if rows else {}


# ------------------------------------------------------------------
# Main fetch
# ------------------------------------------------------------------

def fetch_snapshot(conn: pyodbc.Connection, hotel_ctx: dict[str, Any]) -> dict[str, Any]:
    cur = conn.cursor()
    hotel_id   = hotel_ctx["pms_hotel_id"]
    hotel_name = hotel_ctx["hotel_name"]

    rows: dict[str, Any] = {}

    # ── Core queries — a failure here blocks the briefing (data-level) ──
    cur.execute(Q.Q_KPIS, hotel_id, hotel_id, hotel_id)
    rows["kpi"] = _first(cur)

    cur.execute(Q.Q_INVENTORY, hotel_id)
    rows["inventory"] = _rows(cur)

    cur.execute(Q.Q_INHOUSE, hotel_id)
    rows["inhouse"] = _first(cur)

    cur.execute(Q.Q_PICKUP, hotel_id, hotel_id)
    rows["pickup"] = _first(cur)

    cur.execute(Q.Q_PACE, hotel_id, hotel_id, hotel_id)
    rows["pace"] = _rows(cur)

    cur.execute(Q.Q_SOURCES_OTB, hotel_id, hotel_id)
    rows["sources"] = _rows(cur)

    cur.execute(Q.Q_NEXT7, hotel_id)
    rows["next7"] = _rows(cur)

    cur.execute(Q.Q_BOOKING_CURVE, hotel_id, hotel_id, hotel_id, hotel_id)
    rows["curve"] = _rows(cur)

    cur.execute(Q.Q_BOOKING_CURVE_FULL_MONTHS, hotel_id, hotel_id)
    rows["curve_full"] = _rows(cur)

    cur.execute(Q.Q_PICKUP_DAILY, hotel_id, hotel_id)
    rows["pickup_daily"] = _rows(cur)

    cur.execute(Q.Q_OTB_BY_DATE_90, hotel_id, hotel_id)
    rows["otb_by_date"] = _rows(cur)

    cur.execute(Q.Q_CURRENT_MONTH_REMAINING, hotel_id)
    rows["current_month"] = _first(cur)

    # ── Optional signal queries — fail-open: never block a briefing ──
    optional = (
        ("lead_time", Q.Q_LEAD_TIME,          (hotel_id, hotel_id)),
        ("cancel_daily", Q.Q_CANCEL_DAILY,    (hotel_id,)),
        ("consumed", Q.Q_CONSUMED_BY_SOURCE,  (hotel_id, hotel_id)),
        ("pace_next", Q.Q_PACE_NEXT,          (hotel_id, hotel_id, hotel_id)),
    )
    for key, sql, params in optional:
        try:
            cur.execute(sql, *params)
            rows[key] = _rows(cur)
        except Exception as exc:  # noqa: BLE001
            rows[key] = []
            print(f"[{hotel_name}] {key} query failed (non-blocking): {exc}")

    return assemble_snapshot(rows, hotel_ctx)
