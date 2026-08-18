-- push_subscriptions: unique (user_id, hotel_id) — fixes dead bell upserts.
-- Incident 2026-08-14: the PWA saves subscriptions with
--   upsert(..., { onConflict: 'user_id,hotel_id' })   (since 2026-06-14)
-- but no unique constraint ever existed -> every save fails 42P10 and the
-- PWA swallowed the error. Old rows from the pre-upsert era kept working
-- until the bell toggle (2026-08-10) deleted them on unsubscribe; the
-- re-subscribe then had nothing to save -> notifications stopped.
-- Paste into Supabase SQL Editor. Safe to re-run.

-- 1) Dedupe any (user_id, hotel_id) duplicates (keep one row per pair).
--    Legacy rows with NULL user_id are untouched (NULLs never conflict).
delete from push_subscriptions a
using push_subscriptions b
where a.user_id is not null
  and a.user_id  = b.user_id
  and a.hotel_id = b.hotel_id
  and a.ctid < b.ctid;

-- 2) The constraint the upsert has needed since June.
create unique index if not exists push_subscriptions_user_hotel_uq
  on push_subscriptions (user_id, hotel_id);

-- 3) Verify: expect at least one row again AFTER re-toggling the bell in
--    the app (this SELECT is just a health check, run any time).
-- select count(*) as rows, count(user_id) as with_user
-- from push_subscriptions;
