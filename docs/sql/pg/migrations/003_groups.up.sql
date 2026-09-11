-- 003: the GROUP layer above companies (user 2026-09-11: e.g. "Myconian
-- Collection" -> 3-4 companies -> 14 hotels). Lifted verbatim from the C3
-- tenancy draft (idempotent — C3's own apply stays compatible).
create table if not exists groups (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  slug        text not null unique,
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);
alter table organizations add column if not exists group_id uuid references groups(id);
create index if not exists organizations_group_idx on organizations (group_id);
