-- Step 5 migration: retry-attempt tracking on the run logbook
-- Paste into Supabase Dashboard → SQL Editor → Run. Safe to re-run.
-- (Code tolerates this column being absent — logging falls back gracefully.)

alter table refresh_runs add column if not exists attempt integer not null default 1;
