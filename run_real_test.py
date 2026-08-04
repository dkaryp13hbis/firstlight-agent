"""
Test the new two-layer analyst with REAL data from both hotels.
Does NOT push to cloud, does NOT send email — output only.

Usage:
    # Pome via direct SQL (uses .env credentials):
    py -3 run_real_test.py --pome

    # Potidea via bridge URL:
    py -3 run_real_test.py --potidea --bridge-url https://potidea.hbis.io --bridge-secret <secret>

    # Both (Pome direct, Potidea via bridge):
    py -3 run_real_test.py --both --bridge-url https://potidea.hbis.io --bridge-secret <secret>
"""
import argparse
import io
import json
import sys
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

import config
from briefing.analyst import _compute_signals, generate_insights


def run_hotel(hotel_name: str, total_rooms: int, data: dict) -> None:
    config.HOTEL_NAME  = hotel_name
    config.TOTAL_ROOMS = total_rooms

    print()
    print("=" * 72)
    print(f"  {hotel_name.upper()}")
    print("=" * 72)

    # Layer A: compute signals
    computed = _compute_signals(data)
    ranked   = computed["ranked"]
    hl       = computed["headline"]

    print(f"\nHEADLINE ({hl['report_date']})")
    print(f"  Yesterday:  €{hl['yday_rev']:,.0f}  ({'+' if (hl['yday_rev_var_pct'] or 0) >= 0 else ''}{hl['yday_rev_var_pct'] or 'n/a'}% vs LY)")
    print(f"  MTD:        €{hl['mtd_rev']:,.0f}  occ {hl['mtd_occ_pct']}%  ADR €{hl['mtd_adr']:,.0f}")
    print(f"  Pickup yd:  +{hl['net_pickup_yday_rn']} rn  €{hl['net_pickup_yday_rev']:,.0f}   cancels {hl['cancellations_yday']}")

    print(f"\nCOMPUTE LAYER — {len(ranked)} ranked signals, {len(computed['watchlist'])} watchlist:")
    for i, c in enumerate(ranked, 1):
        print(f"  {i}. [{c['tag']:11s}] score={c['score']:.3f}  {c['title_hint']}")
    if not ranked:
        print("  (no signals above threshold — will use legacy analyst path)")

    print("\nCALLING CLAUDE FOR NARRATION...")
    ai = generate_insights(data)

    print(f"\nEXECUTIVE SUMMARY:\n  {ai.get('executive_summary','')}\n")
    for ins in ai.get("insights", []):
        tag    = ins.get("type", "").upper()
        title  = ins.get("title", "")
        print(f"[{tag}] {title}")
        for kpi in ins.get("kpis", []):
            d = {"up": "↑", "down": "↓"}.get(kpi.get("direction"), "~")
            print(f"  {d} {kpi.get('label')}: {kpi.get('value')}  ({kpi.get('sub','')})")
        for f in ins.get("findings", []):
            print(f"  • {f}")
        print(f"  → {ins.get('action','')}")
        print()

    print(f"  Total insights: {len(ai.get('insights', []))}")


def fetch_pome_direct() -> tuple[dict, int]:
    """Fetch Pome data via direct SQL connection."""
    from db.connection import get_connection
    from briefing.fetcher import fetch_briefing_data

    print("\nConnecting to Pome SQL server …")
    conn = get_connection()
    try:
        data = fetch_briefing_data(conn)
    finally:
        conn.close()

    yd = data.get("yesterday", {})
    cm = data.get("current_month_remaining", {})
    pd = data.get("pickup_daily", [])
    od = data.get("otb_by_date", [])
    print(f"  yd_rev=€{yd.get('revenue',0):,.0f}  occ={yd.get('occupancy',0)*100:.1f}%")
    print(f"  pickup_daily rows: {len(pd)}  |  otb_by_date rows: {len(od)}  |  current_month_remaining: {bool(cm)}")
    return data, config.TOTAL_ROOMS


def fetch_potidea_bridge(bridge_url: str, bridge_secret: str) -> tuple[dict, int]:
    """Fetch Potidea data via bridge URL."""
    from briefing.bridge_fetcher import fetch_from_bridge

    print(f"\nFetching from Potidea bridge: {bridge_url} …")
    data = fetch_from_bridge(bridge_url, bridge_secret)
    yd   = data.get("yesterday", {})
    pd   = data.get("pickup_daily", [])
    od   = data.get("otb_by_date", [])
    total_rooms = data.get("total_rooms", 112)  # fallback
    print(f"  yd_rev=€{yd.get('revenue',0):,.0f}  occ={yd.get('occupancy',0)*100:.1f}%")
    print(f"  pickup_daily rows: {len(pd)}  |  otb_by_date rows: {len(od)}")
    return data, total_rooms


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Real-data analyst test (no cloud push)")
    parser.add_argument("--pome",         action="store_true", help="Run Pome via direct SQL")
    parser.add_argument("--potidea",      action="store_true", help="Run Potidea via bridge")
    parser.add_argument("--both",         action="store_true", help="Run both hotels")
    parser.add_argument("--bridge-url",   default="", help="Potidea bridge URL")
    parser.add_argument("--bridge-secret", default="", help="Potidea bridge secret")
    args = parser.parse_args()

    if not any([args.pome, args.potidea, args.both]):
        # Default: run Pome only
        args.pome = True

    if args.pome or args.both:
        try:
            data, rooms = fetch_pome_direct()
            run_hotel("Pome Hotel", rooms, data)
        except Exception as e:
            import traceback
            print(f"\n[Pome] Error: {e}")
            traceback.print_exc()

    if (args.potidea or args.both) and args.bridge_url:
        try:
            data, rooms = fetch_potidea_bridge(args.bridge_url, args.bridge_secret)
            run_hotel("Potidea Palace", rooms, data)
        except Exception as e:
            import traceback
            print(f"\n[Potidea] Error: {e}")
            traceback.print_exc()
    elif (args.potidea or args.both) and not args.bridge_url:
        print("\n[Potidea] Skipped — provide --bridge-url and --bridge-secret")
        print("  Bridge URL format: https://potidea-data.hbis.io  (or whatever tunnel URL was set up)")
        print("  Secret: from BRIDGE_SECRET in Potidea's .env file on their server")
