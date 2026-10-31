-- Push notifications per DEVICE (2026-10-31, user: "imagine we have a hotel with
-- 10 users 10 phones, they should all get notifications" → "yes go for all fixes now").
--
-- Before: push_subscriptions had UNIQUE (user_id, hotel_id) — a person's second
-- phone silently replaced the first, and the per-type preferences lived on that
-- row (reset to all-on whenever the bell was turned on again).
-- After:  one row per (hotel, device endpoint) — every phone / tablet where the
-- bell is on gets the push; preferences live per (user, hotel) in push_prefs,
-- independent of devices.
--
-- Idempotent. Apply as the database owner:
--   powershell -ExecutionPolicy Bypass -File scripts\pg_migrate.ps1 docs\sql\pg\2026-10-31_push_devices.sql
-- The API tolerates both states: before this runs it keeps the old one-device
-- behaviour (db/store.py falls back), after it the per-device path is used.

-- 1. preferences per person per hotel (copied from the existing rows once)
create table if not exists push_prefs (
  user_id    uuid not null,
  hotel_id   uuid not null references hotels(id),
  prefs      jsonb not null
    default '{"morning": true, "alerts": true, "momentum": true}'::jsonb,
  updated_at timestamptz not null default now(),
  primary key (user_id, hotel_id)
);
insert into push_prefs (user_id, hotel_id, prefs)
  select user_id, hotel_id, notification_prefs from push_subscriptions
  on conflict (user_id, hotel_id) do nothing;

-- 2. one row per hotel per device (keep the newest row if a device appears twice)
delete from push_subscriptions a
 using push_subscriptions b
 where a.hotel_id = b.hotel_id
   and a.subscription->>'endpoint' = b.subscription->>'endpoint'
   and (a.created_at < b.created_at or (a.created_at = b.created_at and a.id < b.id));
alter table push_subscriptions drop constraint if exists push_subscriptions_user_id_hotel_id_key;
create unique index if not exists push_subscriptions_hotel_endpoint
  on push_subscriptions (hotel_id, (subscription->>'endpoint'));

-- 3. the API's data-only role
grant select, insert, update, delete on push_prefs to fl_app;

-- verify
select (select count(*) from push_subscriptions) as devices,
       (select count(*) from push_prefs)         as prefs_rows,
       (select count(*) from pg_indexes where indexname = 'push_subscriptions_hotel_endpoint') as device_index;
