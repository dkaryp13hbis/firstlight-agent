-- 001: append-only audit of every superadmin action (ADMIN_PLAN §10).
-- Actor is the verified admin email pre-C3; becomes users.id after C3.
create table if not exists admin_audit (
  id          bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  admin_email text not null,
  action      text not null,           -- e.g. hotel.refresh, hotel.token_rotate
  target_type text,
  target_id   text,
  before      jsonb,
  after       jsonb,
  reason      text
);
create index if not exists admin_audit_at_idx on admin_audit (at desc);
create index if not exists admin_audit_target_idx on admin_audit (target_id, at desc);
