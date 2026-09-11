# Quarterly security self-check (10 minutes)

*Adapted 2026-09-11 from the external PostgreSQL security guide to our
Railway/Cloudflare stack. Run every quarter (next: Dec 2026); anything
that surprises you becomes the next ticket. Claude runs the SQL parts
through the tunnel on request: "run the quarterly security check".*

## Database (through the tunnel, as postgres)

```sql
-- 1. the app is not the superuser; roles look right
select rolname, rolsuper, rolcreaterole, rolcreatedb, rolconnlimit
from pg_roles where rolname in ('postgres', 'fl_app', 'fl_readonly');
-- expect: only postgres has any true flags; fl_readonly connlimit 5

-- 2. TLS is on
show ssl;                       -- on

-- 3. worker guardrails hold
select rolname, rolconfig from pg_roles
where rolname in ('fl_app', 'fl_readonly');
-- expect statement_timeout=30s on both; read_only=on on fl_readonly

-- 4. visitor cannot see secrets (must ERROR)
--   run as fl_readonly:  select api_token from hotels limit 1;
```

## Platform & machine

- Railway → Postgres service → Settings: **no public networking / TCP
  proxy enabled** (private-only stays private).
- Railway → web service variables: `DATABASE_URL` uses **fl_app**, not
  postgres; `STORAGE=pg`.
- `icacls C:\Users\dkary\.ssh\id_ed25519` → only `DKMACHINE\dkary:(R)`
  (the 2026-09-11 tunnel outage was this drifting).
- `C:\FirstLightBackups` exists, newest backup < 48h old, and BitLocker
  is on for the drive.
- Vendor 2FA still on: GitHub, Railway, Cloudflare, Anthropic, Supabase
  (until retired).

## Repo greps (must return nothing)

```bash
grep -rE "sslmode=disable" --include="*.py" .
grep -rE "postgresql://postgres:[^@]+@" --include="*.py" docs/ scripts/
git log -S"--no-verify" --oneline -5
```

## Process

- `docs/sql/pg/grants.sql` re-applied after every migration (roles see
  new tables only through it).
- Any credential that appears anywhere it shouldn't → rotate same day
  (DB passwords: `setup_roles` script re-run resets them; hotel API
  tokens: portal → Hotels → Rotate).
- Restore drill: last done Sep 2026 — repeat at least twice a year.

## Deferred controls & their triggers (decided 2026-09-11 — not forgotten)

| Control | Trigger to implement |
|---|---|
| pgaudit / DB-level connection logging | first ops hire OR an enterprise client requires it |
| Row-Level Security in PG | any direct SQL surface for users/BI appears |
| pip-audit + gitleaks in CI | second person committing code |
| Scrubbed restore images | guest-level or PII data ever enters our cloud (design says never) |
| KMS-encrypted third-party credentials | we start storing replayable partner API keys beyond pms_config |
