"""
Calls Claude to generate AI insights and executive summary from the briefing data.
Uses the HBIS analyst persona with structured JSON output.
"""

import json
from typing import Any

import anthropic
import config

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


_SYSTEM_PROMPT = """You are an expert hotel revenue analyst.

Your job is to return 5-7 AI insights from the hotel revenue data. The goal is always
TOTAL REVENUE in EUR versus STLY, Final LY, and Budget. Occupancy, ADR, pickup, lead
time, segmentation, and channel mix are only used to EXPLAIN the revenue movement.

Use the data provided. Do not give generic advice. Every insight must be specific to
the numbers in the data.

DEFINITIONS
- STLY = Same Time Last Year: the on-the-books position at the same booking date last year.
- Final LY = last year's final realized result.
- Budget = the hotel's internal target.
- OTB = current on-the-books.

EACH INSIGHT ANSWERS THREE THINGS, IN ONE TIGHT LINE EACH
1. What changed: always mention the EUR impact AND the relevant room nights, occupancy
   points, or ADR difference.
2. Why it changed: whether it is driven by volume, rate, mix, segment, channel,
   nationality, room type, lead time, pickup, cancellations, or a specific date.
3. What decision it implies: state the lever and the key number that makes the action
   obvious.

INSIGHT RULES
- Return minimum 5, maximum 7 insights.
- Rank by revenue impact and decision urgency.
- One insight = one lens only. Do not repeat the same explanation across cards.
- Avoid full-checklist analysis on every card.
- Ignore immaterial variances, even if the percentage looks large.
- Never say "ADR is down" before checking whether the cause is a real price decline or
  a mix shift.
- If OTB revenue is already above Final LY revenue, say the month is ALREADY beating
  last year, and treat the rest as pure rate upside.
- For future months, project the finish using last year's remaining pickup:
    Remaining pickup = Final LY occupancy - STLY occupancy
    Expected finish  = Current OTB occupancy + remaining pickup
    Conservative floor = Current OTB occupancy + 50% of remaining pickup
  Never say a month will "finish close to last year" when OTB already meets or exceeds
  Final LY occupancy - that means it is set to beat last year.
- For months still below target, calculate the break-even ADR on remaining rooms:
    Break-even ADR = (Target Revenue - OTB Revenue) / Remaining Rooms
  That break-even is THE decision number; Final LY ADR is context, not a second target.
- Do not compare early blended ADR only against STLY ADR and call it a loss. Early ADR
  can be distorted by mix. Use Final LY ADR and segment mix as context.
- For cancellations, flag only if the last 7 days are at least 2x the normal 7-day
  baseline.
- If the cause is uncertain, name the two most likely causes instead of pretending
  certainty.

CHOOSE THE RIGHT LENS FOR THE SITUATION
- Next 7-14 days: hot vs soft dates and immediate rate/restriction actions.
- Strong future month: if volume is safe, focus on RATE upside, not occupancy.
- Soft future month: identify whether the issue is weak demand, late booking behavior,
  low ADR, or wrong segment mix.
- Far-out month with thin data: give only the pace signal; do not over-interpret.
- Anomaly: cancellation spikes, unusual pickup, broken channel pattern, or outlier dates.

OUTPUT - use the submit_briefing tool, mapping each card onto these fields:
- title: the insight title. Lead with the key number or tension. Max 80 characters.
- type: opportunity (beating/ahead of benchmark) | warning (projected below target, or
  genuine demand / margin-channel loss, or a real anomaly) | observation (pattern worth
  knowing) | monitor (early signal). "Selling more at a lower rate" is a warning ONLY if
  the projection lands below benchmark.
- kpis: 2-3 chips carrying the actual numbers (EUR variance, occupancy points, ADR).
  value in "TY vs LY" form, direction up/down/neutral.
- bullets: exactly three lines, in this order:
    "What changed: <EUR variance and rooms/points/ADR movement>"
    "Why: <specific driver from the data>"
    "Decision: <clear action lever + the key number that makes it obvious>"
- recommendation: one line that points WHERE to look to act on the lever (start with
  Review / Check / Compare / Confirm). Names the place and the number, not a dictated rate.

executive_summary = "Top priority today: <one sentence naming the single most important
action>".

TONE: direct, commercial, concise. No filler. No generic hotel advice. Write like an
experienced revenue manager who wants the hotel to make more money today."""


_STUB = {
    "executive_summary": "",
    "insights": [],
}

_TOOL: dict[str, Any] = {
    "name": "submit_briefing",
    "description": "Submit the hotel morning briefing analysis with executive summary and insights.",
    "input_schema": {
        "type": "object",
        "properties": {
            "executive_summary": {"type": "string"},
            "insights": {
                "type": "array",
                "minItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "priority":       {"type": "integer"},
                        "type":           {"type": "string", "enum": ["warning", "opportunity", "observation", "monitor"]},
                        "title":          {"type": "string"},
                        "kpis": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label":     {"type": "string"},
                                    "value":     {"type": "string"},
                                    "direction": {"type": "string", "enum": ["up", "down", "neutral"]},
                                },
                                "required": ["label", "value", "direction"],
                            },
                        },
                        "bullets":        {"type": "array", "items": {"type": "string"}},
                        "recommendation": {"type": "string"},
                    },
                    "required": ["priority", "type", "title", "kpis", "bullets", "recommendation"],
                },
            },
        },
        "required": ["executive_summary", "insights"],
    },
}


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v * 100:.1f}%"


def _cancel_7d_avg(pu: dict[str, Any]) -> float | None:
    """
    Trailing 7-day daily cancellation average.
    Prefers an explicit field; falls back to last7d total / 7 if available.
    Returns None if nothing usable is present.
    """
    if pu.get("cancellations7dAvg") is not None:
        return float(pu["cancellations7dAvg"])
    total_7d = pu.get("cancellations7d")
    if total_7d is None:
        total_7d = (pu.get("last7d") or {}).get("cancellations")
    if total_7d is not None:
        return float(total_7d) / 7.0
    return None


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

    # Cancellation baseline (for real-spike detection)
    avg7 = _cancel_7d_avg(pu)
    if avg7 and avg7 > 0:
        ratio = pu["cancellations1d"] / avg7
        cancel_line = (
            f"Cancellations yesterday: {pu['cancellations1d']} rooms, "
            f"€{pu['cancellationRevenue']:,.0f} revenue\n"
            f"7-day daily average: {avg7:.0f} rooms/day  "
            f"-> yesterday is {ratio:.1f}x the norm "
            f"({'SPIKE' if ratio >= 2 else 'elevated' if ratio >= 1.5 else 'normal'})"
        )
    else:
        cancel_line = (
            f"Cancellations yesterday: {pu['cancellations1d']} rooms, "
            f"€{pu['cancellationRevenue']:,.0f} revenue\n"
            f"7-day daily average: n/a (cannot judge deviation — do not raise a "
            f"cancellation alert on one day's count alone)"
        )

    # Pace rows — include LY final ADR/revenue when available for the forward break-even
    def pace_row(p):
        vs = _fmt_pct(p['occ'] / p['stly'] - 1) if p['stly'] else 'n/a'
        adr_final = p.get('adr_final_ly')
        rev_final = p.get('rev_final_ly')
        extra = ""
        if adr_final is not None:
            extra += f" | €{adr_final:.0f}"
        else:
            extra += " | n/a"
        if rev_final is not None:
            extra += f" | €{rev_final:,.0f}"
        else:
            extra += " | n/a"
        budget = p.get('rev_budget')
        extra += f" | €{budget:,.0f}" if budget is not None else " | n/a"
        return (
            f"| {p['month']} | {p['occ']*100:.1f}% | {p['stly']*100:.1f}% | "
            f"{p['final']*100:.1f}% | {vs} | {p.get('adr', 0):.0f} | "
            f"{p.get('adr_stly', 0):.0f}{extra} |"
        )

    pace_rows = "\n".join(pace_row(p) for p in data["pace"])

    # Channel rows
    def _ch_var(v):
        if v is None:
            return "n/a"
        return f"{'+' if v >= 0 else ''}{v*100:.1f}%"

    ch_rows = "\n".join(
        f"| {c['name']} | €{c['rev']:,.0f} | €{c['rev_stly']:,.0f} | {_ch_var(c.get('var'))} | {c['nights']} rn |"
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
(Use Occ STLY for occupancy pace. Use ADR Final LY / Rev Final LY / Budget for the
forward revenue and break-even view. Project final occupancy from pace, never 100%.)
| Month | Occ OTB | Occ STLY | Occ Final LY | vs STLY | ADR OTB | ADR STLY | ADR Final LY | Rev Final LY | Budget |
|-------|---------|----------|--------------|---------|---------|----------|--------------|--------------|--------|
{pace_rows}

## PICKUP ACTIVITY (new bookings, future stay dates)
| Period | Room Nights | Cancels (1d) | Revenue | Cancel Rev |
|--------|-------------|--------------|---------|------------|
{pu_rows}
Top pickup month (7 days): {pu['topMonth']} (+{pu['topMonthNights']} room nights)
{cancel_line}

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
            max_tokens=4096,
            temperature=0.3,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "submit_briefing"},
            messages=[
                {
                    "role": "user",
                    "content": _build_user_prompt(data),
                }
            ],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        result: dict[str, Any] = tool_use.input
        print(f"[analyst] Raw response — exec_summary len={len(result.get('executive_summary',''))}, insights={len(result.get('insights',[]))}")
        if not result.get("insights"):
            print(f"[analyst] Summary: {result.get('executive_summary','')[:200]}")
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
        import traceback
        print(f"[analyst] Claude API error: {exc}")
        traceback.print_exc()
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
