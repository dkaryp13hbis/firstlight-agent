-- Insight feedback: 👍/👎 per AI card from the app (feature 2026-07-30).
-- Paste into Supabase Dashboard → SQL Editor → Run. Safe to re-run.

create table if not exists insight_feedback (
  id          uuid primary key default gen_random_uuid(),
  hotel_id    uuid not null,
  report_date date not null,
  card_id     text not null,
  verdict     smallint not null check (verdict in (-1, 1)),
  reason      text,
  user_id     uuid default auth.uid(),
  created_at  timestamptz not null default now()
);

create index if not exists idx_feedback_hotel_date
  on insight_feedback (hotel_id, report_date);

alter table insight_feedback enable row level security;

-- App users (authenticated) may record feedback; nobody reads it from the
-- client — analysis happens backend-side with the service role.
drop policy if exists fb_insert on insight_feedback;
create policy fb_insert on insight_feedback
  for insert to authenticated with check (true);
