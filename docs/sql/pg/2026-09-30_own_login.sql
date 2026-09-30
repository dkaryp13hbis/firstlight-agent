-- ============================================================================
-- FirstLight — own login system (Phase C3), applied 2026-09-30
-- ============================================================================
-- The auth part of 2026-09-10_tenancy_auth.sql (groups / organizations
-- columns were applied on 2026-09-12 with the portfolio view; users,
-- sessions, memberships and the hotel_access view were not). Idempotent.
--
-- Apply inside the web container (Postgres has no public address):
--   railway ssh -s web -- python scripts/pg_apply.py docs/sql/pg/2026-09-30_own_login.sql
--
-- password_hash format: scrypt$<n>$<r>$<p>$<salt>$<hash> (db/passwords.py,
-- stdlib; the prefix allows argon2id later without a migration).
-- ============================================================================

create extension if not exists pgcrypto;

create table if not exists users (
  id                   uuid primary key default gen_random_uuid(),
  email                text not null,
  password_hash        text not null,
  display_name         text,
  language             text not null default 'en',
  is_platform_admin    boolean not null default false,  -- HBIS staff
  must_change_password boolean not null default true,   -- initial password handed over by phone
  active               boolean not null default true,
  failed_logins        integer not null default 0,
  locked_until         timestamptz,
  created_at           timestamptz not null default now(),
  last_login_at        timestamptz
);
create unique index if not exists users_email_unique on users (lower(email));

create table if not exists sessions (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references users(id) on delete cascade,
  token_hash   text not null unique,   -- sha256 of the opaque bearer token
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,
  last_seen_at timestamptz,
  user_agent   text,
  revoked_at   timestamptz
);
create index if not exists sessions_user_idx on sessions (user_id);

create table if not exists memberships (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references users(id) on delete cascade,
  scope_type text not null check (scope_type in ('group', 'org', 'hotel')),
  scope_id   uuid not null,
  role       text not null default 'viewer' check (role in ('owner', 'viewer')),
  created_at timestamptz not null default now(),
  unique (user_id, scope_type, scope_id)
);
create index if not exists memberships_scope_idx on memberships (scope_type, scope_id);

-- Every membership resolved to the hotels it grants (one query, no recursion).
create or replace view hotel_access as
  select m.user_id, h.id as hotel_id, m.role, 'hotel'::text as via
    from memberships m
    join hotels h on m.scope_type = 'hotel' and h.id = m.scope_id
  union
  select m.user_id, h.id, m.role, 'org'
    from memberships m
    join hotels h on m.scope_type = 'org' and h.org_id = m.scope_id
  union
  select m.user_id, h.id, m.role, 'group'
    from memberships m
    join organizations o on m.scope_type = 'group' and o.group_id = m.scope_id
    join hotels h on h.org_id = o.id;

comment on table hotel_users is 'DEPRECATED 2026-09-10: superseded by memberships + hotel_access; drop after the C3 parallel-run.';

-- Roles (grants.sql, 2026-09-11): this file runs as postgres (builder); the
-- API's fl_app role receives DML on the new tables through the existing
-- ALTER DEFAULT PRIVILEGES. The read-only visitor role must never see
-- password or session hashes:
revoke select on users from fl_readonly;
grant select (id, email, display_name, language, is_platform_admin, must_change_password,
              active, failed_logins, locked_until, created_at, last_login_at)
  on users to fl_readonly;
revoke select on sessions from fl_readonly;
