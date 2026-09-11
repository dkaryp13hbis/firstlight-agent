"""
Opera adapter validation harness — runs the REAL adapter against the hotel's
Oracle server (direct over VPN, or through a local `cloudflared access tcp`
port) for each resort and reconciles the result with the group's own BI.

Run:  set ORA_PW=...  &  py -3.13 scripts/validate_opera.py [host:port]
      (default host 192.168.0.156:1521 — the VPN path)

Checks per resort:
  1. contract: data_quality.complete, legacy_mode, rows_fetched
  2. yesterday / MTD vs Opera's native RESERVATION_STAT_DAILY (nights exact,
     revenue within 1%)
  3. Hotel BI "Quick Insights" candidates: full-year TY OTB vs STLY, YTD TY vs
     LY — printed so the user can match them against the Power BI table
  4. pickup reconciliation: sum(pickup_daily) == pickup card 7d (net) + cancels
"""
import os, sys, time, json
from datetime import date, timedelta

sys.path.insert(0, ".")
import oracledb
from db.adapters.opera_oracle.fetcher import fetch_snapshot, _load_rows, compute_pack, ACTIVE, _alive_at, _rn_of, _rev_of, _year_ago

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.156:1521"
RESORTS = {"CITY": ("City Hotel", 125), "EXCEL": ("The Excelsior", 36), "ONRES": ("ON Residence", 60)}

conn = oracledb.connect(user="opera_ro", password=os.environ["ORA_PW"], dsn=f"{HOST}/opera")
cur = conn.cursor()
# ASOF=YYYY-MM-DD replays a closed business day (night-audit gate is per day)
today = date.fromisoformat(os.environ["ASOF"]) if os.environ.get("ASOF") else date.today()
yesterday = today - timedelta(days=1)


def stat_daily(resort, d1, d2):
    # nights = physical rooms only; revenue = ALL rooms incl. posting masters
    # (the Hotel BI convention the adapter follows)
    cur.execute("""select nvl(sum(case when nvl(psuedo_room_yn,'N')='N' then stay_rooms else 0 end),0),
                          nvl(sum(room_revenue),0)
                   from opera.reservation_stat_daily
                   where resort=:r and business_date between :a and :b""",
                r=resort, a=d1, b=d2)
    rn, rev = cur.fetchone()
    return float(rn), float(rev)


for resort, (name, rooms) in RESORTS.items():
    print(f"\n{'='*70}\n{resort} — {name}\n{'='*70}")
    t0 = time.time()
    snap = fetch_snapshot(conn, {"hotel_name": name, "total_rooms": rooms,
                                 "pms_hotel_id": resort, "hotel_type": "city", "_today": today})
    dt = time.time() - t0
    dq = snap["data_quality"]
    print(f"fetch {dt:.1f}s  grain_rows={snap['pms_meta']['grain_rows']}  pms_rooms={snap['pms_meta']['pms_rooms']}")
    print(f"complete={dq['complete']} legacy={dq['legacy_mode']} missing={dq['missing_fields']} sanity_fail={[k for k,v in dq['sanity'].items() if not v]}")
    print("rows_fetched:", dq["rows_fetched"])

    y, m = snap["yesterday"], snap["mtd"]
    print(f"\nYESTERDAY  rn={y['roomNights']} occ={y['occupancy']:.1%} rev_gross={y['revenue']:,.0f} net={y['revenueNet']:,.0f} adr={y['adr']}  | LY rn={y['roomNightsLY']} rev={y['revenueLY']:,.0f}")
    print(f"           arrivals={y['arrivals']} departures={y['departures']} stayovers={y['stayovers']} inhouse={y['inHouse']}")
    print(f"MTD        rn={m['roomNights']} occ={m['occupancy']:.1%} rev_gross={m['revenue']:,.0f} net={m['revenueNet']:,.0f} | LY rn={m['roomNightsLY']} rev={m['revenueLY']:,.0f}")

    # 2. native stats cross-check (net revenue)
    s_rn, s_rev = stat_daily(resort, yesterday, yesterday)
    ms = yesterday.replace(day=1)
    m_rn, m_rev = stat_daily(resort, ms, yesterday)
    ok_y = s_rn == y["roomNights"] and abs(s_rev - y["revenueNet"]) <= max(1, 0.01 * s_rev)
    ok_m = m_rn == m["roomNights"] and abs(m_rev - m["revenueNet"]) <= max(1, 0.01 * m_rev)
    print(f"STAT_DAILY yesterday rn={s_rn:.0f} net={s_rev:,.0f} -> {'OK' if ok_y else 'MISMATCH'} ; MTD rn={m_rn:.0f} net={m_rev:,.0f} -> {'OK' if ok_m else 'MISMATCH'}")

    # 3. Hotel BI quick-insights candidates
    rows = _load_rows(cur, resort)
    stly_cap = _year_ago(today)
    ty = [r for r in rows if r.sg in ACTIVE and r.sd.year == today.year]
    fy_rn, fy_net, fy_gross = sum(r.rn for r in ty), sum(r.net for r in ty), sum(r.gross for r in ty)
    st = [r for r in rows if r.sd.year == today.year - 1 and _alive_at(r, stly_cap) and r.bd and r.bd <= stly_cap]
    st_rn, st_gross = sum(_rn_of(r) for r in st), sum(_rev_of(r) for r in st)
    st_net = sum(r.net for r in st if r.sg in ACTIVE)
    ytd = [r for r in ty if r.sd <= yesterday]
    ly_ytd = [r for r in rows if r.sg in ACTIVE and r.sd.year == today.year - 1 and r.sd <= _year_ago(yesterday)]
    print(f"\nHOTEL-BI CANDIDATES (their table: revenue / nights / ADR, TY vs STLY)")
    print(f"  full-year TY  : net {fy_net:,.0f}  gross {fy_gross:,.0f}  nights {fy_rn:,.0f}  adr_net {fy_net/fy_rn if fy_rn else 0:,.0f}")
    print(f"  STLY (book<=cap): gross {st_gross:,.0f}  net~{st_net:,.0f}  nights {st_rn:,.0f}")
    print(f"  YTD TY        : net {sum(r.net for r in ytd):,.0f}  gross {sum(r.gross for r in ytd):,.0f}  nights {sum(r.rn for r in ytd):,.0f}")
    print(f"  YTD LY final  : net {sum(r.net for r in ly_ytd):,.0f}  gross {sum(r.gross for r in ly_ytd):,.0f}  nights {sum(r.rn for r in ly_ytd):,.0f}")
    fly = [r for r in rows if r.sg in ACTIVE and r.sd.year == today.year - 1]
    print(f"  LY final year : net {sum(r.net for r in fly):,.0f}  gross {sum(r.gross for r in fly):,.0f}  nights {sum(r.rn for r in fly):,.0f}")

    # 4. pickup reconciliation
    p = snap["pickup"]
    pdaily = snap["pickup_daily"]; cdaily = snap["cancel_daily"]
    seven = (today - timedelta(days=6)).isoformat()
    net7 = sum(r["net_rn"] for r in pdaily if r["ref_date"] >= seven)
    cx7 = sum(r["cancel_rn"] for r in cdaily if r["ref_date"] >= seven)
    print(f"\nPICKUP card 7d: +{p['last7d']['roomNights']} rn / {p['last7d']['revenue']:,.0f} ; cancels 7d {p['cancellations7d']} rn / {p['cancellationRevenue7d']:,.0f}")
    print(f"  daily series 7d: net {net7} + cancels {cx7} = gross {net7+cx7}  -> {'OK' if net7 + cx7 == p['last7d']['roomNights'] else 'MISMATCH'}")
    print(f"  today +{p['today']['roomNights']} / 1d +{p['last1d']['roomNights']} / 3d +{p['last3d']['roomNights']}  top month {p['topMonth']} ({p['topMonthNights']})")

    print("\nPACE (month occ / stly / final, rev):")
    for r in snap["pace"]:
        print(f"  {r['month']}: occ {r['occ']:.0%} stly {r['stly']:.0%} final {r['final']:.0%} | rev {r['rev']:,.0f} stly {r['rev_stly']:,.0f} final {r['rev_final']:,.0f} adr {r['adr']}")
    print("NEXT7:", [(d['date'], d['rooms'], round(d['occ']*100), d['arrivals']) for d in snap["next7days"]])
    print("CHANNELS:", [(c['name'], c['rev'], c['nights'], c['var']) for c in snap["topChannels"]])
    cm = snap["current_month_remaining"]
    print("CURRENT MONTH REMAINING:", cm)
    print("OTB first 5:", snap["otb_by_date"][:5])
    print("LEAD rows:", len(snap["lead_time"]), " consumed:", snap["consumed_by_source"][:4])
    print("PACE NEXT YEAR:", [(r['month'], r['rn'], r['rn_stly']) for r in snap["pace_next_year"]][:6])

    out = f"scripts/_opera_snapshot_{resort}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snap, f, default=str, indent=1)
    print("snapshot saved:", out)
