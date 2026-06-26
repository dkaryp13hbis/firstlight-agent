"""
Test the analyst with realistic dummy data.
Run: python test_analyst.py
"""
import json, sys, os
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, ".")
import config
config.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
config.HOTEL_NAME = "Grand Seaside Hotel"
config.TOTAL_ROOMS = 167

DUMMY_DATA = {
    "hotel_name": "Grand Seaside Hotel",
    "report_date": "2026-06-18",
    "yesterday": {
        "revenue": 12840,
        "revenueLY": 14200,
        "occupancy": 0.77,
        "occupancyLY": 0.84,
        "adr": 99.5,
        "adrLY": 101.0,
        "roomNights": 129,
        "roomNightsLY": 140,
        "arrivals": 38,
        "departures": 42,
        "stayovers": 89,
    },
    "mtd": {
        "month_name": "June",
        "revenue": 198400,
        "revenueLY": 181200,
        "occupancy": 0.81,
        "occupancyLY": 0.76,
        "adr": 146.0,
        "adrLY": 142.5,
        "roomNights": 2430,
        "roomNightsLY": 2280,
    },
    "pace": [
        {
            "month": "Jul 2026",
            "occ": 0.68,
            "stly": 0.71,
            "final": 0.89,
            "adr": 178.0,
            "adr_stly": 169.0,
            "adr_final_ly": 182.0,
            "rev_final_ly": 1_043_000,
            "rev_budget": 1_120_000,
        },
        {
            "month": "Aug 2026",
            "occ": 0.55,
            "stly": 0.72,
            "final": 0.94,
            "adr": 165.0,
            "adr_stly": 171.0,
            "adr_final_ly": 195.0,
            "rev_final_ly": 1_418_000,
            "rev_budget": 1_500_000,
        },
        {
            "month": "Sep 2026",
            "occ": 0.42,
            "stly": 0.38,
            "final": 0.71,
            "adr": 155.0,
            "adr_stly": 148.0,
            "adr_final_ly": 168.0,
            "rev_final_ly": 730_000,
            "rev_budget": 780_000,
        },
        {
            "month": "Oct 2026",
            "occ": 0.28,
            "stly": 0.31,
            "final": 0.62,
            "adr": 128.0,
            "adr_stly": 124.0,
            "adr_final_ly": 141.0,
            "rev_final_ly": 450_000,
            "rev_budget": 470_000,
        },
    ],
    "pickup": {
        "last1d": {"roomNights": 14, "revenue": 3_220},
        "last3d": {"roomNights": 38, "revenue": 8_740},
        "last7d": {"roomNights": 91, "revenue": 20_150},
        "cancellations1d": 9,
        "cancellationRevenue": 1_980,
        "cancellations7dAvg": 3.4,
        "topMonth": "Aug 2026",
        "topMonthNights": 42,
    },
    "topChannels": [
        {"name": "Booking.com", "rev": 312_000, "rev_stly": 268_000, "var": 0.164, "nights": 1_840},
        {"name": "Direct / Web", "rev": 198_000, "rev_stly": 241_000, "var": -0.179, "nights": 980},
        {"name": "Expedia",     "rev": 87_000,  "rev_stly": 74_000,  "var": 0.176, "nights": 490},
        {"name": "Tour Op",     "rev": 143_000, "rev_stly": 138_000, "var": 0.036, "nights": 870},
        {"name": "Corporate",   "rev": 62_000,  "rev_stly": 71_000,  "var": -0.127, "nights": 340},
    ],
    "next7days": [
        {"date": "2026-06-19", "dow": "Fri", "occ": 0.91, "adr": 148.0, "rev": 22_500},
        {"date": "2026-06-20", "dow": "Sat", "occ": 0.97, "adr": 162.0, "rev": 26_300},
        {"date": "2026-06-21", "dow": "Sun", "occ": 0.88, "adr": 134.0, "rev": 19_700},
        {"date": "2026-06-22", "dow": "Mon", "occ": 0.64, "adr": 118.0, "rev": 12_600},
        {"date": "2026-06-23", "dow": "Tue", "occ": 0.59, "adr": 112.0, "rev": 11_000},
        {"date": "2026-06-24", "dow": "Wed", "occ": 0.61, "adr": 115.0, "rev": 11_700},
        {"date": "2026-06-25", "dow": "Thu", "occ": 0.74, "adr": 128.0, "rev": 15_800},
    ],
}

if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    from briefing.analyst import generate_insights
    print("Calling Claude analyst...\n")
    result = generate_insights(DUMMY_DATA)

    print("=" * 70)
    print("EXECUTIVE SUMMARY")
    print("=" * 70)
    print(result.get("executive_summary", ""))
    print()

    for i, ins in enumerate(result.get("insights", []), 1):
        print("-" * 70)
        print(f"[{ins.get('type','').upper()}] #{ins.get('priority',i)} - {ins.get('title','')}")
        for kpi in ins.get("kpis", []):
            d = kpi.get("direction","")
            arrow = "UP" if d == "up" else ("DOWN" if d == "down" else "~")
            print(f"  [{arrow}] {kpi.get('label')}: {kpi.get('value')} / {kpi.get('sub','')}")
        for f in ins.get("findings", []):
            print(f"  . {f}")
        print(f"  -> {ins.get('action','')}")
        print()

    print("=" * 70)
    print(f"Total insights: {len(result.get('insights', []))}")
