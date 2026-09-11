-- 002: client registry (user 2026-09-11 — company, VAT, contact, rates).
-- Company = organizations (existing); contracts = commercial terms per
-- company (supersedes hotel-level subscriptions per ADMIN_PLAN D2).
-- PG-only: the portal is the sole consumer; the nightly Supabase mirror
-- upserts only base columns so these fields are never overwritten.
alter table organizations add column if not exists legal_name    text;
alter table organizations add column if not exists vat_number    text;
alter table organizations add column if not exists country       text not null default 'GR';
alter table organizations add column if not exists contact_name  text;
alter table organizations add column if not exists contact_phone text;
create unique index if not exists organizations_vat_unique
  on organizations (country, vat_number) where vat_number is not null;

create table if not exists contracts (
  org_id         uuid primary key references organizations(id) on delete cascade,
  status         text not null default 'trial',   -- trial | active | suspended | ended
  start_date     date,
  monthly_eur    numeric,
  annual_eur     numeric,
  billing_anchor date,
  notes          text,
  updated_at     timestamptz not null default now()
);
