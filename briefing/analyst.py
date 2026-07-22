"""
AI Analyst — Daily Briefing Generator (v4)

Implements ai-insight-cards-spec v1.2:
  Layer A (_compute_signals): deterministic compute.
    - Facts are period-scoped display strings: {"value": "...", "period": "..."}
    - Hard gates: pickup needs |z| >= 2; other signals need >= 10% deviation
    - Significance floor: value at stake below floor -> suppressed
    - Occupancy/revenue projections exposed ONLY as bands (point +/-2), never a point
    - Global ranking: no per-month cards; same-story candidates merge before ranking
    - Novelty gate: same card id in the last 3 days without >=10% worsening -> watchlist
    - No calc, no figure: value_at_stake never ships without value_at_stake_calc

  Layer B: ONE Claude call per card. Validator rejects: numbers not verbatim in
  input facts; word-cap violations; imperative action openers; full-month/remaining
  period blends in one sentence. Max 2 retries, then deterministic fallback card.

Output keeps legacy fields (title/kpis/findings/action) alongside the new card
anatomy so the current PWA renders unchanged.
"""

import calendar as _cal
import json
import re
import statistics
import time as _time
from datetime import date as _date, timedelta
from typing import Any

import anthropic
import config

_client = None
_MODEL = "claude-sonnet-4-6"
_PROMPT_VERSION = "cards-v1.2"

# claude-sonnet-4-6 USD per million tokens (input / output / cache write 5m / cache read)
_PRICE_IN, _PRICE_OUT, _PRICE_CW, _PRICE_CR = 3.00, 15.00, 3.75, 0.30

# Significance floor (spec C2.2): candidates with less at stake are suppressed
_STAKE_FLOOR_EUR = 1000


def _usage_zero() -> dict:
    return {"input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0}


def _usage_add(total: dict, usage: Any) -> dict:
    """Accumulate an API response's usage into a totals dict; returns the delta."""
    delta = {
        "input_tokens":       getattr(usage, "input_tokens", 0) or 0,
        "output_tokens":      getattr(usage, "output_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens":  getattr(usage, "cache_read_input_tokens", 0) or 0,
    }
    for k, v in delta.items():
        total[k] += v
    return delta


def _estimate_cost_usd(u: dict) -> float:
    return round((u["input_tokens"] * _PRICE_IN + u["output_tokens"] * _PRICE_OUT
                  + u["cache_write_tokens"] * _PRICE_CW
                  + u["cache_read_tokens"] * _PRICE_CR) / 1_000_000, 4)


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ─── Display-string formatters ────────────────────────────────────────────────

def _eur(x: float) -> str:
    x = round(float(x))
    return f"−€{abs(x):,.0f}" if x < 0 else f"€{x:,.0f}"


def _eur_signed(x: float) -> str:
    x = round(float(x))
    return ("+" if x >= 0 else "−") + f"€{abs(x):,.0f}"


def _pct(x: float, dec: int = 1) -> str:
    return f"{x:.{dec}f}%"


def _pct_signed(x: float, dec: int = 1) -> str:
    return ("+" if x >= 0 else "−") + f"{abs(x):.{dec}f}%"


def _pts_signed(x: float, dec: int = 1) -> str:
    return ("+" if x >= 0 else "−") + f"{abs(x):.{dec}f}pts"


def _rn_signed(x: float) -> str:
    n = int(round(x))
    return ("+" if n >= 0 else "−") + f"{abs(n)} rn"


def _occ_band(point_pct: float) -> str:
    """Occupancy projection as a +/-2pt band. The point is never exposed."""
    lo = max(0, int(round(point_pct)) - 2)
    hi = min(100, int(round(point_pct)) + 2)
    return f"{lo}–{hi}%"


def _rev_band(point: float) -> tuple[str, float, float]:
    """Revenue projection as a +/-2% band. Returns (display, lo, hi)."""
    lo, hi = point * 0.98, point * 1.02
    return f"{_eur(lo)}–{_eur(hi)}", lo, hi


def _fact(value: str, period: str) -> dict:
    return {"value": value, "period": period}


# ─── Scoring helpers ──────────────────────────────────────────────────────────

def _urgency(days_out: int) -> float:
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


def _month_bounds(year: int, month: int) -> tuple[_date, _date]:
    last = _cal.monthrange(year, month)[1]
    return _date(year, month, 1), _date(year, month, last)


def _parse_eur(s: str) -> float:
    digits = re.sub(r"[^\d]", "", s or "")
    return float(digits) if digits else 0.0


# ─── Layer A: main compute function ──────────────────────────────────────────

def _compute_signals(data: dict, hotel_id: str | None = None) -> dict:
    """
    Returns {"ranked": [...], "watchlist": [...], "headline": {...}}.
    Ranked entries carry the full narration input contract + fallback_card.
    """
    today = _date.today()
    yesterday = today - timedelta(days=1)
    total_rooms = config.TOTAL_ROOMS

    pace = data.get("pace", [])
    cm   = data.get("current_month_remaining", {})
    mtd  = data.get("mtd", {})
    rev_final_ly_total = sum(p.get("rev_final", 0) for p in pace)
    daily_rev_baseline = max(rev_final_ly_total / 365.0, 1.0)

    candidates: list[dict] = []

    # ── Signal 1: Pickup z-score by stay month (hard gate: |z| >= 2) ─────────
    pickup_daily = data.get("pickup_daily", [])
    if pickup_daily:
        yesterday_str = yesterday.isoformat()
        month_groups: dict[tuple, list] = {}
        for row in pickup_daily:
            key = (row["stay_month"], row["stay_year"])
            month_groups.setdefault(key, []).append(row)

        for (sm, sy), rows in month_groups.items():
            rows_sorted = sorted(rows, key=lambda r: r["ref_date"])
            yday_row   = next((r for r in rows_sorted if r["ref_date"] == yesterday_str), None)
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

            # Spec C2.1: |z| < 2 never becomes a pickup card
            if abs(z) < 2.0:
                continue

            m_name      = _cal.month_abbr[sm]
            month_label = f"{m_name} {sy}"
            m_start, m_end = _month_bounds(sy, sm)
            days_to_start  = max(0, (m_start - today).days)
            window_left    = max(0, (m_end - today).days)

            pace_m = next((p for p in pace if p.get("month_num") == sm), None)
            adr_ly = pace_m.get("adr_final_ly", 150.0) if pace_m else 150.0
            gap_per_day  = abs(yday_rn - mean_rn)
            rev_at_stake = gap_per_day * adr_ly * 7

            if rev_at_stake < _STAKE_FLOOR_EUR:
                continue  # spec C2.2 significance floor

            R = min(rev_at_stake / daily_rev_baseline, 1.0)
            U = _urgency(days_to_start)
            M = _magnitude_z(z)
            C = _confidence(max(abs(yday_rn), abs(mean_rn)))
            score = _score_candidate(R, U, M, C=C)

            negative = z < 0
            tag = "ALERT" if negative else "OPPORTUNITY"

            f_yday_net = _rn_signed(yday_rn)
            f_avg      = ("+" if mean_rn >= 0 else "−") + f"{abs(mean_rn):.1f} rn/day"
            f_z        = ("+" if z >= 0 else "−") + f"{abs(z):.1f}"
            f_yday_rev = _eur_signed(yday_rev)
            f_stake    = _eur(rev_at_stake)
            f_calc     = (f"{gap_per_day:.1f} rn/day {'below' if negative else 'above'} avg "
                          f"× {_eur(adr_ly)} ADR × 7 days = {_eur(rev_at_stake)}")

            p_yday  = f"booked yesterday for {month_label}"
            p_prior = f"prior 7 days for {month_label}"

            facts = {
                "month_label":   month_label,
                "yday_net_rn":   _fact(f_yday_net, p_yday),
                "trailing_avg":  _fact(f_avg, p_prior),
                "z_score":       _fact(f_z, "vs prior 7 days"),
                "yday_net_rev":  _fact(f_yday_rev, p_yday),
                "value_at_stake":      f_stake,
                "value_at_stake_calc": f_calc,
            }
            if negative:
                hypo = [{"text": f"Cancellations or a demand slowdown for {month_label} — a single source or rate code may account for the swing", "confidence": "Low"}]
                directive = {
                    "type": "investigate_cancellations",
                    "target": f"yesterday's {month_label} cancellations and bookings by source and rate code",
                    "deadline": "today",
                    "trigger_if_monitor": None,
                }
                fb_headline = f"{month_label} pickup {'turned negative' if yday_rn < 0 else 'slowed sharply'} vs recent average"
                fb_why = "Cancellations or a demand slowdown may be behind the swing; a single source or rate code could account for it (confidence: Low)."
                fb_action = f"It may be worth checking yesterday's {month_label} cancellations for a common source or rate code."
                fb_by_when = "Today — before the next briefing."
            else:
                hypo = [{"text": f"Demand surge for {month_label} — possibly a campaign, event, or group materialising", "confidence": "Low"}]
                directive = {
                    "type": "rate_review_up",
                    "target": f"open {month_label} nights riding the surge",
                    "deadline": "within 2 days",
                    "trigger_if_monitor": None,
                }
                fb_headline = f"{month_label} pickup surging well above recent average"
                fb_why = "A demand surge may be under way — possibly a campaign, event, or group materialising (confidence: Low)."
                fb_action = f"The position could support a higher rate on open {month_label} nights."
                fb_by_when = "Within 2 days, while the surge lasts."

            fallback_card = {
                "id": f"pickup_{m_name.lower()}_{sy}",
                "tag": tag,
                "headline": fb_headline,
                "evidence": [
                    {"label": "YESTERDAY NET", "value": f_yday_net, "sub": f"vs {f_avg} prior 7 days"},
                    {"label": "REVENUE IMPACT", "value": f_yday_rev, "sub": f"z-score {f_z}"},
                ],
                "what_happened": f"Net pickup for {month_label} was {f_yday_net} yesterday against a 7-day average of {f_avg}.",
                "why_it_matters": fb_why,
                "recommended_action": fb_action,
                "by_when": fb_by_when,
                "at_stake": {"value": f_stake, "calc": f_calc},
            }

            candidates.append({
                "signal":     "pickup",
                "tag":        tag,
                "score":      round(score, 4),
                "title_hint": fb_headline,
                "month_num":  sm,
                "stake_eur":  rev_at_stake,
                "insight": {
                    "id": f"pickup_{m_name.lower()}_{sy}",
                    "tag": tag,
                    "score": round(score, 4),
                    "signal": "pickup",
                    "stay_period": {"from": m_start.isoformat(), "to": m_end.isoformat(), "label": month_label},
                    "days_to_nearest_arrival": days_to_start,
                    "booking_window_days_left": window_left,
                    "facts": facts,
                    "cause_hypotheses": hypo,
                    "action_directives": directive,
                    "history": {"first_raised": None, "previously_advised": None},
                },
                "fallback_card": fallback_card,
            })

    # ── Signal 2: Pace vs STLY by month (hard gate: |gap| >= 10%) ────────────
    for p in pace:
        sm = p.get("month_num", 0)
        if sm < today.month:
            continue

        days_in = _cal.monthrange(today.year, sm)[1]
        m_start, m_end = _month_bounds(today.year, sm)
        days_to_start  = max(0, (m_start - today).days)
        window_left    = max(0, (m_end - today).days)
        current_month  = (sm == today.month)

        rn_ty   = p["rn"]
        rn_stly = p.get("rn_stly", 0)
        if rn_stly == 0:
            continue

        rn_gap  = rn_ty - rn_stly
        pct_gap = rn_gap / rn_stly
        if abs(pct_gap) < 0.10:
            continue  # spec C2.1 magnitude gate

        rev_ty       = p["rev"]
        rev_stly     = p.get("rev_stly", 0)
        rev_final_ly = p.get("rev_final", 0)
        rn_final_ly  = p.get("rn_final_ly", 0)
        adr_ty       = p.get("adr", 0)
        adr_final_ly = p.get("adr_final_ly", 150.0)
        adr_gap      = adr_ty - adr_final_ly

        exp_remaining_rn = max(0, rn_final_ly - rn_stly)
        proj_rn          = rn_ty + exp_remaining_rn
        cap_rn           = total_rooms * days_in
        proj_occ_point   = proj_rn / cap_rn * 100 if cap_rn else 0.0
        final_ly_occ     = p.get("final", 0) * 100

        m_name = _cal.month_abbr[sm]
        if current_month:
            rem_days   = max(1, (m_end - today).days + 1)
            rem_otb_rn = cm.get("rn_remaining_otb_ty", 0) if cm else 0
            open_rn    = max(0, total_rooms * rem_days - rem_otb_rn)
            period_open = (f"remaining {m_name} "
                           f"({today.strftime('%b')} {today.day}–{m_end.strftime('%b')} {m_end.day})")
        else:
            open_rn = max(0, cap_rn - rn_ty)
            period_open = f"{m_name} full month"

        rev_delta    = rev_ty - rev_stly
        rev_at_stake = abs(rn_gap) * adr_final_ly
        if rev_at_stake < _STAKE_FLOOR_EUR:
            continue

        R = min(rev_at_stake / daily_rev_baseline, 1.0)
        U = _urgency(days_to_start)
        M = _magnitude_pct(pct_gap)
        C = _confidence(rn_stly)
        score = _score_candidate(R, U, M, C=C)

        month_label  = f"{m_name} {today.year}"
        p_full       = f"{m_name} full month"
        p_vs_stly    = f"{m_name} full month, vs same time last year"
        p_finish     = f"{m_name} month-end finish"
        ahead        = rn_gap > 0
        adr_dilution = ahead and adr_gap < -5

        f_pct_gap   = _pct_signed(pct_gap * 100)
        f_rev_delta = _eur_signed(rev_delta)
        f_rn_gap    = _rn_signed(rn_gap)
        f_band      = _occ_band(proj_occ_point)
        f_final_occ = _pct(final_ly_occ)
        f_adr_otb   = _eur(adr_ty)
        f_adr_fly   = _eur(adr_final_ly)
        f_adr_gap   = _eur_signed(adr_gap)
        f_open_rn   = f"{open_rn:,} rn"

        facts = {
            "month_label":     month_label,
            "pace_vs_stly":    _fact(f_pct_gap, p_vs_stly),
            ("rev_lead" if ahead else "rev_gap"): _fact(f_rev_delta, p_vs_stly),
            "rn_otb":          _fact(f"{rn_ty:,} rn", p_full),
            "rn_stly":         _fact(f"{rn_stly:,} rn", f"{m_name}, same time last year"),
            "rn_gap":          _fact(f_rn_gap, p_vs_stly),
            "proj_occ_band":   _fact(f_band, p_finish),
            "final_ly_occ":    _fact(f_final_occ, f"{m_name} final last year"),
            "adr_otb":         _fact(f_adr_otb, p_full),
            "adr_final_ly":    _fact(f_adr_fly, f"{m_name} final last year"),
            "adr_delta":       _fact(f_adr_gap, "OTB vs final last year"),
            "open_room_nights": _fact(f_open_rn, period_open),
        }

        if adr_dilution:
            tag   = "OPPORTUNITY"
            stake = open_rn * abs(adr_gap)
            f_stake = _eur(stake)
            f_calc  = f"{open_rn:,} open rn × {_eur(abs(adr_gap))} ADR gap = {_eur(stake)}"
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f_calc
            hypo = [{"text": "Early-season discounted rate codes may still be open despite compression", "confidence": "Medium"}]
            directive = {
                "type": "rate_review_up",
                "target": f"lowest open rate codes, {period_open}",
                "deadline": "this week",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} volume far ahead of last year, rates trailing",
                "evidence": [
                    {"label": f"PACE ({p_full})", "value": f_pct_gap, "sub": "vs same time last year"},
                    {"label": "ADR OTB vs FINAL LY", "value": f"{f_adr_otb} vs {f_adr_fly}", "sub": f"{f_adr_gap} per room night"},
                ],
                "what_happened": f"{m_name} full-month OTB is pacing {f_pct_gap} vs same time last year.",
                "why_it_matters": f"ADR {f_adr_otb} trails final last year {f_adr_fly}; early-season rate codes may still be open (confidence: Medium).",
                "recommended_action": f"Worth reviewing whether the lowest rate codes still need to be open for {m_name}.",
                "by_when": f"This week — about {window_left} selling days left.",
                "at_stake": {"value": f_stake, "calc": f_calc},
            }
        elif ahead:
            tag = "OPPORTUNITY"
            hypo = [{"text": "Demand running above last year — remaining inventory may be underpriced for this demand level", "confidence": "Medium"}]
            directive = {
                "type": "rate_review_up",
                "target": f"{m_name} open nights — lowest codes and peak nights",
                "deadline": "next 2–3 days",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} pacing {f_pct_gap} ahead of same time last year",
                "evidence": [
                    {"label": f"PACE ({p_full})", "value": f_pct_gap, "sub": "vs same time last year"},
                    {"label": "REVENUE LEAD", "value": f_rev_delta, "sub": "vs same time last year"},
                ],
                "what_happened": f"{m_name} full-month OTB revenue is {f_rev_delta} vs same time last year.",
                "why_it_matters": "Demand is running above last year; remaining inventory may be underpriced for this demand level (confidence: Medium).",
                "recommended_action": f"The position could support a higher rate on {m_name} peak nights.",
                "by_when": "Next 2–3 days.",
            }
        else:
            tag   = "ALERT"
            stake = abs(rn_gap) * adr_final_ly
            f_stake = _eur(stake)
            f_calc  = f"{abs(rn_gap):,} rn gap × {f_adr_fly} final LY ADR = {_eur(stake)}"
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f_calc
            hypo = [{"text": "Demand softness or channel shift vs last year — source-level comparison needed to isolate the driver", "confidence": "Low"}]
            directive = {
                "type": "open_promo",
                "target": f"{m_name} soft nights",
                "deadline": "within 7 days",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} pacing {f_pct_gap} behind same time last year",
                "evidence": [
                    {"label": f"PACE ({p_full})", "value": f_pct_gap, "sub": "vs same time last year"},
                    {"label": "ROOM NIGHTS", "value": f"{rn_ty:,} vs {rn_stly:,}", "sub": f"{f_rn_gap} vs same time last year"},
                ],
                "what_happened": f"{m_name} full-month OTB stands {f_rn_gap} behind same time last year.",
                "why_it_matters": "Demand softness or a channel shift may explain the gap; a source-level comparison would isolate the driver (confidence: Low).",
                "recommended_action": f"Consider a targeted offer on {m_name} soft nights if the gap persists.",
                "by_when": "Within 7 days.",
                "at_stake": {"value": f_stake, "calc": f_calc},
            }

        fb["id"]  = f"pace_{m_name.lower()}_{today.year}"
        fb["tag"] = tag

        candidates.append({
            "signal":     "pace",
            "tag":        tag,
            "score":      round(score, 4),
            "title_hint": fb["headline"],
            "month_num":  sm,
            "stake_eur":  facts.get("value_at_stake") and _parse_eur(facts["value_at_stake"]) or 0,
            "insight": {
                "id": fb["id"],
                "tag": tag,
                "score": round(score, 4),
                "signal": "pace",
                "stay_period": {"from": max(m_start, today).isoformat(), "to": m_end.isoformat(),
                                "label": period_open if current_month else month_label},
                "days_to_nearest_arrival": days_to_start,
                "booking_window_days_left": window_left,
                "facts": facts,
                "cause_hypotheses": hypo,
                "action_directives": directive,
                "history": {"first_raised": None, "previously_advised": None},
            },
            "fallback_card": fb,
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
                continue

            occ_ty   = rn_ty / total_rooms
            occ_stly = rn_stly / total_rooms
            occ_gap  = occ_ty - occ_stly
            pct_gap  = occ_gap / occ_stly if occ_stly > 0 else 0.0

            stay_date = _date.fromisoformat(row["stay_date"])
            days_out  = (stay_date - today).days

            entry = {
                "date":       stay_date,
                "label":      stay_date.strftime("%b %d").replace(" 0", " "),
                "dow":        stay_date.strftime("%a"),
                "rn_ty":      rn_ty,
                "rn_stly":    rn_stly,
                "occ_ty":     occ_ty * 100,
                "occ_stly":   occ_stly * 100,
                "gap_pp":     occ_gap * 100,
                "gap_rn":     rn_stly - rn_ty,
                "days_out":   days_out,
                "rooms_left": max(0, total_rooms - rn_ty),
            }

            if pct_gap <= -0.15:
                soft_dates.append(entry)
            elif pct_gap >= 0.15 and occ_ty >= 0.75:
                hot_dates.append(entry)

        if soft_dates:
            soft_dates.sort(key=lambda x: x["days_out"])
            top_soft    = soft_dates[:5]
            avg_urgency = statistics.mean(_urgency(d["days_out"]) for d in top_soft)
            avg_gap_pp  = statistics.mean(abs(d["gap_pp"]) for d in top_soft)
            gap_rn_total = sum(max(0, d["gap_rn"]) for d in top_soft)

            months = [d["date"].month for d in top_soft]
            major_month = max(set(months), key=months.count)
            pace_m  = next((p for p in pace if p.get("month_num") == major_month), None)
            adr_ref = (pace_m.get("adr", 0) or pace_m.get("adr_final_ly", 130.0)) if pace_m else 130.0

            stake = gap_rn_total * adr_ref
            if stake >= _STAKE_FLOOR_EUR:
                f_stake = _eur(stake)
                f_calc  = f"{gap_rn_total} rn gap × {_eur(adr_ref)} ADR = {_eur(stake)}"

                R = min(stake / daily_rev_baseline, 1.0)
                M = _magnitude_pct(avg_gap_pp / 100)
                score = _score_candidate(R, avg_urgency, M, C=0.85)

                first_lbl = top_soft[0]["label"]
                last_lbl  = top_soft[-1]["label"]
                date_range = f"{first_lbl}–{last_lbl}" if len(top_soft) > 1 else first_lbl
                f_avg_gap = _pts_signed(-avg_gap_pp)
                p_dates   = f"stay dates {date_range}"

                sorted_by_gap = sorted(top_soft, key=lambda d: d["gap_pp"])
                softest_str = " · ".join(f"{d['label']} ({_pts_signed(d['gap_pp'])})" for d in sorted_by_gap[:2])

                facts = {
                    "date_range":    date_range,
                    "count":         str(len(top_soft)),
                    "avg_gap_pts":   _fact(f_avg_gap, f"{p_dates}, vs same time last year"),
                    "softest_dates": _fact(softest_str, "vs same time last year"),
                    "per_date": [
                        {"date": d["label"], "dow": d["dow"], "occ_otb": _pct(d["occ_ty"], 0),
                         "occ_same_time_ly": _pct(d["occ_stly"], 0), "gap": _pts_signed(d["gap_pp"])}
                        for d in top_soft
                    ],
                    "nearest_days_out": str(top_soft[0]["days_out"]),
                    "value_at_stake":      f_stake,
                    "value_at_stake_calc": f_calc,
                }
                fb = {
                    "id": f"soft_dates_{_cal.month_abbr[major_month].lower()}",
                    "tag": "ALERT",
                    "headline": f"{len(top_soft)} nights pacing {f_avg_gap} behind same time last year",
                    "evidence": [
                        {"label": "SOFTEST DATES", "value": softest_str, "sub": "vs same time last year"},
                        {"label": f"AVG GAP ({date_range})", "value": f_avg_gap, "sub": f"{f_stake} at stake"},
                    ],
                    "what_happened": f"{len(top_soft)} stay dates between {first_lbl} and {last_lbl} average {f_avg_gap} vs same time last year.",
                    "why_it_matters": "These dates trail last year's booking position; if the gap persists the revenue at stake grows (confidence: Medium).",
                    "recommended_action": f"A softer rate or package could help {date_range} if the trend continues.",
                    "by_when": "Within 7 days — window closing.",
                    "at_stake": {"value": f_stake, "calc": f_calc},
                }
                candidates.append({
                    "signal":     "soft_dates",
                    "tag":        "ALERT",
                    "score":      round(score, 4),
                    "title_hint": fb["headline"],
                    "month_num":  major_month,
                    "stake_eur":  stake,
                    "insight": {
                        "id": fb["id"],
                        "tag": "ALERT",
                        "score": round(score, 4),
                        "signal": "soft_dates",
                        "stay_period": {"from": top_soft[0]["date"].isoformat(), "to": top_soft[-1]["date"].isoformat(), "label": date_range},
                        "days_to_nearest_arrival": top_soft[0]["days_out"],
                        "booking_window_days_left": top_soft[0]["days_out"],
                        "facts": facts,
                        "cause_hypotheses": [{"text": "Demand softness concentrated on these dates — check for last-year events or groups that have not repeated", "confidence": "Medium"}],
                        "action_directives": {
                            "type": "rate_review_down",
                            "target": f"soft dates {date_range}, largest gaps first",
                            "deadline": "within 7 days",
                            "trigger_if_monitor": None,
                        },
                        "history": {"first_raised": None, "previously_advised": None},
                    },
                    "fallback_card": fb,
                })

        if hot_dates:
            hot_dates.sort(key=lambda x: x["days_out"])
            top_hot     = hot_dates[:3]
            avg_urgency = statistics.mean(_urgency(d["days_out"]) for d in top_hot)
            nearest     = top_hot[0]
            first_lbl, last_lbl = top_hot[0]["label"], top_hot[-1]["label"]
            date_range = f"{first_lbl}–{last_lbl}" if len(top_hot) > 1 else first_lbl
            score = _score_candidate(0.5, avg_urgency, 0.7, C=0.9)

            facts = {
                "date_range": date_range,
                "count":      str(len(top_hot)),
                "per_date": [
                    {"date": d["label"], "dow": d["dow"], "occ_otb": _pct(d["occ_ty"], 0),
                     "rooms_left": str(d["rooms_left"])}
                    for d in top_hot
                ],
                "nearest_date":       nearest["label"],
                "nearest_days_out":   str(nearest["days_out"]),
                "nearest_occ":        _fact(_pct(nearest["occ_ty"], 0), f"stay date {nearest['label']}, OTB now"),
                "nearest_rooms_left": _fact(str(nearest["rooms_left"]), f"stay date {nearest['label']}"),
            }
            fb = {
                "id": "hot_dates_near_full",
                "tag": "OPPORTUNITY",
                "headline": f"{date_range} near sell-out, last rooms at everyday rates",
                "evidence": [
                    {"label": f"{nearest['label'].upper()} ({nearest['dow'].upper()})", "value": f"{_pct(nearest['occ_ty'], 0)} occ", "sub": f"{nearest['rooms_left']} rooms left"},
                    {"label": "NEAR-FULL DATES", "value": " · ".join(_pct(d["occ_ty"], 0) for d in top_hot), "sub": date_range},
                ],
                "what_happened": f"{len(top_hot)} nights ({date_range}) are close to selling out at unchanged rates.",
                "why_it_matters": "Close-in compression suggests the remaining rooms would sell at a higher price (confidence: High).",
                "recommended_action": f"The position could support a higher rate on {date_range}; worth reviewing open discount codes.",
                "by_when": f"Today — {nearest['label']} is {nearest['days_out']} days out.",
            }
            candidates.append({
                "signal":     "hot_dates",
                "tag":        "OPPORTUNITY",
                "score":      round(score, 4),
                "title_hint": fb["headline"],
                "month_num":  top_hot[0]["date"].month,
                "stake_eur":  0,
                "insight": {
                    "id": fb["id"],
                    "tag": "OPPORTUNITY",
                    "score": round(score, 4),
                    "signal": "hot_dates",
                    "stay_period": {"from": top_hot[0]["date"].isoformat(), "to": top_hot[-1]["date"].isoformat(), "label": date_range},
                    "days_to_nearest_arrival": nearest["days_out"],
                    "booking_window_days_left": nearest["days_out"],
                    "facts": facts,
                    "cause_hypotheses": [{"text": "Close-in compression — demand exceeding remaining supply on these dates", "confidence": "High"}],
                    "action_directives": {
                        "type": "close_discounts",
                        "target": f"remaining rooms on {date_range}",
                        "deadline": f"today — {nearest['label']} is {nearest['days_out']} days out",
                        "trigger_if_monitor": None,
                    },
                    "history": {"first_raised": None, "previously_advised": None},
                },
                "fallback_card": fb,
            })

    # ── Signal 5: Month-end revenue projection (gate: >= 10% deviation) ──────
    if cm:
        rev_mtd          = float(mtd.get("revenue", 0))
        rn_rem_otb       = cm.get("rn_remaining_otb_ty", 0)
        rev_rem_otb      = cm.get("rev_remaining_otb_ty", 0)
        rev_rem_final_ly = cm.get("rev_remaining_final_ly", 0)
        rev_rem_stly     = cm.get("rev_remaining_stly", 0)
        exp_rem_pickup   = max(0, rev_rem_final_ly - rev_rem_stly)
        proj_rev         = rev_mtd + rev_rem_otb + exp_rem_pickup

        _, m_end  = _month_bounds(today.year, today.month)
        days_left = max(0, (m_end - today).days)
        rem_days_incl = max(1, (m_end - today).days + 1)

        # Spec D1 sanity: remaining OTB rn must fit remaining capacity
        sane = rn_rem_otb <= total_rooms * rem_days_incl
        if not sane:
            print(f"[analyst] DATA ALERT: remaining rn {rn_rem_otb} exceeds capacity "
                  f"{total_rooms}×{rem_days_incl} — projection card blocked.")

        curr_pace    = next((p for p in pace if p.get("month_num") == today.month), None)
        rev_final_ly = curr_pace.get("rev_final", 0) if curr_pace else 0
        rev_budget   = curr_pace.get("rev_budget", 0) if curr_pace else 0

        vs_final_ly_pct = ((proj_rev - rev_final_ly) / rev_final_ly * 100) if rev_final_ly > 0 else None
        vs_budget_pct   = ((proj_rev - rev_budget) / rev_budget * 100) if rev_budget > 0 else None
        vs_pct    = vs_budget_pct if vs_budget_pct is not None else (vs_final_ly_pct or 0)
        ref_label = "budget" if vs_budget_pct is not None else "final last year"

        if sane and abs(vs_pct) >= 10:
            month_name = today.strftime("%B")
            behind     = vs_pct < 0
            tag = "ALERT" if behind else "MONITOR"

            ref   = rev_budget if rev_budget > 0 else rev_final_ly
            R     = min(abs(proj_rev - ref) / daily_rev_baseline, 1.0)
            score = _score_candidate(R, _urgency(0), _magnitude_pct(vs_pct / 100), C=0.85)

            band_str, band_lo, band_hi = _rev_band(proj_rev)
            vs_lo = (band_lo - ref) / ref * 100 if ref else 0
            vs_hi = (band_hi - ref) / ref * 100 if ref else 0
            f_vs_band = f"{_pct_signed(vs_lo, 0)} to {_pct_signed(vs_hi, 0)}"
            f_rem_otb = _eur(rev_rem_otb)
            p_finish  = f"{month_name} month-end finish"

            facts = {
                "month_label":    f"{month_name} {today.year}",
                "proj_rev_band":  _fact(band_str, p_finish),
                "vs_ref_band":    _fact(f_vs_band, f"{p_finish}, vs {ref_label}"),
                "ref_label":      ref_label,
                "rev_mtd":        _fact(_eur(rev_mtd), f"{month_name} month to date"),
                "remaining_otb":  _fact(f_rem_otb, f"remaining {month_name}, on the books"),
                "days_left":      str(days_left),
            }
            if behind:
                stake  = max(0, ref - band_hi)
                if stake >= _STAKE_FLOOR_EUR:
                    f_stake = _eur(stake)
                    f_calc  = f"{ref_label} {_eur(ref)} − upper projection {_eur(band_hi)} = {_eur(stake)}"
                    facts["value_at_stake"]      = f_stake
                    facts["value_at_stake_calc"] = f_calc
                hypo = [{"text": "Remaining-month OTB pacing below last year's close-in pickup", "confidence": "Medium"}]
                directive = {
                    "type": "open_promo",
                    "target": f"remaining {month_name} nights",
                    "deadline": "within 2–3 days",
                    "trigger_if_monitor": None,
                }
                fb = {
                    "headline": f"{month_name} projected around {f_vs_band} vs {ref_label}",
                    "evidence": [
                        {"label": "PROJECTED FINISH", "value": band_str, "sub": f"{f_vs_band} vs {ref_label}"},
                        {"label": "REMAINING OTB", "value": f_rem_otb, "sub": f"{days_left} days left"},
                    ],
                    "what_happened": f"Month-end revenue is projected in the range of {band_str}.",
                    "why_it_matters": "Remaining-month bookings may be pacing below last year's close-in pickup (confidence: Medium).",
                    "recommended_action": f"Consider a close-in offer on remaining {month_name} nights.",
                    "by_when": f"Within 2–3 days; {days_left} days left.",
                }
                if "value_at_stake" in facts:
                    fb["at_stake"] = {"value": facts["value_at_stake"], "calc": facts["value_at_stake_calc"]}
            else:
                hypo = [{"text": "Cancellations in the remaining days are the main risk to the finish", "confidence": "High"}]
                directive = {
                    "type": "monitor_only",
                    "target": f"remaining {month_name} OTB",
                    "deadline": "daily until month close",
                    "trigger_if_monitor": f"cancellations exceed the trailing baseline on any remaining {month_name} date",
                }
                fb = {
                    "headline": f"{month_name} on track to finish around {f_vs_band} vs {ref_label}",
                    "evidence": [
                        {"label": "PROJECTED FINISH", "value": band_str, "sub": f"{f_vs_band} vs {ref_label}"},
                        {"label": "REMAINING OTB", "value": f_rem_otb, "sub": "not yet realised"},
                    ],
                    "what_happened": f"Month-end revenue is projected in the range of {band_str}.",
                    "why_it_matters": f"{f_rem_otb} is still on the books, not realised; cancellations in the final {days_left} days are the main risk (confidence: High).",
                    "recommended_action": "No action suggested — worth watching cancellations daily.",
                    "by_when": "Daily until month close; trigger: cancellations above baseline.",
                }

            fb["id"]  = f"proj_{month_name.lower()}_{today.year}"
            fb["tag"] = tag
            candidates.append({
                "signal":     "projection",
                "tag":        tag,
                "score":      round(score, 4),
                "title_hint": fb["headline"],
                "month_num":  today.month,
                "stake_eur":  _parse_eur(facts.get("value_at_stake", "")),
                "insight": {
                    "id": fb["id"],
                    "tag": tag,
                    "score": round(score, 4),
                    "signal": "projection",
                    "stay_period": {"from": today.isoformat(), "to": m_end.isoformat(), "label": f"remaining {month_name}"},
                    "days_to_nearest_arrival": 0,
                    "booking_window_days_left": days_left,
                    "facts": facts,
                    "cause_hypotheses": hypo,
                    "action_directives": directive,
                    "history": {"first_raised": None, "previously_advised": None},
                },
                "fallback_card": fb,
            })

    # Future months projection (gate: >= 10% deviation)
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
            vs_pct, ref_label, ref = (proj_rev - rev_budget) / rev_budget * 100, "budget", rev_budget
        elif rev_final_ly > 0:
            vs_pct, ref_label, ref = (proj_rev - rev_final_ly) / rev_final_ly * 100, "final last year", rev_final_ly
        else:
            continue
        if abs(vs_pct) < 10:
            continue

        m_start, m_end = _month_bounds(today.year, sm)
        days_to_start  = max(0, (m_start - today).days)
        window_left    = max(0, (m_end - today).days)
        m_name = _cal.month_abbr[sm]
        month_label = f"{m_name} {today.year}"
        behind = vs_pct < 0
        tag    = "ALERT" if behind else "OPPORTUNITY"

        R     = min(abs(proj_rev - ref) / daily_rev_baseline, 1.0)
        score = _score_candidate(R, _urgency(days_to_start), _magnitude_pct(vs_pct / 100), C=0.8)

        band_str, band_lo, band_hi = _rev_band(proj_rev)
        vs_lo = (band_lo - ref) / ref * 100 if ref else 0
        vs_hi = (band_hi - ref) / ref * 100 if ref else 0
        f_vs_band = f"{_pct_signed(vs_lo, 0)} to {_pct_signed(vs_hi, 0)}"
        p_full   = f"{m_name} full month"
        p_finish = f"{m_name} month-end finish"

        facts = {
            "month_label":   month_label,
            "proj_rev_band": _fact(band_str, p_finish),
            "vs_ref_band":   _fact(f_vs_band, f"{p_finish}, vs {ref_label}"),
            "ref_label":     ref_label,
            "rev_otb":       _fact(_eur(rev_ty), f"{p_full}, on the books now"),
            "adr_otb":       _fact(_eur(p.get("adr", 0)), f"{p_full}, OTB"),
            "adr_final_ly":  _fact(_eur(p.get("adr_final_ly", 0)), f"{m_name} final last year"),
            "occ_otb":       _fact(_pct(p.get("occ", 0) * 100), f"{p_full}, OTB now"),
            "occ_same_time_ly": _fact(_pct(p.get("stly", 0) * 100), f"{m_name}, same time last year"),
            "final_ly_occ":  _fact(_pct(p.get("final", 0) * 100), f"{m_name} final last year"),
            "days_to_start": str(days_to_start),
        }
        if behind:
            stake = max(0, ref - band_hi)
            if stake >= _STAKE_FLOOR_EUR:
                facts["value_at_stake"]      = _eur(stake)
                facts["value_at_stake_calc"] = f"{ref_label} {_eur(ref)} − upper projection {_eur(band_hi)} = {_eur(stake)}"
            hypo = [{"text": "Booking pace behind last year — group or contracted business may not have re-materialised", "confidence": "Low"}]
            directive = {
                "type": "open_promo",
                "target": f"{month_label} demand-building offer",
                "deadline": "within 7 days",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} projected around {f_vs_band} vs {ref_label}",
                "evidence": [
                    {"label": "PROJECTED FINISH", "value": band_str, "sub": f"{f_vs_band} vs {ref_label}"},
                    {"label": "OCC OTB vs SAME TIME LY", "value": f"{facts['occ_otb']['value']} vs {facts['occ_same_time_ly']['value']}", "sub": f"final last year {facts['final_ly_occ']['value']}"},
                ],
                "what_happened": f"{m_name} is projecting in the range of {band_str}.",
                "why_it_matters": "Booking pace trails last year; group or contracted business may not have re-materialised (confidence: Low).",
                "recommended_action": f"If the trend continues, an option is a demand-building offer for {m_name}.",
                "by_when": f"Within 7 days; {days_to_start} days to month start.",
            }
            if "value_at_stake" in facts:
                fb["at_stake"] = {"value": facts["value_at_stake"], "calc": facts["value_at_stake_calc"]}
        else:
            hypo = [{"text": "Volume position ahead of last year — early rate floors will anchor the final ADR", "confidence": "Medium"}]
            directive = {
                "type": "rate_review_up",
                "target": f"{month_label} rate floors",
                "deadline": "this week",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} projecting around {f_vs_band} vs {ref_label}",
                "evidence": [
                    {"label": "PROJECTED FINISH", "value": band_str, "sub": f"{f_vs_band} vs {ref_label}"},
                    {"label": "ADR OTB vs FINAL LY", "value": f"{facts['adr_otb']['value']} vs {facts['adr_final_ly']['value']}", "sub": f"OTB occ {facts['occ_otb']['value']}"},
                ],
                "what_happened": f"{m_name} is projecting in the range of {band_str}.",
                "why_it_matters": "The volume position is ahead of last year; early rate floors will anchor the final ADR outcome (confidence: Medium).",
                "recommended_action": f"It may be worth reviewing {m_name} rate floors while demand runs ahead.",
                "by_when": "This week.",
            }
        fb["id"]  = f"proj_{m_name.lower()}_{today.year}"
        fb["tag"] = tag
        candidates.append({
            "signal":     "projection",
            "tag":        tag,
            "score":      round(score, 4),
            "title_hint": fb["headline"],
            "month_num":  sm,
            "stake_eur":  _parse_eur(facts.get("value_at_stake", "")),
            "insight": {
                "id": fb["id"],
                "tag": tag,
                "score": round(score, 4),
                "signal": "projection",
                "stay_period": {"from": m_start.isoformat(), "to": m_end.isoformat(), "label": month_label},
                "days_to_nearest_arrival": days_to_start,
                "booking_window_days_left": window_left,
                "facts": facts,
                "cause_hypotheses": hypo,
                "action_directives": directive,
                "history": {"first_raised": None, "previously_advised": None},
            },
            "fallback_card": fb,
        })

    # ── Merge gates (spec C1/C2.4): same story or same dates → one card ──────
    candidates = _merge_same_month_story(candidates)
    candidates = _merge_pickup_soft(candidates)

    # ── Novelty gate (spec C2.3): raised recently without worsening → demote ─
    demoted: list[dict] = []
    if hotel_id:
        candidates, demoted = _novelty_gate(candidates, hotel_id)

    # ── Global ranking — top 5-6 across ALL periods, no per-month quota ──────
    candidates.sort(key=lambda c: c["score"], reverse=True)
    ranked    = [c for c in candidates if c["score"] >= 0.08][:6]
    watchlist = [c for c in candidates if c not in ranked] + demoted

    # ── Headline KPIs ─────────────────────────────────────────────────────────
    yd = data.get("yesterday", {})
    pu = data.get("pickup", {})

    rev_yd  = float(yd.get("revenue",   0))
    rev_ly  = float(yd.get("revenueLY", 0))
    rev_var = round((rev_yd - rev_ly) / rev_ly * 100, 1) if rev_ly else None

    rev_mtd    = float(mtd.get("revenue",   0))
    rev_mtd_ly = float(mtd.get("revenueLY", 0))
    mtd_var    = round((rev_mtd - rev_mtd_ly) / rev_mtd_ly * 100, 1) if rev_mtd_ly else None

    headline = {
        "report_date":         data.get("report_date", ""),
        "hotel_name":          data.get("hotel_name", ""),
        "yday_rev":            round(rev_yd, 0),
        "yday_rev_ly":         round(rev_ly, 0),
        "yday_rev_var_pct":    rev_var,
        "yday_occ_pct":        round(float(yd.get("occupancy",   0)) * 100, 1),
        "yday_occ_ly_pct":     round(float(yd.get("occupancyLY", 0)) * 100, 1),
        "yday_adr":            round(float(yd.get("adr",   0)), 0),
        "yday_adr_ly":         round(float(yd.get("adrLY", 0)), 0),
        "mtd_rev":             round(rev_mtd, 0),
        "mtd_rev_ly":          round(rev_mtd_ly, 0),
        "mtd_rev_var_pct":     mtd_var,
        "mtd_occ_pct":         round(float(mtd.get("occupancy",   0)) * 100, 1),
        "mtd_adr":             round(float(mtd.get("adr", 0)), 0),
        "net_pickup_yday_rn":  int(pu.get("last1d", {}).get("roomNights", 0)),
        "net_pickup_yday_rev": round(float(pu.get("last1d", {}).get("revenue", 0)), 0),
        "cancellations_yday":  int(pu.get("cancellations1d", 0)),
    }

    return {
        "ranked":    ranked,
        "watchlist": watchlist,
        "headline":  headline,
    }


def _merge_same_month_story(candidates: list[dict]) -> list[dict]:
    """Pace + projection for the same month tell one story — merge into the
    higher-scored candidate, folding in the other's key facts."""
    by_month: dict[int, dict] = {}
    result: list[dict] = []
    month_level = [c for c in candidates if c["signal"] in ("pace", "projection")]
    others      = [c for c in candidates if c["signal"] not in ("pace", "projection")]

    for c in month_level:
        m = c.get("month_num")
        if m in by_month:
            keep, drop = (by_month[m], c) if by_month[m]["score"] >= c["score"] else (c, by_month[m])
            prefix = drop["signal"]
            for k, v in drop["insight"]["facts"].items():
                if k in ("month_label",) or k.startswith("value_at_stake"):
                    continue
                keep["insight"]["facts"].setdefault(f"{prefix}_{k}", v)
            by_month[m] = keep
        else:
            by_month[m] = c

    result.extend(by_month.values())
    result.extend(others)
    return result


def _merge_pickup_soft(candidates: list[dict]) -> list[dict]:
    """Pickup ALERT + soft dates in the same month → one softening story."""
    pickup_alerts = [c for c in candidates if c["signal"] == "pickup" and c["tag"] == "ALERT"]
    soft_cands    = [c for c in candidates if c["signal"] == "soft_dates"]
    if not pickup_alerts or not soft_cands:
        return candidates

    soft = soft_cands[0]
    pick = next((p for p in pickup_alerts if p.get("month_num") == soft.get("month_num")), None)
    if pick is None:
        return candidates

    pf, sf = pick["insight"]["facts"], soft["insight"]["facts"]
    month_label = pf["month_label"]
    m_short = month_label.split()[0]
    score = round(min(1.0, max(pick["score"], soft["score"]) + 0.05), 4)

    facts = {
        "month_label":         month_label,
        "pickup_yday_net_rn":  pf["yday_net_rn"],
        "pickup_trailing_avg": pf["trailing_avg"],
        "pickup_z_score":      pf["z_score"],
        "soft_date_range":     sf["date_range"],
        "soft_count":          sf["count"],
        "soft_avg_gap_pts":    sf["avg_gap_pts"],
        "softest_dates":       sf["softest_dates"],
        "value_at_stake":      sf["value_at_stake"],
        "value_at_stake_calc": sf["value_at_stake_calc"],
    }
    fb = {
        "id": f"softening_{m_short.lower()}",
        "tag": "ALERT",
        "headline": f"{m_short} softening: pickup and pace gap point the same way",
        "evidence": [
            {"label": "YESTERDAY NET", "value": pf["yday_net_rn"]["value"], "sub": f"vs {pf['trailing_avg']['value']} prior 7 days"},
            {"label": f"SOFT DATES ({sf['date_range']})", "value": f"{sf['count']} nights {sf['avg_gap_pts']['value']}", "sub": "vs same time last year"},
        ],
        "what_happened": f"{month_label} pickup was {pf['yday_net_rn']['value']} yesterday and {sf['count']} stay dates trail same time last year.",
        "why_it_matters": "Two independent signals point at the same dates, so this looks like genuine softness rather than a one-off (confidence: Medium).",
        "recommended_action": f"Worth checking {m_short} cancellations for a common source; a targeted offer is an option.",
        "by_when": "Investigate today; offer decision within 3–4 days.",
        "at_stake": {"value": sf["value_at_stake"], "calc": sf["value_at_stake_calc"]},
    }
    merged = {
        "signal":     "softening_cluster",
        "tag":        "ALERT",
        "score":      score,
        "title_hint": fb["headline"],
        "month_num":  soft.get("month_num"),
        "stake_eur":  soft.get("stake_eur", 0),
        "insight": {
            "id": fb["id"],
            "tag": "ALERT",
            "score": score,
            "signal": "softening_cluster",
            "stay_period": soft["insight"]["stay_period"],
            "days_to_nearest_arrival": soft["insight"]["days_to_nearest_arrival"],
            "booking_window_days_left": soft["insight"]["booking_window_days_left"],
            "facts": facts,
            "cause_hypotheses": [{"text": "Cancellations and the pace gap point at the same dates — demand genuinely softening rather than a one-off", "confidence": "Medium"}],
            "action_directives": {
                "type": "investigate_cancellations",
                "target": f"{month_label} cancellations by source and rate code; targeted offer on soft dates if broad",
                "deadline": "investigate today; offer decision within 3–4 days",
                "trigger_if_monitor": None,
            },
            "history": {"first_raised": None, "previously_advised": None},
        },
        "fallback_card": fb,
    }
    return [c for c in candidates if c is not pick and c is not soft] + [merged]


def _novelty_gate(candidates: list[dict], hotel_id: str) -> tuple[list[dict], list[dict]]:
    """Spec C2.3: same card id raised in the last 3 days without the value at
    stake worsening >= 10% → demote to watchlist. Fails open on any error."""
    import os
    import requests as _req
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        return candidates, []
    try:
        since = str(_date.today() - timedelta(days=3))
        r = _req.get(
            f"{supabase_url}/rest/v1/briefings",
            params={"hotel_id": f"eq.{hotel_id}", "report_date": f"gte.{since}",
                    "select": "ai_insights,report_date"},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        r.raise_for_status()
        prev_stakes: dict[str, float] = {}
        for row in r.json():
            for ins in (row.get("ai_insights") or {}).get("insights", []):
                cid = ins.get("id")
                if cid:
                    stake = _parse_eur((ins.get("at_stake") or {}).get("value", ""))
                    prev_stakes[cid] = max(prev_stakes.get(cid, 0), stake)
    except Exception as exc:
        print(f"[analyst] Novelty gate skipped (lookup failed): {exc}")
        return candidates, []

    kept, demoted = [], []
    for c in candidates:
        cid = c["insight"]["id"]
        if cid in prev_stakes:
            old, new = prev_stakes[cid], c.get("stake_eur", 0)
            if old > 0 and new < old * 1.10:
                demoted.append(c)
                continue
        kept.append(c)
    if demoted:
        print(f"[analyst] Novelty gate: {len(demoted)} repeat card(s) → watchlist: "
              f"{[d['insight']['id'] for d in demoted]}")
    return kept, demoted


# ─── Layer B: narration ──────────────────────────────────────────────────────

_NARRATION_SYSTEM = """You are the narration layer of a hotel revenue-management AI analyst.
You receive ONE JSON object containing pre-computed, verified facts about
one insight for one hotel. Your only job is to phrase it clearly.

STRICT RULES
1. Use ONLY numbers present in the JSON, copied character-for-character
   (including currency symbols, signs, and decimals). Never compute,
   round, convert, or estimate any number.
2. Output valid JSON matching the OutputCard schema exactly. No prose
   outside JSON.
3. headline: max 12 words, must contain the tension (what changed + why
   it matters), not a bare statistic.
4. what_happened: 1 sentence, factual, includes the key numbers.
5. why_it_matters: 1-2 sentences. Phrase causes from cause_hypotheses as
   hypotheses ("likely driven by...", "may reflect...") and append
   (confidence: X). If cause_hypotheses is empty, write "Cause unclear
   from available data" (confidence: Low).
6. recommended_action: rephrase action_directives naturally. Suggest,
   never command. Never state a specific price unless it appears in facts.
7. by_when: always present. For tag MONITOR include the recheck cadence
   and trigger briefly.
8. at_stake: copy value and calc verbatim from facts. If absent, omit
   the field entirely - never invent a value.
9. Audience: a general manager without a revenue background must
   understand every sentence. No jargon without a plain-language anchor.
10. Language: write in English.
11. Actions are light suggestions. Never use imperative verbs (change,
    remove, increase, decrease, cut, raise, close, open, act) as the
    instruction itself. Use: "Consider...", "It may be worth...",
    "The position could support...". Vary openers across cards.
12. Length caps: headline <=12 words; what_happened <=20; why_it_matters
    <=35; recommended_action <=25; by_when <=10. Shorter is better.
13. Occupancy/revenue projections: only use the low-high band provided
    ("around 91-95%"). Never present a single-point projection or the
    words "will finish at".
14. Every fact carries a "period" label. Attach every number only to the
    period named in its fact. Never blend full-month and remaining-period
    numbers in one sentence."""

_CARD_TOOL: dict[str, Any] = {
    "name": "submit_card",
    "description": "Submit the narrated insight card.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id":       {"type": "string"},
            "tag":      {"type": "string", "enum": ["ALERT", "OPPORTUNITY", "MONITOR"]},
            "headline": {"type": "string"},
            "evidence": {
                "type": "array", "minItems": 2, "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "string"},
                        "sub":   {"type": "string"},
                    },
                    "required": ["label", "value", "sub"],
                },
            },
            "what_happened":      {"type": "string"},
            "why_it_matters":     {"type": "string"},
            "recommended_action": {"type": "string"},
            "by_when":            {"type": "string"},
            "at_stake": {
                "type": "object",
                "properties": {"value": {"type": "string"}, "calc": {"type": "string"}},
                "required": ["value", "calc"],
            },
        },
        "required": ["id", "tag", "headline", "evidence", "what_happened",
                     "why_it_matters", "recommended_action", "by_when"],
    },
}

_SUMMARY_TOOL: dict[str, Any] = {
    "name": "submit_summary",
    "description": "Submit the one-sentence executive summary.",
    "input_schema": {
        "type": "object",
        "properties": {"executive_summary": {"type": "string"}},
        "required": ["executive_summary"],
    },
}

_NUM_TOKEN = re.compile(r"\d(?:[\d,\.]*\d)?")

_BANNED_IMPERATIVES = {"change", "remove", "increase", "decrease", "cut",
                       "raise", "close", "open", "act", "lower", "lift"}

_WORD_CAPS = {"headline": 12, "what_happened": 20, "why_it_matters": 35,
              "recommended_action": 25, "by_when": 10}


def _bad_numbers(card_out: dict, haystack: str) -> list[str]:
    out_text = json.dumps(card_out, ensure_ascii=False)
    return sorted({tok for tok in _NUM_TOKEN.findall(out_text) if tok not in haystack})


def _style_violations(card: dict) -> list[str]:
    """Spec B1/B2: word caps + banned imperative action openers."""
    v = []
    for field, cap in _WORD_CAPS.items():
        text = card.get(field, "")
        if text and len(text.split()) > cap:
            v.append(f"{field} is {len(text.split())} words (max {cap})")
    first = card.get("recommended_action", "").strip().split(" ")[0].lower().strip(",.:;")
    if first in _BANNED_IMPERATIVES:
        v.append(f"recommended_action starts with imperative '{first}' — use a soft opener (Consider…, It may be worth…)")
    return v


def _period_violations(card: dict, facts: dict) -> list[str]:
    """Spec D1: a sentence must not blend full-month and remaining-period numbers."""
    tok_periods: dict[str, set] = {}
    for f in facts.values():
        if isinstance(f, dict) and "value" in f:
            period = (f.get("period") or "").lower()
            kind = "remaining" if "remaining" in period else ("full" if "full" in period else "")
            if not kind:
                continue
            for t in _NUM_TOKEN.findall(str(f["value"])):
                tok_periods.setdefault(t, set()).add(kind)

    v = []
    text = " ".join(str(card.get(k, "")) for k in ("headline", "what_happened", "why_it_matters"))
    for sentence in re.split(r"(?<=[.;])\s+", text):
        kinds = set()
        for t in _NUM_TOKEN.findall(sentence):
            p = tok_periods.get(t)
            if p and len(p) == 1:
                kinds |= p
        if len(kinds) > 1:
            v.append(f"sentence blends full-month and remaining-period numbers: '{sentence[:80]}'")
    return v


def _narrate_card(wrapper: dict, fallback_card: dict, meta: dict | None = None) -> dict:
    """One Claude call per card; validated; max 2 retries; then fallback.
    When `meta` is given, appends a per-card audit entry (facts given, attempts,
    validation problems, tokens, latency, fallback flag)."""
    haystack = json.dumps(wrapper, ensure_ascii=False)
    facts    = wrapper["insight"]["facts"]
    base_prompt = json.dumps(wrapper, ensure_ascii=False, indent=2)
    prompt = base_prompt

    t0 = _time.monotonic()
    audit = {
        "card_id":   wrapper["insight"]["id"],
        "tag":       wrapper["insight"]["tag"],
        "signal":    wrapper["insight"]["signal"],
        "score":     wrapper["insight"]["score"],
        "facts":     facts,
        "attempts":  0,
        "validation_problems": [],
        "fallback_used": False,
        **_usage_zero(),
    }

    result_card = None
    for attempt in range(3):
        audit["attempts"] = attempt + 1
        try:
            response = _get_client().messages.create(
                model=_MODEL,
                max_tokens=1500,
                temperature=0.2,
                system=[{"type": "text", "text": _NARRATION_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[_CARD_TOOL],
                tool_choice={"type": "tool", "name": "submit_card"},
                messages=[{"role": "user", "content": prompt}],
            )
            _usage_add(audit, response.usage)
            card = next(b for b in response.content if b.type == "tool_use").input
        except Exception as exc:
            print(f"[analyst] Card narration error (attempt {attempt + 1}): {exc}")
            audit["validation_problems"].append(f"api_error: {str(exc)[:200]}")
            continue

        problems = (_bad_numbers(card, haystack)
                    + _style_violations(card)
                    + _period_violations(card, facts))
        if not problems:
            result_card = _harden_card(card, wrapper)
            break

        print(f"[analyst] Card '{wrapper['insight']['id']}' attempt {attempt + 1} rejected: {problems}")
        audit["validation_problems"].extend(str(p) for p in problems)
        prompt = (base_prompt +
                  "\n\nPREVIOUS ATTEMPT REJECTED for these violations:\n- "
                  + "\n- ".join(str(p) for p in problems)
                  + "\nFix every violation. Copy numbers character-for-character from the input only.")

    if result_card is None:
        print(f"[analyst] Card '{wrapper['insight']['id']}': validation failed twice — using templated fallback.")
        audit["fallback_used"] = True
        result_card = dict(fallback_card)

    audit["latency_ms"] = int((_time.monotonic() - t0) * 1000)
    if meta is not None:
        meta["cards_audit"].append(audit)
        _usage_add_dict(meta["usage"], audit)
    return result_card


def _usage_add_dict(total: dict, other: dict) -> None:
    for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
        total[k] += other.get(k, 0)


def _harden_card(card: dict, wrapper: dict) -> dict:
    """id/tag/at_stake are authoritative from the compute layer."""
    insight = wrapper["insight"]
    facts   = insight["facts"]
    card["id"]  = insight["id"]
    card["tag"] = insight["tag"]
    if "value_at_stake" in facts and "value_at_stake_calc" in facts:
        card["at_stake"] = {"value": facts["value_at_stake"], "calc": facts["value_at_stake_calc"]}
    else:
        card.pop("at_stake", None)
    return card


def _narrate_summary(hotel_name: str, cards: list[dict], meta: dict | None = None) -> str:
    fallback = f"Today's focus: {cards[0]['headline']}" if cards else ""
    if not cards:
        return fallback
    digest = [{"tag": c["tag"], "headline": c["headline"],
               "at_stake": (c.get("at_stake") or {}).get("value")} for c in cards[:3]]
    haystack = json.dumps(digest, ensure_ascii=False)
    prompt = (f"Hotel: {hotel_name}. Top insights this morning:\n"
              f"{json.dumps(digest, ensure_ascii=False, indent=2)}\n\n"
              "Write ONE sentence (max 35 words) naming the single most urgent revenue focus today. "
              "Soft, advisory tone — no imperatives. Use ONLY numbers that appear verbatim above.")
    try:
        response = _get_client().messages.create(
            model=_MODEL,
            max_tokens=300,
            temperature=0.2,
            tools=[_SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "submit_summary"},
            messages=[{"role": "user", "content": prompt}],
        )
        if meta is not None:
            _usage_add(meta["usage"], response.usage)
        result = next(b for b in response.content if b.type == "tool_use").input
        summary = result.get("executive_summary", "")
        if summary and not _bad_numbers({"s": summary}, haystack) and len(summary.split()) <= 40:
            return summary
    except Exception as exc:
        print(f"[analyst] Summary narration error: {exc}")
    return fallback


# ─── Legacy field mapping (current PWA renders these) ─────────────────────────

_TAG_TO_TYPE = {"ALERT": "warning", "OPPORTUNITY": "opportunity", "MONITOR": "monitor"}


def _evidence_direction(ev: dict) -> str:
    s = ev.get("value", "") + " " + ev.get("sub", "")
    if "−" in s or "-€" in s:
        return "down"
    if "+" in s:
        return "up"
    return "neutral"


def _card_to_insight(card: dict, priority: int) -> dict:
    action = card["recommended_action"]
    if card.get("by_when"):
        action += f" By when: {card['by_when']}"
    if card.get("at_stake"):
        action += f" At stake: {card['at_stake']['value']}."
    return {
        # New card anatomy (spec v1.2)
        "id":                 card["id"],
        "tag":                card["tag"],
        "headline":           card["headline"],
        "evidence":           card["evidence"],
        "what_happened":      card["what_happened"],
        "why_it_matters":     card["why_it_matters"],
        "recommended_action": card["recommended_action"],
        "by_when":            card["by_when"],
        **({"at_stake": card["at_stake"]} if card.get("at_stake") else {}),
        # Legacy fields (current PWA)
        "priority": priority,
        "type":     _TAG_TO_TYPE.get(card["tag"], "observation"),
        "title":    card["headline"],
        "kpis": [
            {"label": ev["label"], "value": ev["value"], "sub": ev.get("sub", ""),
             "direction": _evidence_direction(ev)}
            for ev in card["evidence"][:2]
        ],
        "findings": [card["what_happened"], card["why_it_matters"]],
        "action":   action,
    }


_STUB = {"executive_summary": "", "insights": []}


# ─── Layer B: entry point ────────────────────────────────────────────────────

def generate_insights(data: dict[str, Any], hotel_id: str | None = None) -> dict[str, Any]:
    if not config.ANTHROPIC_API_KEY:
        print("[analyst] No ANTHROPIC_API_KEY — skipping AI insights.")
        return _STUB

    try:
        # v4 path needs the new SQL payload fields; old-fetcher payloads → legacy.
        # The contract's data_quality block is authoritative when present.
        dq = data.get("data_quality") or {}
        if dq:
            has_new_data = not dq.get("legacy_mode", False)
        else:
            has_new_data = bool(data.get("pickup_daily") or data.get("otb_by_date")
                                or data.get("current_month_remaining"))
        if not has_new_data:
            print("[analyst] Payload has no new signal data (old fetcher on hotel server) "
                  "— using legacy prompt.")
            return _legacy_generate(data)

        if hotel_id is None:
            import os
            hotel_id = os.getenv("SUPABASE_HOTEL_ID") or None

        computed = _compute_signals(data, hotel_id=hotel_id)
        ranked   = computed["ranked"]
        print(f"[analyst] Compute: {len(ranked)} ranked signals, "
              f"{len(computed['watchlist'])} watchlist")
        if not ranked:
            print("[analyst] No signals above threshold — falling back to legacy prompt.")
            return _legacy_generate(data)

        hotel_name = data.get("hotel_name", config.HOTEL_NAME)
        briefing_date = _date.today().isoformat()

        meta: dict[str, Any] = {"cards_audit": [], "usage": _usage_zero(),
                                "model": _MODEL, "prompt_version": _PROMPT_VERSION}
        cards: list[dict] = []
        for cand in ranked[:5]:
            wrapper = {
                "property":       hotel_name,
                "briefing_date":  briefing_date,
                "capacity_rooms": config.TOTAL_ROOMS,
                "insight":        cand["insight"],
            }
            card = _narrate_card(wrapper, cand["fallback_card"], meta=meta)
            cards.append(card)
            print(f"[analyst] Card {len(cards)}: [{card['tag']}] {card['headline'][:60]}")

        summary = _narrate_summary(hotel_name, cards, meta=meta)
        meta["fallback_cards"] = sum(1 for a in meta["cards_audit"] if a["fallback_used"])
        meta["estimated_cost_usd"] = _estimate_cost_usd(meta["usage"])
        result = {
            "executive_summary": summary,
            "insights": [_card_to_insight(c, i + 1) for i, c in enumerate(cards)],
            "_meta": meta,
        }
        print(f"[analyst] Narration complete: {len(cards)} cards "
              f"({meta['fallback_cards']} fallback) — "
              f"{meta['usage']['input_tokens']}+{meta['usage']['output_tokens']} tokens, "
              f"~${meta['estimated_cost_usd']}")

        try:
            from pathlib import Path as _Path
            cacheable = {k: v for k, v in result.items() if k != "_meta"}
            _Path("ai_cache.json").write_text(
                json.dumps(cacheable, ensure_ascii=False, indent=2), encoding="utf-8"
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

_LEGACY_TOOL: dict[str, Any] = {
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


def _legacy_generate(data: dict[str, Any]) -> dict[str, Any]:
    """Original prompt-based approach — used as fallback until new queries deploy."""
    import calendar as _cal2
    from datetime import date as _d2

    _LEGACY_SYSTEM = """You are an expert hotel revenue analyst delivering a morning briefing.
Return 3-5 insights ranked by revenue impact and urgency.
Focus on: OTB pace vs STLY/Final LY, pickup trends, projected month-end outcomes.
For projections: use Proj Finish = OTB + (Final LY - STLY). Never re-derive this.
Output format: executive_summary and insights array via submit_briefing tool.
Type: warning | opportunity | observation | monitor.
Title: lead with EUR number. Max 70 chars.
KPIs: exactly 2 chips with value and sub delta.
Findings: exactly 2 bullets (max 25 words each). Plain text only, no markdown.
Action: one sentence, soft advisory tone ("Consider...", "It may be worth...") — never imperative."""

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
            model=_MODEL,
            max_tokens=4096,
            temperature=0.3,
            system=[{"type": "text", "text": _LEGACY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[_LEGACY_TOOL],
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
        usage = _usage_zero()
        _usage_add(usage, response.usage)
        result["_meta"] = {"cards_audit": [], "usage": usage, "model": _MODEL,
                           "prompt_version": "legacy-v1", "fallback_cards": 0,
                           "estimated_cost_usd": _estimate_cost_usd(usage), "legacy": True}
        try:
            from pathlib import Path as _Path
            cacheable = {k: v for k, v in result.items() if k != "_meta"}
            _Path("ai_cache.json").write_text(
                json.dumps(cacheable, ensure_ascii=False, indent=2), encoding="utf-8"
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
