-- ============================================================================
-- FirstLight — tenancy hierarchy + own login system (decided 2026-09-10)
-- ============================================================================
-- Group (shared owners) → Company (legal entity, unique VAT) → Hotel.
-- Users authenticate against OUR tables (no Supabase Auth): users, sessions,
-- memberships. A membership at ANY level grants every hotel below it; the
-- hotel_access view resolves that for the API (one query, no recursion).
-- No email channel exists: initial passwords are handed over by phone.
--
-- Idempotent. Apply on the Railway instance (private, via CLI tunnel):
--   psql "$DATABASE_URL" -f docs/sql/pg/2026-09-10_tenancy_auth.sql
-- schema.sql carries the same DDL for fresh provisioning.
-- ============================================================================

create extension if not exists pgcrypto;

-- ── groups: parent above companies (same people own several companies) ─────

create table if not exists groups (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  slug        text not null unique,
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

-- ── organizations = companies (the legal entity we invoice) ─────────────────

alter table organizations add column if not exists group_id   uuid references groups(id);
alter table organizations add column if not exists vat_number text;
alter table organizations add column if not exists legal_name text;
alter table organizations add column if not exists country    text not null default 'GR';
create unique index if not exists organizations_vat_unique
  on organizations (country, vat_number) where vat_number is not null;
create index if not exists organizations_group_idx on organizations (group_id);

-- ── own auth ────────────────────────────────────────────────────────────────

create table if not exists users (
  id                   uuid primary key default gen_random_uuid(),
  email                text not null,
  password_hash        text not null,            -- scrypt$n$r$p$salt$hash (db/passwords.py); never plaintext
  display_name         text,
  language             text not null default 'en',
  is_platform_admin    boolean not null default false,  -- HBIS staff: sees every hotel
  must_change_password boolean not null default true,   -- initial password handed over by admin
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
  token_hash   text not null unique,   -- sha256 of the opaque bearer token; raw token lives only on the device
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

-- Resolves every membership to the hotels it grants. The API checks
--   select 1 from hotel_access where user_id = ? and hotel_id = ?
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

-- ── deprecations (columns stay; code must tolerate them) ────────────────────

comment on table  hotel_users            is 'DEPRECATED 2026-09-10: superseded by memberships + hotel_access; drop after Phase C3.';
comment on column hotels.recipient_email is 'DEPRECATED 2026-09-10: no email channel — push + app only.';
comment on column hotels.recipient_name  is 'DEPRECATED 2026-09-10: no email channel — push + app only.';

-- ── manual admin recipes (until scripts/onboard_hotel.py exists) ────────────
-- New company (skip if the VAT already exists):
--   insert into organizations (name, legal_name, vat_number, country, slug)
--   values ('Pome Hotels', 'POME HOTELS AE', '123456789', 'GR', 'pome');
-- Shared owners across companies:
--   insert into groups (name, slug) values ('Papadopoulos family', 'papadopoulos');
--   update organizations set group_id = '<group uuid>' where id in ('<org a>', '<org b>');
-- Move a hotel to another company (history stays on the hotel id):
--   update hotels set org_id = '<new org uuid>' where id = '<hotel uuid>';
-- New user + access (password_hash from the API's hashing helper):
--   insert into users (email, password_hash, display_name, language) values (...);
--   insert into memberships (user_id, scope_type, scope_id, role)
--   values ('<user>', 'org', '<org uuid>', 'owner');       -- or ('hotel', <hotel uuid>, 'viewer')
-- Import existing Supabase Auth users keeping the SAME uuid so feedback /
-- watchlist / push_subscriptions rows stay joinable:
--   insert into users (id, email, password_hash, must_change_password)
--   values ('<auth.users.id>', '<email>', '<hash of a fresh initial password>', true);
