-- ============================================================================
-- FirstLight — Railway Postgres schema (Phase C, prepared 2026-09-04)
-- ============================================================================
-- Derived from the LIVE Supabase columns (2026-09-04 backup, 736 rows) plus
-- every constraint in docs/sql/ history. This file is the single source of
-- truth for C1 provisioning: psql "$DATABASE_URL" -f docs/sql/pg/schema.sql
--
-- Deliberate differences vs Supabase:
--   * NO RLS anywhere — this database is reached ONLY by the backend service
--     (the app goes through the FastAPI layer in C2; auth stays Supabase).
--   * auth.users does not exist here: user_id columns are plain uuid values
--     that reference Supabase Auth identities (no FK to enforce).
--   * timestamptz everywhere; jsonb for every document column.
-- Safe to re-run (IF NOT EXISTS throughout).
-- ============================================================================

create extension if not exists pgcrypto;   -- gen_random_uuid()

-- ── tenancy ─────────────────────────────────────────────────────────────────

create table if not exists organizations (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  slug        text not null unique,
  plan        text not null default 'standard',
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

create table if not exists hotels (
  id              uuid primary key default gen_random_uuid(),
  org_id          uuid not null references organizations(id),
  name            text not null,
  slug            text not null unique,
  total_rooms     integer not null,
  timezone        text not null default 'Europe/Athens',
  pms_type        text not null default 'protel_mssql',
  active          boolean not null default true,
  bridge_url      text,
  bridge_secret   text,
  recipient_email text,
  recipient_name  text,
  pms_config      jsonb,
  season_settings jsonb,
  api_token       text unique,               -- per-hotel Bearer token (Phase A)
  created_at      timestamptz not null default now()
);

create table if not exists hotel_users (
  id          uuid primary key default gen_random_uuid(),
  hotel_id    uuid not null references hotels(id),
  user_id     uuid not null,                 -- Supabase Auth identity
  role        text not null default 'viewer',
  created_at  timestamptz not null default now(),
  unique (hotel_id, user_id)
);

-- ── briefings (the product) ─────────────────────────────────────────────────

create table if not exists briefings (
  id            uuid primary key default gen_random_uuid(),
  hotel_id      uuid not null references hotels(id),
  report_date   date not null,
  data          jsonb not null,
  ai_insights   jsonb,
  kpi_summary   jsonb,
  rendered_html text,                        -- legacy PWA; drop after Vercel retires
  source_run_id uuid,
  generated_at  timestamptz not null default now(),
  unique (hotel_id, report_date)             -- upsert target (on_conflict)
);
create index if not exists briefings_hotel_date_idx
  on briefings (hotel_id, report_date desc);

-- ── pipeline machinery ──────────────────────────────────────────────────────

create table if not exists refresh_commands (
  id           uuid primary key default gen_random_uuid(),
  hotel_id     uuid not null references hotels(id),
  type         text not null default 'manual',   -- manual | data_only
  status       text not null default 'pending',  -- pending | running | done | failed
  requested_at timestamptz not null default now(),
  completed_at timestamptz
);
create index if not exists refresh_commands_pending_idx
  on refresh_commands (status, requested_at) where status = 'pending';

-- C1 step 6: LISTEN/NOTIFY replaces the 30s poller
create or replace function notify_refresh_command() returns trigger as $$
begin
  perform pg_notify('refresh_commands', new.id::text);
  return new;
end $$ language plpgsql;
drop trigger if exists refresh_commands_notify on refresh_commands;
create trigger refresh_commands_notify
  after insert on refresh_commands
  for each row execute function notify_refresh_command();

create table if not exists refresh_runs (
  id                 uuid primary key default gen_random_uuid(),
  hotel_id           uuid not null references hotels(id),
  run_type           text not null,          -- full | data_only
  status             text not null,          -- running | success | degraded | failed | skipped
  attempt            integer,
  started_at         timestamptz not null default now(),
  completed_at       timestamptz,
  error_type         text,
  error_message      text,
  tunnel_error       text,
  fetch_path         text,
  timings            jsonb,
  rows_fetched       jsonb,
  data_quality       jsonb,
  cards_audit        jsonb,
  input_tokens       integer,
  output_tokens      integer,
  cache_read_tokens  integer,
  cache_write_tokens integer,
  estimated_cost_usd numeric(10, 5),
  model              text,
  prompt_version     text
);
create index if not exists refresh_runs_hotel_started_idx
  on refresh_runs (hotel_id, started_at desc);

create table if not exists intraday_log (
  hotel_id   uuid not null references hotels(id),
  day        date not null,
  type       text not null,                  -- alerts | momentum
  created_at timestamptz not null default now(),
  primary key (hotel_id, day, type)
);

-- ── app-user data (written via the C2 API) ──────────────────────────────────

create table if not exists insight_feedback (
  id           uuid primary key default gen_random_uuid(),
  hotel_id     uuid not null references hotels(id),
  report_date  date not null,
  card_id      text not null,
  verdict      smallint not null,            -- 1 = up, -1 = down
  reason       text,
  card_content jsonb,
  user_id      uuid not null,
  created_at   timestamptz not null default now(),
  unique (hotel_id, report_date, card_id, user_id)   -- last-word-wins upsert
);

create table if not exists hotel_prefs (
  hotel_id   uuid primary key references hotels(id),
  language   text not null default 'en',
  updated_at timestamptz not null default now()
);

create table if not exists push_subscriptions (
  id                 uuid primary key default gen_random_uuid(),
  hotel_id           uuid not null references hotels(id),
  user_id            uuid not null,
  subscription       jsonb not null,
  notification_prefs jsonb not null
    default '{"morning": true, "alerts": true, "momentum": true}'::jsonb,
  created_at         timestamptz not null default now(),
  unique (user_id, hotel_id)                 -- one row per user PER HOTEL
);

create table if not exists watchlist (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null,
  hotel_id   uuid not null references hotels(id),
  kind       text not null,                  -- month | range
  key        text not null,                  -- "2026-10" | "2026-09-22..2026-09-28"
  label      text,
  note       text,
  created_at timestamptz not null default now(),
  unique (user_id, hotel_id, kind, key)
);

create table if not exists usage_events (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null,
  hotel_id   uuid,
  session_id text not null,
  event      text not null,
  props      jsonb,
  created_at timestamptz not null default now()
);
create index if not exists usage_events_created_idx on usage_events (created_at desc);
create index if not exists usage_events_user_idx    on usage_events (user_id, created_at desc);
create index if not exists usage_events_event_idx   on usage_events (event);

-- ── verification (run after restore/dual-write; compare with Supabase) ──────
-- select 'organizations', count(*) from organizations union all
-- select 'hotels', count(*) from hotels union all
-- select 'hotel_users', count(*) from hotel_users union all
-- select 'briefings', count(*) from briefings union all
-- select 'refresh_commands', count(*) from refresh_commands union all
-- select 'refresh_runs', count(*) from refresh_runs union all
-- select 'intraday_log', count(*) from intraday_log union all
-- select 'insight_feedback', count(*) from insight_feedback union all
-- select 'hotel_prefs', count(*) from hotel_prefs union all
-- select 'push_subscriptions', count(*) from push_subscriptions union all
-- select 'watchlist', count(*) from watchlist union all
-- select 'usage_events', count(*) from usage_events;
