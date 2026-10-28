# Onboarding a new client — start here

**Audience:** HBIS engineering / operations. **Owner:** whoever changes the
onboarding flow, the superadmin portal (`/superadmin-control` → Onboarding,
Clients, Hotels) or the hotel tables.
**Keep it current:** a change to those screens, the hotel columns or the
platform rules below updates this file in the same commit (CLAUDE.md).
Last verified against backend `ec96996` and PWA `5160440` — 2026-10-28.

This page is the journey every client goes through. The PMS connection is
the only part that depends on where the PMS runs; it has its own runbook:

| The hotel's PMS runs… | Examples | Runbook | Status |
|---|---|---|---|
| on a server **at the hotel** (or the group's own server) | Protel (SQL Server), Opera 5 (Oracle) | [ONBOARDING_ONPREM.md](ONBOARDING_ONPREM.md) | **live** — Pome, Potidea (Protel), City / Excelsior / ON Residence (Opera) |
| in the **vendor's cloud**, reachable by API | Opera Cloud, Hotelizer, Mews | [ONBOARDING_API.md](ONBOARDING_API.md) | **not live** — no cloud adapter yet |

How to tell: "Is there a PMS server your IT can log into (RDP)?" Yes →
on-premises. "We log in to the PMS in a browser and there is no server" →
cloud / API.

---

## The journey at a glance

| Step | What | Who | Where | Time |
|---|---|---|---|---|
| 0 | Intake | sales / ops | intake sheet (this page) | 15 min |
| 1 | Client record (group, company, contract) | ops | portal → **Onboarding** | 5 min |
| 2 | PMS connection | engineering + hotel IT | path runbook | 30 min on-premises · weeks for a new cloud PMS |
| 3 | Hotel row | engineering | SQL (Postgres) | 10 min |
| 4 | Season dates (resorts only) | engineering | SQL | 5 min |
| 5 | Users | ops | portal → **Users** ([USER_ADMIN.md](USER_ADMIN.md)) | 5 min |
| 6 | Verify and go live | engineering | portal → **Hotels** + the app | 15 min + next morning |
| 7 | Hand-over | ops | phone call | 10 min |

## The rules (read once)

| Level | Table | Key | Who sits here |
|---|---|---|---|
| Group | `groups` | slug | Optional. The same people own several companies. |
| Company | `organizations` | **VAT number** (unique per country) | The legal entity we invoice. Every hotel belongs to exactly one. |
| Hotel | `hotels` | id / slug | The property. Briefings, runs, feedback, watchlists all hang off this id. |
| User | `users` + `memberships` | email | A membership at ANY level grants every hotel below it (view `hotel_access`). |

- **One company = one VAT.** Same VAT already exists → attach the new hotel
  to that company; never create a duplicate. Same VAT, different trading
  name = same company: ask for the VAT first, the brand second.
- **Same people own two companies** → one group; both companies point at it;
  owners get a group-level membership and see every hotel in one login.
- **A hotel changes hands** → move it to the new company (Step 8). History
  stays on the hotel id; nothing is deleted or copied.
- **No email, ever.** Passwords go by phone or in person (USER_ADMIN.md).
- **Nothing runs at the hotel** except, for on-premises PMSs, the
  cloudflared connector. All code, queries and keys live on our side.
- **No guest-level data in our cloud.** Only stays, nights, revenue,
  sources and dates — never names, contacts, documents or cards.

> ✅ **One store since 2026-10-28** (Supabase retirement, stage 1 —
> ENGINEERING_LOG "SUPABASE RETIREMENT"). Publication, "Refresh now", the
> app's refresh button and push recipients use Postgres only, and the nightly
> Supabase → Postgres copy (`FirstLightMirror`) is switched off. **Create and
> edit hotels in Postgres only.** Until stage 2, Supabase still receives a
> best-effort copy of new writes; for a hotel that does not exist there the
> copy simply fails, harmlessly.

## Step 0 — Intake (15 min) — mandatory

Collect before touching anything. Path-specific questions (PMS server, API
access) are in the path runbook.

| # | Question | Goes to | Why |
|---|---|---|---|
| 0.1 | **Company legal name + VAT number** (+ country) | company (Step 1) | Unique key. Existing VAT = existing company. |
| 0.2 | **Same owners as another client of ours?** | group (Step 1) | Owners see all their hotels in one login, plus the portfolio view. |
| 0.3 | **Real sellable room count** | `hotels.total_rooms` | Never trust the PMS room list (out-of-order, dummy rooms, fake room types). All occupancy maths divides by it. |
| 0.4 | **Season dates** — opening and first closed night, LAST + THIS (+ NEXT once known) year. City hotels: open all year. | `hotels.season_settings` (Step 4) | Load-bearing: closed nights are legitimate zeros, never compared with open ones. |
| 0.5 | **Hotel type**: resort or city | `pms_config.hotel_type` | Booking lead-time profile. |
| 0.6 | **App users**: name, email, level (owner at company / group, viewer at one hotel) | Step 5 | Individual accounts, never shared. Email = login name only. |
| 0.7 | **Briefing language**: EN or Greek | `pms_config.language` + users | Narration language (the app can change it per hotel later). |
| 0.8 | **Contract**: trial or paid, start date, rate | company (Step 1) | Finance reports per company. |
| 0.9 | **PMS** (name + version) and an **IT / PMS-admin contact** | path runbook | Picks the path. |

## Step 1 — Client record (portal → Onboarding, 5 min)

1. **https://firstlight.hbis.io/superadmin-control** → **Onboarding**.
2. Same VAT already a client? Stop here and edit it under **Clients**
   instead (row → form at the bottom → **Save changes**).
3. In **New client** fill:
   - **Group (owner umbrella, optional)** — pick one, or type a name in
     **… or create a group** and press **+ Group**. Only when 0.2 = yes.
   - **Company (trading name)** \* · **Legal name** · **VAT** (e.g.
     `EL123456789`) · **Country** (2 letters, `GR`).
   - **Contact person** · **Contact phone**.
   - **Contract status** (Trial / Active / Suspended / Ended) · **Start date**
     · **Rate € / month** · **Rate € / year** · **Notes**.
   - **Hotels in this company** — the new hotel does not exist yet; tick it
     after Step 3 (Clients → company row → tick → **Save changes**).
4. **Create client** → "Saved".

Under **Clients** the same form also has **AI insights (all hotels of this
company)**: Inherit (default on) / On / Off — Off publishes deterministic
cards at zero AI cost (useful for long trials).

## Step 2 — PMS connection

Follow the path runbook, then come back here with the `pms_type` and the
`pms_config` block it produces:
[on-premises](ONBOARDING_ONPREM.md) · [API / cloud](ONBOARDING_API.md).

## Step 3 — Hotel row (engineering, 10 min)

Today hotels are created by engineering (the portal's Onboarding screen
says so: "hotels are provisioned by engineering, then registered to their
company here"). Copy this into a dated file `docs/sql/pg/<date>_<hotel>.sql`,
fill it, and run it as the database owner:
`powershell -ExecutionPolicy Bypass -File scripts\pg_migrate.ps1 docs\sql\pg\<file>.sql`.
Never commit the filled-in secrets: commit the file with `<placeholders>`.

```sql
insert into hotels (id, org_id, name, slug, total_rooms, timezone, pms_type,
                    active, season_settings, api_token, pms_config)
select gen_random_uuid(), o.id, '<Hotel name>', '<hotel-slug>', <rooms 0.3>,
       'Europe/Athens', '<pms_type from Step 2>',
       false,                                   -- activated in Step 6
       null,                                    -- Step 4 (resorts)
       encode(gen_random_bytes(32), 'hex'),     -- per-hotel API token
       '<pms_config JSON from Step 2>'::jsonb
  from organizations o where o.vat_number = '<VAT from 0.1>'   -- or o.name = '<trading name>' while the VAT is pending
returning id, name;
```

Then tick the hotel under Clients (Step 1.3).

## Step 4 — Season dates (resorts only, 5 min)

`hotels.season_settings`, e.g.
`{"2025": {"open": "2025-04-25", "close": "2025-10-20"}, "2026": {"open": "2026-04-30", "close": "2026-10-14"}}`
where `close` is the **first closed night**. Template with checks:
`docs/sql/pg/2026-10-26_pome_season_settings.sql`. City hotels stay `null`.

> The season release (built 2026-10-26, **not deployed**) adds the
> season-aware publication gate and lets the hotel owner enter these dates
> in the app (Settings → Season dates). Until it ships, a resort onboarded
> while **closed** will not publish — its zero nights fail the safety gate,
> exactly what keeps Pome / Potidea frozen today.

## Step 5 — Users (portal → Users)

Follow [USER_ADMIN.md](USER_ADMIN.md) §3. Owner of the company (or group)
→ **Owner**; hotel staff → **One hotel** + **Viewer**.

## Step 6 — Verify and go live (portal → Hotels, 15 min + next morning)

1. **Hotels** → filter by name → click the row. The grey line on the right
   must read `tunnel <hostname> · credentials ✓` (on-premises).
2. **Activate** (new hotels are created paused). The scheduler and
   "Refresh now" only see active hotels.
3. **Refresh now** → "refresh queued". After 1–2 minutes close and reopen
   the row. The newest run must show **Type** manual, **Status** success
   (degraded = fallback cards, acceptable), **Path** tunnel, **Rows**
   filled (hover = per-query counts), no **Error**.
4. Sign in to **https://firstlight.hbis.io** as one of the NEW users (not
   an admin) and compare with the PMS: yesterday's revenue and room nights,
   this month's booked revenue. A wrong property id gives plausible but
   wrong numbers — check one known figure.
5. Spot-check: the Pickup boxes equal the butterfly totals; the narrated
   paragraph's numbers equal the KPI cards.
6. Their phone: bell on for the hotel (iPhone: from the home-screen app).
7. **Next morning** (03:30 UTC run; 05:00 Athens for hotels on the early
   slot): exactly ONE push, no email; Hotels row → **Last briefing** =
   yesterday, **Result** success, **AI cost 30d** a few cents.

## Step 7 — Hand-over (phone)

Read out the passwords (USER_ADMIN.md §5), walk them through installing the
app and the bell, and tell them the first briefing arrives tomorrow
morning. Contract status in Clients: Trial or Active.

## Step 8 — Merges, moves, pauses (any time)

| Situation | Portal | Behind it |
|---|---|---|
| Two companies, same owners | Clients → each company → **Group** → Save | `organizations.group_id`; owners get a group membership (Users) |
| Hotel sold / moved | Clients → new company → tick the hotel → Save | `hotels.org_id`; briefings, runs, feedback stay on the hotel id |
| Two companies are one legal entity (same VAT) | move the hotels, then set the loser's contract to Ended | never delete the losing row |
| Pause a hotel (unpaid, closed for good) | Hotels → row → **Pause** (reason required) | `hotels.active = false` — no runs, no briefings; history kept |
| Contract ends | Clients → **Contract status** Ended; Users → **Deactivate** each user | users signed out everywhere |


## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Refresh now" queues but no run appears | hotel still paused (the refresh is closed as failed) | Step 6.2 |
| Run **failed** with `yesterday_nonzero` | resort closed (Step 4 note), or wrong property id | season release; check the PMS id in the path runbook |
| Numbers too high / occupancy too low | `total_rooms` wrong, fake rooms counted | 0.3; path runbook gotchas |
| App: "Data may be out of date" | no briefing since that date | Hotels → row → runs → Error column |
| Owner sees no portfolio entry | fewer than 2 of the group's hotels in their access | USER_ADMIN.md §9 |

## Change history

| Date | Change | Commit |
|---|---|---|
| 2026-08-04 | v1 from the Pome pilot + tunnel migration | — |
| 2026-09-10 | v2: own Postgres, own login, no email, Group → Company → Hotel | — |
| 2026-10-28 | v3: split into start-here + [on-premises](ONBOARDING_ONPREM.md) + [API](ONBOARDING_API.md); portal Onboarding / Clients / Hotels steps with exact labels; the two-store rule; season-release caveat | bb80884 |
| 2026-10-28 | Postgres only (Supabase retirement stage 1): two-store rule and the Supabase hotel step removed | (this commit) |
