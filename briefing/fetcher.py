"""
Backward-compatible shim — the fetch implementation moved to the Protel
adapter (db/adapters/protel_mssql/fetcher.py) as part of the multi-PMS
restructure. On-prem entry points (server.py, main.py) keep calling
fetch_briefing_data(conn), which builds the hotel context from local config
and delegates to the adapter.
"""

from typing import Any

import config
from db.adapters.protel_mssql.fetcher import fetch_snapshot


def fetch_briefing_data(conn) -> dict[str, Any]:
    return fetch_snapshot(conn, {
        "hotel_name":   config.HOTEL_NAME,
        "total_rooms":  config.TOTAL_ROOMS,
        "pms_hotel_id": config.HOTEL_ID,
    })
