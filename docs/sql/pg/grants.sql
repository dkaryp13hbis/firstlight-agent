-- FirstLight Postgres grants — idempotent, NO SECRETS (passwords are set by
-- the operator script; this file only shapes what each key can do).
-- Re-apply after EVERY migration:  role setup script or psql -f grants.sql
-- Keys: postgres = builder (migrations, tunnel-only) · fl_app = worker
-- (the API/pipeline) · fl_readonly = visitor (humans/BI, read-only).
-- Decided 2026-09-11 (CIS/OWASP least-privilege core, no-overkill scope).

-- worker: full data access, zero DDL
grant connect on database railway to fl_app;
grant usage on schema public to fl_app;
grant select, insert, update, delete on all tables in schema public to fl_app;
grant usage, select on all sequences in schema public to fl_app;
alter default privileges for role postgres in schema public
  grant select, insert, update, delete on tables to fl_app;
alter default privileges for role postgres in schema public
  grant usage, select on sequences to fl_app;

-- server-side guardrails on the worker (a runaway query must die, not the briefing)
alter role fl_app set statement_timeout = '30s';
alter role fl_app set lock_timeout = '5s';
alter role fl_app set idle_in_transaction_session_timeout = '60s';

-- visitor: read-only, minus secret-bearing columns; hard-capped server-side
grant connect on database railway to fl_readonly;
grant usage on schema public to fl_readonly;
grant select on all tables in schema public to fl_readonly;
alter default privileges for role postgres in schema public
  grant select on tables to fl_readonly;
-- secrets live in hotels: re-grant column-by-column, never the secret ones
-- (api_token, bridge_secret, pms_config, recipient_* stay invisible)
revoke select on hotels from fl_readonly;
grant select (id, org_id, name, slug, total_rooms, timezone, pms_type,
              active, created_at)
  on hotels to fl_readonly;
alter role fl_readonly set default_transaction_read_only = on;
alter role fl_readonly set statement_timeout = '30s';
alter role fl_readonly with connection limit 5;
