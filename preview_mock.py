"""
Renders the email template with the sample data from the JSX mockup.
Run: python preview_mock.py
Then open preview.html in your browser.
"""

import webbrowser
from pathlib import Path
from briefing.mailer import save_preview

MOCK_DATA = {
    "hotel_name": "Pomegranate Wellness Spa Hotel",
    "report_date": "Sunday, May 10, 2026",
    "generated_at": "07:38",

    "yesterday": {
        "revenue":      31200,
        "revenueLY":    28400,
        "roomNights":   35,
        "roomNightsLY": 31,
        "adr":          891,
        "adrLY":        916,
        "occupancy":    0.10,
        "occupancyLY":  0.089,
        "arrivals":     8,
        "departures":   5,
        "stayovers":    22,
        "inHouse":      30,
    },

    "mtd": {
        "revenue":      102389,
        "revenueLY":    225989,
        "roomNights":   354,
        "roomNightsLY": 751,
        "adr":          289,
        "adrLY":        301,
        "occupancy":    0.07,
        "occupancyLY":  0.148,
        "month_name":   "May",
    },

    "pickup": {
        "last24h":             {"roomNights": 18, "revenue": 5940},
        "last7d":              {"roomNights": 93, "revenue": 34410},
        "topMonth":            "June",
        "topMonthNights":      42,
        "cancellations24h":    2,
        "cancellationRevenue": 1580,
    },

    "pace": [
        {"month": "May", "occ": 0.07,  "stly": 0.148, "final": 0.22, "status": "behind"},
        {"month": "Jun", "occ": 0.26,  "stly": 0.15,  "final": 0.66, "status": "ahead"},
        {"month": "Jul", "occ": 0.16,  "stly": 0.09,  "final": 0.54, "status": "ahead"},
        {"month": "Aug", "occ": 0.05,  "stly": 0.05,  "final": 0.67, "status": "on_track"},
        {"month": "Sep", "occ": 0.05,  "stly": 0.12,  "final": 0.65, "status": "behind"},
        {"month": "Oct", "occ": 0.07,  "stly": 0.05,  "final": 0.61, "status": "ahead"},
    ],

    "topChannels": [
        {"name": "Direct / Reservations", "nights": 1013, "pct": 0.28, "trend": "up"},
        {"name": "Webhotelier",            "nights": 791,  "pct": 0.22, "trend": "up"},
        {"name": "Luxury Travel DMC",      "nights": 203,  "pct": 0.06, "trend": "up"},
        {"name": "Booking.com",            "nights": 194,  "pct": 0.05, "trend": "down"},
    ],

    "next7days": [
        {"date": "11 May", "dow": "Mon", "occ": 0.08, "rooms": 28, "rev": 7840,  "arrivals": 6},
        {"date": "12 May", "dow": "Tue", "occ": 0.07, "rooms": 25, "rev": 6750,  "arrivals": 3},
        {"date": "13 May", "dow": "Wed", "occ": 0.04, "rooms": 14, "rev": 3780,  "arrivals": 2},
        {"date": "14 May", "dow": "Thu", "occ": 0.02, "rooms":  7, "rev": 1890,  "arrivals": 1},
        {"date": "15 May", "dow": "Fri", "occ": 0.04, "rooms": 14, "rev": 4200,  "arrivals": 4},
        {"date": "16 May", "dow": "Sat", "occ": 0.03, "rooms": 11, "rev": 3190,  "arrivals": 3},
        {"date": "17 May", "dow": "Sun", "occ": 0.01, "rooms":  4, "rev": 1080,  "arrivals": 1},
    ],
}

MOCK_AI = {
    "summary": (
        "May is tracking significantly behind last year at 7% occupancy vs 14.8% STLY, "
        "with MTD revenue at €102k vs €226k LY — a 54.7% shortfall. "
        "June is the bright spot, with OTB up 73% vs same time last year and strong pickup momentum."
    ),
    "suggestion": (
        "Activate a flash mid-week rate for May 13–14 (currently 2–4% occupancy) "
        "and consider tightening discounts for June to protect the strong ADR."
    ),
    "alerts": [
        {
            "type": "warning",
            "title": "May pacing significantly behind",
            "detail": (
                "May OTB at 7% vs 14.8% STLY — 52.7% behind with 21 days remaining. "
                "MTD revenue of €102k is less than half last year's €226k. "
                "Consider flash promotions or opening restricted rates immediately."
            ),
        },
        {
            "type": "positive",
            "title": "June demand surging +73% vs STLY",
            "detail": (
                "June OTB at 26% vs 15% same time last year, with 42 room nights picked up "
                "in the last 7 days alone. "
                "Consider tightening discounts and raising BAR for peak June dates."
            ),
        },
        {
            "type": "info",
            "title": "Direct bookings overtaking OTAs",
            "detail": (
                "Direct / Reservations is now the top channel at 28% share (1,013 nights YTD), "
                "up from 17% last year. "
                "Booking.com has dropped from 26% to 5% — net commission savings are significant."
            ),
        },
        {
            "type": "warning",
            "title": "Mid-week occupancy near zero",
            "detail": (
                "Wed–Thu next week are at 2–4% occupancy (7–14 rooms). "
                "Last year similar patterns were closed by activating a 2-night minimum OTA rate. "
                "Act before Thursday to capture any remaining demand."
            ),
        },
    ],
}


if __name__ == "__main__":
    out = Path("preview.html")
    save_preview(MOCK_DATA, MOCK_AI, str(out))
    webbrowser.open(out.resolve().as_uri())
    print(f"[preview] Opened in browser → {out.resolve()}")
