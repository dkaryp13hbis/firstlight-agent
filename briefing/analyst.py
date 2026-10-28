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
import os as _os
import re
import statistics
import threading as _threading
import time as _time
from datetime import date as _date, timedelta
from typing import Any

import anthropic
import config

_client = None
_MODEL = "claude-sonnet-4-6"
_PROMPT_VERSION = "cards-v1.11-review"

# Cost policy (user decision 2026-07-27): every Claude call costs money, so
# narration gets ONE attempt — a validation miss goes straight to the free
# deterministic fallback. Caps are enforced up-front via tool-schema
# descriptions. Set NARRATION_ATTEMPTS=2 to re-enable surgical retries.
_MAX_NARRATION_ATTEMPTS = max(1, int(_os.getenv("NARRATION_ATTEMPTS", "1")))

# Global cap on concurrent Claude calls (matters once REFRESH_CONCURRENCY > 1)
_CLAUDE_SEM = _threading.BoundedSemaphore(int(_os.getenv("CLAUDE_CONCURRENCY", "6")))

# claude-sonnet-4-6 USD per million tokens (input / output / cache write 5m / cache read)
_PRICE_IN, _PRICE_OUT, _PRICE_CW, _PRICE_CR = 3.00, 15.00, 3.75, 0.30

# Significance floor (spec C2.2): candidates with less at stake are suppressed
_STAKE_FLOOR_EUR = 1000
_LEAD_SHIFT_MIN_DAYS = 2.0     # lead-time shift must be >= 2 days AND >= 10%
_QUIET_DAY_CARDS = 2           # fewer ranked cards than this -> pin the top open ALERT


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
    """Occupancy projection as a +/-2pt band. The point is never exposed.
    The point is clamped to 0–100 first: a month on course to beat full
    occupancy read "108–100%" (review 2026-10-28)."""
    point = min(100, max(0, int(round(point_pct))))
    lo = max(0, point - 2)
    hi = min(100, point + 2)
    return f"{lo}–{hi}%"


def _rev_band(point: float) -> tuple[str, float, float]:
    """Revenue projection as a +/-2% band. Returns (display, lo, hi)."""
    lo, hi = point * 0.98, point * 1.02
    return f"{_eur(lo)}–{_eur(hi)}", lo, hi


def _fact(value: str, period: str) -> dict:
    return {"value": value, "period": period}


# ─── Scoring helpers ──────────────────────────────────────────────────────────

def _days_word(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


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


# ─── Attribution helpers (Layer A) ───────────────────────────────────────────

def _cancel_series(cancel_daily: list[dict], sm: int, sy: int,
                   yesterday: _date) -> tuple[int, float, list[int]]:
    """Yesterday's cancellations for one stay month + the zero-filled prior
    7-day series. Q14 only ships rows for days WITH cancellations, so a
    missing day is a real zero, not a gap. Returns (rn, revenue, prior[7])."""
    by_day: dict[str, tuple[int, float]] = {}
    for r in cancel_daily:
        if int(r.get("stay_month", 0)) == sm and int(r.get("stay_year", 0)) == sy:
            rn, rev = by_day.get(str(r.get("ref_date")), (0, 0.0))
            by_day[str(r.get("ref_date"))] = (rn + int(r.get("cancel_rn", 0) or 0),
                                              rev + float(r.get("cancel_rev", 0) or 0))
    yday_rn, yday_rev = by_day.get(yesterday.isoformat(), (0, 0.0))
    prior = [by_day.get((yesterday - timedelta(days=i)).isoformat(), (0, 0.0))[0]
             for i in range(7, 0, -1)]
    return yday_rn, yday_rev, prior


def _source_attribution(sources_by_month: list[dict], sm: int, rn_gap: float) -> dict | None:
    """Which source explains a month's room-night gap vs same time last year.
    Returns None when the data is absent, too thin, or does not reconcile
    with the pace gap — safety-first: say nothing rather than mis-attribute.
    share = top mover's delta / total gap; spread = concentrated (>= 60% in
    one source AND >= 20 pts more than its share of last year's volume —
    review 2026-10-28: a source that is 70% of the business carries ~70% of
    any even gap; that is its size, not a source-specific cause) | broad
    (< 40% and 3+ sources moving the same way) | mixed."""
    rows = [r for r in sources_by_month if int(r.get("stay_month", 0)) == sm]
    if not rows or not rn_gap:
        return None
    deltas: dict[str, float] = {}
    ly_rn: dict[str, float] = {}
    for r in rows:
        name = str(r.get("source") or "Unknown").strip() or "Unknown"
        deltas[name] = deltas.get(name, 0.0) + float(r.get("rn_ty", 0) or 0) - float(r.get("rn_stly", 0) or 0)
        ly_rn[name] = ly_rn.get(name, 0.0) + float(r.get("rn_stly", 0) or 0)
    total = sum(deltas.values())
    total_ly = sum(ly_rn.values())
    # Reconcile: the source deltas must add up to the pace gap (±20%, min 3 rn),
    # otherwise the two queries disagree and any attribution would mislead.
    if abs(total - rn_gap) > max(0.20 * abs(rn_gap), 3.0):
        return None
    sign = 1 if rn_gap > 0 else -1
    movers = sorted(((n, d) for n, d in deltas.items() if d * sign > 0), key=lambda x: -abs(x[1]))
    if not movers or abs(movers[0][1]) < 5:
        return None
    top_name, top_delta = movers[0]
    share = abs(top_delta) / abs(rn_gap)
    weight = ly_rn.get(top_name, 0.0) / total_ly if total_ly else 0.0
    if share >= 0.60 and share - weight >= 0.20:
        spread = "concentrated"
    elif share < 0.40 and len(movers) >= 3:
        spread = "broad"
    else:
        spread = "mixed"
    # above ~100% other sources moved the other way: _share_of says so in
    # words instead of a misleading "nearly all" (review 2026-10-28)
    share_txt = _share_of(share, "gap" if sign < 0 else "lead")
    direct = next(((n, d) for n, d in deltas.items() if n.lower() == "direct"), None)
    return {"top_name": top_name, "top_delta": top_delta, "share": share,
            "share_txt": share_txt, "spread": spread, "movers": movers, "direct": direct}


def _source_totals(sources_by_month: list[dict], sm: int, name: str) -> tuple[float, float]:
    """This year's booked room nights + revenue for one source and stay month
    (same source-name normalisation as _source_attribution)."""
    rn = rev = 0.0
    for r in sources_by_month:
        if int(r.get("stay_month", 0)) != sm:
            continue
        if (str(r.get("source") or "Unknown").strip() or "Unknown") == name:
            rn  += float(r.get("rn_ty", 0) or 0)
            rev += float(r.get("rev_ty", 0) or 0)
    return rn, rev


def _longest_run(entries: list[dict]) -> list[dict]:
    """Longest block of consecutive stay dates (entries carry a 'date')."""
    best: list[dict] = []
    cur: list[dict] = []
    for e in sorted(entries, key=lambda x: x["date"]):
        if cur and (e["date"] - cur[-1]["date"]).days == 1:
            cur.append(e)
        else:
            cur = [e]
        if len(cur) > len(best):
            best = list(cur)
    return best


def _about_pct(share: float) -> str:
    return f"about {int(round(share * 100 / 5.0) * 5)}%"


def _share_of(share: float, noun: str) -> str:
    """'about 70% of the change' — or plain words when other parts offset."""
    if share > 1.05:
        return f"more than the whole {noun}, partly offset by other sources"
    if share >= 0.95:
        return f"nearly all of the {noun}"
    return f"{_about_pct(share)} of the {noun}"


def _lead_shift_attribution(lt_src: dict, sm: int, sy: int, rn_ty: float, rn_ly: float,
                            lead_ly_avg: float, shift_days: float) -> dict | None:
    """Which source accounts for a stay month's change in average booking
    lead (review 2026-10-28). Exact, signed split around last year's average
    L̄:   c_s = w_ty·(L_ty − L̄) − w_ly·(L_ly − L̄)   and   Σ c_s == shift.
    Each c_s = within (w_ty·(L_ty − L_ly): that source books earlier/later)
             + mix    ((w_ty − w_ly)·(L_ly − L̄): more/fewer bookings from it).
    Safety-first: a source is named only when it carries >= 60% of the shift
    in the shift's direction with >= 8 rn this year (and >= 8 last year when
    the story is 'books later/earlier'); otherwise None."""
    if not rn_ty or not rn_ly or not shift_days:
        return None
    best = None
    for src in {k[3] for k in lt_src if k[0] == sm and k[1] == sy}:
        a = lt_src.get((sm, sy, "TY", src)) or {"rn": 0.0, "lxr": 0.0}
        b = lt_src.get((sm, sy, "LY", src)) or {"rn": 0.0, "lxr": 0.0}
        w_ty, w_ly = a["rn"] / rn_ty, b["rn"] / rn_ly
        l_ty = a["lxr"] / a["rn"] if a["rn"] else None
        l_ly = b["lxr"] / b["rn"] if b["rn"] else None
        c = ((w_ty * (l_ty - lead_ly_avg)) if l_ty is not None else 0.0) \
            - ((w_ly * (l_ly - lead_ly_avg)) if l_ly is not None else 0.0)
        within = w_ty * (l_ty - l_ly) if (l_ty is not None and l_ly is not None) else 0.0
        mix = c - within
        if best is None or c * shift_days > best["c"] * shift_days:
            best = {"source": src, "c": c, "within": within, "mix": mix,
                    "rn_ty": a["rn"], "rn_ly": b["rn"], "w_ty": w_ty, "w_ly": w_ly,
                    "l_ref": l_ly if l_ly is not None else l_ty}   # new source: its TY lead
    if not best:
        return None
    share = best["c"] / shift_days
    if share < 0.60 or best["rn_ty"] < 8:
        return None
    # a source with 70% of the bookings carries ~70% of an EVEN shift — that
    # is its size, not a story: it must carry >= 20 pts more of the shift
    # than its average share of bookings (review 2026-10-28)
    if share - (best["w_ty"] + best["w_ly"]) / 2.0 < 0.20:
        return None
    if abs(best["within"]) >= abs(best["mix"]):
        if best["rn_ly"] < 8:
            return None
        kind = "within"
        kind_txt = ("its guests now book closer to arrival" if best["within"] < 0
                    else "its guests now book further ahead")
    else:
        kind = "mix"
        more = "more" if best["w_ty"] > best["w_ly"] else "fewer"
        rel = "closer to arrival" if best["l_ref"] < lead_ly_avg else "further ahead"
        kind_txt = f"{more} bookings now come from it, and it books {rel} than other sources"
    share_txt = _share_of(share, "change")
    return {"source": best["source"], "share": share, "kind": kind, "share_txt": share_txt,
            "kind_txt": kind_txt,
            "sentence": f"{best['source']} accounts for {share_txt}; {kind_txt}."}


def _mix_rate_split(sources_by_month: list[dict], sm: int, rn_ty: float, rev_ty: float,
                    rn_stly: float, rev_stly: float) -> dict | None:
    """Split a month's average-rate change vs same time last year into
    'which sources sold' (mix) and 'rates within each source' (rate), using
    the charts.py ADR bridge on Q17 rows (midpoint weights, exact identity:
    mix + rate == ADR change). Safety-first — None unless Q17 describes the
    same bookings as Q4 (room nights within max(3, 2%), revenue within 2%,
    both periods) and the identity holds. Returns {kind, mix_share, txt}."""
    from decimal import Decimal as D
    try:
        from briefing.charts import _adr_bridge_core
    except Exception:
        return None
    ty: dict = {}
    ly: dict = {}
    for r in sources_by_month:
        if int(r.get("stay_month", 0)) != sm:
            continue
        name = str(r.get("source") or "Unknown").strip() or "Unknown"
        for d, rn_k, rev_k in ((ty, "rn_ty", "rev_ty"), (ly, "rn_stly", "rev_stly")):
            rn, rev = float(r.get(rn_k) or 0), float(r.get(rev_k) or 0)
            if rn > 0:
                a, b = d.get(name, (D(0), D(0)))
                d[name] = (a + D(str(rn)), b + D(str(rev)))
    if not ty or not ly:
        return None

    def _close(a: float, b: float, rel: float, floor: float = 0.0) -> bool:
        return abs(a - b) <= max(floor, rel * abs(b))
    tot = lambda d, i: float(sum(v[i] for v in d.values()))   # noqa: E731
    if not (_close(tot(ty, 0), rn_ty, 0.02, 3.0) and _close(tot(ly, 0), rn_stly, 0.02, 3.0)
            and _close(tot(ty, 1), rev_ty, 0.02) and _close(tot(ly, 1), rev_stly, 0.02)):
        return None
    b = _adr_bridge_core(ly, ty)
    if abs(b["residual"]) > D("0.01") or b["delta"] == 0:
        return None
    mix_share = float(b["mix"]) / float(b["delta"])
    lw = "lower" if b["delta"] < 0 else "higher"
    if mix_share >= 0.60:
        kind = "mix"
        if mix_share > 1.05:
            txt = (f"the change in which sources sold explains all of the {lw} average rate; "
                   f"rates within each source moved the other way")
        elif mix_share >= 0.95:
            txt = (f"nearly all of the {lw} average rate comes from a change in which "
                   f"sources sold, not from rates within each source")
        else:
            txt = (f"{_about_pct(mix_share)} of the {lw} average rate comes from a change in "
                   f"which sources sold; the rest from rates within each source")
    elif mix_share <= 0.40:
        kind, rate_share = "rate", 1.0 - mix_share
        if rate_share >= 0.95:
            txt = (f"the {lw} average rate comes from {lw} rates within each source, "
                   f"not from a change in which sources sold")
        else:
            txt = (f"{_about_pct(rate_share)} of the {lw} average rate comes from {lw} rates "
                   f"within each source; the rest from a change in which sources sold")
    else:
        kind = "both"
        txt = (f"the {lw} average rate comes from both a change in which sources sold "
               f"and {lw} rates within each source")
    return {"kind": kind, "mix_share": mix_share, "txt": txt}


# ─── Layer A: one function per signal ────────────────────────────────────────
# Review 2026-10-28: the signals used to share one function, so one bad row
# anywhere (e.g. a malformed stay_date) wiped EVERY card and the hero for the
# day. Each signal is now its own function and _compute_signals runs each in
# try/except — a failure drops that signal only (logged + cards_audit).

def _anchor_today(data: dict) -> _date:
    """The analyst's single 'today' (review 2026-10-28): the day after the
    PMS report date (`report_date_iso` = yesterday, set by the adapters), so
    every signal, the novelty window and the hero agree on one calendar.
    Payloads that predate the field fall back to the server date."""
    iso = (data or {}).get("report_date_iso")
    if iso:
        try:
            return _date.fromisoformat(str(iso)[:10]) + timedelta(days=1)
        except ValueError:
            pass
    return _date.today()


_FORWARD_DAYS_NEXT_YEAR = 183   # next-year months starting within ~6 months


def _forward_months(pace: list[dict], pace_next_year: list[dict],
                    today: _date, total_rooms: int) -> list[dict]:
    """Stay months the monthly signals (pace, projection) look at, each
    tagged with its YEAR. This year's months from today's month on (Q4), plus
    next year's months starting within _FORWARD_DAYS_NEXT_YEAR (Q16) — so
    Jan–Mar are no longer invisible in Nov/Dec (review 2026-10-28).
    A next-year month takes its 'final last year' figures from THIS year's
    same month, which Q4 holds as actuals because that month has passed."""
    out = [{**p, "year": today.year} for p in pace
           if int(p.get("month_num") or 0) >= today.month]
    this_year = {int(p.get("month_num") or 0): p for p in pace}
    ny = today.year + 1
    for r in pace_next_year:
        sm = int(r.get("month_num") or 0)
        if not 1 <= sm < today.month:
            continue          # later months are > 6 months out (or invalid)
        if (_date(ny, sm, 1) - today).days > _FORWARD_DAYS_NEXT_YEAR:
            continue
        ly = this_year.get(sm) or {}
        inv = total_rooms * _cal.monthrange(ny, sm)[1] if total_rooms else 0
        rn, rn_stly = float(r.get("rn") or 0), float(r.get("rn_stly") or 0)
        rev, rev_stly = float(r.get("rev") or 0), float(r.get("rev_stly") or 0)
        rn_fly, rev_fly = float(ly.get("rn") or 0), float(ly.get("rev") or 0)
        occ, occ_stly = (rn / inv, rn_stly / inv) if inv else (0.0, 0.0)
        out.append({
            "month": _cal.month_abbr[sm], "month_num": sm, "year": ny,
            "rn": int(rn), "rn_stly": int(rn_stly), "rn_final_ly": int(rn_fly),
            "rev": rev, "rev_stly": rev_stly, "rev_final": rev_fly,
            "adr": rev / rn if rn else 0.0,
            "adr_stly": rev_stly / rn_stly if rn_stly else 0.0,
            "adr_final_ly": rev_fly / rn_fly if rn_fly else 0.0,
            "occ": occ, "stly": occ_stly,
            "final": float(ly.get("occ") or 0),   # this year's actual occupancy
            # same rule as assemble._pace_status (±5% vs same time last year)
            "status": ("on_track" if not occ_stly else
                       "ahead" if (occ - occ_stly) / occ_stly >= 0.05 else
                       "behind" if (occ - occ_stly) / occ_stly <= -0.05 else "on_track"),
            "next_year": True,
        })
    return out


def _month_row(ctx: dict, sm: int, sy: int) -> dict | None:
    """The forward-month row for (month, year), else this year's Q4 row for
    that month when sy is this year (past months keep their old lookup)."""
    for p in ctx["months"]:
        if int(p.get("month_num") or 0) == sm and p.get("year") == sy:
            return p
    if sy == ctx["today"].year:
        return next((p for p in ctx["pace"] if p.get("month_num") == sm), None)
    return None


def _signal_context(data: dict) -> dict:
    """Inputs shared by every signal, read once."""
    today = _anchor_today(data)
    yesterday = today - timedelta(days=1)
    pace = [p for p in (data.get("pace") or []) if isinstance(p, dict)]
    pace_next_year = [p for p in (data.get("pace_next_year") or []) if isinstance(p, dict)]
    try:
        rev_final_ly_total = sum(float(p.get("rev_final", 0) or 0) for p in pace)
    except (TypeError, ValueError):
        rev_final_ly_total = 0.0
    try:
        months = _forward_months(pace, pace_next_year, today, int(config.TOTAL_ROOMS or 0))
    except Exception as exc:   # never lose this year's months over Q16
        print(f"[analyst] next-year months skipped: {exc}")
        months = [{**p, "year": today.year} for p in pace
                  if int(p.get("month_num") or 0) >= today.month]
    return {
        "months":             months,
        "data":               data,
        "today":              today,
        "yesterday":          yesterday,
        "yesterday_str":      yesterday.isoformat(),
        "total_rooms":        config.TOTAL_ROOMS,
        "pace":               pace,
        "cm":                 data.get("current_month_remaining") or {},
        "mtd":                data.get("mtd") or {},
        "cancel_daily":       data.get("cancel_daily") or [],       # Q14, optional
        "sources_by_month":   data.get("sources_by_month") or [],   # Q17, optional
        "pickup_daily":       data.get("pickup_daily") or [],
        "daily_rev_baseline": max(rev_final_ly_total / 365.0, 1.0),
    }


def _signal_pickup(ctx: dict) -> list[dict]:
    today, yesterday = ctx["today"], ctx["yesterday"]
    cancel_daily, daily_rev_baseline = ctx["cancel_daily"], ctx["daily_rev_baseline"]
    candidates: list[dict] = []

    # ── Signal 1: Pickup z-score by stay month (hard gate: |z| >= 2) ─────────
    pickup_daily = ctx["pickup_daily"]
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
            if m_end < today:
                continue  # Q9 now includes consumed-night bookings (reconciles
                          # with the Pickup card) — a finished month is never a card
            days_to_start  = max(0, (m_start - today).days)
            window_left    = max(0, (m_end - today).days)

            pace_m = _month_row(ctx, sm, sy)
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
                hypo = [{"text": f"Cancellations or a slowdown in new bookings for {month_label} — one source or rate plan may explain the swing", "confidence": "Low"}]
                directive = {
                    "type": "investigate_cancellations",
                    "target": f"yesterday's {month_label} cancellations and bookings by source and rate plan",
                    "deadline": "today",
                    "trigger_if_monitor": None,
                }
                fb_headline = f"{month_label} new bookings {'turned negative' if yday_rn < 0 else 'slowed sharply'} vs recent average"
                fb_why = "Cancellations or a slowdown in new bookings may be behind the swing; one source or rate plan could explain it (confidence: Low)."
                fb_action = f"It may be worth checking yesterday's {month_label} cancellations for a common source or rate plan."
                fb_by_when = "Today — before the next briefing."
            else:
                hypo = [{"text": f"Demand for {month_label} is rising fast — possibly a campaign, event, or group coming through", "confidence": "Low"}]
                directive = {
                    "type": "review_rates",
                    "target": f"what is driving the extra {month_label} bookings, and whether current rates still fit",
                    "deadline": "within 2 days",
                    "trigger_if_monitor": None,
                }
                fb_headline = f"{month_label} new bookings running well above recent average"
                fb_why = "Demand may be rising fast — possibly a campaign, event, or group coming through (confidence: Low)."
                fb_action = f"It may be worth reviewing what is driving the extra {month_label} bookings, and whether current rates still fit."
                fb_by_when = "Within 2 days, while demand is strong."

            ev2 = {"label": "REVENUE IMPACT", "value": f_yday_rev,
                   "sub": f"swing of {f_z} vs normal (above 2 is unusual)"}
            # Attribution (2026-09-23): the card used to ASK whether the swing
            # was cancellations or a slowdown — Q14 answers it. Extra cancels
            # (vs the prior-7-day normal) covering >= half the swing = mostly
            # cancellations; otherwise new bookings slowed and cancels were normal.
            if negative and cancel_daily:
                c_y, _c_rev, c_prior = _cancel_series(cancel_daily, sm, sy, yesterday)
                c_avg   = statistics.mean(c_prior) if c_prior else 0.0
                f_c_y   = f"{c_y} rn"
                f_c_avg = f"{c_avg:.1f} rn/day"
                gross_new = max(0, yday_rn + c_y)
                p_canc  = f"cancelled yesterday, {month_label} stays"
                facts["cancellations_yday"] = _fact(f_c_y, p_canc)
                facts["cancellations_avg"]  = _fact(f_c_avg, f"cancellations per day, prior 7 days, {month_label} stays")
                facts["new_bookings_before_cancellations"] = _fact(f"{gross_new} rn", p_yday)
                extra_cancels = c_y - c_avg
                if c_y >= 3 and gap_per_day > 0 and extra_cancels >= 0.5 * gap_per_day:
                    hypo = [{"text": f"Mostly cancellations: {f_c_y} for {month_label} were cancelled yesterday against a normal {f_c_avg} — one source, rate plan or group may explain it", "confidence": "Medium"}]
                    directive["target"] = f"yesterday's {month_label} cancellations by source, rate plan and group"
                    fb_why = f"Cancellations explain most of the swing: {f_c_y} cancelled yesterday against a normal {f_c_avg}; one source, rate plan or group may be behind it (confidence: Medium)."
                    fb_action = f"It may be worth checking yesterday's {month_label} cancellations for a common source, rate plan or group."
                    ev2 = {"label": "CANCELLED", "value": f_c_y, "sub": f"yesterday, vs {f_c_avg} prior 7 days"}
                else:
                    hypo = [{"text": f"New bookings slowed rather than cancellations rising — {f_c_y} cancelled yesterday is in line with the normal {f_c_avg}", "confidence": "Medium"}]
                    directive = {
                        "type": "investigate_demand",
                        "target": f"what changed for {month_label} — rates, availability or a source pausing bookings",
                        "deadline": "today",
                        "trigger_if_monitor": None,
                    }
                    fb_why = f"New bookings slowed rather than cancellations rising: {f_c_y} cancelled yesterday is in line with the normal {f_c_avg} (confidence: Medium)."
                    fb_action = f"It may be worth checking what changed for {month_label} — rates, availability or a source pausing bookings."

            fallback_card = {
                "id": f"pickup_{m_name.lower()}_{sy}",
                "tag": tag,
                "headline": fb_headline,
                "evidence": [
                    {"label": "NET NEW BOOKINGS", "value": f_yday_net, "sub": f"yesterday, vs {f_avg} prior 7 days"},
                    ev2,
                ],
                "what_happened": f"Net new bookings for {month_label} were {f_yday_net} yesterday, against a 7-day average of {f_avg}.",
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
                "stay_year":  sy,
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
    return candidates


def _signal_pace(ctx: dict) -> list[dict]:
    today, cm, total_rooms = ctx["today"], ctx["cm"], ctx["total_rooms"]
    sources_by_month, daily_rev_baseline = ctx["sources_by_month"], ctx["daily_rev_baseline"]
    candidates: list[dict] = []

    # ── Signal 2: Pace vs STLY by month (hard gate: |gap| >= 10%) ────────────
    # ctx["months"]: this year's months from today on + next year's within
    # ~6 months, each with its own year (December no longer hides January).
    for p in ctx["months"]:
        sm = p.get("month_num", 0)
        sy = p["year"]

        days_in = _cal.monthrange(sy, sm)[1]
        m_start, m_end = _month_bounds(sy, sm)
        days_to_start  = max(0, (m_start - today).days)
        window_left    = max(0, (m_end - today).days)
        current_month  = (sm == today.month and sy == today.year)

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

        month_label  = f"{m_name} {sy}"
        p_full       = f"{m_name} full month"
        p_vs_stly    = f"{m_name} full month, vs same time last year"
        p_finish     = f"{m_name} month-end finish"
        ahead        = rn_gap > 0
        adr_dilution = ahead and adr_gap < -5
        # More rooms but LESS revenue than same time last year (review
        # 2026-10-28): the old "ahead" card labelled −€3,000 a REVENUE LEAD and
        # hinted at higher rates. It is its own story — the booking mix or the
        # rates changed — and needs a real revenue shortfall (stake floor).
        mixed        = ahead and rev_delta <= -_STAKE_FLOOR_EUR
        # Q17 rows carry this year's months only: never attribute next year
        attr         = (None if p.get("next_year")
                        else _source_attribution(sources_by_month, sm, rn_gap))

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
            ("rev_lead" if rev_delta >= 0 else "rev_gap"): _fact(f_rev_delta, p_vs_stly),
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

        if mixed:
            tag = "ALERT"
            # Revenue = rooms × average rate, so the three changes vs same time
            # last year reconcile exactly: (1 + rooms%)(1 + rate%) = 1 + revenue%.
            # All are ratios of real PMS figures — no derived euro is shown.
            adr_stly_calc = rev_stly / rn_stly          # rn_stly > 0 (gated above)
            adr_ty_calc   = rev_ty / rn_ty              # rn_ty > rn_stly > 0 (ahead)
            f_rev_pct  = _pct_signed(rev_delta / rev_stly * 100) if rev_stly else f_rev_delta
            f_adr_pct  = _pct_signed((adr_ty_calc / adr_stly_calc - 1) * 100)
            f_adr_stly = _eur(adr_stly_calc)
            f_adr_ty   = _eur(adr_ty_calc)
            facts["rev_vs_stly_pct"] = _fact(f_rev_pct, p_vs_stly)
            facts["adr_vs_stly_pct"] = _fact(f_adr_pct, p_vs_stly)
            facts["adr_booked"]      = _fact(f_adr_ty, f"{m_name}, booked so far")
            facts["adr_stly"]        = _fact(f_adr_stly, f"{m_name}, same time last year")
            f_stake = _eur(abs(rev_delta))
            f_calc  = f"revenue booked {f_rev_delta} vs same time last year"
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f_calc
            confidence = "Medium"
            hypo = [{"text": f"The extra {m_name} rooms are coming in at a lower average rate than last year — a change in where bookings come from or which rate plans sell may explain it", "confidence": "Low"}]
            why    = (f"The extra rooms are coming in at a lower average rate: {f_adr_ty} "
                      f"against {f_adr_stly} at the same time last year (confidence: Medium).")
            action = f"It may be worth checking which sources and rate plans the extra {m_name} rooms come from."
            # Mix vs rate (the charts.py ADR bridge on Q17): is the lower average
            # rate a change in WHICH sources sold, or lower rates within sources?
            split = None if p.get("next_year") else _mix_rate_split(
                sources_by_month, sm, rn_ty, rev_ty, rn_stly, rev_stly)
            if split:
                facts["rate_change_explained_by"] = _fact(split["txt"], p_vs_stly)
                hypo = [{"text": split["txt"][0].upper() + split["txt"][1:], "confidence": "Medium"}]
                why = split["txt"][0].upper() + split["txt"][1:] + " (confidence: Medium)."
            # Name the source only when Q17 reconciles with the room-night lead
            if attr:
                top_name = attr["top_name"]
                f_top_rn = _rn_signed(attr["top_delta"])
                src_rn, src_rev = _source_totals(sources_by_month, sm, top_name)
                cheaper = src_rn >= 10 and src_rev / src_rn < adr_stly_calc
                if cheaper:
                    facts["biggest_source_rate"] = _fact(_eur(src_rev / src_rn),
                                                         f"{top_name}, {m_name} booked so far")
                if attr["spread"] == "concentrated":
                    # three independent measurements agree → High: the extra
                    # rooms sit with one source, it sells below last year's
                    # average, and the bridge says the mix drove the rate
                    if cheaper and split and split["kind"] == "mix":
                        confidence = "High"
                    rate_txt = f" at an average rate of {_eur(src_rev / src_rn)}, against {f_adr_stly} for the month last year" if cheaper else ""
                    hypo = [{"text": f"Most of the extra rooms come from {top_name} ({f_top_rn} vs same time last year){rate_txt} — the booking mix has changed", "confidence": confidence}]
                    why = (f"Most of the extra rooms come from {top_name} ({f_top_rn} vs same time last year)"
                           + (rate_txt if cheaper else f"; the average rate booked is {f_adr_ty} against {f_adr_stly} last year")
                           + f" (confidence: {confidence}).")
                    action = f"It may be worth reviewing which {top_name} rate plans the extra {m_name} rooms are booked on."
                elif not split and attr["spread"] == "broad":
                    why = (f"The extra rooms are spread across {len(attr['movers'])} sources; the average rate "
                           f"booked is {f_adr_ty} against {f_adr_stly} last year (confidence: Low).")
                elif not split:
                    why = (f"{top_name} brings the most extra rooms ({attr['share_txt']}); the average rate "
                           f"booked is {f_adr_ty} against {f_adr_stly} last year (confidence: Low).")
            directive = {
                "type": "investigate_mix",
                "target": f"which sources and rate plans the extra {m_name} rooms come from",
                "deadline": "this week",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name}: more rooms booked than last year, but less revenue",
                "evidence": [
                    {"label": f"ROOMS BOOKED ({p_full})", "value": f_pct_gap, "sub": "vs same time last year"},
                    {"label": "REVENUE BOOKED", "value": f_rev_pct,
                     "sub": f"average rate {f_adr_pct} ({f_adr_ty} vs {f_adr_stly})"},
                ],
                "what_happened": f"{m_name} rooms booked are {f_pct_gap} vs the same time last year, but revenue booked is {f_rev_pct}.",
                "why_it_matters": why,
                "recommended_action": action,
                "by_when": "This week.",
                "at_stake": {"value": f_stake, "calc": f_calc},
            }
        elif adr_dilution:
            tag   = "OPPORTUNITY"
            stake = open_rn * abs(adr_gap)
            f_stake = _eur(stake)
            f_calc  = f"{open_rn:,} open rn × {_eur(abs(adr_gap))} ADR gap = {_eur(stake)}"
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f_calc
            hypo = [{"text": "Early-season discounted rate plans may still be open even though demand is strong", "confidence": "Medium"}]
            directive = {
                "type": "review_rates",
                "target": f"whether the lowest rate plans still need to be open, {period_open}",
                "deadline": "this week",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} rooms booked far ahead of last year, but rates lower",
                "evidence": [
                    {"label": f"BOOKINGS ({p_full})", "value": f_pct_gap, "sub": "vs same time last year"},
                    {"label": "AVG RATE BOOKED vs LAST YEAR FINAL", "value": f"{f_adr_otb} vs {f_adr_fly}", "sub": f"{f_adr_gap} per room night"},
                ],
                "what_happened": f"{m_name} bookings so far are {f_pct_gap} vs the same time last year.",
                "why_it_matters": f"Average rate {f_adr_otb} is below last year's final {f_adr_fly}; early-season discounted rate plans may still be open (confidence: Medium).",
                "recommended_action": f"Worth reviewing whether the lowest rate plans still need to be open for {m_name}.",
                "by_when": f"This week — about {window_left} selling days left.",
                "at_stake": {"value": f_stake, "calc": f_calc},
            }
        elif ahead:
            tag = "OPPORTUNITY"
            hypo = [{"text": "Demand is running above last year — the remaining rooms are selling into stronger demand", "confidence": "Medium"}]
            directive = {
                "type": "review_rates",
                "target": f"whether {m_name} rates on the busiest nights still fit this demand",
                "deadline": "next 2–3 days",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} bookings {f_pct_gap} ahead of same time last year",
                "evidence": [
                    {"label": f"BOOKINGS ({p_full})", "value": f_pct_gap, "sub": "vs same time last year"},
                    # a small revenue dip under the stake floor stays here — never call it a lead
                    {"label": "REVENUE LEAD" if rev_delta >= 0 else "REVENUE vs LAST YEAR",
                     "value": f_rev_delta, "sub": "vs same time last year"},
                ],
                "what_happened": f"{m_name} revenue booked so far is {f_rev_delta} vs same time last year.",
                "why_it_matters": "Demand is running above last year, so the remaining rooms are selling into stronger demand (confidence: Medium).",
                "recommended_action": f"It may be worth reviewing whether {m_name} rates on the busiest nights still fit this demand.",
                "by_when": "Next 2–3 days.",
            }
        else:
            tag   = "ALERT"
            stake = abs(rn_gap) * adr_final_ly
            f_stake = _eur(stake)
            f_calc  = f"{abs(rn_gap):,} rn gap × {f_adr_fly} final LY ADR = {_eur(stake)}"
            facts["value_at_stake"]      = f_stake
            facts["value_at_stake_calc"] = f_calc
            hypo = [{"text": "Weaker demand, or bookings coming from different sources than last year — a source-by-source comparison would show which", "confidence": "Low"}]
            directive = {
                "type": "investigate_gap",
                "target": f"which {m_name} dates and sources are behind last year",
                "deadline": "within 7 days",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} bookings {f_pct_gap} behind same time last year",
                "evidence": [
                    {"label": f"BOOKINGS ({p_full})", "value": f_pct_gap, "sub": "vs same time last year"},
                    {"label": "ROOM NIGHTS", "value": f"{rn_ty:,} vs {rn_stly:,}", "sub": f"{f_rn_gap} vs same time last year"},
                ],
                "what_happened": f"{m_name} bookings so far stand {f_rn_gap} behind same time last year.",
                "why_it_matters": "Weaker demand, or bookings coming from different sources, may explain the gap; a source-by-source comparison would show which (confidence: Low).",
                "recommended_action": f"It may be worth checking which {m_name} dates and sources are behind last year.",
                "by_when": "Within 7 days.",
                "at_stake": {"value": f_stake, "calc": f_calc},
            }

        # Attribution (2026-09-23): Q17 says WHICH source moved. The card used
        # to suggest "a source-by-source comparison would show which" — now it
        # shows it. Only when the source deltas reconcile with the pace gap.
        # (attr is computed above; the mixed card tells its own source story.)
        if attr:
            top_name  = attr["top_name"]
            f_top_rn  = _rn_signed(attr["top_delta"])
            f_top     = f"{top_name} {f_top_rn}"
            n_movers  = len(attr["movers"])
            facts["biggest_source_move"]  = _fact(f_top, p_vs_stly)
            facts["biggest_source_share"] = _fact(attr["share_txt"], p_vs_stly)
            facts["sources_moving_same_way"] = _fact(f"{n_movers} sources", p_vs_stly)
            if attr["direct"]:
                facts["direct_bookings_move"] = _fact(f"Direct {_rn_signed(attr['direct'][1])}", p_vs_stly)
            if attr["spread"] == "concentrated":
                facts["biggest_source"] = top_name    # named driver (drill-down filter)
            if mixed:
                pass
            elif not ahead:
                fb["evidence"][1] = {"label": "BIGGEST MOVER", "value": f_top,
                                     "sub": f"{attr['share_txt']} vs same time last year"}
                if attr["spread"] == "concentrated":
                    hypo = [{"text": f"Most of the gap sits with {top_name} ({f_top_rn} vs same time last year) — a source-specific issue rather than weaker demand overall", "confidence": "Medium"}]
                    directive = {
                        "type": "investigate_source",
                        "target": f"what changed with {top_name} for {m_name} — availability, rates or a paused connection",
                        "deadline": "within 7 days",
                        "trigger_if_monitor": None,
                    }
                    fb["why_it_matters"] = f"Most of the gap sits with {top_name}, {f_top_rn} vs same time last year, so this looks like a source-specific issue rather than weaker demand overall (confidence: Medium)."
                    fb["recommended_action"] = f"It may be worth checking what changed with {top_name} for {m_name} — availability, rates or a paused connection."
                elif attr["spread"] == "broad":
                    hypo = [{"text": f"The gap is spread across {n_movers} sources ({top_name} the largest at {f_top_rn}) — weaker demand overall rather than one source", "confidence": "Medium"}]
                    fb["why_it_matters"] = f"The gap is spread across {n_movers} sources, with {top_name} the largest at {f_top_rn}; this points to weaker demand overall rather than one source (confidence: Medium)."
                else:
                    hypo = [{"text": f"{top_name} is the biggest mover ({f_top_rn} vs same time last year), but other sources are also behind", "confidence": "Low"}]
                    fb["why_it_matters"] = f"{top_name} is the biggest mover at {f_top_rn} vs same time last year, but other sources are also behind (confidence: Low)."
            elif attr["spread"] == "concentrated":
                hypo.insert(0, {"text": f"{top_name} is driving most of the lead ({f_top_rn} vs same time last year)", "confidence": "Medium"})
                if not adr_dilution:
                    fb["why_it_matters"] = f"{top_name} is driving most of the lead, {f_top_rn} vs same time last year; the remaining rooms are selling into stronger demand (confidence: Medium)."

        # the mixed card gets its own id: same month, different story (novelty
        # must not read it as a repeat of yesterday's "bookings ahead" card)
        fb["id"]  = f"pace_{'mix_' if mixed else ''}{m_name.lower()}_{sy}"
        fb["tag"] = tag

        candidates.append({
            "signal":     "pace",
            "tag":        tag,
            "score":      round(score, 4),
            "title_hint": fb["headline"],
            "month_num":  sm,
            "stay_year":  sy,
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
    return candidates


# Hidden underperformance by source (review 2026-10-28). Subgroups are
# noisier than whole months, so the source must clear stricter gates itself.
_HIDDEN_MIN_STLY_RN = 20      # the source had real volume at this point last year
_HIDDEN_MIN_GAP_RN  = 10
_HIDDEN_MIN_PCT     = 0.20    # vs the 10% month gate
_HIDDEN_MAX_CARDS   = 2


def _signal_hidden_source(ctx: dict) -> list[dict]:
    """A month that looks steady overall (within ±10% of same time last
    year, so no pace card) while ONE source is far behind and the other
    sources together are ahead — the total hides it. Gates on the source
    itself: >= 20 rn last year, >= 10 rn and >= 20% behind, stake (its rn
    gap × its own same-time-last-year rate) >= €1,000. Q17 must describe the
    same bookings as Q4 (totals within max(3 rn, 2%)) or it stays silent.
    This year's months only (Q17 has no next-year rows)."""
    today, sbm, base = ctx["today"], ctx["sources_by_month"], ctx["daily_rev_baseline"]
    cands: list[dict] = []
    if not sbm:
        return cands
    for p in ctx["months"]:
        if p.get("next_year"):
            continue
        sm, sy = p["month_num"], p["year"]
        rn_ty, rn_stly = float(p.get("rn") or 0), float(p.get("rn_stly") or 0)
        if not rn_stly:
            continue
        total_pct = (rn_ty - rn_stly) / rn_stly
        if abs(total_pct) >= 0.10:
            continue          # the pace card already tells this month's story
        m_start, m_end = _month_bounds(sy, sm)
        if m_end < today:
            continue
        by: dict[str, list[float]] = {}
        for r in sbm:
            if int(r.get("stay_month", 0)) != sm:
                continue
            name = str(r.get("source") or "Unknown").strip() or "Unknown"
            v = by.setdefault(name, [0.0, 0.0, 0.0, 0.0])
            v[0] += float(r.get("rn_ty", 0) or 0)
            v[1] += float(r.get("rev_ty", 0) or 0)
            v[2] += float(r.get("rn_stly", 0) or 0)
            v[3] += float(r.get("rev_stly", 0) or 0)
        q17_ty = sum(v[0] for v in by.values())
        q17_ly = sum(v[2] for v in by.values())
        if (abs(q17_ty - rn_ty) > max(3.0, 0.02 * rn_ty)
                or abs(q17_ly - rn_stly) > max(3.0, 0.02 * rn_stly)):
            continue          # Q17 and Q4 disagree — never attribute
        best = None
        for name, (s_ty, _s_rev, s_ly, s_rev_ly) in by.items():
            gap = s_ty - s_ly
            if s_ly < _HIDDEN_MIN_STLY_RN or gap > -_HIDDEN_MIN_GAP_RN:
                continue
            pct = gap / s_ly
            if pct > -_HIDDEN_MIN_PCT:
                continue
            stake = -gap * (s_rev_ly / s_ly)
            if stake < _STAKE_FLOOR_EUR:
                continue
            if best is None or stake > best[5]:
                best = (name, s_ty, s_ly, gap, pct, stake)
        if not best:
            continue
        name, s_ty, s_ly, gap, pct, stake = best
        offset = (rn_ty - rn_stly) - gap      # every other source combined
        if offset <= 0:
            continue          # others are not covering it — nothing is hidden

        m_name      = _cal.month_abbr[sm]
        month_label = f"{m_name} {sy}"
        p_full      = f"{m_name} full month"
        p_vs        = f"{m_name} full month, vs same time last year"
        f_total     = _pct_signed(total_pct * 100)
        f_src_pct   = _pct_signed(pct * 100)
        f_src_abs   = _pct(abs(pct) * 100, 0)
        f_gap       = _rn_signed(gap)
        f_offset    = _rn_signed(offset)
        days_to_start = max(0, (m_start - today).days)
        window_left   = max(0, (m_end - today).days)
        facts = {
            "month_label":         month_label,
            "bookings_vs_stly":    _fact(f_total, p_vs),
            "source":              name,
            "source_move":         _fact(f"{name} {f_gap}", p_vs),
            "source_vs_stly_pct":  _fact(f_src_pct, f"{name}, {p_vs}"),
            "source_behind_pct":   _fact(f_src_abs, f"{name}, {p_vs}"),
            "source_rn":           _fact(f"{int(s_ty):,} rn vs {int(s_ly):,} rn", f"{name}, {p_vs}"),
            "other_sources_move":  _fact(f_offset, f"all other sources combined, {p_vs}"),
            "value_at_stake":      _eur(stake),
            "value_at_stake_calc": f"{abs(int(gap))} rn × {name} same-time-last-year rate",
        }
        hypo = [{"text": (f"{name} is {f_src_pct} vs same time last year while the other sources "
                          f"together are {f_offset} — the monthly total hides a source-specific gap"),
                 "confidence": "High"}]
        fb = {
            "id": f"hidden_src_{re.sub(r'[^a-z0-9]+', '', name.lower()) or 'unknown'}_{m_name.lower()}_{sy}",
            "tag": "ALERT",
            "headline": f"{m_name} steady overall, but {name} is {f_src_abs} behind",
            "evidence": [
                {"label": f"BOOKINGS ({p_full})", "value": f_total, "sub": "vs same time last year"},
                {"label": "ONE SOURCE BEHIND", "value": f"{name} {f_gap}",
                 "sub": f"{f_src_pct}; other sources {f_offset}"},
            ],
            "what_happened": (f"{m_name} bookings overall are {f_total} vs same time last year, "
                              f"but {name} is {f_gap} ({f_src_pct})."),
            "why_it_matters": (f"The other sources together are {f_offset}, so the monthly total "
                               f"hides this gap (confidence: High)."),
            "recommended_action": (f"It may be worth checking what changed with {name} for "
                                   f"{m_name} — availability, rates or a paused connection."),
            "by_when": "Within 7 days.",
            "at_stake": {"value": facts["value_at_stake"], "calc": facts["value_at_stake_calc"]},
        }
        score = _score_candidate(min(stake / base, 1.0), _urgency(days_to_start),
                                 _magnitude_pct(pct), C=_confidence(s_ly))
        cands.append({
            "signal": "hidden_source", "tag": "ALERT", "score": round(score, 4),
            "title_hint": fb["headline"], "month_num": sm, "stay_year": sy, "stake_eur": stake,
            "insight": {
                "id": fb["id"], "tag": "ALERT", "score": round(score, 4), "signal": "hidden_source",
                "stay_period": {"from": max(m_start, today).isoformat(), "to": m_end.isoformat(),
                                "label": month_label},
                "days_to_nearest_arrival": days_to_start,
                "booking_window_days_left": window_left,
                "facts": facts,
                "cause_hypotheses": hypo,
                "action_directives": {
                    "type": "investigate_source",
                    "target": f"what changed with {name} for {m_name} — availability, rates or a paused connection",
                    "deadline": "within 7 days",
                    "trigger_if_monitor": None,
                },
                "history": {"first_raised": None, "previously_advised": None},
            },
            "fallback_card": fb,
        })
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:_HIDDEN_MAX_CARDS]


# ── Release 2: cross-filter discoveries (briefing/discovery.py) ──────────────
# DARK by default (user decision 2026-10-28): discoveries are recorded in
# cards_audit ("discovery:<id>", tag WATCH) and never reach an owner until
# their numbers have been reconciled against Hotel Bi for each hotel. Set
# DISCOVERY_CARDS=1 to let them compete as cards; even then a discovery about
# a month/event that already has a card FOLDS into that card (one story, one
# card) instead of shipping twice.
_DISCOVERY_CARDS = _os.getenv("DISCOVERY_CARDS", "0") == "1"


def _short_date(d: _date) -> str:
    return d.strftime("%b %d").replace(" 0", " ")


def _slice_desc(dims: dict) -> str:
    """'BK weekday bookings made 31–60 days ahead' — words only from Layer A
    values; source codes verbatim (rule 15)."""
    lead = dims.get("lead_bucket")
    if lead:
        lead = "over 90" if lead == "90+" else lead.replace("-", "–")
    parts = [p for p in (dims.get("source"), dims.get("day_type")) if p]
    desc = " ".join(parts + ["bookings"])
    if lead:
        desc += f" made {lead} days ahead"
    if dims.get("room_cat"):
        desc += f" in room category {dims['room_cat']}"
    return desc


def _discovery_candidate(d: dict, ctx: dict) -> dict:
    """Presentation for one raw discovery: period-scoped facts, a fallback
    card inside the caps, review language, a fingerprint metric, the score."""
    today, base = ctx["today"], ctx["daily_rev_baseline"]
    kind = d["discovery_type"]
    m_name = _cal.month_abbr[d["month"]]
    month_label = f"{m_name} {d['year']}"
    f_from, f_to = _short_date(d["from"]), _short_date(d["to"])
    span = f"{f_from}–{f_to}" if d["from"] != d["to"] else f_from
    p_vs = f"stays {span}, vs same weekdays last year"
    conf = d["confidence"]
    m_start = _date(d["year"], d["month"], 1)
    days_to_start = max(0, (max(d["from"], m_start) - today).days)

    if kind == "hidden_day_type":
        pat, ov = d["pattern"], d["overall"]
        dt = pat["dims"]["day_type"]
        nights = "Friday and Saturday nights" if dt == "weekend" else "Sunday to Thursday nights"
        other = "weekdays" if dt == "weekend" else "weekends"
        other_delta = ov["delta"] - pat["delta"]
        f_total, f_cell = _pct_signed(ov["pct"] * 100), _pct_signed(pat["pct"] * 100)
        f_abs, f_gap, f_other = _pct(abs(pat["pct"]) * 100, 0), _rn_signed(pat["delta"]), _rn_signed(other_delta)
        facts = {
            "month_label":       month_label,
            "bookings_vs_ly":    _fact(f_total, p_vs),
            "nights":            nights,
            "nights_vs_ly":      _fact(f_cell, f"{nights}, {p_vs}"),
            "nights_behind_pct": _fact(f_abs, f"{nights}, {p_vs}"),
            "nights_rn":         _fact(f"{int(pat['ty']):,} rn vs {int(pat['ly']):,} rn", f"{nights}, {p_vs}"),
            "nights_gap":        _fact(f_gap, f"{nights}, {p_vs}"),
            "other_nights_move": _fact(f_other, f"other nights, {p_vs}"),
        }
        why = f"The other nights are {f_other}, which hides a {f_gap} gap in the monthly total"
        action = f"It may be worth checking which {m_name} {dt} dates and sources are behind last year."
        drv = d.get("driver")
        if drv:
            desc = _slice_desc(drv["dims"])
            facts["driver"] = _fact(desc, f"{nights}, {p_vs}")
            facts["driver_share"] = _fact(_share_of(drv["share"], "gap"), f"{nights}, {p_vs}")
            why += f"; {desc} account for {_share_of(drv['share'], 'gap')}"
            action = f"It may be worth reviewing {desc} for {m_name} {dt} nights."
        hypo_text = (f"{nights} are {f_cell} vs the same weekdays last year while the other nights "
                     f"are {f_other} — the monthly total hides it")
        fb = {
            "headline": f"{m_name}: {dt} bookings {f_abs} behind, hidden by {other}",
            "evidence": [
                {"label": f"BOOKINGS ({span})", "value": f_total, "sub": "vs same weekdays last year"},
                {"label": "FRI + SAT NIGHTS" if dt == "weekend" else "SUN–THU NIGHTS", "value": f_cell,
                 "sub": f"{int(pat['ty']):,} rn vs {int(pat['ly']):,} rn"},
            ],
            "what_happened": (f"Stays {span} are {f_total} vs the same weekdays last year, "
                              f"but {nights} are {f_cell}."),
            "why_it_matters": f"{why} (confidence: {conf}).",
            "recommended_action": action,
            "by_when": "Within 7 days.",
        }
        slug, metric, ly_vol, magnitude = dt, pat["pct"] * 100, pat["ly"], _magnitude_pct(pat["pct"])
    elif kind == "gap_driver":
        pat, ov = d["pattern"], d["overall"]
        desc = _slice_desc(pat["dims"])
        f_total, f_total_gap = _pct_signed(ov["pct"] * 100), _rn_signed(ov["delta"])
        f_gap, f_share = _rn_signed(pat["delta"]), _share_of(d["share"], "gap")
        f_rest = _pct_signed(d["rest_pct"] * 100)
        facts = {
            "month_label":    month_label,
            "bookings_vs_ly": _fact(f_total, p_vs),
            "bookings_gap":   _fact(f_total_gap, p_vs),
            "slice":          _fact(desc, p_vs),
            "slice_gap":      _fact(f_gap, p_vs),
            "slice_share":    _fact(f_share, p_vs),
            "rest_of_month":  _fact(f_rest, f"everything else, {p_vs}"),
        }
        rest_txt = (f"the rest of the month is close to last year ({f_rest})" if abs(d["rest_pct"]) < 0.10
                    else f"the rest of the month is {f_rest}")
        headline = f"{m_name} gap: mostly {desc}"
        if len(headline.split()) > _WORD_CAPS["headline"]:
            headline = f"{m_name}: one slice carries most of the gap"
        fb = {
            "headline": headline,
            "evidence": [
                {"label": f"BOOKINGS ({span})", "value": f_total, "sub": "vs same weekdays last year"},
                {"label": "WHERE THE GAP SITS", "value": f_gap, "sub": f_share},
            ],
            "what_happened": (f"Stays {span} are {f_total} ({f_total_gap}) vs the same "
                              f"weekdays last year."),
            "why_it_matters": f"{desc[0].upper() + desc[1:]} account for {f_share} ({f_gap}); {rest_txt} (confidence: {conf}).",
            "recommended_action": f"It may be worth checking what changed for {desc} in {m_name}.",
            "by_when": "Within 7 days.",
        }
        hypo_text = f"{desc[0].upper() + desc[1:]} account for {f_share} — {rest_txt}"
        slug = "_".join(str(v) for v in pat["dims"].values())
        metric, ly_vol, magnitude = pat["pct"] * 100, pat["ly"], _magnitude_pct(pat["pct"])
    else:   # cancellation_driver
        src, top = d["source"], d["top_dates"]
        consecutive = all((b - a).days == 1 for a, b in zip(top, top[1:]))
        dates = (f"{_short_date(top[0])}–{_short_date(top[-1])}" if len(top) > 1 and consecutive
                 else ", ".join(_short_date(x) for x in top[:3]))
        p_y = "cancelled yesterday"
        f_share = _share_of(d["share"], "cancellations")
        f_norm = f"{d['source_prior_avg']:.1f} rn/day"
        facts = {
            "month_label":       month_label,
            "cancelled_yday":    _fact(f"{int(d['total_rn'])} rn", f"{p_y}, all stays"),
            "source_cancelled":  _fact(f"{src} {int(d['source_rn'])} rn", p_y),
            "source_share":      _fact(f_share, p_y),
            "source_normal":     _fact(f_norm, f"{src} cancellations per day, prior 7 days"),
            "stay_dates":        _fact(dates, f"stays hit by yesterday's {src} cancellations"),
            "cancelled_rev":     _fact(_eur(d["source_rev"]), f"{src}, {p_y}"),
        }
        headline = f"Yesterday's cancellations: mostly {src}, for {dates} stays"
        if len(headline.split()) > _WORD_CAPS["headline"]:
            headline = f"Yesterday's cancellations came mostly from {src}"
        fb = {
            "headline": headline,
            "evidence": [
                {"label": "CANCELLED YESTERDAY", "value": f"{int(d['total_rn'])} rn", "sub": f"{src}: {int(d['source_rn'])} rn"},
                {"label": "STAY DATES HIT", "value": dates, "sub": f"{src} normal: {f_norm}"},
            ],
            "what_happened": (f"{int(d['total_rn'])} room nights were cancelled yesterday; "
                              f"{int(d['source_rn'])} came from {src}, against a normal {f_norm} from that source."),
            "why_it_matters": f"{src} accounts for {f_share}, mostly for {dates} stays (confidence: {conf}).",
            "recommended_action": f"Worth checking yesterday's {src} cancellations for {dates} for a common group or contract.",
            "by_when": "Today — before the next briefing.",
        }
        hypo_text = f"{src} accounts for {f_share} of yesterday's cancellations, mostly for {dates} stays"
        slug, metric, ly_vol, magnitude = src, d["share"] * 100, d["source_rn"], _magnitude_z(d["z"])

    cid = f"disc_{kind}_{re.sub(r'[^a-z0-9]+', '', str(slug).lower()) or 'x'}_{m_name.lower()}_{d['year']}"
    facts["value_at_stake"] = _eur(d["exposure_eur"])           # internal only
    facts["value_at_stake_calc"] = "slice room nights × that slice's own last-year rate"
    fb.update({"id": cid, "tag": "ALERT"})
    score = _score_candidate(min(d["exposure_eur"] / base, 1.0), _urgency(days_to_start),
                             magnitude, C=_confidence(ly_vol))
    return {
        "signal": "discovery", "discovery_type": kind, "parent_signal": d["parent_signal"],
        "tag": "ALERT", "score": round(score, 4), "title_hint": fb["headline"],
        "month_num": d["month"], "stay_year": d["year"], "stake_eur": d["exposure_eur"],
        "disc_metric": metric,
        "insight": {
            "id": cid, "tag": "ALERT", "score": round(score, 4), "signal": "discovery",
            "stay_period": {"from": d["from"].isoformat(), "to": d["to"].isoformat(),
                            "label": f"stays {span}"},
            "days_to_nearest_arrival": days_to_start,
            "booking_window_days_left": max(0, (d["to"] - today).days),
            "facts": facts,
            "cause_hypotheses": [{"text": hypo_text, "confidence": conf}],
            "action_directives": {
                "type": "investigate_cancellations" if kind == "cancellation_driver" else "investigate_gap",
                "target": fb["recommended_action"],
                "deadline": fb["by_when"].rstrip("."),
                "trigger_if_monitor": None,
            },
            "history": {"first_raised": None, "previously_advised": None},
        },
        "fallback_card": fb,
        "discovery": {k: v for k, v in d.items() if k not in ("from", "to", "top_dates")},
    }


def _fold_discoveries(candidates: list[dict], discoveries: list[dict]) -> list[dict]:
    """One story, one card: a discovery about a month that already has a pace
    card (or about yesterday's cancellations that already have a pickup /
    cancellation card) folds its facts and leading explanation into that
    card; otherwise it competes as its own candidate."""
    for dc in discoveries:
        if dc["parent_signal"] == "pace":
            host = next((c for c in candidates if c["signal"] == "pace"
                         and (c.get("month_num"), c.get("stay_year")) == (dc["month_num"], dc["stay_year"])), None)
        else:
            host = next((c for c in candidates if c["signal"] in ("pickup", "cancellation")
                         and c.get("tag") == "ALERT"
                         and (c.get("month_num"), c.get("stay_year")) == (dc["month_num"], dc["stay_year"])), None)
        if host is None:
            candidates.append(dc)
            continue
        for k, v in dc["insight"]["facts"].items():
            if k != "month_label" and not k.startswith("value_at_stake"):
                host["insight"]["facts"].setdefault(f"driver_{k}", v)
        host["insight"]["cause_hypotheses"].insert(0, dc["insight"]["cause_hypotheses"][0])
        print(f"[analyst] Discovery {dc['insight']['id']} folded into {host['insight']['id']}")
    return candidates


def _signal_lead_time(ctx: dict) -> list[dict]:
    data, today, pace = ctx["data"], ctx["today"], ctx["pace"]
    daily_rev_baseline = ctx["daily_rev_baseline"]
    candidates: list[dict] = []

    # ── Signal 3: Booking lead time by stay month (hard gate: |shift| >= 10%) ─
    # Compares avg lead of bookings made in the last 28 days vs the same window
    # LY, per stay month; drills into the top-shifting source. Bucket profile
    # (city = close-in detail, resort = far-out detail) comes from hotel_type.
    lead_rows = data.get("lead_time", [])
    if lead_rows:
        hotel_type    = data.get("hotel_type", "resort")
        close_buckets = {"0-3", "4-7"} if hotel_type == "city" else {"0-3", "4-7", "8-15"}
        close_label   = "7 days" if hotel_type == "city" else "15 days"

        lt_month: dict[tuple, dict] = {}
        lt_src:   dict[tuple, dict] = {}
        for r in lead_rows:
            sm, sy = int(r.get("stay_month", 0)), int(r.get("stay_year", 0))
            per = r.get("period")
            if per == "LY":
                sy += 1  # align each LY slice with its TY counterpart month
            rn = float(r.get("rn", 0))
            a = lt_month.setdefault((sm, sy, per),
                                    {"rn": 0.0, "rev": 0.0, "lxr": 0.0, "close_rn": 0.0})
            a["rn"]  += rn
            a["rev"] += float(r.get("rev", 0))
            a["lxr"] += float(r.get("lead_x_rn", 0))
            if r.get("lead_bucket") in close_buckets:
                a["close_rn"] += rn
            s = lt_src.setdefault((sm, sy, per, r.get("source") or "Direct"),
                                  {"rn": 0.0, "lxr": 0.0})
            s["rn"]  += rn
            s["lxr"] += float(r.get("lead_x_rn", 0))

        lt_cands: list[dict] = []
        for sm, sy in sorted({(k[0], k[1]) for k in lt_month if k[2] == "TY"}):
            ty_a = lt_month.get((sm, sy, "TY"))
            ly_a = lt_month.get((sm, sy, "LY"))
            # Sample gate: both periods need volume for a stable average
            if not ty_a or not ly_a or ty_a["rn"] < 15 or ly_a["rn"] < 15:
                continue
            m_start, m_end = _month_bounds(sy, sm)
            if m_end < today:
                continue
            wavg_ty = ty_a["lxr"] / ty_a["rn"]
            wavg_ly = ly_a["lxr"] / ly_a["rn"]
            if wavg_ly <= 0:
                continue
            shift_days = wavg_ty - wavg_ly
            shift_pct  = shift_days / wavg_ly
            if abs(shift_pct) < 0.10:
                continue  # hard gate: <10% shift is noise
            if abs(shift_days) < _LEAD_SHIFT_MIN_DAYS:
                continue  # 2026-09-28: 10d vs 9d cleared 10% on tiny numbers — a
                          # shift under 2 days is not news for a hotel owner
            rev_in_motion = ty_a["rev"]
            if rev_in_motion < _STAKE_FLOOR_EUR:
                continue  # spec C2.2 significance floor

            m_name        = _cal.month_abbr[sm]
            month_label   = f"{m_name} {sy}"
            days_to_start = max(0, (m_start - today).days)
            window_left   = max(0, (m_end - today).days)
            shrinking     = shift_days < 0          # guests book closer-in than LY
            pace_m        = _month_row(ctx, sm, sy)
            pace_status   = pace_m.get("status") if pace_m else None
            behind        = pace_status == "behind"

            share_ty = ty_a["close_rn"] / ty_a["rn"]
            share_ly = ly_a["close_rn"] / ly_a["rn"] if ly_a["rn"] else 0.0

            lead_ty_i = round(wavg_ty)
            lead_ly_i = round(wavg_ly)
            shift_i   = lead_ty_i - lead_ly_i
            f_lead_ty = f"{lead_ty_i} days"
            f_lead_ly = f"{lead_ly_i} days"
            f_shift   = (f"{shift_days:+.1f} days" if abs(shift_i) < 2
                         else f"{shift_i:+d} days")
            f_share_ty = _pct(share_ty * 100, 0)
            f_share_ly = _pct(share_ly * 100, 0)
            f_stake    = _eur(rev_in_motion)
            f_calc     = (f"{_eur(rev_in_motion)} booked in last 28 days for "
                          f"{month_label}, arriving {abs(shift_days):.0f} days "
                          f"{'later' if shrinking else 'earlier'} than last year")

            p_ty = f"bookings made last 28 days for {month_label}"
            p_ly = f"same 28-day window last year for {month_label}"
            facts = {
                "month_label":   month_label,
                "avg_lead":      _fact(f_lead_ty, p_ty),
                "avg_lead_ly":   _fact(f_lead_ly, p_ly),
                "lead_shift":    _fact(f_shift, "vs last year"),
                "close_in_share":    _fact(f_share_ty, f"{p_ty}, within {close_label} of arrival"),
                "close_in_share_ly": _fact(f_share_ly, f"{p_ly}, within {close_label} of arrival"),
                "value_at_stake":      f_stake,
                "value_at_stake_calc": f_calc,
            }

            # Top-shifting source drill-down (needs volume on both sides)
            best_src = None
            for (ssm, ssy, sper, src), s_ty in lt_src.items():
                if (ssm, ssy, sper) != (sm, sy, "TY") or s_ty["rn"] < 8:
                    continue
                s_ly = lt_src.get((sm, sy, "LY", src))
                if not s_ly or s_ly["rn"] < 8:
                    continue
                d = s_ty["lxr"] / s_ty["rn"] - s_ly["lxr"] / s_ly["rn"]
                if best_src is None or abs(d) > abs(best_src[1]):
                    best_src = (src, d, s_ty["lxr"] / s_ty["rn"], s_ly["lxr"] / s_ly["rn"])
            if best_src:
                facts["top_source_shift"] = _fact(
                    f"{best_src[0]}: {best_src[2]:.0f}d vs {best_src[3]:.0f}d",
                    f"avg lead, {p_ty} vs same window last year")

            if shrinking:
                tag = "MONITOR"
                if behind:
                    hypo = [{"text": f"Guests are booking {month_label} closer to arrival than last year — the booking gap may be timing, not lost demand", "confidence": "Medium"}]
                    fb_why = "The booking gap may be timing rather than lost demand — bookings are simply arriving later than last year (confidence: Medium)."
                    fb_action = f"Worth monitoring {month_label} new bookings before reading the gap as lost demand — they appear to be arriving later."
                else:
                    hypo = [{"text": f"Guests are booking later for {month_label} — last-minute bookings are carrying the month", "confidence": "Medium"}]
                    fb_why = "Last-minute bookings are carrying the month, more than they did last year (confidence: Medium)."
                    fb_action = f"Worth monitoring whether last-minute bookings keep coming for {month_label} as they do now."
                directive = {
                    "type": "monitor_timing",
                    "target": f"{month_label} new bookings — guests book closer to arrival than last year",
                    "deadline": "recheck in 7 days",
                    "trigger_if_monitor": f"{month_label} new bookings slowing while guests keep booking late",
                }
                fb_headline = f"{month_label} guests book {abs(shift_i)} days closer to arrival than last year"
                fb_by_when = "Recheck in 7 days — sooner if new bookings slow."
            elif behind:
                tag = "ALERT"
                hypo = [{"text": f"Guests are booking {month_label} earlier than last year while bookings are behind — the late demand being counted on may not come through", "confidence": "Medium"}]
                directive = {
                    "type": "investigate_demand",
                    "target": f"{month_label} pricing and visibility — late bookings are not making up the gap",
                    "deadline": "within 2 days",
                    "trigger_if_monitor": None,
                }
                fb_why = "Guests now book earlier, so waiting for late bookings to close the gap looks riskier than last year (confidence: Medium)."
                fb_action = f"A review of {month_label} prices and where the hotel is listed may be worthwhile — the time to influence the month is running out earlier."
                fb_headline = f"{month_label} books {_days_word(abs(shift_i))} earlier than last year while bookings lag"
                fb_by_when = "Within 2 days."
            else:
                tag = "OPPORTUNITY" if pace_status == "ahead" else "MONITOR"
                hypo = [{"text": f"Guests book {month_label} earlier than last year — demand looks stronger", "confidence": "Medium"}]
                directive = {
                    "type": "review_rates",
                    "target": f"whether {month_label} rates still fit, with guests booking earlier than last year",
                    "deadline": "within 3 days",
                    "trigger_if_monitor": None,
                }
                fb_why = "Guests booking earlier usually means stronger demand for the month (confidence: Medium)."
                fb_action = f"It may be worth reviewing whether {month_label} rates still fit, with guests booking earlier than last year."
                fb_headline = f"{month_label} guests book {_days_word(abs(shift_i))} earlier than last year"
                fb_by_when = "Within 3 days."

            # Which source accounts for the shift (review 2026-10-28) — an
            # exact, signed split; named only when one source carries >= 60%.
            lead_attr = _lead_shift_attribution(lt_src, sm, sy, ty_a["rn"], ly_a["rn"],
                                                wavg_ly, shift_days)
            if lead_attr:
                p_src = f"{p_ty} vs same window last year"
                facts["shift_source"]       = _fact(lead_attr["source"], p_src)
                facts["shift_source_share"] = _fact(lead_attr["share_txt"], f"of the change, {p_src}")
                facts["shift_source_how"]   = _fact(lead_attr["kind_txt"], p_src)
                hypo.insert(0, {"text": lead_attr["sentence"].rstrip("."), "confidence": "Medium"})
                fb_why = f"{lead_attr['sentence']} {fb_why}"

            fallback_card = {
                "id": f"leadtime_{m_name.lower()}_{sy}",
                "tag": tag,
                "headline": fb_headline,
                "evidence": [
                    {"label": "DAYS BOOKED AHEAD", "value": f"{lead_ty_i}d",
                     "sub": f"vs {lead_ly_i}d same window last year"},
                    {"label": "LAST-MINUTE SHARE", "value": f_share_ty,
                     "sub": f"vs {f_share_ly} last year, within {close_label}"},
                ],
                "what_happened": (f"Bookings made in the last 28 days for {month_label} "
                                  f"arrive {f_lead_ty} after booking, versus {f_lead_ly} "
                                  f"last year."),
                "why_it_matters": fb_why,
                "recommended_action": fb_action,
                "by_when": fb_by_when,
                "at_stake": {"value": f_stake, "calc": f_calc},
            }

            R = min(rev_in_motion / (daily_rev_baseline * 28.0), 1.0)
            U = _urgency(days_to_start)
            M = _magnitude_pct(abs(shift_pct))
            C = _confidence(ty_a["rn"])
            score = _score_candidate(R, U, M, C=C)

            lt_cands.append({
                "signal":     "lead_time",
                "tag":        tag,
                "score":      round(score, 4),
                "title_hint": fb_headline,
                "month_num":  sm,
                "stake_eur":  rev_in_motion,
                "insight": {
                    "id": f"leadtime_{m_name.lower()}_{sy}",
                    "tag": tag,
                    "score": round(score, 4),
                    "signal": "lead_time",
                    "stay_period": {"from": max(m_start, today).isoformat(),
                                    "to": m_end.isoformat(), "label": month_label},
                    "days_to_nearest_arrival": days_to_start,
                    "booking_window_days_left": window_left,
                    "facts": facts,
                    "cause_hypotheses": hypo,
                    "action_directives": directive,
                    "history": {"first_raised": None, "previously_advised": None},
                },
                "fallback_card": fallback_card,
            })

        # At most 2 lead-time cards per day — keep the sharpest stories
        lt_cands.sort(key=lambda c: c["score"], reverse=True)
        candidates.extend(lt_cands[:2])
    return candidates


def _signal_dates(ctx: dict) -> list[dict]:
    data, today, pace, total_rooms = ctx["data"], ctx["today"], ctx["pace"], ctx["total_rooms"]
    daily_rev_baseline = ctx["daily_rev_baseline"]
    candidates: list[dict] = []

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

            try:
                stay_date = _date.fromisoformat(str(row["stay_date"])[:10])
            except (KeyError, TypeError, ValueError):
                continue  # one malformed row is skipped, never the whole signal
            days_out  = (stay_date - today).days
            if not 0 <= days_out <= 90:
                continue  # Q10 already returns today..+90; guard anyway (review 2026-10-28)

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
            major_year = next(d["date"].year for d in top_soft if d["date"].month == major_month)
            pace_m  = _month_row(ctx, major_month, major_year)
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
                    "headline": f"{len(top_soft)} nights with bookings {f_avg_gap} behind same time last year",
                    "evidence": [
                        {"label": "LOWEST DATES", "value": softest_str, "sub": "vs same time last year"},
                        {"label": f"AVG GAP ({date_range})", "value": f_avg_gap, "sub": "vs bookings at same time last year"},
                    ],
                    "what_happened": f"{len(top_soft)} stay dates between {first_lbl} and {last_lbl} average {f_avg_gap} vs same time last year.",
                    "why_it_matters": "These dates have fewer bookings than last year at this point; if the gap persists the shortfall grows (confidence: Medium).",
                    "recommended_action": f"It may be worth checking whether last year had an event or group on {date_range} that has not repeated.",
                    "by_when": "Within 7 days — time is running out.",
                    "at_stake": {"value": f_stake, "calc": f_calc},
                }
                # Date cluster (review 2026-10-28): consecutive low nights are
                # one block — a missing event or group shows up as a run, not
                # as scattered dates. Calendar-date comparison, like the rest
                # of this signal (same-weekday alignment needs Release 2 data).
                run = _longest_run(top_soft)
                if len(run) >= 3:
                    run_range = f"{run[0]['label']}–{run[-1]['label']}"
                    f_run = f"{len(run)} nights in a row ({run_range})"
                    facts["consecutive_nights"] = _fact(f_run, "vs same time last year")
                    fb["headline"] = f"{f_run} behind last year"
                    fb["why_it_matters"] = ("Several nights in a row are behind, which may point to "
                                            "an event or group last year that has not repeated "
                                            "(confidence: Low).")
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
                        "cause_hypotheses": [{"text": "Weaker demand on these specific dates — check for last-year events or groups that have not repeated", "confidence": "Medium"}],
                        "action_directives": {
                            "type": "investigate_dates",
                            "target": f"whether last year had an event or group on {date_range} that has not repeated",
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
            # Honest words (user 2026-10-28, threshold stays 75%): a 75%-booked
            # night is filling up, not "near sell-out", and we hold no rate
            # history for these dates — so no claim about rates at all.
            f_near_occ = _pct(nearest["occ_ty"], 0)
            fb = {
                "id": "hot_dates_near_full",
                "tag": "OPPORTUNITY",
                "headline": f"{date_range} filling up fast, ahead of last year",
                "evidence": [
                    {"label": f"{nearest['label'].upper()} ({nearest['dow'].upper()})", "value": f"{f_near_occ} booked", "sub": f"{nearest['rooms_left']} rooms left"},
                    {"label": "FILLING UP FAST", "value": " · ".join(_pct(d["occ_ty"], 0) for d in top_hot), "sub": date_range},
                ],
                "what_happened": (f"{len(top_hot)} nights ({date_range}) are filling up fast; {nearest['label']} "
                                  f"is {f_near_occ} booked with {nearest['rooms_left']} rooms left."),
                "why_it_matters": "Bookings on these dates are well ahead of the same time last year, so demand looks strong for the rooms that are left (confidence: High).",
                "recommended_action": f"It may be worth reviewing the rates and discounts still open on {date_range}.",
                "by_when": f"Today — {nearest['label']} is {_days_word(nearest['days_out'])} out.",
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
                    "cause_hypotheses": [{"text": "Dates filling up fast — bookings well ahead of the same time last year", "confidence": "High"}],
                    "action_directives": {
                        "type": "review_rates",
                        "target": f"rates and discounts still open on {date_range}",
                        "deadline": f"today — {nearest['label']} is {_days_word(nearest['days_out'])} out",
                        "trigger_if_monitor": None,
                    },
                    "history": {"first_raised": None, "previously_advised": None},
                },
                "fallback_card": fb,
            })
    return candidates


def _signal_projection_current(ctx: dict) -> list[dict]:
    today, pace, cm, mtd = ctx["today"], ctx["pace"], ctx["cm"], ctx["mtd"]
    total_rooms, daily_rev_baseline = ctx["total_rooms"], ctx["daily_rev_baseline"]
    candidates: list[dict] = []

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
                hypo = [{"text": "Bookings for the rest of the month are running below last year's last-minute bookings", "confidence": "Medium"}]
                directive = {
                    "type": "investigate_gap",
                    "target": f"which remaining {month_name} dates are furthest behind last year",
                    "deadline": "within 2–3 days",
                    "trigger_if_monitor": None,
                }
                fb = {
                    "headline": f"{month_name} expected to finish around {f_vs_band} vs {ref_label}",
                    "evidence": [
                        {"label": "EXPECTED FINISH", "value": f_vs_band, "sub": f"vs {ref_label}"},
                        {"label": "STILL TO COME", "value": f_rem_otb, "sub": f"{days_left} days left"},
                    ],
                    "what_happened": f"Month-end revenue is expected to land {f_vs_band} vs {ref_label}.",
                    "why_it_matters": "Bookings for the rest of the month may be running below last year's last-minute bookings (confidence: Medium).",
                    "recommended_action": f"It may be worth checking which remaining {month_name} dates are furthest behind last year.",
                    "by_when": f"Within 2–3 days; {days_left} days left.",
                }
                if "value_at_stake" in facts:
                    fb["at_stake"] = {"value": facts["value_at_stake"], "calc": facts["value_at_stake_calc"]}
            else:
                hypo = [{"text": "Cancellations in the remaining days are the main risk to the finish", "confidence": "High"}]
                directive = {
                    "type": "monitor_only",
                    "target": f"remaining {month_name} bookings",
                    "deadline": "daily until month close",
                    "trigger_if_monitor": f"cancellations run above the recent normal on any remaining {month_name} date",
                }
                fb = {
                    # 11 words with a 3-word band + "final last year" (cap 12). The
                    # earlier "on track to finish around …" was 13 → _enforce_caps
                    # clamped it to "… vs final last…" (ON Residence, 2026-09-13).
                    "headline": f"{month_name} on track for {f_vs_band} vs {ref_label}",
                    "evidence": [
                        {"label": "EXPECTED FINISH", "value": f_vs_band, "sub": f"vs {ref_label}"},
                        {"label": "STILL TO COME", "value": f_rem_otb, "sub": "booked, not yet earned"},
                    ],
                    "what_happened": f"Month-end revenue is expected to land {f_vs_band} vs {ref_label}.",
                    "why_it_matters": f"{f_rem_otb} is booked but not yet earned; cancellations in the final {days_left} days are the main risk (confidence: High).",
                    "recommended_action": "No action suggested — worth watching cancellations daily.",
                    "by_when": "Daily until month end; trigger: cancellations above normal.",
                }

            fb["id"]  = f"proj_{month_name.lower()}_{today.year}"
            fb["tag"] = tag
            candidates.append({
                "signal":     "projection",
                "tag":        tag,
                "score":      round(score, 4),
                "title_hint": fb["headline"],
                "month_num":  today.month,
                "stay_year":  today.year,
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
    return candidates


def _signal_projection_future(ctx: dict) -> list[dict]:
    today, daily_rev_baseline = ctx["today"], ctx["daily_rev_baseline"]
    candidates: list[dict] = []

    # Future months projection (gate: >= 10% deviation) — every forward month
    # after the current one, next year's included (each row carries its year)
    for p in ctx["months"]:
        sm = p.get("month_num", 0)
        sy = p["year"]
        if (sy, sm) <= (today.year, today.month):
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

        m_start, m_end = _month_bounds(sy, sm)
        days_to_start  = max(0, (m_start - today).days)
        window_left    = max(0, (m_end - today).days)
        m_name = _cal.month_abbr[sm]
        month_label = f"{m_name} {sy}"
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
            hypo = [{"text": "Bookings behind last year — group or contract business may not have come back", "confidence": "Low"}]
            directive = {
                "type": "investigate_gap",
                "target": f"whether last year's group or contract business for {month_label} is coming back",
                "deadline": "within 7 days",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} projected around {f_vs_band} vs {ref_label}",
                "evidence": [
                    {"label": "EXPECTED FINISH", "value": f_vs_band, "sub": f"vs {ref_label}"},
                    {"label": "OCCUPANCY BOOKED vs SAME TIME LY", "value": f"{facts['occ_otb']['value']} vs {facts['occ_same_time_ly']['value']}", "sub": f"final last year {facts['final_ly_occ']['value']}"},
                ],
                "what_happened": f"{m_name} is expected to land {f_vs_band} vs {ref_label}.",
                "why_it_matters": "Bookings are behind last year; group or contract business may not have come back (confidence: Low).",
                "recommended_action": f"It may be worth checking whether last year's group or contract business for {m_name} is coming back.",
                "by_when": f"Within 7 days; {days_to_start} days to month start.",
            }
            if "value_at_stake" in facts:
                fb["at_stake"] = {"value": facts["value_at_stake"], "calc": facts["value_at_stake_calc"]}
        else:
            hypo = [{"text": "Rooms booked ahead of last year — early minimum rates will set the final average rate", "confidence": "Medium"}]
            directive = {
                "type": "review_rates",
                "target": f"{month_label} minimum rates while demand runs ahead",
                "deadline": "this week",
                "trigger_if_monitor": None,
            }
            fb = {
                "headline": f"{m_name} expected to finish around {f_vs_band} vs {ref_label}",
                "evidence": [
                    {"label": "EXPECTED FINISH", "value": f_vs_band, "sub": f"vs {ref_label}"},
                    {"label": "AVG RATE BOOKED vs LAST YEAR FINAL", "value": f"{facts['adr_otb']['value']} vs {facts['adr_final_ly']['value']}", "sub": f"occupancy booked {facts['occ_otb']['value']}"},
                ],
                "what_happened": f"{m_name} is expected to land {f_vs_band} vs {ref_label}.",
                "why_it_matters": "Rooms booked are ahead of last year; the minimum rates set early will shape the final average rate (confidence: Medium).",
                "recommended_action": f"It may be worth reviewing {m_name} minimum rates while demand runs ahead.",
                "by_when": "This week.",
            }
        fb["id"]  = f"proj_{m_name.lower()}_{sy}"
        fb["tag"] = tag
        candidates.append({
            "signal":     "projection",
            "tag":        tag,
            "score":      round(score, 4),
            "title_hint": fb["headline"],
            "month_num":  sm,
            "stay_year":  sy,
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
    return candidates


def _signal_cancellations(ctx: dict) -> list[dict]:
    today, yesterday, yesterday_str = ctx["today"], ctx["yesterday"], ctx["yesterday_str"]
    cancel_daily, pickup_daily = ctx["cancel_daily"], ctx["pickup_daily"]
    daily_rev_baseline = ctx["daily_rev_baseline"]
    candidates: list[dict] = []

    # ── Signal 6: Cancellation spike by stay month (hard gate: z >= 2) ───────
    # Q14 ships the cancel side of Q9's net pickup as its own series, so a
    # quiet net number can no longer hide a burst of cancellations. Gates:
    # z >= 2 vs the prior 7 days (zero-filled), >= 3 rn cancelled (one or two
    # are noise), stake floor on the REAL revenue of the cancelled stays.
    if cancel_daily:
        cx_cands: list[dict] = []
        months_y = sorted({(int(r["stay_month"]), int(r["stay_year"]))
                           for r in cancel_daily if str(r.get("ref_date")) == yesterday_str})
        for sm, sy in months_y:
            m_start, m_end = _month_bounds(sy, sm)
            if m_end < today:
                continue  # a finished month is never a card
            c_y, c_rev, c_prior = _cancel_series(cancel_daily, sm, sy, yesterday)
            if c_y < 3:
                continue  # volume floor
            mean_c = statistics.mean(c_prior)
            std_c  = statistics.stdev(c_prior) if len(c_prior) > 1 else 0.0
            if std_c > 0:
                z = (c_y - mean_c) / std_c
            else:
                z = (c_y - mean_c) / max(mean_c * 0.20, 1.0)
            if z < 2.0:
                continue  # same hard gate as pickup — only a real outlier
            if c_rev < _STAKE_FLOOR_EUR:
                continue  # spec C2.2 significance floor

            m_name        = _cal.month_abbr[sm]
            month_label   = f"{m_name} {sy}"
            days_to_start = max(0, (m_start - today).days)
            window_left   = max(0, (m_end - today).days)
            net_y = next((int(r.get("net_rn", 0) or 0) for r in pickup_daily
                          if str(r.get("ref_date")) == yesterday_str
                          and int(r.get("stay_month", 0)) == sm
                          and int(r.get("stay_year", 0)) == sy), 0)
            gross_new = max(0, net_y + c_y)

            R = min(c_rev / daily_rev_baseline, 1.0)
            U = _urgency(days_to_start)
            M = _magnitude_z(z)
            C = _confidence(c_y)
            score = _score_candidate(R, U, M, C=C)

            f_c     = f"{c_y} rn"
            f_avg   = f"{mean_c:.1f} rn/day"
            f_z     = f"+{z:.1f}"
            f_rev   = _eur(c_rev)
            f_gross = f"{gross_new} rn"
            f_calc  = f"revenue of {month_label} stays cancelled yesterday = {f_rev}"
            p_yday  = f"cancelled yesterday, {month_label} stays"
            p_prior = f"cancellations per day, prior 7 days, {month_label} stays"

            facts = {
                "month_label":        month_label,
                "cancelled_yday":     _fact(f_c, p_yday),
                "cancel_avg_prior":   _fact(f_avg, p_prior),
                "cancel_swing":       _fact(f_z, "vs prior 7 days (above 2 is unusual)"),
                "cancelled_rev_yday": _fact(f_rev, p_yday),
                "new_bookings_yday":  _fact(f_gross, f"booked yesterday for {month_label}, before cancellations"),
                "value_at_stake":      f_rev,
                "value_at_stake_calc": f_calc,
            }
            hypo = [{"text": f"A group or one source cancelling {month_label} stays in one go, or guests moving to other dates", "confidence": "Low"}]
            directive = {
                "type": "investigate_cancellations",
                "target": f"yesterday's {month_label} cancellations by source, rate plan and group",
                "deadline": "today",
                "trigger_if_monitor": None,
            }
            fb_headline = f"{month_label} cancellations jumped yesterday, well above recent normal"
            fallback_card = {
                "id": f"cancel_{m_name.lower()}_{sy}",
                "tag": "ALERT",
                "headline": fb_headline,
                "evidence": [
                    {"label": "CANCELLED", "value": f_c, "sub": f"yesterday, vs {f_avg} prior 7 days"},
                    {"label": "REVENUE CANCELLED", "value": f_rev, "sub": f"{f_gross} newly booked the same day"},
                ],
                "what_happened": f"{c_y} room nights for {month_label} were cancelled yesterday, against a 7-day average of {f_avg}.",
                "why_it_matters": "A group or one source may be cancelling in one go, or guests may be moving to other dates (confidence: Low).",
                "recommended_action": f"It may be worth checking yesterday's {month_label} cancellations for a common source, rate plan or group.",
                "by_when": "Today — before the next briefing.",
                "at_stake": {"value": f_rev, "calc": f_calc},
            }
            cx_cands.append({
                "signal":     "cancellation",
                "tag":        "ALERT",
                "score":      round(score, 4),
                "title_hint": fb_headline,
                "month_num":  sm,
                "stay_year":  sy,
                "stake_eur":  c_rev,
                "insight": {
                    "id": f"cancel_{m_name.lower()}_{sy}",
                    "tag": "ALERT",
                    "score": round(score, 4),
                    "signal": "cancellation",
                    "stay_period": {"from": max(m_start, today).isoformat(), "to": m_end.isoformat(), "label": month_label},
                    "days_to_nearest_arrival": days_to_start,
                    "booking_window_days_left": window_left,
                    "facts": facts,
                    "cause_hypotheses": hypo,
                    "action_directives": directive,
                    "history": {"first_raised": None, "previously_advised": None},
                },
                "fallback_card": fallback_card,
            })
        # At most 2 cancellation cards per day — keep the sharpest stories
        cx_cands.sort(key=lambda c: c["score"], reverse=True)
        candidates.extend(cx_cands[:2])
    return candidates


_SIGNALS = (
    ("pickup",             _signal_pickup),
    ("pace",               _signal_pace),
    ("hidden_source",      _signal_hidden_source),
    ("lead_time",          _signal_lead_time),
    ("dates",              _signal_dates),
    ("projection_current", _signal_projection_current),
    ("projection_future",  _signal_projection_future),
    ("cancellation",       _signal_cancellations),
)


def _compute_signals(data: dict, hotel_id: str | None = None) -> dict:
    """
    Returns {"ranked": [...], "watchlist": [...], "demoted": [...],
             "headline": {...}, "compute_errors": [...]}.
    Ranked entries carry the full narration input contract + fallback_card.
    Every step is fail-open: a signal (or merge) that raises is skipped and
    reported in compute_errors; the rest of the briefing carries on.
    """
    ctx = _signal_context(data)
    today, yesterday_str, mtd = ctx["today"], ctx["yesterday_str"], ctx["mtd"]

    candidates: list[dict] = []
    compute_errors: list[str] = []
    for name, fn in _SIGNALS:
        try:
            candidates.extend(fn(ctx))
        except Exception as exc:
            compute_errors.append(f"{name}: {type(exc).__name__}: {exc}"[:200])
            print(f"[analyst] Signal '{name}' failed and was skipped "
                  f"(other signals continue): {type(exc).__name__}: {exc}")

    # ── Merge gates (spec C1/C2.4): same story or same dates → one card ──────
    for merge in (_merge_cancel_into_pickup, _merge_same_month_story, _merge_pickup_soft):
        try:
            candidates = merge(candidates)
        except Exception as exc:
            compute_errors.append(f"{merge.__name__}: {type(exc).__name__}: {exc}"[:200])
            print(f"[analyst] {merge.__name__} failed — cards kept unmerged: {exc}")

    # ── Release 2 cross-filter discoveries (DARK unless DISCOVERY_CARDS=1) ───
    # Computed after the merges so a discovery folds into the FINAL card for
    # its month / event (one story, one card).
    discoveries: list[dict] = []
    try:
        from briefing import discovery as _disc
        found, errs = _disc.discover_all(data, today, float(_STAKE_FLOOR_EUR))
        compute_errors.extend(errs)
        for d in found:
            try:
                discoveries.append(_discovery_candidate(d, ctx))
            except Exception as exc:
                compute_errors.append(f"discovery card {d.get('discovery_type')}: {exc}"[:200])
        if discoveries:
            print(f"[analyst] Discoveries ({'LIVE' if _DISCOVERY_CARDS else 'dark'}): "
                  f"{[c['insight']['id'] for c in discoveries]}")
        if _DISCOVERY_CARDS:
            candidates = _fold_discoveries(candidates, discoveries)
    except Exception as exc:
        compute_errors.append(f"discovery: {type(exc).__name__}: {exc}"[:200])

    # ── Fingerprints (2026-09-24): every card carries its state so the gate
    # can tell "same story" from "moved" — with or without a euro stake.
    for c in candidates:
        try:
            c["novelty"] = _fingerprint(c, yesterday_str)
        except Exception as exc:
            c["novelty"] = {"metric": None, "key": None, "days_out": 0}
            compute_errors.append(f"fingerprint {c.get('insight', {}).get('id')}: {exc}"[:200])

    # ── Novelty gate: a repeat without news → watchlist (cards are news) ─────
    demoted: list[dict] = []
    if hotel_id:
        candidates, demoted = _novelty_gate(candidates, hotel_id, today)

    # ── Global ranking — top 5-6 across ALL periods, no per-month quota ──────
    candidates.sort(key=lambda c: c["score"], reverse=True)
    ranked = [c for c in candidates if c["score"] >= 0.08][:6]

    # No floor (user decision 2026-09-24): a quiet day ships 0-2 cards and a
    # one-line Pulse note; repeats live on the watchlist, never as cards.
    # (The 3-card backfill used to promote demoted repeats every quiet day.)
    # Quiet-day pin (2026-09-28): the memory must reach the owner. With fewer
    # than _QUIET_DAY_CARDS fresh cards, the highest-scoring open ALERT ships
    # ONCE as a "Still open" card (max one repeat per day, never on busy days).
    # Everything else stays on the watchlist.
    try:
        ranked, demoted = _pin_open_alert(ranked, demoted, today)
    except Exception as exc:
        compute_errors.append(f"pin_open_alert: {exc}"[:200])
    watchlist = [c for c in candidates if c not in ranked] + demoted

    try:
        headline = _headline_kpis(data, mtd)
    except Exception as exc:
        headline = {}
        compute_errors.append(f"headline: {exc}"[:200])

    return {
        "ranked":    ranked,
        "watchlist": watchlist,
        "demoted":   demoted,     # repeats held back today (open items)
        "headline":  headline,
        "compute_errors": compute_errors,
        "discoveries": discoveries,   # Release 2 — recorded in cards_audit while dark
    }


def _headline_kpis(data: dict, mtd: dict) -> dict:
    yd = data.get("yesterday", {})
    pu = data.get("pickup", {})

    rev_yd  = float(yd.get("revenue",   0))
    rev_ly  = float(yd.get("revenueLY", 0))
    rev_var = round((rev_yd - rev_ly) / rev_ly * 100, 1) if rev_ly else None

    rev_mtd    = float(mtd.get("revenue",   0))
    rev_mtd_ly = float(mtd.get("revenueLY", 0))
    mtd_var    = round((rev_mtd - rev_mtd_ly) / rev_mtd_ly * 100, 1) if rev_mtd_ly else None

    return {
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


def _pin_open_alert(ranked: list[dict], demoted: list[dict],
                    today: _date) -> tuple[list[dict], list[dict]]:
    """Pure rule (unit-tested): on a quiet day (< _QUIET_DAY_CARDS ranked cards)
    promote the single highest-scoring demoted ALERT that still clears the
    ranking floor, marked as still open. Busy days never re-ship a repeat."""
    if len(ranked) >= _QUIET_DAY_CARDS or not demoted:
        return ranked, demoted
    alerts = [c for c in demoted if c.get("tag") == "ALERT" and c.get("score", 0) >= 0.08]
    if not alerts:
        return ranked, demoted
    pin = max(alerts, key=lambda c: c.get("score", 0))
    facts = pin["insight"]["facts"]
    first_iso = pin.get("first_seen_iso")
    days_open = 0
    if first_iso:
        try:
            days_open = max(0, (today - timedelta(days=1) - _date.fromisoformat(first_iso)).days)
        except ValueError:
            days_open = 0
    facts["status"] = (f"still open, first flagged {days_open} days ago"
                       if days_open else "still open")
    pin["title_hint"] = "Still open: " + str(pin.get("title_hint", ""))
    pin["pinned_repeat"] = True
    print(f"[analyst] Quiet day: pinned open ALERT {pin['insight']['id']} "
          f"(score {pin.get('score')}, open {days_open}d)")
    return ranked + [pin], [c for c in demoted if c is not pin]


def _merge_cancel_into_pickup(candidates: list[dict]) -> list[dict]:
    """A cancellation spike and a pickup ALERT for the same stay month are one
    story — the pickup card already carries the cancellation facts (Signal 1
    attribution), so the spike folds into it instead of shipping twice.
    Matched on the month+year suffix of the card ids."""
    def _suffix(c: dict) -> str:
        return c["insight"]["id"].split("_", 1)[-1]
    pickup_alerts = {_suffix(c): c for c in candidates
                     if c["signal"] == "pickup" and c["tag"] == "ALERT"}
    if not pickup_alerts:
        return candidates
    out: list[dict] = []
    for c in candidates:
        if c["signal"] == "cancellation" and _suffix(c) in pickup_alerts:
            keep = pickup_alerts[_suffix(c)]
            for k, v in c["insight"]["facts"].items():
                if k == "month_label" or k.startswith("value_at_stake"):
                    continue
                keep["insight"]["facts"].setdefault(f"cancel_{k}", v)
            if c["score"] > keep["score"]:
                keep["score"] = keep["insight"]["score"] = c["score"]
            print(f"[analyst] Cancel spike {c['insight']['id']} folded into {keep['insight']['id']}")
            continue
        out.append(c)
    return out


def _merge_same_month_story(candidates: list[dict]) -> list[dict]:
    """Pace + projection for the same month tell one story — merge into the
    higher-scored candidate, folding in the other's key facts."""
    by_month: dict[tuple, dict] = {}
    result: list[dict] = []
    month_level = [c for c in candidates if c["signal"] in ("pace", "projection")]
    others      = [c for c in candidates if c["signal"] not in ("pace", "projection")]

    for c in month_level:
        m = (c.get("month_num"), c.get("stay_year"))   # Jan 2027 ≠ Jan 2026
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
        "headline": f"{m_short} weakening: new bookings and booking gap point the same way",
        "evidence": [
            {"label": "NET NEW BOOKINGS", "value": pf["yday_net_rn"]["value"], "sub": f"yesterday, vs {pf['trailing_avg']['value']} prior 7 days"},
            {"label": f"LOW-BOOKING DATES ({sf['date_range']})", "value": f"{sf['count']} nights {sf['avg_gap_pts']['value']}", "sub": "vs same time last year"},
        ],
        "what_happened": f"{month_label} new bookings were {pf['yday_net_rn']['value']} yesterday and {sf['count']} stay dates trail same time last year.",
        "why_it_matters": "Two separate signals point at the same dates, so this looks like genuinely weaker demand rather than a one-off (confidence: Medium).",
        "recommended_action": f"Worth checking {m_short} cancellations and the low-booking dates for a common source.",
        "by_when": "Today — then recheck in 3–4 days.",
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
            "cause_hypotheses": [{"text": "Cancellations and the booking gap point at the same dates — demand is genuinely getting weaker, not a one-off", "confidence": "Medium"}],
            "action_directives": {
                "type": "investigate_cancellations",
                "target": f"{month_label} cancellations and low-booking dates by source and rate plan",
                "deadline": "today, then recheck in 3–4 days",
                "trigger_if_monitor": None,
            },
            "history": {"first_raised": None, "previously_advised": None},
        },
        "fallback_card": fb,
    }
    return [c for c in candidates if c is not pick and c is not soft] + [merged]


_NUM_SIGNED = re.compile(r"[-+−]?\d[\d,]*(?:\.\d+)?")


def _first_signed_number(text: str) -> float | None:
    """First number in a display string, honouring the typographic minus."""
    m = _NUM_SIGNED.search(str(text or "").replace("−", "-"))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _fingerprint(c: dict, yesterday_str: str) -> dict:
    """The card's STATE, so the novelty gate can tell 'same story' from
    'moved' for every card — including the ones without a euro stake (the
    2026-09-23 audit showed those were the daily repeaters).
      metric : a number that moving by >= 10% counts as news
      key    : '|'-joined tokens; a token never seen before counts as news
      days_out: for the 'entered the last 30 days' trigger"""
    sig   = c.get("signal", "")
    facts = c.get("insight", {}).get("facts", {}) or {}
    ins   = c.get("insight", {})
    days_out = int(ins.get("days_to_nearest_arrival") or 0)

    def _fv(name: str):
        v = facts.get(name)
        return v.get("value") if isinstance(v, dict) else v

    metric: float | None = None
    key: str | None = None
    if sig in ("pickup", "cancellation", "softening"):
        key = f"day:{yesterday_str}"          # a spike is an event: always news
    elif sig == "pace":
        # the mixed card (more rooms, less revenue) moves with its revenue gap
        metric = _first_signed_number(_fv("rev_vs_stly_pct") or _fv("rn_gap"))
    elif sig == "projection":
        metric = _first_signed_number(_fv("vs_ref_band"))
    elif sig == "hidden_source":
        metric = _first_signed_number(_fv("source_vs_stly_pct"))   # the source's own gap
    elif sig == "discovery":
        if c.get("discovery_type") == "cancellation_driver":
            key = f"day:{yesterday_str}"      # a cancellation burst is an event
        else:
            metric = c.get("disc_metric")     # the slice's own gap, signed %
    elif sig == "lead_time":
        metric = _first_signed_number(_fv("lead_shift"))
    elif sig in ("soft_dates", "hot_dates"):
        dates = sorted(str(d.get("date", "")) for d in (facts.get("per_date") or [])
                       if isinstance(d, dict))
        key = "|".join(f"date:{d}" for d in dates if d)
        if sig == "soft_dates":
            metric = float(c.get("stake_eur") or 0) or None
    if metric is None and key is None:
        metric = float(c.get("stake_eur") or 0) or None
    return {"metric": metric, "key": key, "days_out": days_out}


def _novelty_decide(candidates: list[dict], prior_rows: list[dict],
                    today: _date) -> tuple[list[dict], list[dict]]:
    """Pure rule (unit-tested): which candidates are NEWS vs repeats of a
    card shipped in the prior 7 days. A repeat returns as a card only when
      (a) its fingerprint key has a token never shipped before (new date,
          new event day), or
      (b) its metric moved >= 10% vs the LAST sighting (worse OR better), or
      (c) it entered the last 30 days before arrival since last seen.
    Prior insights without a fingerprint (pre-2026-09-24 payloads) fall back
    to the old stake rule; a stake-less legacy repeat is a repeat.
    No floor, no scheduled 'still open' resurfacing — the watchlist is the
    memory; cards are news."""
    sightings: dict[str, list[dict]] = {}
    carried: dict[str, str] = {}      # card id -> earliest ISO first sighting carried forward
    for row in sorted(prior_rows, key=lambda x: str(x.get("report_date", ""))):
        rdate = str(row.get("report_date", ""))
        for ins in (row.get("ai_insights") or {}).get("insights", []) or []:
            cid = ins.get("id")
            if not cid:
                continue
            stake = (float(ins.get("_stake_eur") or 0)
                     or _parse_eur((ins.get("at_stake") or {}).get("value", "")))
            sightings.setdefault(cid, []).append(
                {"date": rdate, "stake": stake, "novelty": ins.get("_novelty")})
            if ins.get("_first_seen"):
                carried[cid] = min(carried.get(cid, ins["_first_seen"]), ins["_first_seen"])
        # open items carry the first sighting too, so a card demoted for days
        # keeps its true start date instead of the 7-day window's edge
        for oi in (row.get("ai_insights") or {}).get("open_items", []) or []:
            cid, fs = oi.get("id"), oi.get("first_flagged_iso")
            if cid and fs:
                carried[cid] = min(carried.get(cid, fs), fs)

    def _fmt_day(iso: str) -> str:
        try:
            return _date.fromisoformat(iso).strftime("%a %d %b").replace(" 0", " ")
        except ValueError:
            return iso

    kept, demoted = [], []
    for c in candidates:
        cid  = c["insight"]["id"]
        seen = sightings.get(cid)
        if not seen:
            c["first_seen_iso"] = str(today - timedelta(days=1))
            kept.append(c)
            continue
        first, last = seen[0], seen[-1]
        nov  = c.get("novelty") or {}
        facts = c["insight"]["facts"]
        first_iso = min(first["date"], carried.get(cid, first["date"]))
        c["first_seen_iso"] = first_iso
        facts["first_flagged"] = _fmt_day(first_iso)

        # follow-up wording vs the FIRST sighting
        f_metric = (first.get("novelty") or {}).get("metric")
        ref_first = f_metric if f_metric else (first["stake"] or None)
        cur_ref   = nov.get("metric") if f_metric else (c.get("stake_eur") or None)
        if ref_first and cur_ref:
            move = (abs(cur_ref) - abs(ref_first)) / abs(ref_first) * 100
            if abs(move) >= 15:
                facts["since_first_flagged"] = (
                    f"the underlying gap is ~{abs(move):.0f}% "
                    f"{'wider' if move > 0 else 'narrower'} than when first flagged")

        # (c) entered the near-term window since last seen
        l_nov = last.get("novelty") or {}
        if ("days_out" in l_nov and nov.get("days_out", 99) <= 30
                and int(l_nov.get("days_out") or 99) > 30):
            facts["status"] = "now inside the last 30 days before arrival"
            kept.append(c)
            continue

        is_news = False
        if nov.get("key"):
            # (a) any token never shipped before
            prior_tokens: set[str] = set()
            for sgt in seen:
                k = (sgt.get("novelty") or {}).get("key") or ""
                prior_tokens.update(t for t in k.split("|") if t)
            is_news = any(t not in prior_tokens for t in nov["key"].split("|") if t) \
                if prior_tokens else False
        elif nov.get("metric") is not None and l_nov.get("metric") is not None:
            # (b) moved >= 10% vs the last sighting
            old, new = float(l_nov["metric"]), float(nov["metric"])
            is_news = abs(new - old) / max(abs(old), 1.0) >= 0.10
        elif nov.get("metric") is not None and l_nov.get("metric") is None:
            # legacy prior payload: old stake rule (worsened >= 10%)
            old_stake = max(sgt["stake"] for sgt in seen)
            new_stake = float(c.get("stake_eur") or 0)
            is_news = bool(old_stake > 0 and new_stake >= old_stake * 1.10)
        else:
            is_news = False
        (kept if is_news else demoted).append(c)
    if demoted:
        print(f"[analyst] Novelty gate: {len(demoted)} repeat card(s) → watchlist: "
              f"{[d['insight']['id'] for d in demoted]}")
    return kept, demoted


def _novelty_gate(candidates: list[dict], hotel_id: str,
                  today: _date | None = None) -> tuple[list[dict], list[dict]]:
    """Reads the prior 7 days of shipped cards (PG-first, Supabase fallback)
    and applies `_novelty_decide`. Fails open on any error (all candidates
    kept). Compares against PRIOR report dates only, never the current one —
    a same-day manual refresh must not suppress its own cards (2026-07-23).
    `today` is the analyst's report-date anchor (server date if omitted)."""
    import os
    import requests as _req
    today = today or _date.today()
    current_report = str(today - timedelta(days=1))
    since = str(today - timedelta(days=8))   # 7-day memory window
    rows: list[dict] = []
    try:
        from db import store as _store
        if _store.read_from_pg():
            rows = _store.get_briefings_since(hotel_id, since, current_report,
                                              ["ai_insights", "report_date"]) or []
    except Exception as exc:
        print(f"[analyst] Novelty gate: PG read failed, trying Supabase: {exc}")
        rows = []
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not rows and not (supabase_url and supabase_key):
        return candidates, []
    try:
        if not rows:
            r = _req.get(
                f"{supabase_url}/rest/v1/briefings",
                params={"hotel_id": f"eq.{hotel_id}",
                        "and": f"(report_date.gte.{since},report_date.lt.{current_report})",
                        "select": "ai_insights,report_date"},
                headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
                timeout=10,
            )
            r.raise_for_status()
            rows = r.json()
        return _novelty_decide(candidates, rows, today)
    except Exception as exc:
        print(f"[analyst] Novelty gate skipped (lookup failed): {exc}")
        return candidates, []

# Derived/estimated euro figures (at-stake, projections) never reach the
# narration prompt or the display — only real PMS revenue does. The values
# still drive scoring internally. (User rule 2026-08-23.)
_DERIVED_EUR_FACTS = ("value_at_stake", "proj_rev_band")

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
8. at_stake: always omit — value-at-stake estimates are not narrated
   the field entirely - never invent a value.
9. AUDIENCE: a hotel owner, not a revenue-management expert. Short
   sentences. Everyday words. Never use industry jargon when a simpler
   phrase means the same thing. Translate, do not echo, the technical
   keys in the facts JSON. Required rewordings:
     "demand is firming"        -> "demand is getting stronger"
     "pace is decelerating"     -> "bookings are slowing down"
     "demand deterioration"     -> "demand is getting weaker"
     "compression dates"        -> "dates filling up fast"
     "ADR dilution"             -> "the average rate is falling"
     "pickup velocity weakened" -> "new bookings have slowed"
     "the advantage is eroding" -> "the gap is getting smaller"
     "demand shifted earlier"   -> "guests booked earlier than last year"
     "soft-date cluster"        -> "several dates have low bookings"
     "underlying demand trend"  -> "recent booking trend"
     "pickup"                   -> "new bookings"
     "pace" / "pacing"          -> "bookings" / "running" (vs last year)
     "OTB" / "on the books"     -> "booked so far"
     "lead time"                -> "how far ahead guests book"
     "close-in"                 -> "last-minute"
     "inventory"                -> "rooms"
     "rate codes" / "rate floors" -> "rate plans" / "minimum rates"
     "materialise"              -> "come through"
     "channel shift"            -> "bookings coming from different sources"
   Everyday hotel words are fine: occupancy, room nights, average rate,
   revenue, cancellations, same time last year.
10. Language: write in English.
11. recommended_action names something to REVIEW, CHECK, INVESTIGATE or
    MONITOR — never a commercial move. Never suggest raising, lowering,
    holding or cutting rates or prices, and never suggest an offer,
    promotion, package, discount or restriction — not even softly
    ("consider a targeted offer", "there may be room for a higher rate").
    There is no market or competitor rate data here; pricing decisions
    belong to management. Never open with an imperative verb (change,
    remove, increase, decrease, cut, raise, close, open, act). Good
    openers: "It may be worth reviewing...", "Worth checking...",
    "Worth monitoring...". Vary openers across cards.
12. LENGTH CAPS ARE HARD LIMITS — outputs over cap are REJECTED. Before
    submitting, count the words in every field. Target ~80% of each cap:
    headline ~9 words (max 12); what_happened ~22 (max 28); why_it_matters
    ~33 (max 42); recommended_action ~25 (max 32); by_when ~6 (max 10).
    If a thought does not fit, drop detail — never squeeze more words in.
13. Occupancy/revenue projections: only use the low-high band provided
    ("around 91-95%"). Never present a single-point projection or the
    words "will finish at".
14. Every fact carries a "period" label. Attach every number only to the
    period named in its fact. Never blend full-month and remaining-period
    numbers in one sentence.
15. Booking SOURCE names (e.g. "BK", "TEL", "T.Os", "Mice") are the hotel's
    own codes: copy them exactly as given. Never expand, guess or rename a
    code (never turn "BK" into "Booking.com")."""

_CARD_TOOL: dict[str, Any] = {
    "name": "submit_card",
    "description": "Submit the narrated insight card.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id":       {"type": "string"},
            "tag":      {"type": "string", "enum": ["ALERT", "OPPORTUNITY", "MONITOR"]},
            "headline": {"type": "string", "description": "HARD LIMIT 12 words — write 9 or fewer. Over-limit submissions are discarded."},
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
            "what_happened":      {"type": "string", "description": "HARD LIMIT 28 words — write 22 or fewer. Over-limit submissions are discarded."},
            "why_it_matters":     {"type": "string", "description": "HARD LIMIT 42 words — write 33 or fewer. Over-limit submissions are discarded."},
            "recommended_action": {"type": "string", "description": "HARD LIMIT 32 words — write 25 or fewer. Soft opener, never an imperative verb first. Over-limit submissions are discarded."},
            "by_when":            {"type": "string", "description": "HARD LIMIT 10 words — write 8 or fewer."},
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

_HERO_TOOL: dict[str, Any] = {
    "name": "submit_hero",
    "description": "Submit the morning hero paragraph shown at the top of the briefing.",
    "input_schema": {
        "type": "object",
        "properties": {"hero": {"type": "string", "description": "4-6 short sentences starting exactly with 'Good morning.' HARD LIMIT 125 words — write 100 or fewer. Over-limit submissions are discarded."}},
        "required": ["hero"],
    },
}

_NUM_TOKEN = re.compile(r"\d(?:[\d,\.]*\d)?")

_BANNED_IMPERATIVES = {"change", "remove", "increase", "decrease", "cut",
                       "raise", "close", "open", "act", "lower", "lift"}

# ── Action-language contract (user rule 2026-08-10, enforced 2026-10-28) ─────
# Cards suggest only what to review / check / investigate / monitor — never a
# commercial move, not even softly. The first-word imperative check above
# missed "Consider a targeted offer" and "There may be room for a higher
# rate", so the whole sentence is checked. Two strengths:
#   _COMMERCIAL_ANY    — unambiguous advice, checked in every prose field and
#                        the hero ("raising rates", "room for a higher rate")
#   _COMMERCIAL_ACTION — also checked in recommended_action / by_when only,
#                        where "a lower rate" or "a targeted offer" can only be
#                        advice (in why_it_matters "a lower average rate than
#                        last year" is a fact and must stay allowed)
_PRICING_OBJ = (r"(?:rates?|prices?|pricing|discounts?|offers?|promotions?|promos?|"
                r"packages?|restrictions?|rate\s+plans?|availability|minimum\s+stays?)")
_COMMERCIAL_ANY = re.compile(
    r"\b(?:"
    r"(?:rais|lower|increas|decreas|cutt|dropp|hold|open|clos|withdraw|restrict|remov|"
    r"discount|push|protect)ing\s+(?:\w+\s+){0,2}?" + _PRICING_OBJ +
    r"|room\s+for\s+(?:a\s+)?(?:higher|lower|firmer|softer)"
    r"|(?:targeted|last-minute|special|early-bird)\s+(?:offer|promotion|promo|deal|discount)s?"
    r"|priced\s+too\s+(?:low|high)"
    r"|consider(?:ing)?\s+(?:a|an)\s+(?:\w+\s+)?(?:offer|promotion|promo|package|discount|deal)"
    # 2026-10-28, from the real October FL Pulse export: phrasings the first
    # version let through ("pulling discounted rate plans", "keeping discounted
    # rate plans active gives away revenue", "discounts may be unnecessary")
    r"|(?:pull(?:ing)?|switch(?:ing)?\s+off|turn(?:ing)?\s+off|scal(?:e|ing)\s+back|tighten(?:ing)?|"
    r"trim(?:ming)?|withdraw(?:ing)?|remov(?:e|ing)|clos(?:e|ing))\s+(?:back\s+)?(?:any\s+|the\s+|all\s+)?"
    r"(?:\w+\s+){0,2}?(?:discount\w*|offers?|promotions?|promos?|packages?|rate\s+plans?)"
    r"|keep(?:ing)?\s+(?:\w+\s+){0,3}?(?:discount\w*|offers?|promotions?|rate\s+plans?)\s+"
    r"(?:active|open|live|running|on)"
    r"|giv(?:e|es|ing|en)\s+(?:money|revenue)\s+away|giv(?:e|es|ing)\s+away\s+(?:money|revenue)"
    r"|money\s+on\s+the\s+table|revenue\s+you\s+don.t\s+need"
    r"|discount\w*\s+(?:\w+\s+){0,3}?(?:(?:may|might)\s+be|are|is|seem)\s+(?:no\s+longer\s+)?"
    r"(?:unnecessary|needed|necessary)"
    r"|(?:little|no)\s+reason\s+to\s+(?:offer|keep|open|close|discount)"
    r"|offer(?:ing)?\s+discount\w*"
    r")\b", re.IGNORECASE)
_COMMERCIAL_ACTION = re.compile(
    r"\b(?:"
    r"(?:raise|lower|increase|decrease|cut|drop|hold|close|withdraw|restrict|remove|push|protect)"
    r"\s+(?:the\s+|your\s+|its\s+|their\s+|any\s+)?(?:\w+\s+){0,2}?" + _PRICING_OBJ +
    r"|(?:higher|lower|firmer|softer)\s+(?:rates?|prices?|pricing)"
    r"|(?:a|an)\s+(?:\w+\s+){0,2}?(?:offer|promotion|promo|package|discount|deal)"
    r"|(?:stronger|best\s+available|full)\s+(?:rates?|prices?)"
    r")\b", re.IGNORECASE)
_ACTION_FIELDS = ("recommended_action", "by_when")


def _action_violations(card: dict) -> list[str]:
    """Commercial-move advice anywhere in a card (English only)."""
    v = []
    for field in ("headline", "what_happened", "why_it_matters", "recommended_action", "by_when"):
        text = str(card.get(field) or "")
        m = _COMMERCIAL_ANY.search(text) or (
            _COMMERCIAL_ACTION.search(text) if field in _ACTION_FIELDS else None)
        if m:
            v.append(f"{field} suggests a commercial move ('{m.group(0)}') — "
                     f"only review / check / investigate / monitor")
    return v


# ── Card shape (review 2026-10-28) ───────────────────────────────────────────
# The tool schema asks for every field, but a response that skips one used to
# pass every validator and then crash the legacy mapping — wiping ALL cards
# and the hero for the hotel that day. Now it fails validation for that card
# only, which ships its deterministic fallback.
_TEXT_FIELDS = ("headline", "what_happened", "why_it_matters", "recommended_action", "by_when")


def _shape_problems(card: Any) -> list[str]:
    if not isinstance(card, dict):
        return ["card is not an object"]
    v = [f"missing or empty field: {f}" for f in _TEXT_FIELDS
         if not isinstance(card.get(f), str) or not card[f].strip()]
    ev = card.get("evidence")
    if not isinstance(ev, list) or len(ev) != 2:
        v.append("evidence must be exactly 2 items")
    else:
        for i, e in enumerate(ev):
            if (not isinstance(e, dict)
                    or not all(isinstance(e.get(k), str) and e[k].strip() for k in ("label", "value"))
                    or not isinstance(e.get("sub", ""), str)):
                v.append(f"evidence[{i}] needs label / value / sub text")
    return v


# ── Number watch (review 2026-10-28, WATCH-ONLY) ─────────────────────────────
# _bad_numbers only checks that the digits occur SOMEWHERE in the input, so
# "−20%" → "+20%" and "80%" → "€80" pass. This stricter check compares whole
# numbers with their sign, € and unit against the input. It never rejects a
# card yet: findings go to cards_audit["number_watch"] so a week of real runs
# shows its false-positive rate before it is switched on (ENGINEERING_LOG).
_NUM_EXPR = re.compile(
    r"(?:(?<![\w.,])(?P<sign>[+\-−]))?(?P<cur>€)?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?P<unit>%|\s?pts\b)?")


def _num_kind(m: re.Match) -> str:
    if m.group("cur"):
        return "€"
    unit = (m.group("unit") or "").strip()
    return unit or "plain"


def _number_watch(texts: list[str], haystack: str) -> list[str]:
    allowed: dict[str, set[tuple[str, str]]] = {}
    for m in _NUM_EXPR.finditer(haystack):
        sign = (m.group("sign") or "").replace("-", "−")
        allowed.setdefault(m.group("num"), set()).add((sign, _num_kind(m)))
    issues: list[str] = []
    for text in texts:
        for m in _NUM_EXPR.finditer(str(text or "")):
            num, kind = m.group("num"), _num_kind(m)
            sign = (m.group("sign") or "").replace("-", "−")
            variants = allowed.get(num)
            shown = m.group(0).strip()
            if not variants:
                issues.append(f"'{shown}' is not a number in the input")
                continue
            if kind != "plain" and kind not in {k for _, k in variants}:
                issues.append(f"'{shown}' — the input has {num} only as "
                              f"{', '.join(sorted(k for _, k in variants))}")
                continue
            if sign and sign not in {s for s, k in variants if kind == "plain" or k == kind}:
                issues.append(f"'{shown}' — sign differs from the input")
    return sorted(set(issues))


# ── Source-code fidelity (rule 15, enforced 2026-10-28) ──────────────────────
# Claude once expanded the hotel's code "BK" into "Booking.com". Number checks
# cannot see that, so any well-known channel brand in the output that is NOT
# in the input is a rejection (the hotel's own codes are copied verbatim).
_CHANNEL_BRANDS = re.compile(
    r"(?<![\w.])(?:booking\.com|expedia|airbnb|agoda|hotels\.com|trip\.com|hrs|"
    r"hotelbeds|jet2(?:holidays)?|tui|lastminute(?:\.com)?|kayak|trivago)(?![\w])",
    re.IGNORECASE)


def _source_violations(texts: list[str], haystack: str) -> list[str]:
    seen = {m.group(0).lower() for m in _CHANNEL_BRANDS.finditer(haystack)}
    out = {m.group(0) for t in texts for m in _CHANNEL_BRANDS.finditer(str(t or ""))
           if m.group(0).lower() not in seen}
    return [f"source name '{b}' is not in the input — copy source codes exactly as given"
            for b in sorted(out)]


def _card_texts(card: dict) -> list[str]:
    texts = [str(card.get(f) or "") for f in _TEXT_FIELDS]
    for e in card.get("evidence") or []:
        if isinstance(e, dict):
            texts += [str(e.get(k) or "") for k in ("label", "value", "sub")]
    return texts

# ── Word-limit contract (canonical — see ENGINEERING_LOG §3) ─────────────────
# Every text the briefing ships obeys these caps, on EVERY path:
#   1. tool-schema descriptions steer the single Claude attempt to ~80% of cap
#   2. validators reject over-cap narration → deterministic fallback ships
#   3. fallback templates are test-enforced within caps (test_leadtime etc.)
#   4. _enforce_caps() is the last-resort runtime clamp — nothing ever ships over
# Caps relaxed 2026-09-05 (+~20% for plain-language vocabulary) and again
# 2026-09-11 (user "go"): Pome/Potidea were degrading near-DAILY on 1-5
# word overshoots (observed misses: what_happened 29-32 vs 28, action 33
# vs 32 — refresh_runs cards_audit). Sized to cover the observed max +1.
# Policy 2026-07-27 unchanged: relax caps rather than pay for retries.
_WORD_CAPS = {"headline": 12, "what_happened": 33, "why_it_matters": 46,
              "recommended_action": 36, "by_when": 10}
_HERO_WORD_CAP = 132


def _clamp_words(text: str, cap: int) -> str:
    words = str(text).split()
    if len(words) <= cap:
        return text
    return " ".join(words[:cap]).rstrip(",;:.") + "…"


# ── Plain-language contract (2026-08-24) ─────────────────────────────────────
# The reader is a hotel owner, not a revenue manager. The prompts ask Claude for
# everyday words; this deterministic pass catches what slips through and also
# cleans the templated fallbacks. Ordered: longer phrases first. English only.
_PLAIN_TERMS: list[tuple[re.Pattern, str]] = [(re.compile(p, re.IGNORECASE), r) for p, r in [
    (r"demand is firming",             "demand is getting stronger"),
    (r"\bfirming\b",                   "getting stronger"),
    (r"pace is decelerating",          "bookings are slowing down"),
    (r"\bdecelerating\b",              "slowing down"),
    (r"demand deterioration",          "weaker demand"),
    (r"\bdeteriorating\b",             "getting weaker"),
    (r"close-in compression",          "dates filling up fast"),
    (r"compression dates",             "dates filling up fast"),
    (r"\bcompression\b",               "high demand"),
    (r"\b(?:ADR|rate) dilution\b",     "falling average rate"),
    (r"pickup velocity weakened",      "new bookings have slowed"),
    (r"pickup velocity",               "the speed of new bookings"),
    (r"\bnet pick-?up\b",              "net new bookings"),
    (r"\bpick-?up\b",                  "new bookings"),
    (r"advantage is eroding",          "gap is getting smaller"),
    (r"\beroding\b",                   "shrinking"),
    (r"demand shifted earlier",        "guests booked earlier than last year"),
    (r"soft-date cluster",             "several dates with low bookings"),
    (r"\bsoft dates\b",                "low-booking dates"),
    (r"\bsoft nights\b",               "low-booking nights"),
    (r"underlying demand trend",       "recent booking trend"),
    (r"\bon[ -]the[ -]books\b",        "booked so far"),
    (r"\bOTB\b",                       "booked so far"),
    (r"\bSTLY\b",                      "same time last year"),
    (r"\bADR\b",                       "average rate"),
    (r"\bRevPAR\b",                    "revenue per available room"),
    (r"\bmateriali[sz]ed?\b",          "come through"),
    (r"\bremaining inventory\b",       "remaining rooms"),
    (r"\binventory\b",                 "rooms"),
    (r"\bclose-in\b",                  "last-minute"),
    (r"\brate floors?\b",              "minimum rates"),
    (r"\brate codes\b",                "rate plans"),
    (r"\brate code\b",                 "rate plan"),
    (r"\bfirmer (rates?|pricing|prices?)\b", r"higher \1"),
    (r"\bdemand softness\b",           "weaker demand"),
    (r"\bsoftening\b",                 "getting weaker"),
    (r"\bsofter (rates?|pricing|prices?)\b", r"lower \1"),
    (r"\bsofter occupancy\b",          "lower occupancy"),
    (r"\bchannel shift\b",             "a change in where bookings come from"),
    (r"\bbooking window\b",            "how far ahead guests book"),
    (r"\bpace gap\b",                  "booking gap"),
    (r"\bbooking pace\b",              "bookings"),
    (r"\bpacing\b",                    "running"),
    (r"the position could support",    "there may be room for"),
]]
_PLAIN_FIELDS = ("headline", "what_happened", "why_it_matters",
                 "recommended_action", "by_when")


def _plainify_text(text: str) -> tuple[str, list[str]]:
    """Replace jargon phrases with everyday wording. Keeps the capital letter
    when the match opened a sentence. Never touches digits. Returns
    (new_text, [jargon phrases replaced])."""
    if not text:
        return text, []
    hits: list[str] = []
    def _sub(m: re.Match, repl: str) -> str:
        out = m.expand(repl)
        hits.append(m.group(0))
        # keep a capital only where the match opened a sentence — never for
        # mid-sentence acronyms (STLY/OTB/ADR), which become lowercase words
        before = m.string[:m.start()].rstrip()
        at_start = not before or before[-1] in ".!?"
        return out[0].upper() + out[1:] if at_start else out[0].lower() + out[1:]
    for pat, repl in _PLAIN_TERMS:
        text = pat.sub(lambda m, r=repl: _sub(m, r), text)
    return text, hits


def _plainify_card(card: dict) -> tuple[dict, list[str]]:
    """Apply the plain-language pass to every prose field of a card."""
    hits: list[str] = []
    for f in _PLAIN_FIELDS:
        if card.get(f):
            card[f], h = _plainify_text(str(card[f]))
            hits += h
    return card, hits


def _enforce_caps(card: dict) -> dict:
    """Last-resort guarantee: no card field ever ships over its cap."""
    for field, cap in _WORD_CAPS.items():
        text = str(card.get(field, ""))
        if len(text.split()) > cap:
            print(f"[analyst] CAP CLAMP: '{card.get('id', '?')}' {field} "
                  f"{len(text.split())}>{cap} words — trimmed (template needs fixing).")
            card[field] = _clamp_words(text, cap)
    return card


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
        v.append(f"recommended_action starts with imperative '{first}' — use a soft opener (It may be worth reviewing…, Worth checking…)")
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


_CAP_VIOLATION_RE = re.compile(r"^(\w+) is (\d+) words \(max (\d+)\)$")


def _retry_feedback(card: dict, bad_nums: list[str], style: list[str],
                    period: list[str]) -> str:
    """Surgical retry instructions: quote the model's own offending text and
    say exactly what to change. Unlisted fields must be resubmitted verbatim."""
    fixes: list[str] = []
    for p in style:
        m = _CAP_VIOLATION_RE.match(p)
        if m:
            field, n, cap = m.group(1), m.group(2), int(m.group(3))
            fixes.append(
                f'"{field}" is {n} words — the cap is {cap}. You wrote:\n'
                f'      "{card.get(field, "")}"\n'
                f'    Rewrite exactly this text in at most {cap} words '
                f'(target {max(cap - 2, 3)}). Cut adjectives and merge clauses; '
                f'keep every number character-for-character.')
        elif "imperative" in p:
            fixes.append(
                f'"recommended_action" opens with a banned imperative. You wrote:\n'
                f'      "{card.get("recommended_action", "")}"\n'
                f'    Rephrase the same content with a soft opener '
                f'("It may be worth reviewing…", "Worth checking…").')
        else:
            fixes.append(p)
    if bad_nums:
        fixes.append(
            "these numbers do NOT exist in the input and are forbidden: "
            + ", ".join(bad_nums)
            + ". Remove each one or replace it with a value copied "
              "character-for-character from the input.")
    fixes.extend(period)
    return ("\n\nYOUR PREVIOUS SUBMISSION (above input unchanged) WAS REJECTED. "
            "Fix ONLY the issues below — every field not mentioned was accepted "
            "and must be resubmitted word-for-word unchanged:\n\n"
            + "\n\n".join(f"- {f}" for f in fixes))


def _narrate_card(wrapper: dict, fallback_card: dict, meta: dict | None = None,
                  lang: str = "en") -> dict:
    """One Claude call per card; validated; max 2 retries; then fallback.
    When `meta` is given, appends a per-card audit entry (facts given, attempts,
    validation problems, tokens, latency, fallback flag)."""
    facts    = wrapper["insight"]["facts"]
    # € at-stake estimates are internal (scoring/novelty) — never narrated.
    prompt_wrapper = {**wrapper, "insight": {**wrapper["insight"], "facts": {
        k: v for k, v in facts.items() if not k.startswith(_DERIVED_EUR_FACTS)}}}
    haystack = json.dumps(prompt_wrapper, ensure_ascii=False)
    base_prompt = json.dumps(prompt_wrapper, ensure_ascii=False, indent=2)
    if lang == "el":
        base_prompt += ("\n\nOUTPUT LANGUAGE: GREEK. Write every text field in natural, "
                        "professional Greek for a hotel general manager. Copy every number, "
                        "currency amount and percentage EXACTLY as given. Month names may "
                        "stay as given (Aug 2026).")
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
    for attempt in range(_MAX_NARRATION_ATTEMPTS):
        audit["attempts"] = attempt + 1
        try:
            with _CLAUDE_SEM:
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

        shape = _shape_problems(card)
        if shape:   # incomplete response → this card's fallback, nothing else
            bad_nums, style, period = [], shape, []
        else:
            bad_nums = _bad_numbers(card, haystack)
            style    = _style_violations(card)
            if lang == "en":  # both wording checks are English-only
                style += _action_violations(card)
            else:
                style = [v for v in style if "imperative" not in v]
            style += _source_violations(_card_texts(card), haystack)   # any language
            period   = _period_violations(card, facts)
            watch = _number_watch(_card_texts(card), haystack)
            if watch:   # watch-only: recorded, never rejected (see _NUM_EXPR)
                audit["number_watch"] = watch
                print(f"[analyst] Card '{wrapper['insight']['id']}' number watch: {watch}")
        problems = [f"invented number: {t}" for t in bad_nums] + style + period
        if not problems:
            result_card = _harden_card(card, wrapper)
            if lang == "en":
                result_card, jargon = _plainify_card(result_card)
                if jargon:
                    audit["jargon_replaced"] = jargon
                    print(f"[analyst] Card '{wrapper['insight']['id']}': plain-language pass replaced {jargon}")
                result_card = _enforce_caps(result_card)
            break

        print(f"[analyst] Card '{wrapper['insight']['id']}' attempt {attempt + 1} rejected: {problems}")
        audit["validation_problems"].extend(str(p) for p in problems)
        prompt = (base_prompt
                  + "\n\nYour previous submission was:\n"
                  + json.dumps(card, ensure_ascii=False, indent=2)
                  + _retry_feedback(card, bad_nums, style, period))

    if result_card is None:
        print(f"[analyst] Card '{wrapper['insight']['id']}': validation failed — using free templated fallback (no retry, cost policy).")
        audit["fallback_used"] = True
        result_card = _deterministic_card(fallback_card, lang)

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


def _display_hhmm() -> str:
    """HH:MM in the hotel display timezone (Athens; Phase 4 makes it per-hotel)."""
    from datetime import datetime as _dtz
    try:
        from zoneinfo import ZoneInfo
        return _dtz.now(ZoneInfo(_os.getenv("DISPLAY_TZ", "Europe/Athens"))).strftime("%H:%M")
    except Exception:
        return _dtz.now().strftime("%H:%M")


def _driver_hint(vol_pct: float | None, adr_pct: float | None) -> str:
    """What drove a revenue variance: volume (room nights), rate (ADR), or both."""
    if vol_pct is None or adr_pct is None:
        return ""
    v, a = vol_pct, adr_pct
    if abs(v) < 1.5 and abs(a) < 1.5:
        return "rooms sold and rates both about the same"
    if v >= 0 and a >= 0:
        if abs(a) >= 2 * max(abs(v), 0.1): return "mostly from higher rates"
        if abs(v) >= 2 * max(abs(a), 0.1): return "mostly from more rooms sold"
        return "more rooms sold and higher rates"
    if v <= 0 and a <= 0:
        if abs(a) >= 2 * max(abs(v), 0.1): return "mainly lower rates"
        if abs(v) >= 2 * max(abs(a), 0.1): return "mainly fewer rooms sold"
        return "fewer rooms sold and lower rates"
    return "higher rates, fewer rooms sold" if a > 0 else "more rooms sold, lower rates"


def _closed_season_slots(data: dict) -> dict | None:
    """Seasonal-closure detection for the hero. When the hotel had zero stays
    yesterday AND zero MTD but next year has bookings on the books (Q16), the
    hero pivots to next-year OTB vs the same booking stage a year ago.
    Returns the formatted slot block, or None when the hotel is operating."""
    yd, mtd = data.get("yesterday", {}) or {}, data.get("mtd", {}) or {}
    if float(yd.get("revenue") or 0) or float(yd.get("roomNights") or 0):
        return None
    if float(mtd.get("roomNights") or 0):
        return None
    pnx = [p for p in (data.get("pace_next_year") or []) if isinstance(p, dict)]
    rn = sum(float(p.get("rn") or 0) for p in pnx)
    if rn <= 0:
        return None
    rn_stly  = sum(float(p.get("rn_stly") or 0) for p in pnx)
    rev      = sum(float(p.get("rev") or 0) for p in pnx)
    rev_stly = sum(float(p.get("rev_stly") or 0) for p in pnx)
    ty = _anchor_today(data).year
    ny = ty + 1
    top = sorted(pnx, key=lambda p: float(p.get("rn") or 0), reverse=True)[:2]
    slot = {
        "status": (f"closed for the season — comparing {ny} on the books "
                   f"against {ty} at the same booking stage last year"),
        "rn_otb":      f"{int(rn):,} room nights",
        "revenue_otb": _eur(rev),
        "top_months":  [f"{p.get('month')}: {int(float(p.get('rn') or 0)):,} rn"
                        for p in top if float(p.get("rn") or 0) > 0],
    }
    if rn_stly:
        slot["rn_vs_stly"] = _pct_signed((rn - rn_stly) / rn_stly * 100)
    if rev_stly:
        slot["rev_vs_stly"] = _pct_signed((rev - rev_stly) / rev_stly * 100)
    return slot


def _build_hero_slots(data: dict) -> dict:
    """Deterministic facts for the hero paragraph. Every number the narration
    may use must appear here verbatim (numeric validator input)."""
    closed = _closed_season_slots(data)
    if closed:
        return {"closed_season": closed}
    yd, mtd = data.get("yesterday", {}) or {}, data.get("mtd", {}) or {}

    def _vs(ty: float, ly: float) -> str | None:
        return _pct_signed((ty - ly) / ly * 100) if ly else None

    def _vpct(ty: float, ly: float) -> float | None:
        return (ty - ly) / ly * 100 if ly else None

    def _block(d: dict) -> dict:
        rev, rev_ly = float(d.get("revenue", 0)), float(d.get("revenueLY", 0))
        rn,  rn_ly  = float(d.get("roomNights", 0)), float(d.get("roomNightsLY", 0))
        adr, adr_ly = float(d.get("adr", 0)), float(d.get("adrLY", 0))
        b = {
            "revenue":   _eur(rev),
            "vs_ly":     _vs(rev, rev_ly),
            "occ":       _pct(float(d.get("occupancy", 0)) * 100, 0),
            "occ_ly":    _pct(float(d.get("occupancyLY", 0)) * 100, 0),
            "adr":       _eur(adr),
            "adr_ly":    _eur(adr_ly),
            "adr_vs_ly": _vs(adr, adr_ly),
            "rn_vs_ly":  _vs(rn, rn_ly),
            "driver":    _driver_hint(_vpct(rn, rn_ly), _vpct(adr, adr_ly)),
        }
        return {k: v for k, v in b.items() if v not in (None, "")}

    slots = {"yesterday": _block(yd)}
    m = _block(mtd)
    if mtd.get("month_name"):
        m["month"] = f"{mtd['month_name']} MTD"
    slots["mtd"] = m
    return slots


def _hero_violations(text: str, greeting: str = "Good morning") -> list[str]:
    v = []
    words = len(text.split())
    if words > _HERO_WORD_CAP:
        v.append(f"hero is {words} words (max {_HERO_WORD_CAP})")
    if not text.strip().startswith(greeting):
        v.append(f"hero must start with '{greeting}.'")
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        first = sentence.strip().split(" ")[0].lower().strip(",.:;")
        if first in _BANNED_IMPERATIVES:
            v.append(f"sentence starts with imperative '{first}' — use a soft opener")
    if "!" in text:
        v.append("no exclamation marks")
    if greeting == "Good morning":   # action-language check is English-only
        m = _COMMERCIAL_ANY.search(text)
        if m:
            v.append(f"hero suggests a commercial move ('{m.group(0)}') — "
                     f"only what is worth reviewing or watching")
    return v


def _hero_fallback(slots: dict, cards: list[dict]) -> str:
    """Deterministic hero — always available, numbers straight from slots."""
    cs = slots.get("closed_season")
    if cs:
        parts = ["Good morning.", "The hotel is closed for the season."]
        s = f"Next year already has {cs['rn_otb']} booked so far at {cs['revenue_otb']}"
        if cs.get("rn_vs_stly"):
            s += f", {cs['rn_vs_stly']} vs the same time last year"
        parts.append(s + ".")
        if cs.get("top_months"):
            parts.append("Strongest months so far — " + "; ".join(cs["top_months"]) + ".")
        for c in cards[:2]:
            parts.append(c["headline"].rstrip(".") + ".")
        return " ".join(parts)
    y, m = slots.get("yesterday", {}), slots.get("mtd", {})
    parts = ["Good morning."]
    if y.get("revenue"):
        s = f"Yesterday finished at {y['revenue']}"
        if y.get("vs_ly"):
            s += f", {y['vs_ly']} vs last year"
        if y.get("driver"):
            s += f" ({y['driver']})"
        parts.append(s + ".")
    if m.get("revenue"):
        s = f"{m.get('month', 'Month to date')} stands at {m['revenue']}"
        if m.get("vs_ly"):
            s += f", {m['vs_ly']} vs last year"
        parts.append(s + ".")
    for c in cards[:2]:
        s = c["headline"].rstrip(".")
        parts.append(s + ".")
    parts.extend(_pulse_note_parts(slots))
    return " ".join(parts)


def _pulse_note_parts(slots: dict) -> list[str]:
    """One plain sentence on today's Pulse (quiet days / open items)."""
    pz = slots.get("pulse") or {}
    if not pz:
        return []
    new_n, open_n = str(pz.get("new_items", "0")), str(pz.get("open_items", "0"))
    if new_n == "0":
        s = "Nothing new in the Pulse today"
    else:
        s = f"{new_n} new item{'s' if new_n != '1' else ''} in the Pulse today"
    if open_n != "0":
        s += f"; {open_n} item{'s' if open_n != '1' else ''} still open on the watchlist"
    return [s + "."]


def _narrate_hero(hotel_name: str, slots: dict, cards: list[dict],
                  meta: dict | None = None, lang: str = "en") -> str:
    """The hero paragraph at the top of the app: yesterday → MTD → top signals.
    One Claude call, numeric validator, deterministic fallback."""
    greeting = "Καλημέρα" if lang == "el" else "Good morning"
    fallback = _hero_fallback(slots, cards)
    if lang == "el":
        fallback = greeting + "." + fallback[len("Good morning."):]
    # "status" lets the hero lead with what is NEW (review 2026-10-28); a
    # quiet-day pinned repeat is "still open", never presented as news
    digest = [{"tag": c["tag"], "headline": c["headline"],
               "status": "still open" if c.get("_pinned") else "new"} for c in cards[:3]]
    haystack = (json.dumps(slots, ensure_ascii=False)
                + json.dumps(digest, ensure_ascii=False))
    if "closed_season" in slots:
        order_text = (
            "(1) note calmly that the hotel is closed for the season — zero activity is "
            "normal, not a problem; (2) next year's on-the-books position — room nights "
            "and revenue vs the same booking stage last year; (3) the strongest months "
            "so far; then any forward-looking points from TOP INSIGHTS if present. "
            "Do NOT mention yesterday or month-to-date"
        )
    else:
        order_text = (
            "(1) yesterday's result and what drove it "
            "— occupancy vs rate, per the driver hints; (2) the month-to-date position; "
            "(3) then the most important NEW finding from TOP INSIGHTS (status \"new\") in "
            "one short sentence that says what it reveals, not just which number moved; any "
            "other new insight in one clause; an insight with status \"still open\" at most "
            "as \"still open\", never as news"
        )
    if slots.get("pulse"):
        order_text += (
            "; (4) close with ONE short sentence on today's Pulse using the PULSE "
            "numbers verbatim — how many new items today (0 = 'nothing new') and how "
            "many are still open on the watchlist"
        )
    prompt_base = (
        f"Hotel: {hotel_name}. You write the short morning hero paragraph that opens "
        f"the general manager's daily briefing.\n\n"
        f"PERFORMANCE DATA (use ONLY these numbers, verbatim):\n"
        f"{json.dumps(slots, ensure_ascii=False, indent=2)}\n\n"
        f"TOP INSIGHTS (shown as full cards below the hero — preview them, don't repeat them fully):\n"
        f"{json.dumps(digest, ensure_ascii=False, indent=2)}\n\n"
        f"Write ONE flowing paragraph of 4-6 short sentences, max {_HERO_WORD_CAP} words, starting "
        "exactly with \"" + greeting + ".\" Order: " + order_text + ". "
        "Write for a hotel owner, not a revenue expert: short sentences, everyday "
        "words, no industry jargon (say 'booked so far' not 'on the books', 'new "
        "bookings' not 'pickup', 'bookings vs last year' not 'pace', 'average rate' "
        "not 'ADR', 'last-minute' not 'close-in'). "
        "Soft advisory tone, no imperatives, no "
        "exclamation marks. Never suggest a pricing or offer move (higher or lower "
        "rates, offers, promotions, discounts) — only what is worth reviewing or "
        "watching. Never invent causes (nationalities, room types, events, "
        "weather) — only what the data shows. Every number must appear verbatim in the "
        "data above."
    )
    if lang == "el":
        prompt_base += ("\n\nOUTPUT LANGUAGE: GREEK — natural, professional hotel Greek. "
                        "Copy every number, currency amount and percentage exactly as given.")
    t0 = _time.time()
    problems: list[str] = []
    attempts = 0
    for attempt in range(1, _MAX_NARRATION_ATTEMPTS + 1):
        attempts = attempt
        prompt = prompt_base if attempt == 1 else (
            prompt_base + "\n\nYour previous attempt was REJECTED: "
            + "; ".join(problems) + ". Fix these and resubmit.")
        try:
            with _CLAUDE_SEM:
                response = _get_client().messages.create(
                    model=_MODEL,
                    max_tokens=500,
                    temperature=0.2,
                    tools=[_HERO_TOOL],
                    tool_choice={"type": "tool", "name": "submit_hero"},
                    messages=[{"role": "user", "content": prompt}],
                )
            if meta is not None:
                _usage_add(meta["usage"], response.usage)
            hero = next(b for b in response.content if b.type == "tool_use").input.get("hero", "")
            problems = _hero_violations(hero, greeting) + _source_violations([hero], haystack)
            bad = _bad_numbers({"h": hero}, haystack)
            if bad:
                problems.append(f"numbers not in input data: {', '.join(bad[:5])}")
            if hero and not problems:
                watch = _number_watch([hero], haystack)   # watch-only
                if lang == "en":
                    hero, jargon = _plainify_text(hero)
                    if jargon:
                        print(f"[analyst] Hero: plain-language pass replaced {jargon}")
                    hero = _clamp_words(hero, _HERO_WORD_CAP)
                if meta is not None:
                    meta["cards_audit"].append({
                        "card_id": "hero", "signal": "hero", "tag": "HERO",
                        "attempts": attempts, "fallback_used": False,
                        "latency_ms": int((_time.time() - t0) * 1000),
                        "validation_problems": [],
                        **({"number_watch": watch} if watch else {}),
                    })
                return hero
        except Exception as exc:
            print(f"[analyst] Hero narration error: {exc}")
            break
    if meta is not None:
        meta["cards_audit"].append({
            "card_id": "hero", "signal": "hero", "tag": "HERO",
            "attempts": attempts, "fallback_used": True,
            "latency_ms": int((_time.time() - t0) * 1000),
            "validation_problems": problems,
        })
    if lang == "en":
        fallback, _ = _plainify_text(fallback)
    return _clamp_words(fallback, _HERO_WORD_CAP)


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
        # at-stake stays INTERNAL: a bare number for the novelty gate, no display
        **({"_stake_eur": _parse_eur(card["at_stake"]["value"])}
           if card.get("at_stake") else {}),
        # fingerprint (internal): what tomorrow's novelty gate compares against
        **({"_novelty": card["_novelty"]} if card.get("_novelty") else {}),
        **({"_first_seen": card["_first_seen"]} if card.get("_first_seen") else {}),
        # Layer A metadata (2026-10-28): confidence of the leading explanation
        # and deterministic filters for a future BI drill-down
        **({"confidence": card["_confidence"]} if card.get("_confidence") else {}),
        **({"drilldown": card["_drilldown"]} if card.get("_drilldown") else {}),
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


def _drilldown(cand: dict) -> dict:
    """Deterministic filters describing what a card is about — stay period,
    signal, and the source when a Layer A attribution named one. Metadata
    only: no BI deep-link contract exists yet (ENGINEERING_LOG TO-DO)."""
    ins   = cand.get("insight") or {}
    facts = ins.get("facts") or {}
    sp    = ins.get("stay_period") or {}
    d: dict[str, Any] = {"signal": ins.get("signal"),
                         "stay_from": sp.get("from"), "stay_to": sp.get("to")}
    shift = facts.get("shift_source")
    src = (facts.get("source") or facts.get("biggest_source")
           or (shift.get("value") if isinstance(shift, dict) else None))
    if src:
        d["source"] = src
    return {k: v for k, v in d.items() if v}


def _cards_to_insights(cards: list[dict]) -> list[dict]:
    """Map each card to the payload shape; one card that cannot be mapped is
    dropped (logged) instead of failing the whole briefing."""
    out: list[dict] = []
    for c in cards:
        try:
            out.append(_card_to_insight(c, len(out) + 1))
        except Exception as exc:
            print(f"[analyst] Card '{c.get('id', '?')}' could not be mapped — dropped: {exc}")
    return out


def _deterministic_card(fallback_card: dict, lang: str = "en") -> dict:
    """The templated card, through the same plain-language pass and word-cap
    clamp as narrated cards (AI-off hotels used to skip both)."""
    card = dict(fallback_card)
    if lang == "en":
        card, _ = _plainify_card(card)
    return _enforce_caps(card)


_STUB = {"executive_summary": "", "insights": []}


# ─── Layer B: entry point ────────────────────────────────────────────────────

def generate_insights(data: dict[str, Any], hotel_id: str | None = None,
                      lang: str = "en", narrate: bool = True) -> dict[str, Any]:
    """narrate=False (per-entity AI toggle, 2026-09-11): every card ships its
    deterministic fallback and the hero is skipped — ZERO Claude tokens; the
    briefing still publishes with full data and computed signals.

    Review 2026-10-28: the old prompt-only analyst (_legacy_generate) is no
    longer routed to. It bypassed every validator, ignored narrate=False and
    fired whenever ONE signal field was empty — e.g. a closed resort with no
    stays in the next 90 days. A payload without signal fields now computes
    what it supports (often a quiet, hero-only day). A missing API key means
    deterministic cards, not an empty briefing. One card that fails in any
    way ships its fallback; the others are untouched."""
    if narrate and not config.ANTHROPIC_API_KEY:
        print("[analyst] No ANTHROPIC_API_KEY — deterministic cards only.")
        narrate = False

    try:
        dq = data.get("data_quality") or {}
        if dq.get("legacy_mode"):
            print("[analyst] Some signal fields are empty (legacy_mode) — computing what "
                  "the payload supports (the old prompt analyst is retired).")

        if hotel_id is None:
            import os
            hotel_id = os.getenv("SUPABASE_HOTEL_ID") or None

        computed = _compute_signals(data, hotel_id=hotel_id)
        ranked   = computed["ranked"]
        print(f"[analyst] Compute: {len(ranked)} ranked signals, "
              f"{len(computed['watchlist'])} watchlist")
        if not ranked:
            # Quiet day (2026-09-24): hero only, no cards, no legacy prompt —
            # nothing new is a valid, cheaper briefing.
            print("[analyst] No new signals today — hero-only briefing.")

        hotel_name = data.get("hotel_name", config.HOTEL_NAME)
        briefing_date = _anchor_today(data).isoformat()

        meta: dict[str, Any] = {"cards_audit": [], "usage": _usage_zero(),
                                "model": _MODEL, "prompt_version": _PROMPT_VERSION}
        if computed.get("compute_errors"):
            # visible in refresh_runs.cards_audit (ops-monitoring) and ai_stats
            meta["cards_audit"].append({
                "card_id": "compute_errors", "signal": "compute", "tag": "SYSTEM",
                "attempts": 0, "fallback_used": False,
                "validation_problems": computed["compute_errors"],
            })
        if not _DISCOVERY_CARDS:
            # Release 2 dark launch: what the engine found, for reconciliation
            # against Hotel Bi — never shown to the owner
            for dc in computed.get("discoveries") or []:
                meta["cards_audit"].append({
                    "card_id": f"discovery:{dc['insight']['id']}", "signal": "discovery",
                    "tag": "WATCH", "attempts": 0, "fallback_used": False,
                    "validation_problems": [], "score": dc["score"],
                    "headline": dc["fallback_card"]["headline"],
                    "why": dc["fallback_card"]["why_it_matters"],
                    "facts": {k: v for k, v in dc["insight"]["facts"].items()
                              if not k.startswith("value_at_stake")},
                    "confidence": dc["insight"]["cause_hypotheses"][0]["confidence"],
                })
        cards: list[dict] = []
        for cand in ranked[:5]:
            if not narrate:
                card = _deterministic_card(cand["fallback_card"], lang)
                meta["cards_audit"].append({
                    "card_id": card.get("id") or f"card_{len(cards) + 1}",
                    "fallback_used": True, "attempts": 0,
                    "validation_problems": ["ai_disabled_for_entity"],
                })
            else:
                wrapper = {
                    "property":       hotel_name,
                    "briefing_date":  briefing_date,
                    "capacity_rooms": config.TOTAL_ROOMS,
                    "insight":        cand["insight"],
                }
                try:
                    card = _narrate_card(wrapper, cand["fallback_card"], meta=meta, lang=lang)
                except Exception as exc:
                    print(f"[analyst] Card '{cand['insight']['id']}' narration crashed "
                          f"— shipping its fallback: {exc}")
                    card = _deterministic_card(cand["fallback_card"], lang)
                    meta["cards_audit"].append({
                        "card_id": cand["insight"]["id"], "fallback_used": True, "attempts": 0,
                        "validation_problems": [f"narration_crash: {str(exc)[:200]}"],
                    })
            card["_novelty"] = cand.get("novelty")
            card["_first_seen"] = cand.get("first_seen_iso")
            card["_pinned"] = bool(cand.get("pinned_repeat"))
            # Layer A's confidence + filter metadata for a future drill-down
            # (no BI navigation contract yet — data only, the PWA ignores it)
            hyps = cand["insight"].get("cause_hypotheses") or []
            card["_confidence"] = hyps[0].get("confidence") if hyps else None
            card["_drilldown"] = _drilldown(cand)
            cards.append(card)
            print(f"[analyst] Card {len(cards)}: [{card['tag']}] {card['headline'][:60]}")

        open_items = [{"id": c["insight"]["id"], "tag": c["tag"],
                       "title": c.get("title_hint", ""),
                       "first_flagged": c["insight"]["facts"].get("first_flagged"),
                       "first_flagged_iso": c.get("first_seen_iso")}
                      for c in computed.get("demoted", [])]
        try:
            hero_slots = _build_hero_slots(data)
        except Exception as exc:
            print(f"[analyst] Hero slots failed — hero falls back to card headlines: {exc}")
            hero_slots = {}
        if not cards or open_items:
            fresh = sum(1 for cand in ranked[:5] if not cand.get("pinned_repeat"))
            hero_slots["pulse"] = {"new_items": str(fresh),
                                   "open_items": str(len(open_items))}
        summary = ""
        if narrate:
            try:
                summary = _narrate_hero(hotel_name, hero_slots, cards, meta=meta, lang=lang)
            except Exception as exc:
                print(f"[analyst] Hero failed — briefing ships without it: {exc}")
        if not narrate:
            print(f"[analyst] AI narration OFF for this hotel — "
                  f"{len(cards)} deterministic cards, zero tokens")
        meta["fallback_cards"] = sum(1 for a in meta["cards_audit"] if a["fallback_used"])
        meta["estimated_cost_usd"] = _estimate_cost_usd(meta["usage"])
        from datetime import datetime as _dtnow
        result = {
            "executive_summary": summary,
            # The hero is written ONCE per day; data refreshes intraday. This
            # stamp lets the UI label the narrative with ITS time, so small
            # drifts vs the live KPI cards read as timing, not as errors.
            "generated_at": _display_hhmm(),
            "insights": _cards_to_insights(cards),
            # Open items = repeats held back today; the app's watchlist is the
            # memory, this list lets it label them without re-computing.
            "open_items": open_items,
            "quiet_day": not cards,
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
    """Original prompt-based approach. NOT CALLED since 2026-10-28 (it bypassed
    every validator) — kept only until the post-Xenia cleanup deletes it."""
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
        with _CLAUDE_SEM:
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
        try:
            result = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:   # corrupt cache → cloud fallback
            print(f"[analyst] ai_cache.json unreadable, ignoring it: {exc}")
            result = {}
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
