-- refresh_runs: let app users READ their own hotels' run history (2026-09-04)
-- for the in-app "Data health" section (Power-BI-style refresh history).
-- The backend writes with the service role (bypasses RLS) — unaffected.
-- Paste into Supabase SQL Editor. Safe to re-run.

alter table refresh_runs enable row level security;

drop policy if exists rr_sel on refresh_runs;
create policy rr_sel on refresh_runs
  for select to authenticated
  using (hotel_id in (select hotel_id from hotel_users where user_id = auth.uid()));
-- no insert/update/delete policies: writes stay service-role only

-- Verify (as any app user, via the app): Settings → Data health shows runs.
