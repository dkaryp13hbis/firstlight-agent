-- push_subscriptions RLS: users manage THEIR OWN subscriptions (2026-08-10).
-- Needed by the bell subscribe/unsubscribe toggle (upsert + delete from the
-- app). The backend sender uses the service role and bypasses RLS.
-- Paste into Supabase SQL Editor. Safe to re-run.

alter table push_subscriptions enable row level security;

drop policy if exists ps_ins on push_subscriptions;
create policy ps_ins on push_subscriptions
  for insert to authenticated with check (user_id = auth.uid());

drop policy if exists ps_upd on push_subscriptions;
create policy ps_upd on push_subscriptions
  for update to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists ps_sel on push_subscriptions;
create policy ps_sel on push_subscriptions
  for select to authenticated using (user_id = auth.uid());

drop policy if exists ps_del on push_subscriptions;
create policy ps_del on push_subscriptions
  for delete to authenticated using (user_id = auth.uid());
