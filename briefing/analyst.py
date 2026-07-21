"""
AI Analyst — Daily Briefing Generator (v3)

Implements ai-insight-cards-spec v1.1:
  Layer A (_compute_signals): deterministic compute — all math happens here. Each
      candidate insight is a full narration-contract object: facts pre-formatted as
      display strings, value_at_stake with its calc, cause hypotheses with confidence,
      an action directive enum, and a templated fallback card.

  Layer B (generate_insights): narration — ONE Claude call per card. A numeric
      validator rejects any output number not present verbatim in the input facts
      (max 2 retries, then the deterministic fallback card ships instead).

Output keeps the legacy fields (title/kpis/findings/action) alongside the new card
anatomy (headline/evidence/what_happened/why_it_matters/recommended_action/by_when/
at_stake) so the current PWA renders unchanged until it adopts the new layout.
"""

import calendar as _cal
import json
import re
import statistics
from datetime import date as _date, timedelta
from typing import Any

import anthropic
import config

_client = None
_MODEL = "claude-sonnet-4-6"


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ─── Display-string formatters (all facts are FINAL display strings) ──────────

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


# ─── Layer A: main compute function ──────────────────────────────────────────

def _compute_signals(data: dict) -> dict:
    """
    Returns {"ranked": [insight_input, ...], "watchlist": [...], "headline": {...}}.
    Each ranked entry is the FULL narration input contract (spec §2) plus
    tag/score/title_hint at top level and a deterministic fallback_card.
    """
    today = _date.today()
    yesterday = today - timedelta(days=1)
    total_rooms = config.TOTAL_ROOMS

    pace = data.get("pace", [])
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

            if abs(z) < 1.5 and abs(yday_rn) < 5:
                continue

            m_name      = _cal.month_abbr[sm]
            month_label = f"{m_name} {sy}"
            m_start, m_end = _month_bounds(sy, sm)
            days_to_start  = max(0, (m_start - today).days)
            window_left    = max(0, (m_end - today).days)

            pace_m = next((p for p in pace if p.get("month_num") == sm), None)
            adr_ly = pace_m.get("adr_final_ly", 150.0) if pace_m else 150.0
            gap_per_day  = abs(yday_rn - mean_rn)
            rev_at_stake = gap_per_day * adr_ly * 7  # 7-day extrapolation

            R = min(rev_at_stake / daily_rev_baseline, 1.0)
            U = _urgency(days_to_start)
            M = _magnitude_z(z)
            C = _confidence(max(abs(yday_rn), abs(mean_rn)))
            score = _score_candidate(R, U, M, C=C)

            # Spec: negative pickup is never a passive MONITOR
            negative = yday_rn < 0 or z < 0
            tag = "ALERT" if negative else "OPPORTUNITY"

            f_yday_net  = _rn_signed(yday_rn)
            f_avg       = ("+" if mean_rn >= 0 else "−") + f"{abs(mean_rn):.1f} rn/day"
            f_z         = ("+" if z >= 0 else "−") + f"{abs(z):.1f}"
            f_yday_rev  = _eur_signed(yday_rev)
            f_stake     = _eur(rev_at_stake)
            f_stake_calc = (f"{gap_per_day:.1f} rn/day below avg × {_eur(adr_ly)} ADR × 7 days = {_eur(rev_at_stake)}"
                            if negative else
                            f"{gap_per_day:.1f} rn/day above avg × {_eur(adr_ly)} ADR × 7 days = {_eur(rev_at_stake)}")

            if negative:
                hypo = [{"text": f"Cancellations or a demand slowdown for {month_label} — a single source, rate code, or group may account for the swing", "confidence": "Low"}]
                directive = {
                    "type": "investigate_cancellations",
                    "target": f"yesterday's {month_label} cancellations and new bookings by source and rate code",
                    "deadline": "today — confirm whether the slowdown is one-off or a trend",
                    "trigger_if_monitor": None,
                }
                fb_headline = f"{month_label} pickup {'turned negative' if yday_rn < 0 else 'slowing'}: {f_yday_net} yesterday vs {f_avg} average"
                fb_why = f"Likely cancellations or a demand slowdown for {month_label} — a single source, rate code, or group may account for the swing (confidence: Low)."
                fb_action = f"Check yesterday's {month_label} cancellations and bookings for a common source or rate code."
                fb_by_when = "Today — confirm whether the slowdown is one-off before the next briefing."
            else:
                hypo = [{"text": f"Demand surge for {month_label} — possibly a campaign, event, or group materialising", "confidence": "Low"}]
                directive = {
                    "type": "rate_review_up",
                    "target": f"open {month_label} nights riding the surge",
                    "deadline": "within 2 days — capture the demand at higher rates while it lasts",
                    "trigger_if_monitor": None,
                }
                fb_headline = f"{month_label} pickup surge: {f_yday_net} yesterday vs {f_avg} average"
                fb_why = f"Likely a demand surge for {month_label} — possibly a campaign, event, or group materialising (confidence: Low)."
                fb_action = f"Review rates on open {month_label} nights — the surge supports testing a lift."
                fb_by_when = "Within 2 days — while the surge lasts."

            facts = {
                "month_label":     month_label,
                "yday_net_rn":     f_yday_net,
                "trailing_avg":    f_avg,
                "z_score":         f_z,
                "yday_net_rev":    f_yday_rev,
                "days_to_start":   str(days_to_start),
                "value_at_stake":  f_stake,
                "value_at_stake_calc": f_stake_calc,
            }
            fallback_card = {
                "id": f"pickup_{m_name.lower()}_{sy}",
                "tag": tag,
                "headline": fb_headline,
                "evidence": [
                    {"label": "YESTERDAY NET", "value": f_yday_net, "sub": f"vs {f_avg} 7-day avg"},
                    {"label": "REVENUE IMPACT", "value": f_yday_rev, "sub": f"z-score {f_z}"},
                ],
                "what_happened": f"Net pickup for {month_label} was {f_yday_net} yesterday against a 7-day average of {f_avg}.",
                "why_it_matters": fb_why,
                "recommended_action": fb_action,
                "by_when": fb_by_when,
                "at_stake": {"value": f_stake, "calc": f_stake_calc},
            }

            candidates.append({
                "signal":     "pickup",
                "tag":        tag,
                "score":      round(score, 4),
                "title_hint": fb_headline,
                "month_num":  sm,
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

    # ── Signal 2: Pace vs STLY by future month ────────────────────────────────
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
        if abs(pct_gap) < 0.05 and abs(rn_gap) < 10:
            continue

        rev_ty       = p["rev"]
        rev_stly     = p.get("rev_stly", 0)
        rev_final_ly = p.get("rev_final", 0)
        rn_final_ly  = p.get("rn_final_ly", 0)
        adr_ty       = p.get("adr", 0)
        adr_final_ly = p.get("adr_final_ly", 150.0)
        adr_gap      = adr_ty - adr_final_ly

        exp_remaining_rn  = max(0, rn_final_ly - rn_stly)
        proj_rn           = rn_ty + exp_remaining_rn
        cap_rn            = total_rooms * days_in
        proj_occ          = proj_rn / cap_rn * 100 if cap_rn else 0.0
        final_ly_occ      = p.get("final", 0) * 100
        occ_delta         = proj_occ - final_ly_occ
        open_rn           = max(0, cap_rn - rn_ty)

        rev_delta    = rev_ty - rev_stly
        rev_at_stake = abs(rn_gap) * adr_final_ly

        R = min(rev_at_stake / daily_rev_baseline, 1.0)
        U = _urgency(days_to_start)
        M = _magnitude_pct(pct_gap)
        C = _confidence(rn_stly)
        score = _score_candidate(R, U, M, C=C)

        m_name       = _cal.month_abbr[sm]
        month_label  = f"{m_name} {today.year}"
        period_label = f"remaining {today.strftime('%B')}" if current_month else month_label
        ahead        = rn_gap > 0
        adr_dilution = ahead and adr_gap < -5

        f_pct_gap    = _pct_signed(pct_gap * 100)
        f_rev_delta  = _eur_signed(rev_delta)
        f_rn_gap     = _rn_signed(rn_gap)
        f_proj_occ   = _pct(proj_occ)
        f_final_occ  = _pct(final_ly_occ)
        f_occ_delta  = _pts_signed(occ_delta)
        f_adr_otb    = _eur(adr_ty)
        f_adr_fly    = _eur(adr_final_ly)
        f_adr_gap    = _eur_signed(adr_gap)
        f_open_rn    = f"{open_rn:,}"

        facts = {
            "month_label":       month_label,
            "pace_vs_stly":      f_pct_gap,
            "rev_lead" if ahead else "rev_gap": f_rev_delta,
            "rn_otb":            f"{rn_ty:,}",
            "rn_stly":           f"{rn_stly:,}",
            "rn_gap":            f_rn_gap,
            "proj_finish_occ":   f_proj_occ,
            "final_ly_occ":      f_final_occ,
            "occ_delta_pts":     f_occ_delta,
            "adr_otb":           f_adr_otb,
            "adr_final_ly":      f_adr_fly,
            "adr_delta":         f_adr_gap,
            "open_room_nights_remaining": f_open_rn,
            "booking_window_days_left":   str(window_left),
        }

        if adr_dilution:
            tag   = "OPPORTUNITY"
            stake = open_rn * abs(adr_gap)
            f_stake      = _eur(stake)
            f_stake_calc = f"{f_open_rn} open rn × {_eur(abs(adr_gap))} ADR gap = {_eur(stake)}"
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f_stake_calc
            hypo = [{"text": "Early-season discounted rate codes still open despite compression", "confidence": "Medium"}]
            directive = {
                "type": "rate_review_up",
                "target": f"{period_label} open nights, lowest rate codes",
                "deadline": f"this week — window closes as {m_name} fills",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name}: OTB {f_occ_delta} vs LY but ADR {f_adr_gap} eroding the upside",
                "evidence": [
                    {"label": "PROJ FINISH OCC", "value": f_proj_occ, "sub": f"{f_occ_delta} vs Final LY {f_final_occ}"},
                    {"label": "ADR OTB VS FINAL LY", "value": f"{f_adr_otb} vs {f_adr_fly}", "sub": f"{f_adr_gap} per room night"},
                ],
                "what_happened": f"{m_name} is projected to finish at {f_proj_occ} occupancy, {f_occ_delta} vs Final LY — volume is not the problem.",
                "why_it_matters": f"At {f_adr_gap} ADR vs Final LY, the rate gap is diluting a strong revenue beat. Likely early-season discounted rate codes still open despite compression (confidence: Medium).",
                "recommended_action": f"Review rate floors on {period_label} open nights — close or lift the lowest rate codes; the occupancy lead supports it.",
                "by_when": f"This week — the window closes as {m_name} fills ({window_left} days left).",
                "at_stake": {"value": f_stake, "calc": f_stake_calc},
            }
        elif ahead:
            tag = "OPPORTUNITY"
            hypo = [{"text": "Demand running above last year — remaining inventory likely underpriced for this demand level", "confidence": "Medium"}]
            directive = {
                "type": "rate_review_up",
                "target": f"{period_label} nights — close lowest rate codes, test a lift on peak nights",
                "deadline": "next 2–3 days, before close-in bookings fill the gap at current rates",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} pacing {f_pct_gap} ahead of LY — remaining nights can carry higher rates",
                "evidence": [
                    {"label": "PACE VS STLY", "value": f_pct_gap, "sub": f"{f_rev_delta} revenue lead"},
                    {"label": "OPEN RN REMAINING", "value": f"{f_open_rn} rn", "sub": f"{window_left} days of window left"},
                ],
                "what_happened": f"{m_name} OTB revenue is {f_rev_delta} vs the same time last year, {f_pct_gap}.",
                "why_it_matters": f"Demand is running above last year with {window_left} selling days left — remaining inventory is likely underpriced for this demand level (confidence: Medium).",
                "recommended_action": f"Review rates on {period_label} nights — close the lowest rate codes and test a lift on peak nights.",
                "by_when": "Next 2–3 days, before close-in bookings fill the gap at current rates.",
            }
        else:
            tag   = "ALERT"
            stake = abs(rn_gap) * adr_final_ly
            f_stake      = _eur(stake)
            f_stake_calc = f"{abs(rn_gap):,} rn gap × {f_adr_fly} Final LY ADR = {_eur(stake)}"
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f_stake_calc
            hypo = [{"text": "Demand softness or channel shift vs last year — source-level comparison needed to isolate the driver", "confidence": "Low"}]
            directive = {
                "type": "open_promo",
                "target": f"{month_label} soft nights — targeted offer or rate action",
                "deadline": f"within 7 days — {window_left} days of booking window remain",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} pacing {f_pct_gap} behind LY — {f_stake} at stake",
                "evidence": [
                    {"label": "PACE VS STLY", "value": f_pct_gap, "sub": f"{f_rev_delta} revenue"},
                    {"label": "ROOM NIGHTS", "value": f"{rn_ty:,} vs {rn_stly:,}", "sub": f"{f_rn_gap} vs STLY"},
                ],
                "what_happened": f"{m_name} OTB is {f_rn_gap} vs the same time last year ({rn_ty:,} vs {rn_stly:,} room nights).",
                "why_it_matters": "May reflect demand softness or a channel shift vs last year — a source-level comparison is needed to isolate the driver (confidence: Low).",
                "recommended_action": f"Consider a targeted offer or rate action on {month_label} soft nights.",
                "by_when": f"Within 7 days — {window_left} days of booking window remain.",
                "at_stake": {"value": f_stake, "calc": f_stake_calc},
            }

        fb["id"]  = f"pace_{m_name.lower()}_{today.year}"
        fb["tag"] = tag

        candidates.append({
            "signal":     "pace",
            "tag":        tag,
            "score":      round(score, 4),
            "title_hint": fb["headline"],
            "month_num":  sm,
            "insight": {
                "id": fb["id"],
                "tag": tag,
                "score": round(score, 4),
                "signal": "pace",
                "stay_period": {"from": max(m_start, today).isoformat(), "to": m_end.isoformat(), "label": period_label},
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

            # ADR reference: OTB ADR of the majority month, else 130
            months = [d["date"].month for d in top_soft]
            major_month = max(set(months), key=months.count)
            pace_m  = next((p for p in pace if p.get("month_num") == major_month), None)
            adr_ref = pace_m.get("adr", 0) or pace_m.get("adr_final_ly", 130.0) if pace_m else 130.0

            stake        = gap_rn_total * adr_ref
            f_stake      = _eur(stake)
            f_stake_calc = f"{gap_rn_total} rn gap × {_eur(adr_ref)} ADR = {_eur(stake)}"

            R = min(stake / daily_rev_baseline, 1.0)
            M = _magnitude_pct(avg_gap_pp / 100)
            score = _score_candidate(R, avg_urgency, M, C=0.85)

            sorted_by_gap = sorted(top_soft, key=lambda d: d["gap_pp"])
            softest = sorted_by_gap[:2]
            date_labels = ", ".join(d["label"] for d in top_soft)
            f_avg_gap   = _pts_signed(-avg_gap_pp)

            facts = {
                "dates":            date_labels,
                "count":            str(len(top_soft)),
                "avg_gap_pts":      f_avg_gap,
                "softest_dates":    " · ".join(f"{d['label']} ({_pts_signed(d['gap_pp'])})" for d in softest),
                "per_date":         [
                    {"date": d["label"], "dow": d["dow"], "occ_otb": _pct(d["occ_ty"], 0),
                     "occ_stly": _pct(d["occ_stly"], 0), "gap": _pts_signed(d["gap_pp"])}
                    for d in top_soft
                ],
                "nearest_date":     top_soft[0]["label"],
                "nearest_days_out": str(top_soft[0]["days_out"]),
                "value_at_stake":   f_stake,
                "value_at_stake_calc": f_stake_calc,
            }
            fb = {
                "id": f"soft_dates_{_cal.month_abbr[major_month].lower()}",
                "tag": "ALERT",
                "headline": f"{len(top_soft)} nights {f_avg_gap} behind LY — rate action window closing",
                "evidence": [
                    {"label": "SOFTEST", "value": facts["softest_dates"], "sub": "vs same time LY"},
                    {"label": "AVG GAP", "value": f"{f_avg_gap} vs STLY", "sub": f"{f_stake} at risk"},
                ],
                "what_happened": f"{len(top_soft)} stay dates ({date_labels}) are pacing an average {f_avg_gap} behind the same time last year.",
                "why_it_matters": "May reflect genuine demand softness on these dates rather than a one-off (confidence: Medium).",
                "recommended_action": f"Consider a targeted rate reduction or package on the soft dates; prioritise {facts['softest_dates'].split(' · ')[0]} (largest gap).",
                "by_when": f"Within 7 days — the typical booking window for these dates is closing.",
                "at_stake": {"value": f_stake, "calc": f_stake_calc},
            }
            candidates.append({
                "signal":     "soft_dates",
                "tag":        "ALERT",
                "score":      round(score, 4),
                "title_hint": fb["headline"],
                "month_num":  major_month,
                "insight": {
                    "id": fb["id"],
                    "tag": "ALERT",
                    "score": round(score, 4),
                    "signal": "soft_dates",
                    "stay_period": {"from": top_soft[0]["date"].isoformat(), "to": top_soft[-1]["date"].isoformat(), "label": date_labels},
                    "days_to_nearest_arrival": top_soft[0]["days_out"],
                    "booking_window_days_left": top_soft[0]["days_out"],
                    "facts": facts,
                    "cause_hypotheses": [{"text": "Demand softness concentrated on these dates — check for LY events or group business that has not repeated", "confidence": "Medium"}],
                    "action_directives": {
                        "type": "rate_review_down",
                        "target": f"soft dates {date_labels}, largest gaps first",
                        "deadline": "within 7 days — booking window closing",
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
            date_labels = ", ".join(d["label"] for d in top_hot)
            nearest     = top_hot[0]
            score = _score_candidate(0.5, avg_urgency, 0.7, C=0.9)

            facts = {
                "dates": date_labels,
                "count": str(len(top_hot)),
                "per_date": [
                    {"date": d["label"], "dow": d["dow"], "occ_otb": _pct(d["occ_ty"], 0),
                     "rooms_left": str(d["rooms_left"])}
                    for d in top_hot
                ],
                "nearest_date":      nearest["label"],
                "nearest_days_out":  str(nearest["days_out"]),
                "nearest_occ":       _pct(nearest["occ_ty"], 0),
                "nearest_rooms_left": str(nearest["rooms_left"]),
            }
            fb = {
                "id": "hot_dates_near_full",
                "tag": "OPPORTUNITY",
                "headline": f"{date_labels} near sell-out — last rooms selling at everyday rates",
                "evidence": [
                    {"label": f"{nearest['label'].upper()} ({nearest['dow'].upper()})", "value": f"{_pct(nearest['occ_ty'], 0)} occ", "sub": f"{nearest['rooms_left']} rooms left"},
                    {"label": "NEAR-FULL DATES", "value": " · ".join(_pct(d["occ_ty"], 0) for d in top_hot), "sub": date_labels},
                ],
                "what_happened": f"{len(top_hot)} nights ({date_labels}) are near sell-out with only a handful of rooms left, still selling at unchanged rates.",
                "why_it_matters": "Compression this close-in means the remaining rooms would very likely sell at a higher price — every unlifted euro is left on the table (confidence: High).",
                "recommended_action": f"Raise BAR and close discounted rate codes on {date_labels} for the remaining rooms.",
                "by_when": f"Today — {nearest['label']} is {nearest['days_out']} day(s) out; the window is hours, not days.",
            }
            candidates.append({
                "signal":     "hot_dates",
                "tag":        "OPPORTUNITY",
                "score":      round(score, 4),
                "title_hint": fb["headline"],
                "month_num":  top_hot[0]["date"].month,
                "insight": {
                    "id": fb["id"],
                    "tag": "OPPORTUNITY",
                    "score": round(score, 4),
                    "signal": "hot_dates",
                    "stay_period": {"from": top_hot[0]["date"].isoformat(), "to": top_hot[-1]["date"].isoformat(), "label": date_labels},
                    "days_to_nearest_arrival": nearest["days_out"],
                    "booking_window_days_left": nearest["days_out"],
                    "facts": facts,
                    "cause_hypotheses": [{"text": "Close-in compression — demand exceeding remaining supply on these dates", "confidence": "High"}],
                    "action_directives": {
                        "type": "close_discounts",
                        "target": f"remaining rooms on {date_labels} — raise BAR, close discounted codes",
                        "deadline": f"today — {nearest['label']} is {nearest['days_out']} day(s) out",
                        "trigger_if_monitor": None,
                    },
                    "history": {"first_raised": None, "previously_advised": None},
                },
                "fallback_card": fb,
            })

    # ── Signal 5: Month-end revenue projection ────────────────────────────────
    cm  = data.get("current_month_remaining", {})
    mtd = data.get("mtd", {})

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

        vs_final_ly_pct = ((proj_rev - rev_final_ly) / rev_final_ly * 100) if rev_final_ly > 0 else None
        vs_budget_pct   = ((proj_rev - rev_budget) / rev_budget * 100) if rev_budget > 0 else None
        vs_pct    = vs_budget_pct if vs_budget_pct is not None else (vs_final_ly_pct or 0)
        ref_label = "budget" if vs_budget_pct is not None else "Final LY"

        if abs(vs_pct) >= 3:
            month_name  = today.strftime("%B")
            _, m_end    = _month_bounds(today.year, today.month)
            days_left   = max(0, (m_end - today).days)
            behind      = vs_pct < 0
            # Spec: an on-track/ahead projection is a position to defend → MONITOR
            tag = "ALERT" if behind else "MONITOR"

            ref   = rev_budget if rev_budget > 0 else rev_final_ly
            R     = min(abs(proj_rev - ref) / daily_rev_baseline, 1.0)
            score = _score_candidate(R, _urgency(0), _magnitude_pct(vs_pct / 100), C=0.85)

            f_proj    = _eur(proj_rev)
            f_vs      = _pct_signed(vs_pct)
            f_rem_otb = _eur(rev_rem_otb)

            facts = {
                "month_label":     f"{month_name} {today.year}",
                "proj_revenue":    f_proj,
                "vs_ref":          f_vs,
                "ref_label":       ref_label,
                "rev_mtd":         _eur(rev_mtd),
                "remaining_otb":   f_rem_otb,
                "exp_rem_pickup":  _eur(exp_rem_pickup),
                "days_left":       str(days_left),
            }
            if behind:
                stake = abs(proj_rev - ref)
                f_stake = _eur(stake)
                facts["value_at_stake"]      = f_stake
                facts["value_at_stake_calc"] = f"{ref_label} {_eur(ref)} − projected {f_proj} = {f_stake} shortfall"
                hypo = [{"text": "Remaining-month OTB pacing below last year's close-in pickup", "confidence": "Medium"}]
                directive = {
                    "type": "open_promo",
                    "target": f"remaining {month_name} nights — close-in offer or rate action",
                    "deadline": f"within 2–3 days — {days_left} days left in the month",
                    "trigger_if_monitor": None,
                }
                fb = {
                    "headline": f"{month_name} projected {f_vs} vs {ref_label} — {f_stake} shortfall forming",
                    "evidence": [
                        {"label": "PROJECTED REVENUE", "value": f_proj, "sub": f"{f_vs} vs {ref_label}"},
                        {"label": "REMAINING OTB", "value": f_rem_otb, "sub": f"{days_left} days left"},
                    ],
                    "what_happened": f"Month-end projection is {f_proj}, {f_vs} vs {ref_label}.",
                    "why_it_matters": f"Remaining-month OTB may be pacing below last year's close-in pickup (confidence: Medium).",
                    "recommended_action": f"Consider a close-in offer or rate action on remaining {month_name} nights.",
                    "by_when": f"Within 2–3 days — {days_left} days left in the month.",
                    "at_stake": {"value": f_stake, "calc": facts["value_at_stake_calc"]},
                }
            else:
                hypo = [{"text": "Cancellations in the remaining days are the main risk to the finish", "confidence": "High"}]
                directive = {
                    "type": "monitor_only",
                    "target": f"remaining {month_name} OTB — protect the projected finish",
                    "deadline": "daily until month close",
                    "trigger_if_monitor": f"cancellations exceed the trailing baseline on any remaining {month_name} date",
                }
                fb = {
                    "headline": f"{month_name} projected to close {f_vs} vs {ref_label} — on track, protect the finish",
                    "evidence": [
                        {"label": "PROJECTED REVENUE", "value": f_proj, "sub": f"{f_vs} vs {ref_label}"},
                        {"label": "REMAINING OTB", "value": f_rem_otb, "sub": "not yet realised"},
                    ],
                    "what_happened": f"Month-end projection is {f_proj}, {f_vs} vs {ref_label}.",
                    "why_it_matters": f"{f_rem_otb} of that is still on the books, not realised — cancellations in the last {days_left} days are the main risk to the finish (confidence: High).",
                    "recommended_action": "No action yet — hold rates and watch cancellations daily.",
                    "by_when": f"No action yet — recheck daily until month close. Trigger: cancellations exceed the trailing baseline on any remaining {month_name} date.",
                }

            fb["id"]  = f"proj_{month_name.lower()}_{today.year}"
            fb["tag"] = tag
            candidates.append({
                "signal":     "projection",
                "tag":        tag,
                "score":      round(score, 4),
                "title_hint": fb["headline"],
                "month_num":  today.month,
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
            vs_pct, ref_label, ref = (proj_rev - rev_budget) / rev_budget * 100, "budget", rev_budget
        elif rev_final_ly > 0:
            vs_pct, ref_label, ref = (proj_rev - rev_final_ly) / rev_final_ly * 100, "Final LY", rev_final_ly
        else:
            continue
        if abs(vs_pct) < 5:
            continue

        m_start, m_end = _month_bounds(today.year, sm)
        days_to_start  = max(0, (m_start - today).days)
        window_left    = max(0, (m_end - today).days)
        m_name = _cal.month_abbr[sm]
        month_label = f"{m_name} {today.year}"
        behind = vs_pct < 0
        tag    = "ALERT" if behind else "OPPORTUNITY"

        rn_ty_m = p.get("rn", 0)
        R     = min(abs(proj_rev - ref) / daily_rev_baseline, 1.0)
        score = _score_candidate(R, _urgency(days_to_start), _magnitude_pct(vs_pct / 100), C=0.8)

        f_proj = _eur(proj_rev)
        f_vs   = _pct_signed(vs_pct)
        f_otb  = _eur(rev_ty)
        facts = {
            "month_label":    month_label,
            "proj_revenue":   f_proj,
            "vs_ref":         f_vs,
            "ref_label":      ref_label,
            "rev_otb":        f_otb,
            "exp_rem_pickup": _eur(exp_pickup),
            "adr_otb":        _eur(p.get("adr", 0)),
            "adr_final_ly":   _eur(p.get("adr_final_ly", 0)),
            "occ_otb":        _pct(p.get("occ", 0) * 100),
            "occ_stly":       _pct(p.get("stly", 0) * 100),
            "final_ly_occ":   _pct(p.get("final", 0) * 100),
            "days_to_start":  str(days_to_start),
        }
        if behind:
            stake   = abs(proj_rev - ref)
            f_stake = _eur(stake)
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f"{ref_label} {_eur(ref)} − projected {f_proj} = {f_stake} shortfall"
            hypo = [{"text": "Booking pace behind last year — group or contracted business may not have re-materialised", "confidence": "Low"}]
            directive = {
                "type": "open_promo",
                "target": f"{month_label} — demand-building offer while the booking window is open",
                "deadline": f"within 7 days — {days_to_start} days to month start",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} projected {f_vs} vs {ref_label} — {f_stake} shortfall forming",
                "evidence": [
                    {"label": "PROJ REVENUE", "value": f_proj, "sub": f"{f_vs} vs {ref_label}"},
                    {"label": "OTB VS STLY OCC", "value": f"{facts['occ_otb']} vs {facts['occ_stly']}", "sub": f"Final LY {facts['final_ly_occ']}"},
                ],
                "what_happened": f"{m_name} is projecting {f_proj}, {f_vs} vs {ref_label}.",
                "why_it_matters": "Booking pace is behind last year — group or contracted business may not have re-materialised (confidence: Low).",
                "recommended_action": f"Consider a demand-building offer for {month_label} while the booking window is open.",
                "by_when": f"Within 7 days — {days_to_start} days to month start.",
                "at_stake": {"value": f_stake, "calc": facts["value_at_stake_calc"]},
            }
        else:
            hypo = [{"text": "Volume position secured ahead of last year — early rate floors will anchor the final ADR", "confidence": "Medium"}]
            directive = {
                "type": "rate_review_up",
                "target": f"{month_label} rate floors — protect or lift while demand is ahead",
                "deadline": f"this week — early-window pricing anchors the {m_name} close",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} projected {f_vs} vs {ref_label} — rate upside with volume secured",
                "evidence": [
                    {"label": "PROJ REVENUE", "value": f_proj, "sub": f"{f_vs} vs {ref_label}"},
                    {"label": "ADR OTB VS FINAL LY", "value": f"{facts['adr_otb']} vs {facts['adr_final_ly']}", "sub": f"OTB occ {facts['occ_otb']}"},
                ],
                "what_happened": f"{m_name} is projecting {f_proj}, {f_vs} vs {ref_label}.",
                "why_it_matters": "The volume position is secured ahead of last year — early rate floors will anchor the final ADR outcome (confidence: Medium).",
                "recommended_action": f"Review {month_label} rate floors — protect or lift while demand runs ahead.",
                "by_when": f"This week — early-window pricing anchors the {m_name} close.",
            }
        fb["id"]  = f"proj_{m_name.lower()}_{today.year}"
        fb["tag"] = tag
        candidates.append({
            "signal":     "projection",
            "tag":        tag,
            "score":      round(score, 4),
            "title_hint": fb["headline"],
            "month_num":  sm,
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

    # ── Merge gate: pickup ALERT + soft dates in the same month → one story ──
    candidates = _merge_same_dates(candidates)

    # ── Rank + hard gates ─────────────────────────────────────────────────────
    candidates.sort(key=lambda c: c["score"], reverse=True)

    seen_signals: set[str] = set()
    seen_months:  set[int] = set()
    ranked:    list[dict] = []
    watchlist: list[dict] = []

    for c in candidates:
        sig = c["signal"]
        month_level = sig in ("pace", "projection")
        duplicate_month = month_level and c.get("month_num") in seen_months
        if sig not in seen_signals and not duplicate_month and c["score"] >= 0.08:
            ranked.append(c)
            seen_signals.add(sig)
            if month_level and c.get("month_num") is not None:
                seen_months.add(c["month_num"])
        else:
            watchlist.append(c)

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
        "ranked":    ranked[:6],
        "watchlist": watchlist,
        "headline":  headline,
    }


def _merge_same_dates(candidates: list[dict]) -> list[dict]:
    """Spec §6 gate: cards pointing at the same stay dates merge into one story."""
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
    score = round(min(1.0, max(pick["score"], soft["score"]) + 0.05), 4)

    facts = {
        "month_label":          month_label,
        "pickup_yday_net_rn":   pf["yday_net_rn"],
        "pickup_trailing_avg":  pf["trailing_avg"],
        "pickup_z_score":       pf["z_score"],
        "soft_dates":           sf["dates"],
        "soft_count":           sf["count"],
        "soft_avg_gap_pts":     sf["avg_gap_pts"],
        "softest_dates":        sf["softest_dates"],
        "value_at_stake":       sf["value_at_stake"],
        "value_at_stake_calc":  sf["value_at_stake_calc"],
    }
    fb = {
        "id": f"softening_{month_label.split()[0].lower()}",
        "tag": "ALERT",
        "headline": f"{month_label.split()[0]} is softening: pickup {pf['yday_net_rn']} + {sf['count']} dates {sf['avg_gap_pts']} behind LY",
        "evidence": [
            {"label": "YESTERDAY NET", "value": pf["yday_net_rn"], "sub": f"vs {pf['trailing_avg']} 7-day avg"},
            {"label": "SOFT DATES", "value": f"{sf['count']} nights {sf['avg_gap_pts']}", "sub": sf["dates"]},
        ],
        "what_happened": f"Net pickup for {month_label} was {pf['yday_net_rn']} yesterday, and {sf['count']} stay dates ({sf['dates']}) pace an average {sf['avg_gap_pts']} behind the same time last year.",
        "why_it_matters": "The cancellation slowdown and the pace gap point at the same dates — the two signals reinforce each other: this demand is genuinely soft, not a one-off (confidence: Medium).",
        "recommended_action": f"Check the {month_label} cancellations for a common source or rate code; if broad, prepare a targeted offer on the soft dates.",
        "by_when": "Investigate today; decide on an offer within 3–4 days if pickup stays below average.",
        "at_stake": {"value": sf["value_at_stake"], "calc": sf["value_at_stake_calc"]},
    }
    merged = {
        "signal":     "softening_cluster",
        "tag":        "ALERT",
        "score":      score,
        "title_hint": fb["headline"],
        "month_num":  soft.get("month_num"),
        "insight": {
            "id": fb["id"],
            "tag": "ALERT",
            "score": score,
            "signal": "softening_cluster",
            "stay_period": soft["insight"]["stay_period"],
            "days_to_nearest_arrival": soft["insight"]["days_to_nearest_arrival"],
            "booking_window_days_left": soft["insight"]["booking_window_days_left"],
            "facts": facts,
            "cause_hypotheses": [{"text": "Cancellations and the pace gap point at the same dates — demand is genuinely softening rather than a one-off", "confidence": "Medium"}],
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
   never command ("Consider...", "Review..."). Never state a specific price
   unless it appears in facts.
7. by_when: always present. For tag MONITOR use: "No action yet -
   recheck [date]. Trigger: [trigger_if_monitor]."
8. at_stake: copy value and calc verbatim from facts. If absent, omit
   the field entirely - never invent a value.
9. Audience: a general manager without a revenue background must
   understand every sentence. No jargon without a plain-language anchor.
10. Language: write in English."""

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


def _bad_numbers(card_out: dict, haystack: str) -> list[str]:
    """Every number token in the output must appear verbatim in the input JSON."""
    out_text = json.dumps(card_out, ensure_ascii=False)
    return sorted({tok for tok in _NUM_TOKEN.findall(out_text) if tok not in haystack})


def _narrate_card(wrapper: dict, fallback_card: dict) -> dict:
    """One Claude call per card. Numeric validator; max 2 retries; then fallback."""
    haystack = json.dumps(wrapper, ensure_ascii=False)
    base_prompt = json.dumps(wrapper, ensure_ascii=False, indent=2)
    prompt = base_prompt

    for attempt in range(3):
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
            card = next(b for b in response.content if b.type == "tool_use").input
        except Exception as exc:
            print(f"[analyst] Card narration error (attempt {attempt + 1}): {exc}")
            continue

        bad = _bad_numbers(card, haystack)
        if not bad:
            return _harden_card(card, wrapper)

        print(f"[analyst] Card '{wrapper['insight']['id']}' attempt {attempt + 1}: "
              f"numbers not in facts: {bad} — retrying")
        prompt = (base_prompt +
                  f"\n\nPREVIOUS ATTEMPT REJECTED. These numbers do not appear in the input JSON: "
                  f"{', '.join(bad)}. Copy every number character-for-character from the input only.")

    print(f"[analyst] Card '{wrapper['insight']['id']}': validation failed twice — using templated fallback.")
    return dict(fallback_card)


def _harden_card(card: dict, wrapper: dict) -> dict:
    """Post-process: id/tag/at_stake are authoritative from the compute layer."""
    insight = wrapper["insight"]
    facts   = insight["facts"]
    card["id"]  = insight["id"]
    card["tag"] = insight["tag"]
    if "value_at_stake" in facts:
        card["at_stake"] = {"value": facts["value_at_stake"], "calc": facts["value_at_stake_calc"]}
    else:
        card.pop("at_stake", None)
    return card


def _narrate_summary(hotel_name: str, cards: list[dict]) -> str:
    """One-sentence executive summary from the narrated cards. Validated; template fallback."""
    fallback = f"Today's focus: {cards[0]['headline']}" if cards else ""
    if not cards:
        return fallback
    digest = [{"tag": c["tag"], "headline": c["headline"],
               "at_stake": c.get("at_stake", {}).get("value")} for c in cards[:3]]
    haystack = json.dumps(digest, ensure_ascii=False)
    prompt = (f"Hotel: {hotel_name}. Top insights this morning:\n"
              f"{json.dumps(digest, ensure_ascii=False, indent=2)}\n\n"
              "Write ONE sentence (max 40 words) naming the single most urgent revenue focus today. "
              "Use ONLY numbers that appear verbatim above — never compute or estimate.")
    try:
        response = _get_client().messages.create(
            model=_MODEL,
            max_tokens=300,
            temperature=0.2,
            tools=[_SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "submit_summary"},
            messages=[{"role": "user", "content": prompt}],
        )
        result = next(b for b in response.content if b.type == "tool_use").input
        summary = result.get("executive_summary", "")
        if summary and not _bad_numbers({"s": summary}, haystack):
            return summary
    except Exception as exc:
        print(f"[analyst] Summary narration error: {exc}")
    return fallback


# ─── Legacy field mapping (current PWA renders these) ─────────────────────────

_TAG_TO_TYPE = {"ALERT": "warning", "OPPORTUNITY": "opportunity", "MONITOR": "monitor"}


def _evidence_direction(ev: dict, tag: str) -> str:
    s = ev.get("value", "") + " " + ev.get("sub", "")
    if "−" in s or "-€" in s:
        return "down"
    if "+" in s:
        return "up"
    return "neutral"


def _card_to_insight(card: dict, priority: int) -> dict:
    """Card (new anatomy) → insight object carrying BOTH new and legacy fields."""
    action = card["recommended_action"]
    if card.get("by_when"):
        action += f" By when: {card['by_when']}"
    if card.get("at_stake"):
        action += f" At stake: {card['at_stake']['value']}."
    return {
        # New card anatomy (spec v1.1)
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
             "direction": _evidence_direction(ev, card["tag"])}
            for ev in card["evidence"][:2]
        ],
        "findings": [card["what_happened"], card["why_it_matters"]],
        "action":   action,
    }


_STUB = {"executive_summary": "", "insights": []}


# ─── Layer B: entry point ────────────────────────────────────────────────────

def generate_insights(data: dict[str, Any]) -> dict[str, Any]:
    if not config.ANTHROPIC_API_KEY:
        print("[analyst] No ANTHROPIC_API_KEY — skipping AI insights.")
        return _STUB

    try:
        computed = _compute_signals(data)
        ranked   = computed["ranked"]
        print(f"[analyst] Compute: {len(ranked)} ranked signals, "
              f"{len(computed['watchlist'])} watchlist")
        if not ranked:
            print("[analyst] No signals above threshold — falling back to legacy prompt.")
            return _legacy_generate(data)

        hotel_name = data.get("hotel_name", config.HOTEL_NAME)
        briefing_date = _date.today().isoformat()

        cards: list[dict] = []
        for cand in ranked[:5]:
            wrapper = {
                "property":       hotel_name,
                "briefing_date":  briefing_date,
                "capacity_rooms": config.TOTAL_ROOMS,
                "insight":        cand["insight"],
            }
            card = _narrate_card(wrapper, cand["fallback_card"])
            cards.append(card)
            print(f"[analyst] Card {len(cards)}: [{card['tag']}] {card['headline'][:60]}")

        summary = _narrate_summary(hotel_name, cards)
        result = {
            "executive_summary": summary,
            "insights": [_card_to_insight(c, i + 1) for i, c in enumerate(cards)],
        }
        print(f"[analyst] Narration complete: {len(cards)} cards")

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
