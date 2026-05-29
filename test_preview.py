"""
Test template rendering + AI call using a fixed payload (no DB needed).
Run: python test_preview.py
"""

import webbrowser
from pathlib import Path
from briefing.analyst import generate_insights
from briefing.mailer import save_preview

# Last known good payload from the DB
SAMPLE_DATA = {
    "hotel_name": "Pomegranate Wellness Spa Hotel",
    "report_date": "Sunday, May 10, 2025",
    "generated_at": "07:30",
    "yesterday": {
        "revenue": 4567.0, "revenueLY": 8045.0,
        "roomNights": 15, "roomNightsLY": 34,
        "adr": 304.47, "adrLY": 236.62,
        "occupancy": 0.0893, "occupancyLY": 0.2024,
        "arrivals": 5, "departures": 22, "stayovers": 10, "inHouse": 15,
    },
    "mtd": {
        "revenue": 97300.0, "revenueLY": 143200.0,
        "roomNights": 457, "roomNightsLY": 555,
        "adr": 213.0, "adrLY": 258.0,
        "occupancy": 0.272, "occupancyLY": 0.330,
        "month_name": "May",
    },
    "pickup": {
        "last1d": {"roomNights": 32, "revenue": 17438.0},
        "last3d": {"roomNights": 175, "revenue": 76490.0},
        "last7d": {"roomNights": 420, "revenue": 195726.0},
        "topMonth": "Jun",
        "topMonthNights": 153,
        "cancellations1d": 8,
        "cancellationRevenue": 3200.0,
    },
    "pace": [
        {"month": "May", "occ": 0.128, "stly": 0.156, "final": 0.550, "status": "behind",
         "rev": 87000.0, "rev_stly": 121000.0, "rev_final": 480000.0,
         "rn": 380, "adr": 228.9, "adr_stly": 195.0},
        {"month": "Jun", "occ": 0.426, "stly": 0.196, "final": 0.810, "status": "ahead",
         "rev": 497000.0, "rev_stly": 210000.0, "rev_final": 1320000.0,
         "rn": 1327, "adr": 374.6, "adr_stly": 331.0},
        {"month": "Jul", "occ": 0.307, "stly": 0.163, "final": 0.880, "status": "ahead",
         "rev": 432000.0, "rev_stly": 190000.0, "rev_final": 1580000.0,
         "rn": 808, "adr": 534.7, "adr_stly": 478.0},
        {"month": "Aug", "occ": 0.114, "stly": 0.060, "final": 0.670, "status": "ahead",
         "rev": 165000.0, "rev_stly": 152000.0, "rev_final": 1850000.0,
         "rn": 291, "adr": 567.0, "adr_stly": 538.0},
        {"month": "Sep", "occ": 0.083, "stly": 0.065, "final": 0.650, "status": "ahead",
         "rev": 98000.0, "rev_stly": 95000.0, "rev_final": 820000.0,
         "rn": 209, "adr": 469.4, "adr_stly": 453.0},
        {"month": "Oct", "occ": 0.056, "stly": 0.012, "final": 0.380, "status": "ahead",
         "rev": 34000.0, "rev_stly": 8000.0, "rev_final": 310000.0,
         "rn": 141, "adr": 241.1, "adr_stly": 229.0},
    ],
    "topChannels": [
        {"name": "Mice",     "nights": 364, "pct": 0.399, "trend": "up"},
        {"name": "Direct",   "nights": 260, "pct": 0.285, "trend": "down"},
        {"name": "T.Os",     "nights": 122, "pct": 0.134, "trend": "up"},
        {"name": "O.T.As",   "nights": 93,  "pct": 0.102, "trend": "down"},
        {"name": "Complimentary", "nights": 66, "pct": 0.072, "trend": "up"},
        {"name": "Tompoulidis Apollon", "nights": 8, "pct": 0.009, "trend": "up"},
    ],
    "next7days": [
        {"date": "11 May", "dow": "Mon", "occ": 0.131, "rooms": 22, "rev": 6820.0, "adr": 310.0, "arrivals": 12},
        {"date": "12 May", "dow": "Tue", "occ": 0.083, "rooms": 14, "rev": 3920.0, "adr": 280.0, "arrivals": 8},
        {"date": "13 May", "dow": "Wed", "occ": 0.107, "rooms": 18, "rev": 5220.0, "adr": 290.0, "arrivals": 10},
        {"date": "14 May", "dow": "Thu", "occ": 0.095, "rooms": 16, "rev": 4680.0, "adr": 292.5, "arrivals": 9},
        {"date": "15 May", "dow": "Fri", "occ": 0.143, "rooms": 24, "rev": 8160.0, "adr": 340.0, "arrivals": 14},
        {"date": "16 May", "dow": "Sat", "occ": 0.185, "rooms": 31, "rev": 12400.0, "adr": 400.0, "arrivals": 18},
        {"date": "17 May", "dow": "Sun", "occ": 0.155, "rooms": 26, "rev": 9360.0, "adr": 360.0, "arrivals": 6},
    ],
}

print("[test] Calling Claude API for AI insights...")
ai = generate_insights(SAMPLE_DATA)
print(f"[test] Got executive_summary: {ai.get('executive_summary', '')[:80]}...")
print(f"[test] Got {len(ai.get('insights', []))} insights")

out = Path("preview.html")
save_preview(SAMPLE_DATA, ai, str(out))
webbrowser.open(out.resolve().as_uri())
print(f"[test] Preview saved and opened -> {out.resolve()}")
