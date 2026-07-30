-- Hotel preferences set from the app (language selector, 2026-07-31).
-- Paste into Supabase Dashboard → SQL Editor → Run. Safe to re-run.

create table if not exists hotel_prefs (
  hotel_id   uuid primary key,
  language   text not null default 'en' check (language in ('en', 'el')),
  updated_at timestamptz not null default now()
);

alter table hotel_prefs enable row level security;

-- App users (authenticated) may read and set their hotel's preferences.
drop policy if exists prefs_select on hotel_prefs;
create policy prefs_select on hotel_prefs
  for select to authenticated using (true);

drop policy if exists prefs_insert on hotel_prefs;
create policy prefs_insert on hotel_prefs
  for insert to authenticated with check (true);

drop policy if exists prefs_update on hotel_prefs;
create policy prefs_update on hotel_prefs
  for update to authenticated using (true) with check (true);
