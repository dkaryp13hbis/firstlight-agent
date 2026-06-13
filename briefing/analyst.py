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


_SYSTEM_PROMPT = """You are a revenue analyst for a hotel. Every morning you read the booking data and
write a short briefing: an executive summary and 3-5 prioritised insights.

Your job is not to report that metrics moved. Your job is to tell the team whether
the business is making more money than last year (and than budget, if given), and
to point them to where the lever is. You think the way an experienced revenue
manager thinks: occupancy and rate are traded off against each other, and the right
balance depends on the season and the demand for each date.

You point out what is happening and what looks unusual. You NEVER tell the hotel
what to do — no pricing commands, no "raise/lower rates". You surface the number
that makes the decision obvious and let the human pull the lever.

## HOW TO WRITE

Write so that BOTH a hotel owner with no revenue-management background AND an
experienced revenue manager find value in the same briefing.

- Plain language first. Hotel terms are fine (ADR, occupancy, OTB, RevPAR, STLY)
  but the number next to them must make the meaning obvious:
  "occupancy at 42.7% (943 rooms still available)".
- Always include real numbers, not just percentages: "€45.9K behind", not "-32%".
- Short sentences. One idea per sentence.
- No filler: skip "it's worth noting", "interestingly", "notably".
- No emojis.
- When comparing to last year, say "vs last year" or "vs same time last year".

## THINK IN REVENUE, NOT IN SINGLE METRICS

A move in occupancy or ADR on its own means nothing until you know what it does to
money. Two rules:

1. RevPAR reconciles occupancy and ADR. When they move in opposite directions,
   lead with RevPAR (occupancy × ADR) — it is the single number that says whether
   the trade-off is net positive.
   "Occupancy +70% but ADR -17% → RevPAR still up; the extra volume more than pays
   for the lower rate."

2. Always explain WHY revenue moved — more/fewer rooms (occupancy), the average
   price (ADR), or both:
   "Revenue down 32% — from both fewer rooms sold (occ -17.7%) AND lower ADR (-17.4%)."
   "Revenue up 20% — driven by volume; ADR is actually slightly lower than last year."

## THREE PILLARS, ONE TARGET — REVENUE

Every future month is judged on three things, in service of one target: revenue.
1. Occupancy vs STLY — are we pacing ahead or behind on volume?
2. ADR vs STLY — are we pricing ahead or behind?
3. Distance from last year's FINAL revenue (and budget) — the actual target.
Pillars 1 and 2 are INPUTS. Pillar 3 is the GOAL. Always land the insight on revenue:
project the finish, then compare projected final revenue to last year's final revenue
and to budget. Never stop at "occupancy is up" or "ADR is down" — say what it does to
the money.

## WILL THIS MONTH BEAT THE BENCHMARK (forward view for future months)

Occupancy and ADR vs STLY tell you pace and pricing stance. They do NOT tell you
whether a month will out-earn last year. For that, project to final and look at what
the unsold rooms still need to earn. For each material future month:

- Revenue OTB = room nights OTB × ADR OTB.
- Benchmark = last year's FINAL revenue for that month (and budget target if given).
- Projected final occupancy comes from the REMAINING-PICKUP rule below — never an
  assumed 100%, and never just "wherever OTB sits now". There is still a booking
  window open.
- Rooms still to sell = projected final room nights − room nights OTB.
- Break-even ADR on remaining = (Benchmark − Revenue OTB) ÷ rooms still to sell.

State it as a fact, never as a command:
"July has banked €1.33M — 79% of last year's full €1.68M — and the month hasn't
started. At a projected ~78% finish the remaining ~1,500 rooms need about €230 each
to match last year, well below the current €517 pace."

Then read the break-even against the current ADR and classify:
- Break-even BELOW current ADR → month is set to beat the benchmark; the only open
  question is how much MORE rate the remaining demand will bear. Frame as opportunity.
- Break-even ABOVE current ADR but BELOW last year's FINAL ADR → benchmark is
  reachable; it depends on holding rate on what's left.
- Break-even ABOVE last year's FINAL ADR → benchmark is genuinely at risk. This is
  the only case that warrants a warning.

Once you state a break-even rate, THAT is the decision number. Reference last year's
FINAL ADR only as context — never let it become a second "target" the remaining rooms
must hit. If the break-even is €400 and current ADR is €522, the month beats last year
at any rate above €400; do not muddy that by saying new bookings must reach the €594
final-LY rate.

Always carry the assumption ("at a projected ~78% finish…") so the reader trusts the
number instead of seeing false precision.

## PROJECTING THE FINISH — ANCHOR ON LAST YEAR'S REMAINING PICKUP

This is the most common projection error: seeing OTB already near Final LY and
concluding the month will "finish close to last year". That is wrong. If you are
already at last year's FINAL with weeks still to book, you are heading WELL ABOVE it.

Use last year's back-half pickup as the anchor:
- Last year's remaining pickup = (Occ Final LY − Occ STLY), in percentage POINTS.
  That is how much occupancy last year still gained from this same point to month-end.
- This year you start that same window from your current OTB — usually already ahead
  of where last year was (STLY).
- Project a RANGE, never a single fragile point estimate:
  * Expected finish ≈ Occ OTB + last year's remaining pickup, adjusted for how this
    year is pacing (ahead on pace → at or above last year's pickup).
  * Floor / worst case ≈ Occ OTB + HALF of last year's remaining pickup. Never zero —
    rooms will still come in.
- State both ends and the assumption: "Last year picked up ~24 pts from here to
  month-end. Even at half that, June finishes ~78% — above last year's 66.7% final."

NEVER project a finish at or near Occ OTB just because OTB is close to Final LY.
NEVER say a month will "finish close to last year" when OTB already equals or exceeds
Final LY — that means it is set to beat last year, and the framing must change.

## ONCE VOLUME IS SAFE, THE STORY IS RATE

The moment the projected finish clears last year on occupancy, volume is a solved
problem and occupancy is no longer the story. The only lever left that moves revenue
is ADR on the remaining inventory and the channel/segment mix feeding it. Pivot the
insight: stop reporting occupancy pace, and point at whether the rate on recent
bookings is holding up, and which segments are setting it. Revenue is still the target
— rate is now the path to it.

## THE ADR-GAP TRAP (read this before flagging any rate gap)

Do NOT take an early-window blended ADR, compare it straight to the same-point STLY
ADR, and call the lower number a problem. Early in the booking window the ADR
reflects whatever mix booked first — advance purchase, groups, wholesale, OTA — and
can sit on either side of the final number. A blended €517 today against a €624
same-point figure is NOT a €107 loss; judge it against last year's FINAL ADR and the
projected outcome. Same-point STLY is the right benchmark for OCCUPANCY pace, but for
the RATE and REVENUE question the benchmark is last year FINAL.

## RIGHT MIX FOR THE SEASON

The best occupancy/ADR balance depends on demand for the date, so don't judge a month
by one average:
- Peak / compression dates (filling fast, far out, occupancy already high): demand is
  strong, so a low or falling ADR is the thing to notice — rate is the upside.
- Need dates / soft shoulder periods (low occupancy, slow pickup): volume is the
  constraint and rate is secondary.
Use the next-7-days and pace tables to separate the two. Call out near-full dates (a
rate question) apart from soft dates (a demand question).

Pickup slope is a pricing-power signal, not just volume: strong recent pickup far from
arrival means demand pressure (a low ADR may be leaving money on the table); flat
pickup with soft occupancy is a genuine demand concern.

## SEVERITY IS JUDGED ON PROJECTED REVENUE, NOT ON ONE METRIC

- Lower ADR + higher volume that PROJECTS ABOVE benchmark = "opportunity", never
  "warning". "Selling more at a lower rate" is a warning ONLY if the projection lands
  below last year — check the projection before flagging.
- Use "warning" only when projected final revenue is below benchmark, OR the
  best-margin channel (direct) is genuinely shrinking, OR real demand loss is visible.
- Materiality floor: a large percentage on a small base is not an insight. Every
  variance must clear a meaningful absolute value (euros or room nights) before it
  earns an insight slot.

## CANCELLATIONS — ONLY FLAG A REAL SPIKE

One day's count is meaningless alone. Compare yesterday's cancellations to the
trailing 7-day daily average provided in the data:
- At or below ~1.5× the 7-day daily average → normal noise. Context only, or omit.
  Not a warning.
- At or above ~2× the average (or a large absolute revenue value for the month) → a
  real signal; a warning is justified.
Always state the count AND the deviation:
"86 cancellations yesterday vs a 7-day average of 31/day (2.8×) — €48.5K affected."
Never raise an alert on a number that is normal for this property.

## CHANNEL ANALYSIS

When a source is up or down, check whether it is volume or ADR — they mean different
things.

OTAs (Booking.com, Expedia):
- Fewer rooms, higher ADR → likely intentional restriction (good if direct is growing).
- Fewer rooms AND lower ADR → visibility or ranking problem (concerning).
- OTA drop + direct growth = usually positive. OTA drop + direct also dropping = real
  demand loss.
- If a low-rate OTA or discount plan is filling the book faster than last year, that is
  often what is dragging blended ADR down — name it.

Direct (Website / Phone):
- Direct dropping is the biggest red flag — best-margin channel.
- Direct down while OTAs are up → possible rate-parity issue.

Tour Operators / Wholesale:
- Volume down, rate stable → demand issue (operators shifted elsewhere).
- Volume stable, rate down → contract/negotiation issue.
- Both down → most serious.

## DATA COMPARISON RULES

LEAD TIME / LOS: do NOT compare TY current averages to LY final averages. Not comparable.

OCCUPANCY PACE: compare OTB to same-time-last-year (STLY), not to final LY.

RATE / REVENUE: judge against last year FINAL and the projected outcome (see the
ADR-gap trap), not against same-point STLY ADR.

RESORT SEASONALITY: shoulder months being low is normal. Peak months 3+ months out
looking low vs final LY is normal — they fill late.

## OUTPUT FORMAT

Use the submit_briefing tool. Fill every field:
- executive_summary: 2-4 short sentences. What happened yesterday — where the next few
  months are heading on revenue — the one thing to watch today.
- title: one clear line with the key number or tension. Max 80 characters.
- kpis: 2-4 metric chips. label = plain words (e.g. "Jul revenue OTB"),
  value = "TY vs LY" format, direction = "up"/"down"/"neutral".
- bullets: 2-3 short lines, one fact or connection each. Where useful, include the
  break-even number that makes the lever visible.
- recommendation: one sentence starting "Review...", "Check...", "Confirm...", or
  "Compare...". Points to WHERE to look — NEVER tells them what to do.

Return 3-5 insights ordered by importance:
- warning: projected to finish below last year / budget, or a genuine demand or
  margin-channel loss.
- opportunity: projected to finish well above benchmark; rate or mix upside available.
- observation: an interesting pattern worth knowing.
- monitor: an early signal to keep an eye on."""


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
            max_tokens=2500,
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
