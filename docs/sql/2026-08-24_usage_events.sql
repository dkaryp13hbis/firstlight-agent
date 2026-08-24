-- usage_events: first-party app usage tracking (2026-08-24).
-- The React app batches events and inserts them directly (anon key + RLS).
-- For now the client only tracks the demo account (demo@hbis.io) — the
-- gate is client-side; widening to all users is a one-line app change.
-- Paste into Supabase SQL Editor. Safe to re-run.

create table if not exists usage_events (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null,
  hotel_id    uuid,
  session_id  text not null,            -- client-generated per app open
  event       text not null,            -- app_open, tab_nav, share, refresh, ...
  props       jsonb,                    -- small event payload (tab name, setting value, ...)
  created_at  timestamptz not null default now()
);

create index if not exists usage_events_created_idx on usage_events (created_at desc);
create index if not exists usage_events_user_idx    on usage_events (user_id, created_at desc);
create index if not exists usage_events_event_idx   on usage_events (event);

alter table usage_events enable row level security;

-- app users may WRITE their own events, never read anything back
drop policy if exists ue_ins on usage_events;
create policy ue_ins on usage_events
  for insert to authenticated with check (user_id = auth.uid());
-- (no select/update/delete policies: reads are service-role only)

-- Verify after browsing the app on the demo account:
-- select event, props, created_at from usage_events order by created_at desc limit 20;
