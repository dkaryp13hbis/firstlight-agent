"""
Test the analyst with realistic dummy data.
Run: python test_analyst.py
"""
import json, sys, os
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, ".")
import config
config.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
config.HOTEL_NAME  = "Grand Seaside Hotel"
config.TOTAL_ROOMS = 167

# Build pickup_daily: last 14 days of daily net pickup for future stay months
# Simulate slowdown for August (z-score negative)
today = date.today()
_pickup_daily = []
for i in range(14):
    book_date = (today - timedelta(days=13 - i)).isoformat()
    # Jul picks: normal ~8/day; Aug picks: recently slowed from ~12 to 4
    jul_rn = 8 if i < 12 else 6
    aug_rn = 12 if i < 10 else 4   # slowing down in last 4 days
    _pickup_daily += [
        {"ref_date": book_date, "stay_month": 7, "stay_year": today.year,
         "net_rn": jul_rn, "net_rev": jul_rn * 162.0},
        {"ref_date": book_date, "stay_month": 8, "stay_year": today.year,
         "net_rn": aug_rn, "net_rev": aug_rn * 188.0},
    ]

# Build otb_by_date: next 90 days — simulate some soft dates in late August
_otb_by_date = []
for d in range(1, 91):
    stay = today + timedelta(days=d)
    # Soft: late August weekdays (days 35-45 approx)
    is_soft = 35 <= d <= 45 and stay.weekday() < 5
    is_hot  = d <= 7 and stay.weekday() >= 5   # next two weekends near full
    rn_stly = 140 if stay.weekday() >= 5 else 105
    if is_soft:
        rn_ty = int(rn_stly * 0.78)   # 22% below STLY → soft flag
    elif is_hot:
        rn_ty = min(167, int(rn_stly * 1.22))  # near-full → hot flag
    else:
        rn_ty = int(rn_stly * 0.97)
    _otb_by_date.append({
        "stay_date": stay.isoformat(),
        "rn_ty":     rn_ty,
        "rev_ty":    round(rn_ty * 175.0, 0),
        "rn_stly":   rn_stly,
        "rev_stly":  round(rn_stly * 178.0, 0),
    })

DUMMY_DATA = {
    "hotel_name": "Grand Seaside Hotel",
    "report_date": f"{today.strftime('%A, %B')} {today.day-1}, {today.year}",
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
        "inHouse": 127,
    },
    "mtd": {
        "month_name": today.strftime("%B"),
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
            "month": "Jul", "month_num": 7,
            "occ": 0.68, "stly": 0.71, "final": 0.89,
            "rn": 3522, "rn_stly": 3677, "rn_final_ly": 4608,
            "rev": 626_000, "rev_stly": 621_000, "rev_final": 839_000,
            "adr": 178.0, "adr_stly": 169.0, "adr_final_ly": 182.0,
            "status": "behind",
        },
        {
            "month": "Aug", "month_num": 8,
            "occ": 0.55, "stly": 0.72, "final": 0.94,
            "rn": 2846, "rn_stly": 3727, "rn_final_ly": 4864,
            "rev": 468_000, "rev_stly": 637_000, "rev_final": 948_000,
            "adr": 165.0, "adr_stly": 171.0, "adr_final_ly": 195.0,
            "status": "behind",
        },
        {
            "month": "Sep", "month_num": 9,
            "occ": 0.42, "stly": 0.38, "final": 0.71,
            "rn": 2100, "rn_stly": 1899, "rn_final_ly": 3549,
            "rev": 325_000, "rev_stly": 281_000, "rev_final": 597_000,
            "adr": 155.0, "adr_stly": 148.0, "adr_final_ly": 168.0,
            "status": "ahead",
        },
        {
            "month": "Oct", "month_num": 10,
            "occ": 0.28, "stly": 0.31, "final": 0.62,
            "rn": 1445, "rn_stly": 1599, "rn_final_ly": 3200,
            "rev": 185_000, "rev_stly": 198_000, "rev_final": 451_000,
            "adr": 128.0, "adr_stly": 124.0, "adr_final_ly": 141.0,
            "status": "behind",
        },
    ],
    "pickup": {
        "last1d": {"roomNights": 14, "revenue": 3_220},
        "last3d": {"roomNights": 38, "revenue": 8_740},
        "last7d": {"roomNights": 91, "revenue": 20_150},
        "cancellations1d": 9,
        "cancellations7d": 24,
        "cancellationRevenue": 1_980,
        "topMonth": "Aug",
        "topMonthNights": 42,
        "date1d": (today - timedelta(1)).strftime("%d/%m"),
        "date7d": f"{(today-timedelta(6)).strftime('%d/%m')}–{today.strftime('%d/%m')}",
    },
    "topChannels": [
        {"name": "Booking.com", "rev": 312_000, "rev_stly": 268_000, "var": 0.164, "nights": 1_840, "trend": "up"},
        {"name": "Direct / Web", "rev": 198_000, "rev_stly": 241_000, "var": -0.179, "nights": 980, "trend": "down"},
        {"name": "Expedia",     "rev": 87_000,  "rev_stly": 74_000,  "var": 0.176, "nights": 490, "trend": "up"},
        {"name": "Tour Op",     "rev": 143_000, "rev_stly": 138_000, "var": 0.036, "nights": 870, "trend": "up"},
        {"name": "Corporate",   "rev": 62_000,  "rev_stly": 71_000,  "var": -0.127, "nights": 340, "trend": "down"},
    ],
    "next7days": [
        {"date": (today+timedelta(1)).strftime("%d %b"), "dow": (today+timedelta(1)).strftime("%a"), "occ": 0.91, "rooms": 152, "adr": 148.0, "rev": 22_500, "arrivals": 28},
        {"date": (today+timedelta(2)).strftime("%d %b"), "dow": (today+timedelta(2)).strftime("%a"), "occ": 0.97, "rooms": 162, "adr": 162.0, "rev": 26_300, "arrivals": 21},
        {"date": (today+timedelta(3)).strftime("%d %b"), "dow": (today+timedelta(3)).strftime("%a"), "occ": 0.88, "rooms": 147, "adr": 134.0, "rev": 19_700, "arrivals": 18},
        {"date": (today+timedelta(4)).strftime("%d %b"), "dow": (today+timedelta(4)).strftime("%a"), "occ": 0.64, "rooms": 107, "adr": 118.0, "rev": 12_600, "arrivals": 14},
        {"date": (today+timedelta(5)).strftime("%d %b"), "dow": (today+timedelta(5)).strftime("%a"), "occ": 0.59, "rooms":  99, "adr": 112.0, "rev": 11_000, "arrivals": 12},
        {"date": (today+timedelta(6)).strftime("%d %b"), "dow": (today+timedelta(6)).strftime("%a"), "occ": 0.61, "rooms": 102, "adr": 115.0, "rev": 11_700, "arrivals": 11},
        {"date": (today+timedelta(7)).strftime("%d %b"), "dow": (today+timedelta(7)).strftime("%a"), "occ": 0.74, "rooms": 124, "adr": 128.0, "rev": 15_800, "arrivals": 17},
    ],
    # New fields for the two-layer analyst
    "pickup_daily":   _pickup_daily,
    "otb_by_date":    _otb_by_date,
    "current_month_remaining": {
        "rn_remaining_otb_ty":    480,
        "rev_remaining_otb_ty":   76_800,
        "rn_remaining_stly":      510,
        "rev_remaining_stly":     85_680,
        "rn_remaining_final_ly":  820,
        "rev_remaining_final_ly": 149_240,
    },
}

if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    # First show what the compute layer produces
    from briefing.analyst import _compute_signals
    computed = _compute_signals(DUMMY_DATA)
    print("=" * 70)
    print(f"COMPUTE LAYER: {len(computed['ranked'])} ranked / {len(computed['watchlist'])} watchlist")
    print("=" * 70)
    for c in computed["ranked"]:
        print(f"  [{c['tag']}] score={c['score']:.3f}  {c['title_hint']}")
    print()

    # Then run full pipeline including Claude narration
    from briefing.analyst import generate_insights
    print("Calling Claude for narration...\n")
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
            d = kpi.get("direction", "")
            arrow = "UP" if d == "up" else ("DOWN" if d == "down" else "~")
            print(f"  [{arrow}] {kpi.get('label')}: {kpi.get('value')} / {kpi.get('sub','')}")
        for f in ins.get("findings", []):
            print(f"  . {f}")
        print(f"  -> {ins.get('action','')}")
        print()

    print("=" * 70)
    print(f"Total insights: {len(result.get('insights', []))}")
