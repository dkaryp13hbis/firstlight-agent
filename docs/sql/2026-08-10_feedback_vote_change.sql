-- Feedback vote changing: one row per user per card per day, last word wins
-- (2026-08-10). The app upserts on (hotel_id, report_date, card_id, user_id)
-- so a GM can revise a vote; guideline analysis reads the FINAL opinion.
-- Paste into Supabase SQL Editor. Safe to re-run.

-- keep only the newest vote per user+card+day before adding the constraint
delete from insight_feedback a
using insight_feedback b
where a.hotel_id = b.hotel_id
  and a.report_date = b.report_date
  and a.card_id = b.card_id
  and coalesce(a.user_id::text, '') = coalesce(b.user_id::text, '')
  and a.created_at < b.created_at;

create unique index if not exists uq_feedback_user_card
  on insight_feedback (hotel_id, report_date, card_id, user_id);

-- users may update THEIR OWN feedback (needed by the upsert path)
drop policy if exists fb_update on insight_feedback;
create policy fb_update on insight_feedback
  for update to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
