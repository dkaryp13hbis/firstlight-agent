"""
PMS adapter interface.

Every adapter is a package under db/adapters/<pms_type>/ exposing:

    fetch_snapshot(conn, hotel_ctx) -> dict

- `conn`: an open DB connection (pyodbc for SQL Server PMSs, oracledb for
  Opera/Fidelio) or an API client for cloud PMSs (Hotelizer). The adapter
  does not open or close connections — the caller owns the connection
  lifecycle (today: server.py/main.py on-prem; after the cloud migration:
  the Railway Tunnel Connection Manager).
- `hotel_ctx`: {"hotel_name": str, "total_rooms": int, "pms_hotel_id": Any}
  plus any adapter-specific keys. Identity is per-call — never global config
  — so one process can serve many hotels.
- Return value: a HotelDataSnapshot payload (see db/contract.py) with
  `data_quality` already attached. Queries MUST stay bounded (stateless
  windowed aggregates — no full-history reloads).

Registered adapters:
    protel_mssql   Protel on-prem (SQL Server / BiData)     — implemented
    pylon_mssql    Pylon on-prem (SQL Server)               — planned
    opera5_oracle  Opera 5 on-prem (Oracle)                 — planned
    fidelio_oracle Fidelio V8 on-prem (Oracle)              — planned
    hotelizer_api  Hotelizer cloud (REST)                   — planned
"""

from importlib import import_module

_REGISTRY = {
    "protel_mssql": "db.adapters.protel_mssql.fetcher",
}


def get_adapter(pms_type: str):
    """Return the adapter module for a PMS type. Raises for unknown types."""
    module_path = _REGISTRY.get(pms_type)
    if module_path is None:
        raise ValueError(
            f"Unknown pms_type '{pms_type}'. Registered: {sorted(_REGISTRY)}")
    return import_module(module_path)
