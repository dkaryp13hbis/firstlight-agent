# Apply a SQL migration to the Railway Postgres AS THE DATABASE OWNER.
#
# The web container connects as fl_app (data-only, no DDL — grants.sql), and the
# database has no public address, so migrations run through psql INSIDE the
# Postgres container, whose own DATABASE_URL carries the builder credentials.
# Nothing is copied to the laptop: the file is base64-encoded into the command.
#
# From Command Prompt or PowerShell, in the backend folder:
#   powershell -ExecutionPolicy Bypass -File scripts\pg_migrate.ps1 docs\sql\pg\<file>.sql
#
# Idempotent files only (create ... if not exists / create or replace).
param([Parameter(Mandatory = $true)][string]$SqlFile)

$path = (Resolve-Path $SqlFile).Path
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($path))
Write-Host "applying $SqlFile ($([IO.FileInfo]::new($path).Length) bytes) as the database owner..."
# The backtick keeps $DATABASE_URL for the container's shell (not PowerShell).
railway ssh -s Postgres -- sh -c "echo $b64 | base64 -d | psql -X -v ON_ERROR_STOP=1 `$DATABASE_URL"
if ($LASTEXITCODE -ne 0) { Write-Host "FAILED (exit $LASTEXITCODE)"; exit $LASTEXITCODE }
Write-Host "done."
