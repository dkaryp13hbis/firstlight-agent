---
name: ops-monitoring
description: Checking production health — refresh_runs queries, triggering test refreshes, reading cards_audit, incident patterns (tunnel errors, 502s, missing briefings), and Supabase REST access from this Windows machine.
---

# Ops & monitoring

## Supabase access (from this machine)

- URL `https://tqfupsvymisnskiwtjut.supabase.co`; service-role JWT is in
  Railway env / user's records (also in session memory).
- **Use `Invoke-RestMethod`** with headers `apikey` + `Authorization: Bearer` —
  `curl`/`curl.exe` hit a local SSL error (exit 35). POST bodies: write to a
  temp file + `-InFile` (inline `-d` JSON breaks in PS 5.1).
- Schema note: `refresh_runs.attempt` may still be missing (Step 5 SQL not
  pasted); `briefings` has NO `created_at` — order by `report_date`.

## Daily health check

```
GET /rest/v1/refresh_runs?select=hotel_id,run_type,status,fetch_path,tunnel_error,
    started_at,timings,estimated_cost_usd,error_type,error_message
    &started_at=gte.<today>T00:00:00Z&order=started_at.desc
```
Expect per hotel (UTC): 03:30 full (Pome: fetch_path=tunnel, cost ~$0.03–0.05),
11:00 + 17:00 data_only (no cost). Potidea: bridge, plus a stray 04:00 /trigger
full (its old Task Scheduler — silent, $0, dies at Phase 3).
Statuses: `success` clean · `degraded` = fallback cards used (check
cards_audit) · `failed` = check error_type/error_message + retry ladder rows
(5/15/45 min) · `skipped` = lock contention or briefing already exists.

## Per-card audit

`refresh_runs.cards_audit` (AI runs only): per card + hero — facts, attempts,
validation_problems, fallback_used, tokens, latency. Word-cap near-misses show
here as attempt-1 problems that succeed on attempt 2.

## Trigger a test run (free, silent)

Insert into `refresh_commands` `{hotel_id, status:"pending"}` — poller claims
within 30s, runs full pipeline, REUSES the day's AI (no cost, no
notifications), writes a refresh_runs row. This is the standard way to verify
a deploy changed fetch behavior (check `data_quality.rows_fetched`).

## Known incident patterns (details: ENGINEERING_LOG §6)

- 502 from bridge → hotel-side daemon dead (Potidea only, until Phase 3).
- Tunnel fetch fails → no bridge fallback on Pome; retry ladder handles
  transients; check Cloudflare tunnel health + `tunnel_error` column.
- App shows "No briefing available" → briefings row missing `rendered_html`
  (PWA renders from it — it MUST be stored until PWA renders from data).
- Only 1–2 insights → check novelty gate self-suppression / legacy_mode.
- Notification storm → something notifying on manual runs; manual must be
  silent (`notify=not manual`).

## Key IDs

Pome `0b83ecdc-5216-4c53-ba96-3ddb67e1e253` (tunnel) ·
Potidea `08b4b6f3-ce6d-4b7d-ba02-e48aec3d213f` (bridge, legacy).
