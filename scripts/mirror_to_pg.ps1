# Daily Supabase->Postgres mirror (Phase C dual-write window, 2026-09-05).
# Runs at 08:40 local, right after the 08:30 backup: mirrors today's dump into
# Railway Postgres through the encrypted CLI tunnel, so app-written tables
# (watchlist, feedback, usage, push) stay in sync until C2 moves them.
# Retire this task when STORAGE=pg + C2 complete.
# Task Scheduler task: FirstLightMirror.

$ErrorActionPreference = 'Stop'
$Repo = 'C:\Users\dkary\Downloads\hotel-morning-briefing'
$Log = "C:\FirstLightBackups\mirror.log"
$Day = Get-Date -Format 'yyyy-MM-dd'
$TunnelLog = Join-Path $env:TEMP 'fl_pg_tunnel.log'

function Say($m) { Add-Content -Encoding utf8 $Log ("{0} {1}" -f (Get-Date -Format o), $m); Write-Output $m }

if (-not (Test-Path "C:\FirstLightBackups\$Day")) { Say "no backup for $Day - abort"; exit 1 }

Set-Location $Repo
Remove-Item $TunnelLog -ErrorAction SilentlyContinue
$tunnel = Start-Process -FilePath "C:\Users\dkary\AppData\Roaming\npm\node_modules\@railway\cli\bin\railway.exe" -ArgumentList @('connect', 'Postgres', '--tunnel-only') `
  -RedirectStandardOutput $TunnelLog -RedirectStandardError "$TunnelLog.err" -NoNewWindow -PassThru
try {
  $port = $null
  for ($i = 0; $i -lt 20 -and -not $port; $i++) {
    Start-Sleep -Seconds 3
    foreach ($f in @($TunnelLog, "$TunnelLog.err")) {   # banner goes to stderr
      if ($port -or -not (Test-Path $f)) { continue }
      $m = Select-String -Path $f -Pattern 'Port:\s+(\d+)' -ErrorAction SilentlyContinue
      if ($m) { $port = $m.Matches[0].Groups[1].Value }
    }
  }
  if (-not $port) { Say "tunnel did not open - abort"; exit 1 }
  Say "tunnel on port $port"

  $pw = ''
  foreach ($l in (Get-Content 'C:\FirstLightBackups\pg.env')) {
    if ($l -match '^PGPASSWORD=(.+)$') { $pw = $Matches[1].Trim() }
  }
  $env:DATABASE_URL = "postgresql://postgres:${pw}@127.0.0.1:${port}/railway"
  $env:STORAGE = 'dual'
  $env:PYTHONIOENCODING = 'utf-8'

  $py = @"
import gzip, json, sys
from pathlib import Path
from datetime import date
sys.path.insert(0, r'$Repo')
from db import store
d = Path(r'C:\FirstLightBackups') / '$Day'
order = ['organizations', 'hotels', 'hotel_users', 'briefings', 'refresh_commands',
         'refresh_runs', 'intraday_log', 'insight_feedback', 'hotel_prefs',
         'push_subscriptions', 'watchlist', 'usage_events']
total = 0
for t in order:
    f = d / (t + '.ndjson.gz')
    if not f.exists():
        continue
    with gzip.open(f, 'rt', encoding='utf-8') as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    total += store.mirror_rows(t, rows)
print('mirrored', total, 'rows')
"@
  # no 2>&1 here: PS5.1 wraps native stderr into ErrorRecords and, with
  # ErrorActionPreference=Stop, harmless psycopg pool warnings became a throw
  $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  $out = $py | py -3.13 -
  $ErrorActionPreference = $prev
  Say ("mirror: " + ($out -join ' | '))
} finally {
  if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -Confirm:$false }
  Say "tunnel closed"
}
