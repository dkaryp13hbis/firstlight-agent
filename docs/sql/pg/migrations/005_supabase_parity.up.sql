-- 005: Postgres parity with the Supabase-era tables (2026-10-28, Supabase
-- retirement — user: "lets totally kill supabase, everything should go to
-- postgresql"). Production already has all of this (applied by hand through
-- the tunnel in Sep 2026, per docs/sql/2026-09-10_watch_followup.sql and
-- docs/sql/2026-09-11_subscriptions.sql); this file makes a fresh database
-- built from schema.sql + migrations identical. Idempotent — safe on prod.

-- hotel-level commercial state (superadmin Clients; fallback for Finance)
create table if not exists subscriptions (
  hotel_id   uuid primary key references hotels(id) on delete cascade,
  plan       text not null default 'trial',    -- trial | monthly | annual
  status     text not null default 'active',   -- active | paused | cancelled
  price_eur  numeric,
  started_on date,
  renews_on  date,
  notes      text,
  updated_at timestamptz not null default now()
);

-- follow-up engine rows on the watchlist ("one watchlist, two authors")
alter table watchlist alter column user_id drop not null;
alter table watchlist add column if not exists source         text not null default 'user';
alter table watchlist add column if not exists flagged_date   date;
alter table watchlist add column if not exists first_gap      numeric;
alter table watchlist add column if not exists last_gap       numeric;
alter table watchlist add column if not exists last_gap_date  date;
alter table watchlist add column if not exists resolve_streak int not null default 0;
create unique index if not exists watchlist_firstlight_key
  on watchlist (hotel_id, key) where source = 'firstlight';
