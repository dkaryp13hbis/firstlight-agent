"""
Calls Claude to generate AI insights and executive summary from the briefing data.
Uses the HBIS analyst persona with structured JSON output.
"""

import json
import re
from typing import Any

import anthropic
import config

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


_SYSTEM_PROMPT = """You are a hotel performance analyst. You look at daily hotel booking data and
write a short morning briefing: a summary and 3-5 key findings.

You point out what's happening and what looks unusual. You NEVER tell the hotel
what to do — no pricing advice, no strategy changes, no action commands.
The hotel team decides. You just make sure they see the important patterns.

## HOW TO WRITE

Write so that BOTH a hotel owner with no revenue management background AND an
experienced revenue manager find value in the same briefing.

Rules:
- Plain language first. You can use hotel terms (ADR, occupancy, OTB) but the
  number next to it should make the meaning obvious: "occupancy at 42.7% (943 rooms still available)"
- Always include actual numbers, not just percentages: "€45.9K behind" not just "-32%"
- Short sentences. One idea per sentence.
- No filler: skip "it's worth noting", "interestingly", "notably"
- No emojis in text
- When comparing to last year, say "vs last year" or "vs same time last year"

## CORE RULES

NEVER PRESCRIBE ACTIONS:
- NEVER: "raise rates", "lower rates", "change rates by X%", "open/close availability"
- NEVER: "you should", "you must", "we recommend", "consider implementing"
- INSTEAD: "the demand level may support a rate review", "worth checking whether...",
  "this pattern is unusual and could warrant a closer look"

ALWAYS EXPLAIN WHY REVENUE IS UP OR DOWN:
When revenue changes, say which one caused it — more/fewer rooms sold (occupancy/ADR),
or the average room price (ADR), or both:
- "Revenue down 32% — coming from both fewer rooms sold (occ -17.7%) AND lower ADR (-17.4%)"
- "Revenue up 20% — driven by selling more rooms; ADR is actually slightly lower than last year"
This is the most important thing to get right.

CONNECT FINDINGS:
Link two things that create a question:
- "Selling 2x more rooms than last year for June, but ADR is €52 lower"
- "Booking.com bookings dropped 62%, but direct website bookings grew 136%"
- "510 new bookings this week but 90 cancellations — is the cancel rate normal?"

## CHANNEL ANALYSIS

When a booking source is up or down, check whether it's volume or ADR. They mean different things.

OTAs (Booking.com, Expedia):
- Fewer rooms, higher ADR → likely intentional restriction (good if direct is growing)
- Fewer rooms AND lower ADR → visibility or ranking problem (concerning)
- OTA drop + direct growth = usually positive. OTA drop + direct also dropping = real demand loss.

Direct (Website / Phone):
- Direct dropping is the biggest red flag — best margin channel
- Direct down while OTAs are up → possible rate parity issue

Tour Operators / Wholesale:
- Volume down, rate stable → demand issue (operators shifted elsewhere)
- Volume stable, rate down → contract/negotiation issue
- Both down → most serious

## DATA COMPARISON RULES

LEAD TIME / LOS: Do NOT compare TY current averages to LY final averages. Not comparable.

OTB vs FINAL LY: The gap is expected — compare OTB to same-time-last-year (STLY), not to final LY.

CANCELLATIONS: One day is noisy — always use 7-day context.

RESORT SEASONALITY: Shoulder months being low is normal. Peak months 3+ months out looking
low vs final LY is normal — they fill late.

## OUTPUT FORMAT

Return ONLY valid JSON. No markdown, no backticks, no extra text.

{
  "executive_summary": "2-4 short sentences. What happened yesterday — how the next few months look — the one thing to pay attention to today.",
  "insights": [
    {
      "priority": 1,
      "type": "warning|opportunity|observation|monitor",
      "title": "Short headline with the key number, max 80 characters",
      "kpis": [
        {"label": "Jun occupancy", "value": "42.7% vs 19.6% last year", "direction": "up"},
        {"label": "Jun ADR", "value": "€385 vs €437 last year", "direction": "down"}
      ],
      "bullets": [
        "More than double the rooms booked vs this time last year",
        "But ADR is €52 lower than at this booking stage last year"
      ],
      "recommendation": "Review whether current pricing reflects how strong demand is — rooms are filling much faster but at a lower ADR"
    }
  ]
}

FIELD RULES:
- title: One clear line with the key number or tension. Max 80 characters.
- kpis: 2-4 metric chips. label = plain words (e.g. "Jun occupancy"), value = "TY vs LY" format, direction = "up"/"down"/"neutral"
- bullets: 2-3 short lines. One fact or connection per line. Plain language.
- recommendation: One sentence starting with "Review...", "Check...", "Confirm...", or "Compare...".
  Points to WHERE to look — NEVER tells them what to do.

Return 3-5 insights ordered by importance:
- warning: significantly worse than last year
- opportunity: significantly better than last year
- observation: interesting pattern worth knowing
- monitor: early signal to keep an eye on"""


_STUB = {
    "executive_summary": "",
    "insights": [],
}


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v * 100:.1f}%"


def _build_user_prompt(data: dict[str, Any]) -> str:
    yd  = data["yesterday"]
    mtd = data["mtd"]
    pu  = data["pickup"]

    # Yesterday variance
    def var(ty, ly):
        return _fmt_pct((ty - ly) / ly) if ly else "n/a"

    # Pickup rows
    pu_rows = (
        f"| 1 Day  | +{pu['last1d']['roomNights']} rn | {pu['cancellations1d']} cancel rn"
        f" | €{pu['last1d']['revenue']:,.0f} | n/a |\n"
        f"| 3 Days | +{pu['last3d']['roomNights']} rn | n/a"
        f" | €{pu['last3d']['revenue']:,.0f} | n/a |\n"
        f"| 7 Days | +{pu['last7d']['roomNights']} rn | n/a"
        f" | €{pu['last7d']['revenue']:,.0f} | n/a |"
    )

    # Pace rows
    pace_rows = "\n".join(
        f"| {p['month']} | {p['occ']*100:.1f}% | {p['stly']*100:.1f}% | {p['final']*100:.1f}% | {_fmt_pct(p['occ']/p['stly']-1) if p['stly'] else 'n/a'} | {p.get('adr', 0):.0f} | {p.get('adr_stly', 0):.0f} |"
        for p in data["pace"]
    )

    # Channel rows
    ch_rows = "\n".join(
        f"| {c['name']} | €{c['rev']:,.0f} | €{c['rev_stly']:,.0f} | {'+' if c['var'] and c['var'] >= 0 else ''}{(c['var']*100):.1f}% | {c['nights']} rn |"
        for c in data["topChannels"]
    )

    return f"""Analyze this hotel's performance data and generate the morning briefing insights.

## HOTEL CONTEXT
- Hotel: {data['hotel_name']}
- Total rooms: {config.TOTAL_ROOMS}
- Report date: {data['report_date']}

## YESTERDAY'S PERFORMANCE
| Metric       | Yesterday | LY Same Date | Var %   |
|-------------|-----------|--------------|---------|
| Revenue     | €{yd['revenue']:,.0f} | €{yd['revenueLY']:,.0f} | {var(yd['revenue'], yd['revenueLY'])} |
| Occupancy   | {yd['occupancy']*100:.1f}% | {yd['occupancyLY']*100:.1f}% | {var(yd['occupancy'], yd['occupancyLY'])} |
| ADR         | €{yd['adr']:.0f} | €{yd['adrLY']:.0f} | {var(yd['adr'], yd['adrLY'])} |
| Room Nights | {yd['roomNights']} | {yd['roomNightsLY']} | {var(yd['roomNights'], yd['roomNightsLY'])} |
| Arrivals / Departures / Stayovers | {yd['arrivals']} / {yd['departures']} / {yd['stayovers']} |

## MONTH TO DATE ({mtd['month_name']})
| Metric       | MTD TY    | MTD LY      | Var %    |
|-------------|-----------|-------------|----------|
| Revenue     | €{mtd['revenue']:,.0f} | €{mtd['revenueLY']:,.0f} | {var(mtd['revenue'], mtd['revenueLY'])} |
| Occupancy   | {mtd['occupancy']*100:.1f}% | {mtd['occupancyLY']*100:.1f}% | {var(mtd['occupancy'], mtd['occupancyLY'])} |
| ADR         | €{mtd['adr']:.0f} | €{mtd['adrLY']:.0f} | {var(mtd['adr'], mtd['adrLY'])} |
| Room Nights | {mtd['roomNights']} | {mtd['roomNightsLY']} | {var(mtd['roomNights'], mtd['roomNightsLY'])} |

## ON THE BOOKS — PACE BY FUTURE MONTH
| Month | Occ OTB | Occ STLY | Occ Final LY | vs STLY | ADR OTB | ADR STLY |
|-------|---------|----------|--------------|---------|---------|----------|
{pace_rows}

## PICKUP ACTIVITY (new bookings, future stay dates)
| Period | Room Nights | Cancels (1d) | Revenue | Cancel Rev |
|--------|-------------|--------------|---------|------------|
{pu_rows}
Top pickup month (7 days): {pu['topMonth']} (+{pu['topMonthNights']} room nights)
Cancellations yesterday: {pu['cancellations1d']} rooms, €{pu['cancellationRevenue']:,.0f} revenue

## TOP SOURCES OTB (full-year booked revenue)
| Source | Rev TY | Rev LY (same date) | Var % | Room Nights |
|--------|--------|-------------------|-------|-------------|
{ch_rows}

## NEXT 7 DAYS OTB
| Date | Occ | ADR | Rev |
|------|-----|-----|-----|
""" + "\n".join(
        f"| {d['date']} {d['dow']} | {d['occ']*100:.0f}% | €{d['adr']:.0f} | €{d['rev']:,.0f} |"
        for d in data["next7days"]
    ) + "\n\nNow analyze this data and return the JSON response with executive_summary and 3-5 prioritized insights."


def _strip_json(text: str) -> str:
    """Strip markdown fences and find the JSON object in the response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # Find the outermost JSON object if there's surrounding text
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text


def generate_insights(data: dict[str, Any]) -> dict[str, Any]:
    """
    Sends the briefing data to Claude and returns parsed AI output.
    Returns an empty stub if no API key is configured.
    """
    if not config.ANTHROPIC_API_KEY:
        print("[analyst] No ANTHROPIC_API_KEY — skipping AI insights.")
        return _STUB

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            temperature=0.3,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_prompt(data),
                }
            ],
        )
        raw = response.content[0].text
        text = _strip_json(raw)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as je:
            print(f"[analyst] JSON parse error at char {je.pos}: {je.msg}")
            raise
        result.setdefault("executive_summary", "")
        result.setdefault("insights", [])
        for ins in result["insights"]:
            ins.setdefault("kpis", [])
            ins.setdefault("bullets", [])
            ins.setdefault("recommendation", ins.pop("review_suggestion", ""))
        # Cache to disk so --no-api preview mode can reuse last response
        try:
            from pathlib import Path as _Path
            _Path("ai_cache.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
        return result

    except Exception as exc:
        print(f"[analyst] Claude API error: {exc}")
        return {
            "executive_summary": "Data retrieved successfully. AI narrative unavailable.",
            "insights": [],
        }


def load_cached_insights() -> dict:
    """Return last AI insights: local cache first, then cloud fallback."""
    from pathlib import Path
    cache = Path("ai_cache.json")
    if cache.exists():
        result = json.loads(cache.read_text(encoding="utf-8"))
        if result.get("insights"):
            return result

    # No local cache — fetch from cloud
    print("[analyst] No local cache — fetching AI insights from cloud...")
    try:
        import os, requests as _req
        api_url = os.getenv("FIRSTLIGHT_API_URL", "").rstrip("/")
        api_key = os.getenv("FIRSTLIGHT_API_KEY", "")
        if api_url and api_key:
            resp = _req.get(
                f"{api_url}/my/ai-insights",
                headers={"x-api-key": api_key},
                timeout=10,
            )
            if resp.ok:
                ai = resp.json().get("ai_insights") or {}
                if ai.get("insights"):
                    print("[analyst] Loaded AI insights from cloud.")
                    return ai
    except Exception as exc:
        print(f"[analyst] Cloud fallback failed: {exc}")

    print("[analyst] No AI insights available — returning empty.")
    return _STUB
