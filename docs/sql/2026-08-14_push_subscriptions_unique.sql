-- push_subscriptions: ONE row per (user_id, hotel_id) — replaces the old
-- one-row-per-USER constraint. Paste into Supabase SQL Editor. Safe to re-run.
--
-- Incident 2026-08-14 / root cause found 2026-08-19:
--   * the table carries  push_subscriptions_user_id_key  = UNIQUE (user_id)
--     -> a user with 2+ hotels can only ever hold ONE subscription row, so
--        the bell "saves" for the first hotel and 23505s on the second
--        (React app: "Notifications on — for 1 hotel" instead of 2;
--         legacy app: upsert on (user_id,hotel_id) 42P10 because no such
--         index existed at all).
--   * the sender (briefing/cloud_push.py) selects by hotel_id, so the second
--     hotel's morning push never had a target.
--
-- 1) Drop the per-user constraint (name as created by Supabase; the DO block
--    tolerates a different name / already-dropped).
do $$
declare c record;
begin
  for c in
    select conname from pg_constraint
    where conrelid = 'push_subscriptions'::regclass
      and contype = 'u'
      and array_length(conkey, 1) = 1
      and conkey[1] = (select attnum from pg_attribute
                       where attrelid = 'push_subscriptions'::regclass and attname = 'user_id')
  loop
    execute format('alter table push_subscriptions drop constraint %I', c.conname);
  end loop;
end $$;

-- 2) Dedupe any (user_id, hotel_id) duplicates (keep one row per pair).
delete from push_subscriptions a
using push_subscriptions b
where a.user_id is not null
  and a.user_id  = b.user_id
  and a.hotel_id = b.hotel_id
  and a.ctid < b.ctid;

-- 3) The constraint both apps upsert against.
create unique index if not exists push_subscriptions_user_hotel_uq
  on push_subscriptions (user_id, hotel_id);

-- 4) Verify (run any time): expect one row per hotel the user has, after
--    toggling the bell OFF then ON again in the app.
-- select user_id, hotel_id, created_at from push_subscriptions order by created_at desc;
