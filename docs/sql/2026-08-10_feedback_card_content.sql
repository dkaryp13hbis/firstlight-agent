-- Feedback rows carry the FULL insight card JSON they rate (2026-08-10).
-- Denormalized on purpose: briefings rows get overwritten by later
-- refreshes, and analyst-guideline reviews should read one table with the
-- exact text the user saw. Paste into Supabase SQL Editor. Safe to re-run.

alter table insight_feedback add column if not exists card_content jsonb;
