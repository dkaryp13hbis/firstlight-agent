-- My Watchlist (2026-08-24): items a user pins — a stay month or a date
-- range — reported on every refresh by the app (deterministic, no Claude).
-- Paste into Supabase Dashboard → SQL Editor → Run. Safe to re-run.
-- Until pasted: the app hides the watchlist section (reads fail → null) and
-- "Watch" actions toast "Watchlist not available yet".
--
-- Per USER (not per hotel): GM and revenue manager watch different things;
-- a multi-hotel owner watches per property. Spec: docs/WATCHLIST_SPEC.md

create table if not exists watchlist (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid(),
  hotel_id    uuid not null,
  kind        text not null check (kind in ('month', 'range')),
  key         text not null,               -- 'YYYY-MM' | 'YYYY-MM-DD..YYYY-MM-DD'
  label       text,                        -- optional ("Board meeting", "Wedding")
  note        text,                        -- reserved: Decision Journal seed
  created_at  timestamptz not null default now(),
  unique (user_id, hotel_id, kind, key)
);

create index if not exists watchlist_hotel_idx on watchlist (hotel_id, user_id);

alter table watchlist enable row level security;

-- Own rows only. The service role (backend, future push line) bypasses RLS.
drop policy if exists wl_select on watchlist;
create policy wl_select on watchlist
  for select to authenticated using (user_id = auth.uid());

drop policy if exists wl_insert on watchlist;
create policy wl_insert on watchlist
  for insert to authenticated with check (user_id = auth.uid());

drop policy if exists wl_update on watchlist;
create policy wl_update on watchlist
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists wl_delete on watchlist;
create policy wl_delete on watchlist
  for delete to authenticated using (user_id = auth.uid());

-- verify:
-- select kind, key, label, created_at from watchlist order by created_at desc limit 20;
