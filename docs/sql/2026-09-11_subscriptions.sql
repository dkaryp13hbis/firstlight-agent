-- Admin portal (2026-09-11): commercial state per hotel — plan, status,
-- price, renewal. Managed ONLY through the admin portal (service role);
-- no app read/write policies on purpose.
-- PASTE IN THE SUPABASE SQL EDITOR. Claude applies the same DDL to the
-- Railway Postgres via the tunnel.

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

alter table subscriptions enable row level security;
-- no policies: service-role only (the admin portal reads/writes via the API)
