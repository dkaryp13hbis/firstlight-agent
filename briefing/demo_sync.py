"""
Demo mirror sync (2026-08-16): clones the latest briefing of each real hotel
onto its anonymized demo twin, applying the name replacements. Runs daily
after the morning pipeline (scheduled in api.py) — FAIL-OPEN: any error is
logged and swallowed; the real pipeline must never notice.
"""
import os

import requests

# real hotel id -> (demo hotel id)
DEMO_MAP = {
    "0b83ecdc-5216-4c53-ba96-3ddb67e1e253": "beaed5c5-3af6-4dde-b0b9-ade606951978",  # Pome -> Azure Bay
    "08b4b6f3-ce6d-4b7d-ba02-e48aec3d213f": "18dd1171-f66f-401c-af77-3132bb9b52a4",  # Potidea -> Thalassa
}

# ORDER MATTERS: full phrases before their substrings.
REPLACEMENTS = [
    ("Pomegranate Wellness Spa Hotel", "Azure Bay Resort"),
    ("Pomegranate", "Azure Bay"),
    ("Potidea Palace", "Thalassa Palace Resort"),
    ("Potidea", "Thalassa"),
    ("Tompoulidis Apollon", "Partner Agency"),
    ("Tompoulidis", "Partner"),
    ("Apollon", "Agency"),
]


def sync_demo_briefings() -> None:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    for src, dst in DEMO_MAP.items():
        try:
            r = requests.get(
                f"{url}/rest/v1/briefings",
                params={"hotel_id": f"eq.{src}", "order": "report_date.desc", "limit": "1",
                        "select": "report_date,generated_at,data,ai_insights,kpi_summary,rendered_html"},
                headers=headers, timeout=30,
            )
            r.raise_for_status()
            raw = r.text
            for a, b in REPLACEMENTS:
                raw = raw.replace(a, b)
            row = raw.strip().lstrip("[").rstrip("]").strip()
            if not row:
                continue
            row = row[:1] + f'"hotel_id":"{dst}",' + row[1:]
            w = requests.post(
                f"{url}/rest/v1/briefings?on_conflict=hotel_id,report_date",
                data=row.encode("utf-8"),
                headers={**headers, "Content-Type": "application/json",
                         "Prefer": "resolution=merge-duplicates,return=minimal"},
                timeout=30,
            )
            w.raise_for_status()
            print(f"[demo-sync] {src[:8]}… -> {dst[:8]}… OK")
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            print(f"[demo-sync] {src[:8]}… failed (non-blocking): {exc}")
