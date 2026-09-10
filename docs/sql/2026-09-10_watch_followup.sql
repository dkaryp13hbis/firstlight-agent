-- Follow-up engine ("one watchlist, two authors", decided 2026-09-10):
-- FirstLight auto-adds month-performance issues to the SAME watchlist as
-- hotel-level rows (user_id NULL, source='firstlight'), keeps their gap
-- current on every refresh, and removes them on recovery (2-day confirm)
-- or after 14 stuck days. Owner rows are never touched.
--
-- PASTE IN THE SUPABASE SQL EDITOR. The identical DDL is applied to the
-- Railway Postgres by Claude via the tunnel (dual-store window).
-- Until pasted, the feature is OFF and everything else works unchanged
-- (schema-tolerant code paths).

alter table watchlist alter column user_id drop not null;

alter table watchlist add column if not exists source         text not null default 'user';
alter table watchlist add column if not exists flagged_date   date;
alter table watchlist add column if not exists first_gap      numeric;
alter table watchlist add column if not exists last_gap       numeric;
alter table watchlist add column if not exists last_gap_date  date;
alter table watchlist add column if not exists resolve_streak int not null default 0;

-- one FirstLight row per hotel+key (episodes are serial, never parallel)
create unique index if not exists watchlist_firstlight_key
  on watchlist (hotel_id, key) where source = 'firstlight';

-- the app reads the watchlist directly (RLS): let any member of the hotel
-- SEE FirstLight rows; only the service key ever writes/deletes them
drop policy if exists watchlist_read_firstlight on watchlist;
create policy watchlist_read_firstlight on watchlist for select
  using (
    source = 'firstlight'
    and exists (select 1 from hotel_users hu
                where hu.hotel_id = watchlist.hotel_id
                  and hu.user_id = auth.uid())
  );

comment on column watchlist.source is
  'user = added by a person (never auto-removed) · firstlight = auto-added by the analyst (self-resolving)';
