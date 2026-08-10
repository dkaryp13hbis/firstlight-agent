-- Phase A (FastAPI) storage additions. Paste into Supabase SQL Editor.
-- Safe to re-run. Code is schema-tolerant either way.

-- Per-day KPI snapshot written at publish time; read by GET /briefing/history
alter table briefings add column if not exists kpi_summary jsonb;

-- Per-hotel API bearer tokens for the FastAPI read endpoints.
-- Values are set from the operator side (service key), never from the app.
alter table hotels add column if not exists api_token text;
