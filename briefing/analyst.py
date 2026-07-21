"""
AI Analyst — Daily Briefing Generator (v2)

Two-layer architecture:
  Layer A (_compute_signals): Deterministic Python/SQL compute — calculates ALL metrics,
      z-scores, projections, and scoring. Outputs ranked candidate insights with
      pre-verified numbers. No Claude call here.

  Layer B (generate_insights): LLM narration — Claude receives the pre-computed candidates
      and writes professional narrative text ONLY. No arithmetic, no new figures.
      All numbers in the output must appear verbatim in the input JSON.
"""

import calendar as _cal
import json
import statistics
from datetime import date as _date, timedelta
from typing import Any

import anthropic
import config

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ─── Layer A: scoring helpers ─────────────────────────────────────────────────

def _urgency(days_out: int) -> float:
    """Revenue urgency weight by days to stay date / month start."""
    if days_out <= 7:   return 1.00
    if days_out <= 14:  return 0.90
    if days_out <= 30:  return 0.70
    if days_out <= 60:  return 0.45
    if days_out <= 90:  return 0.30
    return 0.15


def _magnitude_pct(pct: float) -> float:
    p = abs(pct)
    if p >= 0.20: return min(p / 0.20 * 0.8, 1.0)
    if p >= 0.10: return 0.5
    return p / 0.10 * 0.5


def _magnitude_z(z: float) -> float:
    return min(abs(z) / 3.0, 1.0)


def _confidence(room_nights: float) -> float:
    if room_nights >= 30: return 1.0
    if room_nights >= 10: return 0.7
    return 0.5


def _score_candidate(R: float, U: float, M: float, N: float = 1.0, C: float = 1.0) -> float:
    return (0.35 * R + 0.25 * U + 0.25 * M + 0.15 * N) * C


# ─── Layer A: main compute function ──────────────────────────────────────────

def _compute_signals(data: dict) -> dict:
    """
    Deterministic compute layer. Returns:
      {
        "ranked": [candidate_insight, ...],   # scored and sorted, top candidates first
        "watchlist": [...],                   # below threshold — worth monitoring
        "headline": {...},                    # yesterday + MTD summary numbers
      }
    Each candidate has "facts" dict with ALL pre-computed numbers Claude may use.
    """
    today = _date.today()
    yesterday = today - timedelta(days=1)
    total_rooms = config.TOTAL_ROOMS

    pace = data.get("pace", [])
    # Daily revenue baseline for R normalization
    rev_final_ly_total = sum(p.get("rev_final", 0) for p in pace)
    daily_rev_baseline = max(rev_final_ly_total / 365.0, 1.0)

    candidates: list[dict] = []

    # ── Signal 1: Pickup z-score by stay month ────────────────────────────────
    pickup_daily = data.get("pickup_daily", [])
    if pickup_daily:
        yesterday_str = yesterday.isoformat()
        month_groups: dict[tuple, list] = {}
        for row in pickup_daily:
            key = (row["stay_month"], row["stay_year"])
            month_groups.setdefault(key, []).append(row)

        for (sm, sy), rows in month_groups.items():
            rows_sorted = sorted(rows, key=lambda r: r["ref_date"])
            yday_row  = next((r for r in rows_sorted if r["ref_date"] == yesterday_str), None)
            prior_rows = [r for r in rows_sorted if r["ref_date"] < yesterday_str]

            if not yday_row or len(prior_rows) < 3:
                continue

            prior_rn = [r["net_rn"] for r in prior_rows[-7:]]
            mean_rn  = statistics.mean(prior_rn)
            std_rn   = statistics.stdev(prior_rn) if len(prior_rn) > 1 else 0.0

            yday_rn  = yday_row["net_rn"]
            yday_rev = yday_row["net_rev"]

            if std_rn > 0:
                z = (yday_rn - mean_rn) / std_rn
            else:
                z = (yday_rn - mean_rn) / max(abs(mean_rn) * 0.20, 1.0)

            if abs(z) < 1.5 and abs(yday_rn) < 5:
                continue

            m_name        = _cal.month_abbr[sm]
            month_label   = f"{m_name} {sy}"
            days_to_start = max(0, (_date(sy, sm, 1) - today).days)

            pace_m    = next((p for p in pace if p.get("month_num") == sm), None)
            adr_ly    = pace_m.get("adr_final_ly", 150.0) if pace_m else 150.0
            rev_at_stake = abs(yday_rn - mean_rn) * adr_ly * 7  # 7-day extrapolation

            R = min(rev_at_stake / daily_rev_baseline, 1.0)
            U = _urgency(days_to_start)
            M = _magnitude_z(z)
            C = _confidence(max(abs(yday_rn), abs(mean_rn)))
            score = _score_candidate(R, U, M, C=C)

            tag = "ALERT" if z < -1.5 else ("OPPORTUNITY" if z > 1.5 else "MONITOR")
            pct_vs_avg = round((yday_rn - mean_rn) / max(abs(mean_rn), 0.1) * 100, 1)

            candidates.append({
                "signal":      "pickup",
                "tag":         tag,
                "score":       round(score, 4),
                "title_hint":  f"Pickup {'slowdown' if z < 0 else 'surge'} for {month_label}: {yday_rn:+d} rn vs {mean_rn:.1f} avg",
                "facts": {
                    "month_label":      month_label,
                    "yday_rn":          yday_rn,
                    "yday_rev":         round(yday_rev, 0),
                    "trailing_mean_rn": round(mean_rn, 1),
                    "trailing_std_rn":  round(std_rn, 1),
                    "z_score":          round(z, 2),
                    "pct_vs_avg":       pct_vs_avg,
                    "days_to_start":    days_to_start,
                    "rev_at_stake":     round(rev_at_stake, 0),
                    "adr_final_ly":     round(adr_ly, 0),
                    "direction":        "down" if z < 0 else "up",
                },
            })

    # ── Signal 2: Pace vs STLY by future month ────────────────────────────────
    for p in pace:
        sm = p.get("month_num", 0)
        if sm < today.month:
            continue  # past months are closed — no action possible

        days_in       = _cal.monthrange(today.year, sm)[1]
        days_to_start = max(0, (_date(today.year, sm, 1) - today).days)

        rn_ty   = p["rn"]
        rn_stly = p.get("rn_stly", 0)
        if rn_stly == 0:
            continue

        rn_gap  = rn_ty - rn_stly
        pct_gap = rn_gap / rn_stly

        if abs(pct_gap) < 0.05 and abs(rn_gap) < 10:
            continue  # significance floor

        rev_ty       = p["rev"]
        rev_stly     = p.get("rev_stly", 0)
        rev_final_ly = p.get("rev_final", 0)
        rn_final_ly  = p.get("rn_final_ly", 0)
        adr_final_ly = p.get("adr_final_ly", 150.0)

        # Expected remaining pickup from LY behaviour = Final LY - STLY
        exp_remaining_rn  = max(0, rn_final_ly  - rn_stly)
        exp_remaining_rev = max(0, rev_final_ly - rev_stly)
        proj_rn           = rn_ty  + exp_remaining_rn
        proj_rev          = rev_ty + exp_remaining_rev
        proj_occ_pct      = round(proj_rn / (total_rooms * days_in) * 100, 1) if total_rooms * days_in > 0 else 0.0
        vs_final_ly_pct   = round((proj_rev - rev_final_ly) / rev_final_ly * 100, 1) if rev_final_ly > 0 else None

        rev_at_stake = abs(rn_gap) * adr_final_ly
        R = min(rev_at_stake / daily_rev_baseline, 1.0)
        U = _urgency(days_to_start)
        M = _magnitude_pct(pct_gap)
        C = _confidence(rn_stly)
        score = _score_candidate(R, U, M, C=C)

        tag       = "ALERT" if pct_gap < -0.05 else ("OPPORTUNITY" if pct_gap > 0.05 else "MONITOR")
        direction = "down" if rn_gap < 0 else "up"
        m_name    = _cal.month_abbr[sm]

        candidates.append({
            "signal":      "pace",
            "tag":         tag,
            "score":       round(score, 4),
            "title_hint":  f"{m_name} pace {abs(pct_gap*100):.1f}% {'behind' if rn_gap < 0 else 'ahead'} STLY",
            "facts": {
                "month_label":      f"{m_name} {today.year}",
                "month_num":        sm,
                "rn_ty":            rn_ty,
                "rn_stly":          rn_stly,
                "rn_gap":           rn_gap,
                "pct_gap_pct":      round(pct_gap * 100, 1),
                "rev_ty":           round(rev_ty, 0),
                "rev_stly":         round(rev_stly, 0),
                "rev_final_ly":     round(rev_final_ly, 0),
                "rev_gap":          round(rev_ty - rev_stly, 0),
                "occ_ty_pct":       round(p["occ"] * 100, 1),
                "occ_stly_pct":     round(p["stly"] * 100, 1),
                "occ_final_ly_pct": round(p["final"] * 100, 1),
                "proj_rev":         round(proj_rev, 0),
                "proj_occ_pct":     proj_occ_pct,
                "vs_final_ly_pct":  vs_final_ly_pct,
                "adr_ty":           round(p.get("adr", 0), 0),
                "adr_stly":         round(p.get("adr_stly", 0), 0),
                "adr_final_ly":     round(adr_final_ly, 0),
                "days_to_start":    days_to_start,
                "rev_at_stake":     round(rev_at_stake, 0),
                "direction":        direction,
            },
        })

    # ── Signal 4: Soft / Hot dates in next 90 days ───────────────────────────
    otb_by_date = data.get("otb_by_date", [])
    if otb_by_date and total_rooms > 0:
        soft_dates: list[dict] = []
        hot_dates:  list[dict] = []

        for row in otb_by_date:
            rn_ty   = row["rn_ty"]
            rn_stly = row["rn_stly"]
            if rn_stly < 5:
                continue  # too thin for a meaningful comparison

            occ_ty   = rn_ty   / total_rooms
            occ_stly = rn_stly / total_rooms
            occ_gap  = occ_ty - occ_stly
            pct_gap  = occ_gap / occ_stly if occ_stly > 0 else 0.0

            stay_date = _date.fromisoformat(row["stay_date"])
            days_out  = (stay_date - today).days

            entry = {
                "stay_date":    row["stay_date"],
                "dow":          stay_date.strftime("%a"),
                "rn_ty":        rn_ty,
                "rn_stly":      rn_stly,
                "occ_ty_pct":   round(occ_ty * 100, 0),
                "occ_stly_pct": round(occ_stly * 100, 0),
                "gap_pp":       round(occ_gap * 100, 1),
                "pct_gap_pct":  round(pct_gap * 100, 1),
                "days_out":     days_out,
            }

            if pct_gap <= -0.15:
                soft_dates.append(entry)
            elif pct_gap >= 0.15 and occ_ty >= 0.75:
                hot_dates.append(entry)

        if soft_dates:
            soft_dates.sort(key=lambda x: x["days_out"])
            top_soft     = soft_dates[:5]
            avg_urgency  = statistics.mean(_urgency(d["days_out"]) for d in top_soft)
            avg_gap_pp   = statistics.mean(abs(d["gap_pp"]) for d in top_soft)
            avg_occ_stly = statistics.mean(d["occ_stly_pct"] for d in top_soft)
            rev_at_stake = len(top_soft) * total_rooms * (avg_gap_pp / 100) * 130  # ~ADR estimate
            R = min(rev_at_stake / daily_rev_baseline, 1.0)
            M = _magnitude_pct(avg_gap_pp / 100)
            score = _score_candidate(R, avg_urgency, M, C=0.85)
            candidates.append({
                "signal":     "soft_dates",
                "tag":        "ALERT",
                "score":      round(score, 4),
                "title_hint": f"{len(top_soft)} soft date(s) below STLY occupancy",
                "facts": {
                    "count":            len(top_soft),
                    "dates":            top_soft,
                    "avg_gap_pp":       round(avg_gap_pp, 1),
                    "avg_occ_stly_pct": round(avg_occ_stly, 0),
                    "nearest_days_out": top_soft[0]["days_out"],
                    "nearest_date":     top_soft[0]["stay_date"],
                    "rev_at_stake":     round(rev_at_stake, 0),
                },
            })

        if hot_dates:
            hot_dates.sort(key=lambda x: x["days_out"])
            top_hot     = hot_dates[:3]
            avg_urgency = statistics.mean(_urgency(d["days_out"]) for d in top_hot)
            avg_occ_ty  = statistics.mean(d["occ_ty_pct"] for d in top_hot)
            score = _score_candidate(0.5, avg_urgency, 0.7, C=0.9)
            candidates.append({
                "signal":     "hot_dates",
                "tag":        "OPPORTUNITY",
                "score":      round(score, 4),
                "title_hint": f"{len(top_hot)} date(s) near-full — rate review opportunity",
                "facts": {
                    "count":           len(top_hot),
                    "dates":           top_hot,
                    "avg_occ_ty_pct":  round(avg_occ_ty, 0),
                    "nearest_days_out": top_hot[0]["days_out"],
                    "nearest_date":    top_hot[0]["stay_date"],
                },
            })

    # ── Signal 5: Month-end revenue projection ────────────────────────────────
    cm = data.get("current_month_remaining", {})
    mtd = data.get("mtd", {})

    # Current month
    if cm:
        rev_mtd          = float(mtd.get("revenue", 0))
        rev_rem_otb      = cm.get("rev_remaining_otb_ty", 0)
        rev_rem_final_ly = cm.get("rev_remaining_final_ly", 0)
        rev_rem_stly     = cm.get("rev_remaining_stly", 0)
        exp_rem_pickup   = max(0, rev_rem_final_ly - rev_rem_stly)
        proj_rev         = rev_mtd + rev_rem_otb + exp_rem_pickup

        curr_pace    = next((p for p in pace if p.get("month_num") == today.month), None)
        rev_final_ly = curr_pace.get("rev_final", 0) if curr_pace else 0
        rev_budget   = curr_pace.get("rev_budget", 0) if curr_pace else 0

        if rev_final_ly > 0:
            vs_final_ly_pct = round((proj_rev - rev_final_ly) / rev_final_ly * 100, 1)
        else:
            vs_final_ly_pct = None
        vs_budget_pct = round((proj_rev - rev_budget) / rev_budget * 100, 1) if rev_budget > 0 else None

        deviation = abs(vs_budget_pct or vs_final_ly_pct or 0)
        if deviation >= 3:
            tag   = "ALERT" if (vs_budget_pct or vs_final_ly_pct or 0) < 0 else "OPPORTUNITY"
            ref   = rev_budget if rev_budget > 0 else rev_final_ly
            R     = min(abs(proj_rev - ref) / daily_rev_baseline, 1.0)
            score = _score_candidate(R, _urgency(0), _magnitude_pct(deviation / 100), C=0.85)
            candidates.append({
                "signal":     "projection_current",
                "tag":        tag,
                "score":      round(score, 4),
                "title_hint": f"{today.strftime('%B')} proj €{proj_rev:,.0f} — {abs(deviation):.1f}% {'below' if (vs_budget_pct or vs_final_ly_pct or 0) < 0 else 'above'} {'budget' if rev_budget > 0 else 'Final LY'}",
                "facts": {
                    "month_label":       today.strftime("%B %Y"),
                    "proj_rev":          round(proj_rev, 0),
                    "rev_mtd":           round(rev_mtd, 0),
                    "rev_rem_otb":       round(rev_rem_otb, 0),
                    "exp_rem_pickup":    round(exp_rem_pickup, 0),
                    "rev_final_ly":      round(rev_final_ly, 0),
                    "rev_budget":        round(rev_budget, 0) if rev_budget > 0 else None,
                    "vs_final_ly_pct":   vs_final_ly_pct,
                    "vs_budget_pct":     vs_budget_pct,
                    "direction":         "down" if (vs_budget_pct or vs_final_ly_pct or 0) < 0 else "up",
                },
            })

    # Future months projection
    for p in pace:
        sm = p.get("month_num", 0)
        if sm <= today.month:
            continue

        rev_ty       = p["rev"]
        rev_stly     = p.get("rev_stly", 0)
        rev_final_ly = p.get("rev_final", 0)
        rev_budget   = p.get("rev_budget", 0)
        if rev_final_ly == 0 and rev_budget == 0:
            continue

        exp_pickup = max(0, rev_final_ly - rev_stly)
        proj_rev   = rev_ty + exp_pickup

        if rev_budget > 0:
            vs_pct = (proj_rev - rev_budget) / rev_budget * 100
            ref_label = "Budget"
        elif rev_final_ly > 0:
            vs_pct = (proj_rev - rev_final_ly) / rev_final_ly * 100
            ref_label = "Final LY"
        else:
            continue

        if abs(vs_pct) < 5:
            continue  # not material for future months

        days_to_start = max(0, (_date(today.year, sm, 1) - today).days)
        m_name = _cal.month_abbr[sm]
        tag    = "ALERT" if vs_pct < 0 else "OPPORTUNITY"
        ref    = rev_budget if rev_budget > 0 else rev_final_ly
        R      = min(abs(proj_rev - ref) / daily_rev_baseline, 1.0)
        score  = _score_candidate(R, _urgency(days_to_start), _magnitude_pct(vs_pct / 100), C=0.8)

        candidates.append({
            "signal":     "projection_future",
            "tag":        tag,
            "score":      round(score, 4),
            "title_hint": f"{m_name} proj €{proj_rev:,.0f} — {abs(vs_pct):.1f}% {'below' if vs_pct < 0 else 'above'} {ref_label}",
            "facts": {
                "month_label":    f"{m_name} {today.year}",
                "proj_rev":       round(proj_rev, 0),
                "rev_otb_ty":     round(rev_ty, 0),
                "exp_pickup":     round(exp_pickup, 0),
                "rev_final_ly":   round(rev_final_ly, 0),
                "rev_budget":     round(rev_budget, 0) if rev_budget > 0 else None,
                "vs_pct":         round(vs_pct, 1),
                "ref_label":      ref_label,
                "days_to_start":  days_to_start,
                "direction":      "down" if vs_pct < 0 else "up",
            },
        })

    # ── Score, rank, apply hard gates ────────────────────────────────────────
    candidates.sort(key=lambda c: c["score"], reverse=True)

    # Limit to 1 insight per signal type for diversity; extras go to watchlist
    seen_signals: set[str] = set()
    ranked:    list[dict] = []
    watchlist: list[dict] = []

    for c in candidates:
        sig = c["signal"].replace("_current", "").replace("_future", "")
        if sig not in seen_signals and c["score"] >= 0.08:
            ranked.append(c)
            seen_signals.add(sig)
        else:
            watchlist.append(c)

    # ── Headline KPIs ─────────────────────────────────────────────────────────
    yd  = data.get("yesterday", {})
    pu  = data.get("pickup", {})

    rev_yd  = float(yd.get("revenue",   0))
    rev_ly  = float(yd.get("revenueLY", 0))
    rev_var = round((rev_yd - rev_ly) / rev_ly * 100, 1) if rev_ly else None

    rev_mtd    = float(mtd.get("revenue",   0))
    rev_mtd_ly = float(mtd.get("revenueLY", 0))
    mtd_var    = round((rev_mtd - rev_mtd_ly) / rev_mtd_ly * 100, 1) if rev_mtd_ly else None

    headline = {
        "report_date":           data.get("report_date", ""),
        "hotel_name":            data.get("hotel_name", ""),
        "yday_rev":              round(rev_yd, 0),
        "yday_rev_ly":           round(rev_ly, 0),
        "yday_rev_var_pct":      rev_var,
        "yday_occ_pct":          round(float(yd.get("occupancy",   0)) * 100, 1),
        "yday_occ_ly_pct":       round(float(yd.get("occupancyLY", 0)) * 100, 1),
        "yday_adr":              round(float(yd.get("adr",   0)), 0),
        "yday_adr_ly":           round(float(yd.get("adrLY", 0)), 0),
        "mtd_rev":               round(rev_mtd, 0),
        "mtd_rev_ly":            round(rev_mtd_ly, 0),
        "mtd_rev_var_pct":       mtd_var,
        "mtd_occ_pct":           round(float(mtd.get("occupancy",   0)) * 100, 1),
        "mtd_adr":               round(float(mtd.get("adr", 0)), 0),
        "net_pickup_yday_rn":    int(pu.get("last1d", {}).get("roomNights", 0)),
        "net_pickup_yday_rev":   round(float(pu.get("last1d", {}).get("revenue", 0)), 0),
        "cancellations_yday":    int(pu.get("cancellations1d", 0)),
    }

    return {
        "ranked":    ranked[:6],
        "watchlist": watchlist,
        "headline":  headline,
    }


# ─── Layer A: System prompt (narration only) ──────────────────────────────────

_SYSTEM_PROMPT = """You are an expert hotel revenue analyst writing a morning briefing.

You will receive a list of pre-computed performance signals ranked by importance.
Your ONLY job is to write professional narrative text for each signal.

CRITICAL RULES:
1. Use ONLY numbers that appear verbatim in the provided "facts" JSON for each signal.
2. Do NOT perform any arithmetic or derive any new figures.
3. Do NOT mix numbers from different signals.
4. Every number you write in the text must exist in the signal's facts dict.

OUTPUT FORMAT (use submit_briefing tool):
- executive_summary: one sentence capturing the single most urgent revenue focus today.
- insights (3 to 5): for each ranked signal provided:
    - type: warning | opportunity | observation | monitor
      (use "warning" for ALERT signals, "opportunity" for OPPORTUNITY, "monitor" for MONITOR)
    - title: lead with the key €EUR number or key tension. Max 70 chars. Use + or − sign.
    - kpis: exactly 2 chips. Values and subs must use numbers from the signal's facts.
    - findings: exactly 2 bullets — WHAT the data shows and WHY it matters.
      Each bullet max 25 words. Use no markdown bold/italic.
    - action: ONE sentence, professional and measured. Example tones:
      "worth reviewing", "consider protecting", "monitor closely", "a candidate for rate review".

TONE: analytical, measured, commercial. Zero filler. No markdown formatting. Plain text only."""


# ─── Layer A → Layer B handoff ────────────────────────────────────────────────

def _build_narration_prompt(computed: dict, data: dict) -> str:
    headline = computed["headline"]
    ranked   = computed["ranked"]

    yd  = data.get("yesterday", {})
    mtd = data.get("mtd", {})
    pu  = data.get("pickup", {})

    def var(ty, ly):
        if not ly:
            return "n/a"
        v = (ty - ly) / ly * 100
        return f"{'+' if v >= 0 else ''}{v:.1f}%"

    cancel_7d = pu.get("cancellations7d", 0)
    cancel_avg = cancel_7d / 7.0 if cancel_7d else 0
    cancel_1d  = pu.get("cancellations1d", 0)
    if cancel_avg > 0:
        ratio = cancel_1d / cancel_avg
        cancel_note = f"Yesterday: {cancel_1d} rooms cancelled ({ratio:.1f}x 7-day avg of {cancel_avg:.0f}/day)."
    else:
        cancel_note = f"Yesterday: {cancel_1d} rooms cancelled."

    pu_note = (
        f"Pickup yesterday: +{pu.get('last1d', {}).get('roomNights', 0)} rn, "
        f"€{pu.get('last1d', {}).get('revenue', 0):,.0f}. "
        f"7-day: +{pu.get('last7d', {}).get('roomNights', 0)} rn. "
        + cancel_note
    )

    ranked_json = json.dumps(ranked, indent=2, ensure_ascii=False)

    return f"""Generate the morning briefing for {headline['hotel_name']}.
Report date: {headline['report_date']}  |  Total rooms: {config.TOTAL_ROOMS}

## YESTERDAY
Revenue: €{headline['yday_rev']:,.0f} ({var(headline['yday_rev'], headline['yday_rev_ly'])} vs LY)
Occupancy: {headline['yday_occ_pct']}%  ADR: €{headline['yday_adr']:,.0f}

## MONTH TO DATE ({mtd.get('month_name', '')})
Revenue: €{headline['mtd_rev']:,.0f} ({var(headline['mtd_rev'], headline['mtd_rev_ly'])} vs LY)
Occupancy: {headline['mtd_occ_pct']}%  ADR: €{headline['mtd_adr']:,.0f}

## PICKUP
{pu_note}

## PRE-COMPUTED SIGNALS (ranked by impact × urgency)
These are the performance signals to narrate. Use ONLY the numbers in each signal's "facts" dict.
Do NOT perform any arithmetic on these numbers. Do NOT invent any figure not listed here.

{ranked_json}

Write 3–5 insights using the top ranked signals above.
Every number in your output must appear verbatim in the corresponding signal's "facts".
Start with the most urgent insight (rank 1 = highest score)."""


# ─── Tool schema ─────────────────────────────────────────────────────────────

_TOOL: dict[str, Any] = {
    "name": "submit_briefing",
    "description": "Submit the hotel morning briefing with executive summary and insights.",
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

_STUB = {"executive_summary": "", "insights": []}


# ─── Layer B: LLM narration ───────────────────────────────────────────────────

def generate_insights(data: dict[str, Any]) -> dict[str, Any]:
    if not config.ANTHROPIC_API_KEY:
        print("[analyst] No ANTHROPIC_API_KEY — skipping AI insights.")
        return _STUB

    try:
        # Layer A: deterministic compute
        computed = _compute_signals(data)
        print(f"[analyst] Compute: {len(computed['ranked'])} ranked signals, "
              f"{len(computed['watchlist'])} watchlist")
        if not computed["ranked"]:
            print("[analyst] No signals above threshold — falling back to legacy prompt.")
            return _legacy_generate(data)

        # Layer B: LLM narration
        prompt = _build_narration_prompt(computed, data)
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            temperature=0.2,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "submit_briefing"},
            messages=[{"role": "user", "content": prompt}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        result: dict[str, Any] = tool_use.input
        print(f"[analyst] Narration: {len(result.get('insights', []))} insights")

        result.setdefault("executive_summary", "")
        result.setdefault("insights", [])
        for ins in result["insights"]:
            ins.setdefault("kpis", [])
            ins.setdefault("findings", [])
            ins.setdefault("action", "")

        # Cache for preview/data-only mode
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
        print(f"[analyst] Error: {exc}")
        traceback.print_exc()
        return {"executive_summary": "Data retrieved. AI narrative unavailable.", "insights": []}


# ─── Legacy fallback (used when no new signal data is available yet) ──────────

def _legacy_generate(data: dict[str, Any]) -> dict[str, Any]:
    """Original prompt-based approach — used as fallback until new queries deploy."""
    import calendar as _cal2
    from datetime import date as _d2, datetime as _dt2

    _LEGACY_SYSTEM = """You are an expert hotel revenue analyst delivering a morning briefing.
Return 3-5 insights ranked by revenue impact and urgency.
Focus on: OTB pace vs STLY/Final LY, pickup trends, projected month-end outcomes.
For projections: use Proj Finish = OTB + (Final LY - STLY). Never re-derive this.
Output format: executive_summary and insights array via submit_briefing tool.
Type: warning | opportunity | observation | monitor.
Title: lead with €EUR number. Max 70 chars.
KPIs: exactly 2 chips with value and sub delta.
Findings: exactly 2 bullets (max 25 words each). Plain text only, no markdown.
Action: one professional sentence."""

    yd  = data["yesterday"]
    mtd = data["mtd"]
    pu  = data["pickup"]

    def var(ty, ly):
        return f"{(ty-ly)/ly*100:+.1f}%" if ly else "n/a"

    def pace_row(p):
        occ, stly, final_ly = p["occ"], p["stly"], p["final"]
        remaining_ly = final_ly - stly
        projected    = min(max(occ + remaining_ly, 0), 1.0)
        occ_gap      = projected - final_ly
        days_in = _cal2.monthrange(_d2.today().year, p["month_num"])[1]
        adr_ref = p.get("adr_final_ly", p.get("adr", 150))
        rev_risk = f"-€{abs(occ_gap) * config.TOTAL_ROOMS * days_in * adr_ref:,.0f}" if occ_gap < 0 else "BEATING LY"
        rev_final_s = f"€{p['rev_final']:,.0f}" if p.get("rev_final") else "n/a"
        return (f"| {p['month']} | {occ*100:.1f}% | {stly*100:.1f}% | {final_ly*100:.1f}%"
                f" | {remaining_ly*100:.1f}% | {projected*100:.1f}% | {occ_gap*100:+.1f}%"
                f" | €{p.get('adr',0):.0f} | {rev_final_s} | {rev_risk} |")

    pace_rows = "\n".join(pace_row(p) for p in data["pace"])
    ch_rows   = "\n".join(
        f"| {c['name']} | €{c['rev']:,.0f} | €{c['rev_stly']:,.0f} | {c['nights']} rn |"
        for c in data["topChannels"]
    )
    next7_rows = "\n".join(
        f"| {d['date']} {d['dow']} | {d['occ']*100:.0f}% | €{d['adr']:.0f} | €{d['rev']:,.0f} |"
        for d in data["next7days"]
    )

    user_msg = f"""Analyze this hotel's performance and generate the morning briefing.

Hotel: {data['hotel_name']}  |  Rooms: {config.TOTAL_ROOMS}  |  Date: {data['report_date']}

YESTERDAY
| Metric | TY | LY | Var |
|--------|----|----|-----|
| Revenue | €{yd['revenue']:,.0f} | €{yd['revenueLY']:,.0f} | {var(yd['revenue'],yd['revenueLY'])} |
| Occ | {yd['occupancy']*100:.1f}% | {yd['occupancyLY']*100:.1f}% | {var(yd['occupancy'],yd['occupancyLY'])} |
| ADR | €{yd['adr']:.0f} | €{yd['adrLY']:.0f} | {var(yd['adr'],yd['adrLY'])} |

MTD ({mtd['month_name']})
Revenue: €{mtd['revenue']:,.0f} vs €{mtd['revenueLY']:,.0f} LY ({var(mtd['revenue'],mtd['revenueLY'])})
Occ: {mtd['occupancy']*100:.1f}%  ADR: €{mtd['adr']:.0f}

PACE (Proj = OTB + (Final LY - STLY) — use these exact numbers)
| Month | Occ OTB | Occ STLY | Final LY | Rem LY | Proj | vs LY | ADR OTB | Rev Final LY | Rev Risk |
|-------|---------|----------|----------|--------|------|-------|---------|--------------|----------|
{pace_rows}

PICKUP (yesterday): +{pu['last1d']['roomNights']} rn  €{pu['last1d']['revenue']:,.0f}
Cancellations: {pu['cancellations1d']} rn
Top pickup month (7d): {pu['topMonth']} +{pu['topMonthNights']} rn

TOP SOURCES
| Source | Rev TY | Rev LY | Rn |
|--------|--------|--------|----|
{ch_rows}

NEXT 7 DAYS
{next7_rows}

Generate 3-5 insights using the submit_briefing tool."""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            temperature=0.3,
            system=[{"type": "text", "text": _LEGACY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "submit_briefing"},
            messages=[{"role": "user", "content": user_msg}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        result: dict[str, Any] = tool_use.input
        result.setdefault("executive_summary", "")
        result.setdefault("insights", [])
        for ins in result["insights"]:
            ins.setdefault("kpis", [])
            ins.setdefault("findings", [])
            ins.setdefault("action", "")
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
        print(f"[analyst] Legacy fallback error: {exc}")
        traceback.print_exc()
        return {"executive_summary": "Data retrieved. AI narrative unavailable.", "insights": []}


# ─── Cache helpers (unchanged) ────────────────────────────────────────────────

def load_cached_insights() -> dict:
    """Return last AI insights: local cache first, then cloud fallback."""
    from pathlib import Path
    cache = Path("ai_cache.json")
    if cache.exists():
        result = json.loads(cache.read_text(encoding="utf-8"))
        if result.get("insights"):
            return result

    print("[analyst] No local cache — fetching from cloud...")
    try:
        import os, requests as _req
        api_url = os.getenv("FIRSTLIGHT_API_URL", "").rstrip("/")
        api_key = os.getenv("FIRSTLIGHT_API_KEY", "")
        if api_url and api_key:
            resp = _req.get(f"{api_url}/my/ai-insights",
                            headers={"x-api-key": api_key}, timeout=10)
            if resp.ok:
                ai = resp.json().get("ai_insights") or {}
                if ai.get("insights"):
                    print("[analyst] Loaded from cloud.")
                    return ai
    except Exception as exc:
        print(f"[analyst] Cloud fallback failed: {exc}")

    print("[analyst] No insights available — returning empty.")
    return _STUB
