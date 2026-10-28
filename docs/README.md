# FirstLight — internal engineering docs

**Internal to HBIS engineering. Not for clients.** This folder is the
engineering handbook: it lives next to the code, is versioned with it, and
every change to a system updates its doc in the same commit.

New here? Read in this order: **CLAUDE.md** (repo root, the rules) →
**ENGINEERING_LOG.md** §1–4 (what the system is, the TO-DO list) → the
runbook for the task in front of you.

## Runbooks — how we do things

| Doc | Use it when | Keep current when |
|---|---|---|
| [USER_ADMIN.md](USER_ADMIN.md) | creating users, giving access, resets, leavers, the demo account | **anything changes in the superadmin portal (`/superadmin-control`), the login flow, roles or `scripts/users.py`** |
| [ONBOARDING.md](ONBOARDING.md) | **start here** for a new client: intake, client record (portal Onboarding / Clients), hotel row, season dates, users, verify in portal Hotels, merges and pauses | the portal Onboarding / Clients / Hotels screens, the hotel columns, or the two-store rule change |
| [ONBOARDING_ONPREM.md](ONBOARDING_ONPREM.md) | the PMS runs on a server at the hotel (Protel, Opera 5): tunnel, read-only login, pre-flight, `pms_config` | an on-premises adapter or the tunnel set-up changes |
| [ONBOARDING_API.md](ONBOARDING_API.md) | the PMS is cloud-only (Opera Cloud, Hotelizer, Mews): qualify, access, discovery, build, backfill — **not live yet** | the first cloud adapter ships, or a vendor's access model changes |
| [PHASE_C_RUNBOOK.md](PHASE_C_RUNBOOK.md) | the Supabase → Postgres migration steps (C1–C5), DB migrations as the builder role | a migration step is done or changes |
| [SECURITY_CHECKS.md](SECURITY_CHECKS.md) | the quarterly 10-minute security self-check (next: Dec 2026) | a check is added or the stack changes |
| `.claude/skills/` | task knowledge for Claude Code: `pms-queries`, `analyst-signals`, `ops-monitoring`, `hotel-onboarding` | the area they describe changes |

## Living records

| Doc | What |
|---|---|
| [ENGINEERING_LOG.md](ENGINEERING_LOG.md) | architecture, migration tracker, **TO-DO list**, decision log, incident log, release history — updated after every step, incident or decision |
| [COMMERCIAL.md](COMMERCIAL.md) | positioning, pricing, bundles, market policies |

## Plans, specs and decision records (point in time)

| Doc | What |
|---|---|
| [STACK_PLAN.md](STACK_PLAN.md) | target infrastructure and migration order (2026-08) |
| [POSTGRES_VS_SUPABASE.md](POSTGRES_VS_SUPABASE.md) | why we left Supabase, in plain language |
| [ADMIN_PLAN.md](ADMIN_PLAN.md) | build plan of the superadmin portal |
| [WATCHLIST_SPEC.md](WATCHLIST_SPEC.md) | My Watchlist spec |
| [MOBILE_APP_PLAN.md](MOBILE_APP_PLAN.md) | native app placeholder (not started) |
| [FIRSTLIGHT_BRIEF.md](FIRSTLIGHT_BRIEF.md) | self-contained product brief for advisors (no secrets) |
| `design-handoff/` | design handoffs for the app and templates |
| `sql/`, `sql/pg/` | every schema / data change as a dated SQL file (Supabase era, then Postgres) |

## Rules for these docs

1. **Docs change with the code.** A pull/commit that changes behaviour
   described here updates the doc in the same commit, with the date.
2. **Write for the next engineer**, not for yourself: what it is, how to
   do it step by step, how to check it worked, what to do when it fails.
3. **No secrets, ever** — no passwords, tokens or connection strings.
   Commands show placeholders.
4. **Exact UI labels** in runbooks (copy them from the screen or the
   source), so a step can be followed without guessing.
5. Each runbook ends with a **change history** table.
