"""
Cross-filter discovery engine (review 2026-10-28, Release 2 — ships DARK).

Layer A only: pure Python, no Claude, no I/O. It looks INSIDE a movement
that the monthly KPIs show (or hide) and measures which slice of the
business explains it. It returns raw numbers; briefing/analyst.py turns them
into period-scoped facts, a fallback card and a candidate.

Inputs (both optional, contract in db/contract.py):
  booking_cube   Q18 — stay date × source × room category × booking window,
                 TY on the books vs the SAME WEEKDAY last year (−364 days) at
                 the same booking stage. room_cat is None on Opera.
  cancel_detail  Q19 — cancellations by cancel date × stay date × source.

Discoveries:
  hidden_day_type        a month that looks fine (not ≥ 10% behind) while its
                         weekend (Fri/Sat nights) or weekday nights are ≥ 20%
                         behind and the other nights are covering it
  gap_driver             a month ≥ 10% behind: which slice carries the gap
  cancellation_driver    yesterday's cancellations concentrated in one source
                         (and which stay dates)

False-discovery control — the same safety-first rules as the analyst, made
stricter because many slices are searched:
  * a slice "explains" a gap only when it carries >= 60% of it in the gap's
    direction, had >= 15 rn last year and moved >= 10 rn;
  * Poisson z = (ty − ly) / sqrt(ty + ly) must clear max(2, sqrt(2·ln k)),
    k = slices tested — the more we search, the higher the bar (room nights
    are not independent events, so the floors above stay in force too);
  * an extra dimension is added only when the narrower slice keeps >= 85% of
    the wider slice's move (no "strongest-looking combination" hunting);
  * contributions are SIGNED and always add up to the parent gap (a partition);
    a share above 100% means other slices offset — reported, never hidden;
  * financial exposure = room nights × that slice's own last-year rate, used
    for the floor and ranking only (never displayed — no-derived-euro rule).
"""

from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import date, timedelta
from itertools import combinations
from typing import Any, Callable

MIN_LY_RN = 15            # the slice had real volume at this point last year
MIN_MOVE_RN = 10          # and moved by at least this many room nights
MIN_SHARE = 0.60          # ... carrying >= 60% of the parent gap
MIN_OVERWEIGHT = 0.20     # ... and >= 20 pts more of the gap than of last
                          # year's volume: weekdays are ~74% of a month's
                          # nights, so they "carry 74%" of any even gap —
                          # that is size, not an explanation
REFINE_KEEP = 0.85        # an extra dimension must keep >= 85% of the move
MIN_SUBGROUP_PCT = 0.20   # a hidden weakness is itself >= 20% behind
MIN_MONTH_LY_RN = 30      # the month window had real volume last year
MIN_WINDOW_DAYS = 7       # at least a week of stay dates in the 90-day window
MAX_DIMS = 3

WEEKEND_NIGHTS = {4, 5}   # Friday and Saturday nights (date.weekday(), Mon = 0)

_DIMS: dict[str, Callable[[dict], Any]] = {
    "source":      lambda r: r.get("source"),
    "day_type":    lambda r: "weekend" if r["_date"].weekday() in WEEKEND_NIGHTS else "weekday",
    "lead_bucket": lambda r: r.get("lead_bucket"),
    "room_cat":    lambda r: r.get("room_cat"),
}


def z_crit(k: int) -> float:
    """Significance bar that rises with the number of slices tested."""
    return max(2.0, math.sqrt(2.0 * math.log(max(k, 2))))


def poisson_z(ty: float, ly: float) -> float:
    return (ty - ly) / math.sqrt(ty + ly) if ty + ly > 0 else 0.0


class _T:
    """Room nights + revenue, this year vs same weekday last year."""
    __slots__ = ("ty", "ly", "rev_ty", "rev_ly")

    def __init__(self, rows: list[dict]):
        self.ty = sum(r["rn_ty"] for r in rows)
        self.ly = sum(r["rn_ly"] for r in rows)
        self.rev_ty = sum(r["rev_ty"] for r in rows)
        self.rev_ly = sum(r["rev_ly"] for r in rows)

    @property
    def delta(self) -> float:
        return self.ty - self.ly

    @property
    def pct(self) -> float:
        return self.delta / self.ly if self.ly else 0.0


def _prepare(cube: list[dict]) -> list[dict]:
    """Parse and validate rows; a malformed row is skipped, never fatal."""
    out = []
    for r in cube or []:
        try:
            out.append({
                "_date":       date.fromisoformat(str(r["stay_date"])[:10]),
                "source":      str(r.get("source") or "Direct"),
                "room_cat":    None if r.get("room_cat") in (None, "") else str(r["room_cat"]),
                "lead_bucket": r.get("lead_bucket") or None,
                "rn_ty":  float(r.get("rn_ty") or 0), "rev_ty": float(r.get("rev_ty") or 0),
                "rn_ly":  float(r.get("rn_ly") or 0), "rev_ly": float(r.get("rev_ly") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _available_dims(rows: list[dict], exclude: tuple = ()) -> list[str]:
    """Dimensions with at least two values in the data (Opera has no room_cat)."""
    dims = []
    for name, fn in _DIMS.items():
        if name in exclude:
            continue
        values = {fn(r) for r in rows}
        values.discard(None)
        if len(values) >= 2:
            dims.append(name)
    return dims


def _group(rows: list[dict], dims: tuple) -> dict[tuple, list[dict]]:
    g: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = tuple(_DIMS[d](r) for d in dims)
        if None not in key:
            g[key].append(r)
    return g


def _month_windows(rows: list[dict], today: date) -> dict[tuple, dict]:
    """Stay months inside the 90-day cube window, with the exact date span
    compared (a month can be partly outside the window — say so)."""
    horizon = today + timedelta(days=90)
    out: dict[tuple, dict] = {}
    for r in rows:
        d = r["_date"]
        if not today <= d <= horizon:
            continue
        key = (d.year, d.month)
        if key not in out:
            last = calendar.monthrange(d.year, d.month)[1]
            out[key] = {"rows": [], "from": max(date(d.year, d.month, 1), today),
                        "to": min(date(d.year, d.month, last), horizon)}
        out[key]["rows"].append(r)
    return {k: v for k, v in out.items()
            if (v["to"] - v["from"]).days + 1 >= MIN_WINDOW_DAYS}


def _cell(key_dims: tuple, key: tuple, rows: list[dict], parent_gap: float,
          parent_ly: float) -> dict:
    t = _T(rows)
    return {"dims": dict(zip(key_dims, key)), "rows": rows, "ty": t.ty, "ly": t.ly,
            "rev_ly": t.rev_ly, "delta": t.delta, "pct": t.pct,
            "share": t.delta / parent_gap if parent_gap else 0.0,
            "weight": t.ly / parent_ly if parent_ly else 0.0,
            "z": poisson_z(t.ty, t.ly)}


def _passes(c: dict, sign: int, crit: float, min_share: float = MIN_SHARE) -> bool:
    return (c["delta"] * sign > 0 and c["share"] >= min_share
            and c["share"] - c["weight"] >= MIN_OVERWEIGHT
            and c["ly"] >= MIN_LY_RN and abs(c["delta"]) >= MIN_MOVE_RN and c["z"] * sign >= crit)


def explain(rows: list[dict], parent_gap: float, dims: list[str]) -> dict | None:
    """Which slice carries a gap. Single dimensions first; an extra dimension
    only when the narrower slice keeps >= REFINE_KEEP of the move; pairs only
    when no single dimension explains it. Returns the slice with its signed
    contribution, the rest of the parent, and how many slices were tested."""
    if not parent_gap or not dims:
        return None
    sign = 1 if parent_gap > 0 else -1
    parent_ly = _T(rows).ly
    cells = [_cell((d,), k, sub, parent_gap, parent_ly)
             for d in dims for k, sub in _group(rows, (d,)).items()]
    tested = len(cells)
    passing = [c for c in cells if _passes(c, sign, z_crit(tested))]
    if not passing:
        pair_cells = [_cell(pair, k, sub, parent_gap, parent_ly) for pair in combinations(dims, 2)
                      for k, sub in _group(rows, pair).items()]
        tested += len(pair_cells)
        passing = [c for c in pair_cells if _passes(c, sign, z_crit(tested))]
        if not passing:
            return None
    best = max(passing, key=lambda c: (c["share"] if c["share"] <= 1.0 else 1.0, abs(c["z"])))

    # greedy refinement: add a dimension only if it keeps the story whole
    while len(best["dims"]) < MAX_DIMS:
        extra = [d for d in dims if d not in best["dims"]]
        refined = []
        for d in extra:
            sub_dims = tuple(best["dims"]) + (d,)
            for k, sub in _group(best["rows"], (d,)).items():
                c = _cell(sub_dims, tuple(best["dims"].values()) + k, sub, parent_gap, parent_ly)
                tested += 1
                refined.append(c)
        keep = [c for c in refined
                if c["delta"] * sign > 0 and abs(c["delta"]) >= REFINE_KEEP * abs(best["delta"])
                and c["ly"] >= MIN_LY_RN and c["z"] * sign >= z_crit(tested)]
        if not keep:
            break
        best = max(keep, key=lambda c: abs(c["delta"]))

    parent = _T(rows)
    rest_ty, rest_ly = parent.ty - best["ty"], parent.ly - best["ly"]
    # canonical order: the same slice must give the same id every day, or the
    # novelty gate would read yesterday's discovery as a new one
    dims_out = {k: best["dims"][k] for k in _DIMS if k in best["dims"]}
    return {
        "dims":      dims_out,
        "ty":        best["ty"], "ly": best["ly"], "delta": best["delta"],
        "pct":       best["pct"], "share": best["share"], "z": best["z"],
        "adr_ly":    best["rev_ly"] / best["ly"] if best["ly"] else 0.0,
        "rest_ty":   rest_ty, "rest_ly": rest_ly,
        "rest_delta": rest_ty - rest_ly,
        "rest_pct":  (rest_ty - rest_ly) / rest_ly if rest_ly else 0.0,
        "tested":    tested,
        "crit":      z_crit(tested),
        # signed partition check: slice + rest == parent, exactly
        "reconciles": abs((best["delta"] + (rest_ty - rest_ly)) - parent_gap) < 1e-6,
    }


def _confidence(share: float, z: float, crit: float, ly: float) -> str:
    """High = strong reconciliation + a clearly significant, sizeable slice;
    otherwise Medium. Anything weaker never becomes a discovery."""
    return "High" if (min(share, 1.0) >= 0.80 and abs(z) >= crit + 1.0 and ly >= 40) else "Medium"


def hidden_day_type(cube: list[dict], today: date, stake_floor: float) -> list[dict]:
    """A month that looks fine overall while weekend (or weekday) nights are
    far behind the same weekdays last year and the other nights cover it."""
    rows = _prepare(cube)
    out = []
    windows = _month_windows(rows, today)
    crit = z_crit(2 * len(windows))      # 2 night types × every month searched
    for (y, m), w in windows.items():
        month = _T(w["rows"])
        if month.ly < MIN_MONTH_LY_RN or month.pct <= -0.10:
            continue          # a month ≥ 10% behind is a gap_driver story
        groups = _group(w["rows"], ("day_type",))
        if len(groups) < 2:
            continue
        for (dt,), sub in groups.items():
            c = _T(sub)
            if c.ly < 20 or c.pct > -MIN_SUBGROUP_PCT or -c.delta < MIN_MOVE_RN:
                continue
            z = poisson_z(c.ty, c.ly)
            if z > -crit:
                continue
            other_delta = month.delta - c.delta
            if other_delta <= 0:
                continue      # nothing is covering it — not hidden
            exposure = -c.delta * (c.rev_ly / c.ly)
            if exposure < stake_floor:
                continue
            driver = explain(sub, c.delta, _available_dims(sub, exclude=("day_type",)))
            out.append({
                "discovery_type": "hidden_day_type", "parent_signal": "pace",
                "year": y, "month": m, "from": w["from"], "to": w["to"],
                "overall": {"ty": month.ty, "ly": month.ly, "delta": month.delta, "pct": month.pct},
                "pattern": {"dims": {"day_type": dt}, "ty": c.ty, "ly": c.ly,
                            "delta": c.delta, "pct": c.pct, "z": z},
                "contributions": [{"day_type": dt, "delta": c.delta},
                                  {"day_type": "weekday" if dt == "weekend" else "weekend",
                                   "delta": other_delta}],
                "reconciles": abs(c.delta + other_delta - month.delta) < 1e-6,
                "driver": driver,
                "exposure_eur": exposure,
                "confidence": _confidence(1.0, z, crit, c.ly),
                "limitations": (["no room-category dimension on this PMS"]
                                if not any(r["room_cat"] for r in sub) else []),
            })
    return out


def gap_drivers(cube: list[dict], today: date, stake_floor: float) -> list[dict]:
    """A month ≥ 10% behind (same weekdays last year): which slice carries it."""
    rows = _prepare(cube)
    out = []
    for (y, m), w in _month_windows(rows, today).items():
        month = _T(w["rows"])
        if month.ly < MIN_MONTH_LY_RN or month.pct > -0.10:
            continue
        ex = explain(w["rows"], month.delta, _available_dims(w["rows"]))
        if not ex:
            continue
        exposure = -ex["delta"] * ex["adr_ly"]
        if exposure < stake_floor:
            continue
        out.append({
            "discovery_type": "gap_driver", "parent_signal": "pace",
            "year": y, "month": m, "from": w["from"], "to": w["to"],
            "overall": {"ty": month.ty, "ly": month.ly, "delta": month.delta, "pct": month.pct},
            "pattern": {"dims": ex["dims"], "ty": ex["ty"], "ly": ex["ly"], "delta": ex["delta"],
                        "pct": ex["pct"], "z": ex["z"]},
            "contributions": [{"slice": ex["dims"], "delta": ex["delta"]},
                              {"slice": "rest of the month", "delta": ex["rest_delta"]}],
            "reconciles": ex["reconciles"],
            "share": ex["share"], "rest_pct": ex["rest_pct"],
            "exposure_eur": exposure,
            "confidence": _confidence(ex["share"], ex["z"], ex["crit"], ex["ly"]),
            "tested": ex["tested"],
            "limitations": (["no room-category dimension on this PMS"]
                            if not any(r["room_cat"] for r in w["rows"]) else []),
        })
    return out


def cancellation_driver(detail: list[dict], today: date, stake_floor: float) -> list[dict]:
    """Yesterday's cancellations concentrated in ONE source (vs that source's
    own zero-filled prior 7 days), with the stay dates they hit."""
    yday = today - timedelta(days=1)
    rows = []
    for r in detail or []:
        try:
            rows.append({"cd": date.fromisoformat(str(r["cancel_date"])[:10]),
                         "sd": date.fromisoformat(str(r["stay_date"])[:10]),
                         "source": str(r.get("source") or "Direct"),
                         "rn": float(r.get("cancel_rn") or 0),
                         "rev": float(r.get("cancel_rev") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    y_rows = [r for r in rows if r["cd"] == yday]
    total = sum(r["rn"] for r in y_rows)
    if total < 3:
        return []
    by_src: dict[str, float] = defaultdict(float)
    for r in y_rows:
        by_src[r["source"]] += r["rn"]
    src, s_rn = max(by_src.items(), key=lambda kv: kv[1])
    share = s_rn / total
    prior = [sum(r["rn"] for r in rows if r["source"] == src and r["cd"] == yday - timedelta(days=i))
             for i in range(1, 8)]
    mean = sum(prior) / 7.0
    var = sum((p - mean) ** 2 for p in prior) / 6.0
    std = math.sqrt(var)
    z = (s_rn - mean) / std if std > 0 else (s_rn - mean) / max(mean * 0.20, 1.0)
    s_rows = [r for r in y_rows if r["source"] == src]
    s_rev = sum(r["rev"] for r in s_rows)
    if s_rn < 3 or share < MIN_SHARE or z < 2.0 or s_rev < stake_floor:
        return []
    by_date: dict[date, float] = defaultdict(float)
    for r in s_rows:
        by_date[r["sd"]] += r["rn"]
    dates = sorted(by_date)
    # the stay dates that carry most of it: all of them when there are <= 3,
    # else the smallest set covering >= 60% — plus any date tied with the
    # last one taken (never drop one of three equally hit dates)
    ranked = sorted(by_date.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) <= 3:
        top, acc = [d for d, _ in ranked], s_rn
    else:
        top, acc, last = [], 0.0, None
        for d, rn in ranked:
            if acc >= MIN_SHARE * s_rn and rn != last:
                break
            top.append(d)
            acc += rn
            last = rn
    top.sort()
    return [{
        "discovery_type": "cancellation_driver", "parent_signal": "cancellation",
        "year": top[0].year, "month": top[0].month, "from": dates[0], "to": dates[-1],
        "total_rn": total, "source": src, "source_rn": s_rn, "share": share,
        "source_prior_avg": mean, "z": z, "source_rev": s_rev,
        "top_dates": top, "top_dates_rn": acc,
        "contributions": [{"source": s, "rn": v} for s, v in sorted(by_src.items(), key=lambda kv: -kv[1])],
        "reconciles": abs(sum(by_src.values()) - total) < 1e-6,
        "exposure_eur": s_rev,
        "confidence": "High" if share >= 0.80 and z >= 3.0 else "Medium",
        "limitations": [],
    }]


def discover_all(data: dict, today: date, stake_floor: float = 1000.0) -> tuple[list[dict], list[str]]:
    """Every discovery, each fail-open. Returns (discoveries, errors)."""
    found: list[dict] = []
    errors: list[str] = []
    cube, detail = data.get("booking_cube") or [], data.get("cancel_detail") or []
    for name, fn, src in (("hidden_day_type", hidden_day_type, cube),
                          ("gap_driver", gap_drivers, cube),
                          ("cancellation_driver", cancellation_driver, detail)):
        if not src:
            continue
        try:
            found.extend(fn(src, today, stake_floor))
        except Exception as exc:   # noqa: BLE001 — never break a briefing
            errors.append(f"discovery {name}: {type(exc).__name__}: {exc}"[:200])
    return found, errors
