# FirstLight Superadmin Portal — Build Plan

*Written 2026-09-11 against the portal spec (11 Phase-1 sections). Status:
AWAITING APPROVAL — no code until the founder approves the sequence and the
two decisions at the end.*

---

## 0. What exists that this reuses (explored, not assumed)

| Spec need | Existing code — REUSED, not duplicated |
|---|---|
| Overview verdict, missed/failed/frozen/stale | `briefing/audit.py` — `audit_all()` + `stale_hotels` (already powers the daily ops email and `/health`) |
| Run history, durations, AI cost, fallback rate | `refresh_runs` (per-stage timings, `cards_audit` incl. tokens per card, `data_quality`, attempts) |
| Usage: DAU, opens, last-seen, per-user activity | `usage_events` + the aggregation already in `GET /admin/usage` (08c316d) |
| Feedback inbox | `insight_feedback` (verdict, reasons, note, card text) |
| Notifications state & stats | `push_subscriptions`, `hotel_prefs`, `intraday_log` (claim rows = delivery log) |
| Subscription fields v1 | `subscriptions` (hotel-level, 2026-09-11) — folds into §4 Clients, see D2 |
| Auth + tenancy foundation | `docs/sql/pg/2026-09-10_tenancy_auth.sql` — `users` (argon2id, `is_platform_admin`, `must_change_password`, lockout fields), `sessions` (hashed opaque tokens, revocation), `memberships` + `hotel_access` view, `groups`/`organizations` (unique VAT). **Schema drafted; C3 code NOT built yet.** |
| Fail-open manual runs | `refresh_commands` poller — portal triggers insert rows, same pipeline |
| Design tokens | The app's canon (Manrope, navy/blue/cyan, white cards on #EAEDF1) |

## 1. Architecture decisions

**URL & hosting** — `https://firstlight.hbis.io/superadmin-control`. Same
Cloudflare Pages app (no new hosting target): the SPA looks at
`location.pathname` on boot and mounts either the phone app or the portal
shell. The path is a convenience, not a defense — every portal API call is
authorized server-side.

**Portal shell** — desktop-first left-nav layout (fixed 220px menu: Overview
· Hotels · Onboarding · Clients · Users · Health · Feedback · Notifications
· Kill switches · Audit log · Security), dense tables, app tokens, no
marketing polish. One new top-level component tree (`src/portal/`), zero
changes to the phone app's components.

**API** — same FastAPI service, new `/superadmin/*` router. Auth chain on
EVERY route: C3 session token → `users.is_platform_admin` → TOTP-verified
flag on the session (set at login) → 404 (not 403) for non-admins, so the
surface is invisible. Every write appends to `admin_audit` inside the same
request.

**Storage** — all new tables live in the Railway Postgres and ship as
versioned, reversible migrations in `docs/sql/pg/migrations/`
(`NNN_name.up.sql` / `.down.sql`). The portal ships AFTER the Postgres
read-flip (step 0), so it never reads Supabase at all — one less thing to
migrate later.

**Read-heavy rule** — a nightly `rollup_daily` table (05:30 UTC job:
per-hotel per-day events, opens, AI cost, fallback count, push deliveries)
feeds every trend chart; live queries touch only indexed single-day data.
No portal query runs during the 03:30 window.

**Kill switches** — a `platform_settings` key/value table read by the
pipeline at each run start (60s cache, fail-open to "on" if unreadable:
a broken settings read must never stop briefings). Keys: `ai_narration`,
`push`, `logins`, `pipeline`. Each flip = reason (required) + audit row.

## 2. New tables (migrations, in order)

| # | Table | For |
|---|---|---|
| 001 | `admin_audit` (id, at, admin_id, action, target_type, target_id, before jsonb, after jsonb, reason) — append-only, no update/delete grants | §10, and every other section writes to it |
| 002 | `login_audit` (at, user_id, email_tried, ok, failure, ip, user_agent) | §5, §11 |
| 003 | `platform_settings` (key pk, value jsonb, updated_at, updated_by) | §9 kill switches, §4 plan tiers/prices as config |
| 004 | `sessions.impersonated_by uuid null` + `sessions.read_only bool` (alter) | §5 impersonation — an impersonation IS a session with the author stamped; banner + read-only enforced server-side (writes 403 on read_only) |
| 005 | `onboarding_runs` (hotel_id, step, status, at, note) | §3 wizard timestamps |
| 006 | `rollup_daily` (day, hotel_id, events, opens, dau, ai_cost_usd, fallbacks, pushes) pk(day,hotel_id) | §1, §6 trends |
| 007 | `contracts` (org_id pk, status trial/paid/suspended, trial_start, trial_end, billing_anchor, plan_tier, list_price_eur, notes, updated_at) | §4 — company-level commercial state (see D2) |

TOTP secret: `users.totp_secret text` + `users.totp_verified_at` (alter, part
of C3 migration). PMS credentials stay in `hotels.pms_config` and are
NEVER returned by any portal endpoint — detail views expose only
`credentials_present: bool` and `last_validated_at`.

## 3. Endpoints (per section, all under `/superadmin`)

- **§10 Audit**: `GET /audit` (search, filters, cursor). Written first —
  every later write depends on `audit(action, target, before, after)`.
- **§5 Users**: `GET /users` · `POST /users` (returns generated initial
  password ONCE, never stored readable) · `POST /users/{id}/reset-password`
  · `POST /users/{id}/lock|unlock` · `POST /users/{id}/revoke-sessions` ·
  `PUT /users/{id}/memberships` · `DELETE /users/{id}` (soft) ·
  `GET /users/{id}/logins` · `POST /users/{id}/impersonate` → read-only
  session token, portal opens the phone app in a banded iframe/tab.
- **§2 Hotels**: `GET /hotels` (search/filter) · `GET /hotels/{id}`
  (identity, adapter, tunnel last-seen, token state, IT contact, run
  history 30, latest briefing preview, push stats, cost) ·
  `GET /hotels/{id}/timeline` (union view: runs ∪ logins ∪ feedback ∪
  notifications ∪ admin actions) · `POST /hotels/{id}/refresh` (data-only)
  · `POST /hotels/{id}/run` (full, 1/day guard) · `POST .../token/rotate` ·
  `PUT .../settings` (non-sensitive only) · `PUT .../cards` (types on/off,
  daily cap).
- **§1 Overview**: `GET /overview` — verdict via `audit_all()`, counts,
  per-hotel today row, AI cost yesterday + 30d (rollup), trials expiring,
  silent clients (no opens 7d).
- **§6 Health**: `GET /health/pipeline` (7d job matrix from refresh_runs +
  scheduler expectations) · `GET /health/hotels` · `GET /health/ai`
  (validator/fallback rates per card type from cards_audit) ·
  `GET /health/infra` (db size, backup age, dual-verify result, versions).
- **§9 Kill switches**: `GET/PUT /settings/{key}` (reason required).
- **§3 Onboarding**: `POST /onboarding` (create hotel skeleton) · step
  endpoints (adapter, tunnel instructions + connector token, test, dry
  run, first briefing) · demo reset/reseed · booth-mode toggle.
- **§4 Clients**: CRUD `groups`, `orgs` (VAT-unique), hotel→org moves,
  `PUT /contracts/{org_id}`, `GET /contracts/export.csv`.
- **§7 Feedback**: `GET /feedback` (group by card type/hotel, filters,
  csv).
- **§8 Notifications**: per-hotel prefs view/edit, delivery stats
  (intraday_log + push subs), `POST /push/test` (reuses existing).
- **§11 Security**: `GET /sessions` + revoke · failed logins by user/IP ·
  superadmin login history (filter of login_audit).

## 4. The dependency that shapes everything: C3

Sections 5, 10 (actor identity), 11, and impersonation **cannot exist**
on Supabase Auth — they are C3 features. C3 today = drafted schema only.
So the portal build necessarily starts by finishing C3:

**Step C3 (2–3 days)**: apply tenancy/auth migration · auth endpoints
(login w/ lockout + uniform errors, logout, change-password w/
forced-first-change, TOTP setup/verify for platform admins) · import
Supabase users keeping uuids · switch the phone app's login/session to C3
(`AUTH=own` env flip, Supabase fallback for 14 days) · per-hotel API
tokens stored hashed · security regression tests (lockout, expiry, reuse,
non-admin 404s).

**Step 0 before that (half-day)**: Postgres read-flip (`STORAGE=pg`) —
the gate has been satisfied since Sep 8; the portal then targets one
canonical database.

*Coordination note: the tenancy schema files are another workstream's
uncommitted drafts — that work gets committed first (or absorbed here),
not worked around.*

## 5. Build sequence & calendar (working days from approval)

| Days | Step | Ships |
|---|---|---|
| 0.5 | **0. Read-flip** | STORAGE=pg live, Supabase 14-day freeze starts |
| 2–3 | **C3** | own login live in the phone app, superadmin TOTP |
| 1 | **10 + shell** | migrations 001-003, audit log API+UI, portal shell at /superadmin-control (left nav, auth boundary tested) |
| 1.5 | **5 Users** | full user management + impersonation |
| 2 | **2 Hotels** | list, detail, timeline, actions, token rotate |
| 1 | **1 Overview** | the verdict home |
| 1.5 | **6 Health** | pipeline matrix, AI quality, infra |
| 0.5 | **9 Kill switches** | 4 switches + pipeline reads them |
| — | **CHECKPOINT (~Oct 1)** | see D3 |
| 1.5 | **3 Onboarding** | wizard + demo tools |
| 1 | **4 Clients** | groups/companies/contracts + CSV |
| 0.5 | **7 Feedback** | inbox |
| 1 | **8 Notifications** | console + test push |
| 0.5 | **11 Security** | sessions/failed-logins views |
| **≈15d** | | **~Oct 3–8 done** if started ~Sep 12 and nothing else preempts |

## 6. What this displaces (the honest trade-off)

Portal-as-primary-track through early October pushes against the other
Nov-1 items: Greek translation (needs founder review cycles — the known
bottleneck), the AI-quality pass, booth demo mode, landing page. The
checkpoint (D3) exists so sections 3/4/7/8/11 — valuable but not
launch-critical — can consciously slip behind Greek + booth if October
tightens. Sections 0–9 (through kill switches) are the "cannot run a
launch without it" core.

## 7. Phase 2

Everything in the spec's Phase 2 goes to `docs/ADMIN_PARKED.md` verbatim
at build start. Nothing from it is built.

---

## Decisions needed before code (the approval)

- **D1 — Sequence**: approve read-flip → C3 → portal in the order above,
  as the primary engineering track starting now.
- **D2 — Subscriptions v1**: this week's hotel-level `subscriptions`
  table folds into company-level `contracts` (§4). Until §4 ships, the
  in-app Clients view keeps working off `subscriptions`; migration 007
  copies values over and the old table is dropped post-Xenia. OK?
- **D3 — Checkpoint**: agree now that at the ~Oct 1 checkpoint, Greek +
  booth-demo readiness outrank portal sections 3/4/7/8/11 if we're
  behind. (Prevents the freeze-week scramble.)
