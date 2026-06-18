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


_SYSTEM_PROMPT = """You are an expert hotel revenue analyst delivering a morning briefing.

Return 3-5 insights. Each insight must surface something a revenue manager cannot easily
see by glancing at a spreadsheet — a cross-metric pattern, a forward projection, a
channel anomaly, or a rate opportunity hidden in the mix.

PRIORITY ORDER (always in this order):
1. Total Revenue vs STLY and vs Final LY — this is the only scorecard that matters.
2. Occupancy vs STLY and vs Final LY — only as a volume lever explaining revenue.
3. ADR vs STLY and vs Final LY — only as a rate lever explaining revenue.
Never mention OCC or ADR as standalone metrics. They exist only to explain revenue.

DEFINITIONS
- STLY = same booking position last year (on-the-books at same date last year)
- Final LY = last year's actual closed result
- OTB = current on-the-books

PROJECTION RULES — READ CAREFULLY
- The data table contains a pre-calculated "Proj Finish" column. USE THAT EXACT NUMBER.
  Never recalculate projected occupancy yourself — any number you derive independently
  will contradict the table and produce an inconsistent output.
- If Proj Finish >= Final LY → month is SET TO BEAT last year → focus on rate upside.
- If Proj Finish < Final LY → use "Rev Risk vs LY" from the table for the revenue gap.
- Booking window matters: EARLY position (>90d to start) means remaining pickup LY was
  large — a small OTB gap is often recoverable. CLOSE-IN (<30d) means what you see is
  close to the final outcome — gaps are urgent.
- Cancellations: flag only if yesterday >= 2x the 7-day daily average.

INSIGHT RULES
- 3 insights minimum, 5 maximum
- Rank by revenue impact and urgency
- One insight = one lens. No repetition across cards.
- Skip immaterial variances. Skip anything obvious.

OUTPUT FORMAT (use submit_briefing tool):
- title: lead with the €EUR number or key tension. Use inline numbers with + or − sign.
  Max 70 chars. Example: "August: −€400K risk, OTB 17pts behind STLY"
- type: opportunity | warning | observation | monitor
- kpis: exactly 2 chips — the two most important numbers for this insight.
  value: the primary number (e.g. "+€17K", "€347 vs €365", "9 rooms"). Keep it short.
  sub: one short delta line giving context (e.g. "+9.5% ahead of LY", "−€18 per room night", "2.6× the 7-day norm").
  direction: up / down / neutral
- findings: exactly 2 short bullet strings — WHAT the data shows and WHY it matters.
  Each bullet max 25 words. Use **bold** for key terms and numbers.
  First bullet: the pattern or fact. Second bullet: the risk or implication.
- action: ONE sentence. The specific thing to review or protect, with a number if possible.
  Use measured, professional language — avoid commanding verbs like "raise", "change",
  "push", "force". Instead use: "worth reviewing", "consider protecting", "monitor closely",
  "the data supports reviewing", "flag for attention", "a candidate for rate review".
  Example: "August is a candidate for rate floor review toward €175+ — the OTB pace
  suggests the market position supports it and each recovered room closes the gap."

executive_summary: "Today: <single most urgent revenue focus in one sentence>."

TONE: analytical, measured, commercial. Zero filler. Write like a senior revenue analyst
presenting findings — precise numbers, clear observations, professional recommendations.
Do NOT use markdown formatting. No asterisks, no **bold**, no *italic*. Plain text only."""


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
                "minItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "priority":   {"type": "integer"},
                        "type":       {"type": "string", "enum": ["warning", "opportunity", "observation", "monitor"]},
                        "title":      {"type": "string"},
                        "kpis": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 2,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label":     {"type": "string"},
                                    "value":     {"type": "string"},
                                    "sub":       {"type": "string"},
                                    "direction": {"type": "string", "enum": ["up", "down", "neutral"]},
                                },
                                "required": ["label", "value", "sub", "direction"],
                            },
                        },
                        "findings": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 2,
                            "items": {"type": "string"},
                        },
                        "action": {"type": "string"},
                    },
                    "required": ["priority", "type", "title", "kpis", "findings", "action"],
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

    # Booking window helpers — pre-calculate all math so AI never re-derives numbers
    import calendar as _cal
    from datetime import date as _date, datetime as _dt

    def _month_meta(month_str: str):
        """Returns (days_to_start, days_to_end, days_in_month) from report_date."""
        try:
            report = _date.fromisoformat(data["report_date"])
            dt = _dt.strptime(month_str, "%b %Y")
            m_start = _date(dt.year, dt.month, 1)
            days_in = _cal.monthrange(dt.year, dt.month)[1]
            m_end   = _date(dt.year, dt.month, days_in)
            return max(0, (m_start - report).days), max(0, (m_end - report).days), days_in
        except Exception:
            return None, None, 30

    def _bw_label(days_to_start):
        if days_to_start is None:
            return "unknown"
        if days_to_start > 90:
            return f"EARLY ({days_to_start}d to 1st)"
        if days_to_start > 30:
            return f"MID ({days_to_start}d to 1st)"
        return f"CLOSE-IN ({days_to_start}d to 1st)"

    # Pace rows — all projections pre-calculated here; AI must use these exact values
    def pace_row(p):
        occ      = p['occ']
        stly     = p['stly']
        final_ly = p['final']
        # THE projection formula — computed once, AI must not re-derive
        remaining_ly = final_ly - stly                   # pickup that happened after this date LY
        projected    = min(max(occ + remaining_ly, 0), 1.0)
        occ_gap      = projected - final_ly              # + = beating LY, - = behind LY

        adr_final  = p.get('adr_final_ly')
        rev_final  = p.get('rev_final_ly')
        budget     = p.get('rev_budget')
        days_to_start, _, days_in = _month_meta(p['month'])
        bw = _bw_label(days_to_start)

        # Revenue at risk vs Final LY (only if behind)
        if rev_final and occ_gap < 0:
            adr_ref = adr_final or p.get('adr', 0)
            rev_risk = abs(occ_gap) * config.TOTAL_ROOMS * days_in * adr_ref
            risk_str = f"-€{rev_risk:,.0f}"
        elif rev_final and occ_gap >= 0:
            risk_str = "BEATING LY"
        else:
            risk_str = "n/a"

        adr_final_s = f"€{adr_final:.0f}" if adr_final else "n/a"
        rev_final_s = f"€{rev_final:,.0f}" if rev_final else "n/a"
        budget_s    = f"€{budget:,.0f}" if budget else "n/a"
        return (
            f"| {p['month']} | {occ*100:.1f}% | {stly*100:.1f}% | {final_ly*100:.1f}%"
            f" | {remaining_ly*100:.1f}% | {projected*100:.1f}% | {occ_gap*100:+.1f}%"
            f" | {p.get('adr', 0):.0f} | {adr_final_s}"
            f" | {rev_final_s} | {budget_s} | {risk_str} | {bw} |"
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
IMPORTANT: The "Proj Finish" column is pre-calculated = OTB + (Final LY − STLY).
Use this exact number in every KPI chip and finding. Never derive a different projected occupancy.
"Rem Pickup LY" = how many occ-pts were picked up after this date last year (= Final LY − STLY).
"Rev Risk vs LY" = revenue gap if projected finish < Final LY. "BEATING LY" means projected > Final LY.
Booking window context: EARLY = >90d to month start (STLY comparison reliable, close-in pickup TBD).
MID = 30–90d (trend is directional). CLOSE-IN = <30d (what you see is close to what you get).

| Month | Occ OTB | Occ STLY | Occ Final LY | Rem Pickup LY | Proj Finish | vs Final LY | ADR OTB | ADR Final LY | Rev Final LY | Budget | Rev Risk vs LY | Booking Window |
|-------|---------|----------|--------------|---------------|-------------|-------------|---------|--------------|--------------|--------|----------------|----------------|
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
    ) + "\n\nNote: lead time, LOS, and segment data are not yet available in this feed. Focus only on what the numbers above reveal.\n\nNow analyze this data and return the JSON with executive_summary and 3-5 prioritized insights. Each insight must end with a single conclusion sentence — no bullet breakdowns."


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
        def _strip_md(s: str) -> str:
            return s.replace("**", "").replace("*", "")

        for ins in result["insights"]:
            ins["title"] = _strip_md(ins.get("title", ""))
            ins.setdefault("kpis", [])
            for kpi in ins["kpis"]:
                kpi.setdefault("sub", "")
                kpi["sub"] = _strip_md(kpi["sub"])
            raw_findings = ins.pop("conclusion", ins.pop("recommendation", ins.pop("review_suggestion", "")))
            ins.setdefault("findings", [raw_findings] if isinstance(raw_findings, str) else [])
            ins["findings"] = [_strip_md(f) for f in ins["findings"]]
            ins.setdefault("action", "")
            ins["action"] = _strip_md(ins["action"])
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
