-- Step 3 migration: refresh_runs operational log + stop storing rendered HTML
-- Paste into Supabase Dashboard → SQL Editor → Run. Safe to re-run.

-- 1. The operational logbook — one row per refresh attempt
create table if not exists refresh_runs (
  id                 uuid primary key default gen_random_uuid(),
  hotel_id           uuid references hotels(id),
  run_type           text not null default 'full',     -- full | data_only | manual
  status             text not null default 'running',  -- running | success | degraded | failed
  started_at         timestamptz not null default now(),
  completed_at       timestamptz,
  error_type         text,
  error_message      text,
  timings            jsonb,   -- {fetch_ms, ai_ms, publish_ms}
  rows_fetched       jsonb,   -- per-query row counts from data_quality
  data_quality       jsonb,   -- full contract verdict for this snapshot
  cards_audit        jsonb,   -- per-card: facts given, attempts, validation, fallback, tokens
  input_tokens       integer,
  output_tokens      integer,
  cache_read_tokens  integer,
  cache_write_tokens integer,
  estimated_cost_usd numeric(10, 4),
  model              text,
  prompt_version     text
);

create index if not exists refresh_runs_hotel_started_idx
  on refresh_runs (hotel_id, started_at desc);

-- Lock the table from the public API (service key bypasses RLS; app users never read this)
alter table refresh_runs enable row level security;

-- 2. Link each published briefing to the run that produced it
alter table briefings add column if not exists source_run_id uuid;

-- 3. One-time cleanup: drop stored HTML older than 30 days
--    (new briefings no longer store HTML at all — pages render from data on demand)
update briefings set rendered_html = null
where report_date < current_date - 30 and rendered_html is not null;
