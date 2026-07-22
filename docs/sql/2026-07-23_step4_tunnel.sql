-- Step 4 migration: per-hotel PMS config for tunnel-direct fetching
-- Paste into Supabase Dashboard → SQL Editor → Run. Safe to re-run.

-- 1. Which PMS adapter serves this hotel + its tunnel/SQL connection config
alter table hotels add column if not exists pms_type text not null default 'protel_mssql';
alter table hotels add column if not exists pms_config jsonb;

-- pms_config shape (filled in per hotel during the Phase 2 pilot):
-- {
--   "fetch_mode": "tunnel" | "bridge",        -- the pilot switch; absent = bridge
--   "tunnel_hostname": "sql-pome.hbis.io",
--   "cf_access_client_id": "...",
--   "cf_access_client_secret": "...",
--   "sql": {"database": "bidata", "user": "...", "password": "...", "pms_hotel_id": 1}
-- }

-- 2. Logbook additions: which fetch path served the run + tunnel failure detail
alter table refresh_runs add column if not exists fetch_path text;
alter table refresh_runs add column if not exists tunnel_error text;
