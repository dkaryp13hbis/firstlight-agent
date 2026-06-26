"""
Renders the email template using the last cached AI insights (ai_cache.json).
No API call — zero cost. Use this to preview template changes.

Run: python preview_cached.py
Then refresh preview.html in your browser.
"""

import json
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import config
config.HOTEL_NAME  = "Grand Seaside Hotel"
config.TOTAL_ROOMS = 167

from briefing.mailer import save_preview
from test_analyst import DUMMY_DATA

cache = Path("ai_cache.json")
if not cache.exists():
    print("[preview] No ai_cache.json found — run test_analyst.py first to generate one.")
    sys.exit(1)

ai = json.loads(cache.read_text(encoding="utf-8"))
print(f"[preview] Loaded {len(ai.get('insights', []))} insights from cache.")

# Patch DUMMY_DATA pickup to add fields the template may need
data = dict(DUMMY_DATA)
pu = dict(data.get("pickup", {}))
pu.setdefault("today",   pu.get("last1d", {"roomNights": 0, "revenue": 0}))
pu.setdefault("last24h", pu.get("last1d", {"roomNights": 0, "revenue": 0}))
pu.setdefault("cancellationsToday",       pu.get("cancellations1d", 0))
pu.setdefault("cancellations3d",          0)
pu.setdefault("cancellations7d",          0)
pu.setdefault("cancellationRevenueToday", pu.get("cancellationRevenue", 0))
pu.setdefault("cancellationRevenue3d",    0)
pu.setdefault("cancellationRevenue7d",    0)
pu.setdefault("date1d", "yesterday")
pu.setdefault("date3d", "3 days")
pu.setdefault("date7d", "7 days")
data["pickup"] = pu

# Patch pace items — add fields the template may need
import calendar as _cal
from datetime import datetime as _dt
patched_pace = []
for p in data.get("pace", []):
    p = dict(p)
    try:
        dt = _dt.strptime(p["month"], "%b %Y")
        days_in = _cal.monthrange(dt.year, dt.month)[1]
    except Exception:
        days_in = 30
    p.setdefault("rev",       p["occ"]   * config.TOTAL_ROOMS * days_in * p.get("adr", 150))
    p.setdefault("rev_stly",  p["stly"]  * config.TOTAL_ROOMS * days_in * p.get("adr_stly", p.get("adr", 150)))
    p.setdefault("rev_final", p.get("rev_final_ly", p["final"] * config.TOTAL_ROOMS * days_in * p.get("adr_final_ly", p.get("adr", 150))))
    p.setdefault("rn",        int(p["occ"] * config.TOTAL_ROOMS * days_in))
    p.setdefault("status",    "ahead" if p["occ"] >= p["stly"] else "behind")
    patched_pace.append(p)
data["pace"] = patched_pace

# Patch topChannels — add pct if missing
channels = data.get("topChannels", [])
total_nights = sum(c.get("nights", 0) for c in channels) or 1
for c in channels:
    c.setdefault("pct",      c.get("nights", 0) / total_nights)
    c.setdefault("rev_stly", c.get("rev_stly", 0))
    c.setdefault("var",      (c["rev"] - c["rev_stly"]) / c["rev_stly"] if c.get("rev_stly") else None)
    c.setdefault("trend",    "up" if (c.get("var") or 0) >= 0 else "down")

out = Path("preview.html")
save_preview(data, ai, str(out))
webbrowser.open(out.resolve().as_uri())
print(f"[preview] Opened -> {out.resolve()}")
