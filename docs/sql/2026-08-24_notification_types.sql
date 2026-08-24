-- Notification types (2026-08-24): per-type preferences + intraday send log.
-- Paste into Supabase SQL Editor. Safe to re-run.
-- Until pasted: morning pushes keep working (all types treated as ON);
-- intraday alert/momentum pushes stay SILENT (the claim insert fails safe).

-- 1) per-subscription notification preferences (morning | alerts | momentum)
alter table push_subscriptions
  add column if not exists notification_prefs jsonb
  default '{"morning": true, "alerts": true, "momentum": true}'::jsonb;

-- 2) intraday dedupe/cap log: at most ONE push per hotel/day/type
create table if not exists intraday_log (
  hotel_id   uuid not null,
  day        date not null,
  type       text not null,          -- 'alerts' | 'momentum'
  created_at timestamptz not null default now(),
  primary key (hotel_id, day, type)
);
alter table intraday_log enable row level security;
-- no policies: service-role only (the backend claims rows before sending)

-- Verify later:
-- select * from intraday_log order by created_at desc limit 10;
-- select notification_prefs from push_subscriptions;
