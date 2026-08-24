"""
Pushes the daily briefing to the FirstLight cloud API and sends push notifications.
Requires FIRSTLIGHT_API_URL and FIRSTLIGHT_API_KEY in .env.
For push notifications also requires VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY,
VAPID_EMAIL, SUPABASE_URL, and SUPABASE_SERVICE_KEY.
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Any

import requests

import config


def _kpi_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Compact per-day KPI snapshot stored on the briefing row — the payload
    of GET /briefing/history. Pure selection/summation, no derived math."""
    yd   = data.get("yesterday") or {}
    mtd  = data.get("mtd") or {}
    pace = [p for p in (data.get("pace") or []) if isinstance(p, dict)]
    pu   = data.get("pickup") or {}
    return {
        "yesterday": {k: yd.get(k) for k in
                      ("revenue", "revenueLY", "roomNights", "roomNightsLY",
                       "adr", "adrLY", "occupancy", "occupancyLY")},
        "mtd": {k: mtd.get(k) for k in
                ("revenue", "revenueLY", "roomNights", "occupancy", "adr",
                 "month_name")},
        "otb_year": {
            "rev":      sum(float(p.get("rev") or 0) for p in pace),
            "rev_stly": sum(float(p.get("rev_stly") or 0) for p in pace),
            "rn":       sum(int(p.get("rn") or 0) for p in pace),
        },
        "pickup_7d": {
            "rn":        (pu.get("last7d") or {}).get("roomNights"),
            "cancel_rn": pu.get("cancellations7d"),
        },
    }


def push_to_cloud(data: dict[str, Any], ai: dict[str, Any], rendered_html: str | None = None,
                  hotel_id: str | None = None, source_run_id: str | None = None,
                  notify: bool = True) -> bool:
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if hotel_id is None:
        hotel_id = os.getenv("SUPABASE_HOTEL_ID", "")

    if not all([supabase_url, supabase_key, hotel_id]):
        print("[cloud] Skipped — SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_HOTEL_ID not set.")
        return False

    yesterday = date.today() - timedelta(days=1)

    # Never persist analyst audit metadata into the customer-facing briefing
    ai = {k: v for k, v in ai.items() if k != "_meta"}

    payload = {
        "hotel_id":     hotel_id,
        "report_date":  str(yesterday),
        "data":         data,
        "ai_insights":  ai,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        # Compact per-day KPI snapshot for GET /briefing/history (Phase A).
        # Schema-tolerant: stripped and retried if the column doesn't exist.
        "kpi_summary":  _kpi_summary(data),
    }
    # JSON is canonical; HTML is only stored if a caller explicitly provides it
    if rendered_html is not None:
        payload["rendered_html"] = rendered_html
    if source_run_id is not None:
        payload["source_run_id"] = source_run_id

    try:
        headers = {
            "apikey":        supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates,return=minimal",
        }
        url = f"{supabase_url}/rest/v1/briefings?on_conflict=hotel_id,report_date"
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code == 400 and "kpi_summary" in resp.text:
            payload.pop("kpi_summary", None)
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        print(f"[cloud] Pushed briefing for {yesterday} (hotel {hotel_id[:8]}…) -> HTTP {resp.status_code}")
        if notify:
            _send_push_notifications(ai, hotel_id,
                                     hotel_name=data.get("hotel_name") or config.HOTEL_NAME,
                                     data=data)
        else:
            print("[cloud] Push notifications suppressed (manual/data-only run).")
        return True
    except requests.RequestException as exc:
        print(f"[cloud] Push failed: {exc}")
        return False


def _send_push_notifications(ai: dict[str, Any], hotel_id: str, hotel_name: str | None = None,
                             data: dict[str, Any] | None = None) -> None:
    """Morning-briefing push: deterministic headline body (no AI phrasing)."""
    hotel_name = hotel_name or config.HOTEL_NAME or "Hotel"
    title = f"{hotel_name} · Morning Briefing"[:80]
    body = ""
    if data:
        try:
            from briefing.intraday import headline
            body = headline(data)
        except Exception as exc:  # noqa: BLE001 — never block the morning push
            print(f"[push] headline failed, falling back: {exc}")
    if not body:
        insights = ai.get("insights", [])
        body = (insights[0].get("title") if insights
                else (ai.get("executive_summary") or "Your morning briefing is ready."))[:140]
    send_typed_push(hotel_id, hotel_name, title, body, "morning", section_id="sec-ai",
                    title_is_full=True)


def send_typed_push(hotel_id: str, hotel_name: str, title: str, body: str,
                    ntype: str, section_id: str = "sec-ai",
                    title_is_full: bool = False) -> None:
    """Send one typed Web Push to every subscription of the hotel whose
    notification_prefs allow `ntype` (morning | alerts | momentum). Missing
    prefs column/value = all types on (schema-tolerant)."""
    vapid_private     = os.getenv("VAPID_PRIVATE_KEY", "")
    vapid_email       = os.getenv("VAPID_EMAIL", "mailto:dk@bi-automations.com")
    supabase_url      = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key      = os.getenv("SUPABASE_SERVICE_KEY", "")
    supabase_hotel_id = hotel_id or os.getenv("SUPABASE_HOTEL_ID", "")

    if not all([vapid_private, supabase_url, supabase_key, supabase_hotel_id]):
        print("[push] Skipped — VAPID_PRIVATE_KEY / SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_HOTEL_ID not set.")
        return

    # If VAPID_PRIVATE_KEY is a file path that doesn't exist (e.g. Windows path on Linux),
    # treat the value as PEM content and write it to a temp file.
    if not os.path.exists(vapid_private):
        import tempfile
        pem_content = vapid_private.replace("\\n", "\n")
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
        tmp.write(pem_content)
        tmp.close()
        vapid_private = tmp.name

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("[push] pywebpush not installed — run: pip install pywebpush")
        return

    if not title_is_full:
        title = f"{hotel_name} · {title}"[:80]
    pwa_url = os.getenv("PWA_URL", "https://app.hbis.io")
    push_payload = json.dumps({"title": title, "body": body[:180],
                               "sectionId": section_id, "url": pwa_url})

    # Fetch subscriptions (+ per-type prefs; column may not exist yet)
    try:
        params = {"hotel_id": f"eq.{supabase_hotel_id}",
                  "select": "subscription,notification_prefs"}
        r = requests.get(
            f"{supabase_url}/rest/v1/push_subscriptions", params=params,
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        if r.status_code == 400 and "notification_prefs" in r.text:
            params["select"] = "subscription"
            r = requests.get(
                f"{supabase_url}/rest/v1/push_subscriptions", params=params,
                headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
                timeout=10,
            )
        r.raise_for_status()
        subscriptions = r.json()
    except Exception as exc:
        print(f"[push] Failed to fetch subscriptions: {exc}")
        return

    if not subscriptions:
        print("[push] No push subscriptions registered.")
        return

    sent = 0
    for row in subscriptions:
        sub_info = row.get("subscription")
        if not sub_info:
            continue
        prefs = row.get("notification_prefs") or {}
        if prefs.get(ntype) is False:
            continue
        try:
            webpush(
                subscription_info=sub_info,
                data=push_payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": vapid_email},
            )
            sent += 1
        except WebPushException as exc:
            if exc.response is not None and exc.response.status_code == 410:
                print(f"[push] Subscription expired (410) — consider pruning.")
            else:
                print(f"[push] WebPush error: {exc}")
        except Exception as exc:
            print(f"[push] Error sending to subscription: {exc}")

    print(f"[push] Sent {sent}/{len(subscriptions)} {ntype} notifications.")
