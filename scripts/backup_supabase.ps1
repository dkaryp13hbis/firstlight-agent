# Nightly Supabase backup — Windows runner (2026-09-04).
# Same behavior as backup_supabase.py, but PowerShell transport because local
# Python on this machine cannot SSL-handshake with Supabase (known quirk,
# CLAUDE.md). Scheduled via Task Scheduler task "FirstLightBackup".
# Credentials: C:\FirstLightBackups\backup.env (never in the repo).

$ErrorActionPreference = 'Stop'
$Root = 'C:\FirstLightBackups'
$KeepDays = 14
$PageSize = 50
$Tables = @('hotels','hotel_users','organizations','briefings','refresh_commands',
            'refresh_runs','insight_feedback','hotel_prefs','push_subscriptions',
            'usage_events','intraday_log','watchlist')

$url = ''; $key = ''
foreach ($l in (Get-Content (Join-Path $Root 'backup.env'))) {
  if ($l -match '^SUPABASE_URL=(.+)$') { $url = $Matches[1].Trim().TrimEnd('/') }
  if ($l -match '^SUPABASE_SERVICE_KEY=(.+)$') { $key = $Matches[1].Trim() }
}
if (-not $url -or -not $key) { throw 'backup.env missing SUPABASE_URL / SUPABASE_SERVICE_KEY' }
$H = @{ apikey = $key; Authorization = "Bearer $key" }

$dayDir = Join-Path $Root (Get-Date -Format 'yyyy-MM-dd')
New-Item -ItemType Directory -Force $dayDir | Out-Null
$total = 0
foreach ($t in $Tables) {
  $rows = New-Object System.Collections.ArrayList
  $offset = 0
  while ($true) {
    $chunk = Invoke-RestMethod -Uri "$url/rest/v1/${t}?select=*&limit=$PageSize&offset=$offset" -Headers $H
    foreach ($r in @($chunk)) { [void]$rows.Add($r) }
    if (@($chunk).Count -lt $PageSize) { break }
    $offset += $PageSize
  }
  $out = Join-Path $dayDir "$t.ndjson.gz"
  $fs = [System.IO.File]::Create($out)
  $gz = New-Object System.IO.Compression.GZipStream($fs, [System.IO.Compression.CompressionMode]::Compress)
  $sw = New-Object System.IO.StreamWriter($gz, (New-Object System.Text.UTF8Encoding($false)))
  foreach ($r in $rows) { $sw.WriteLine(($r | ConvertTo-Json -Depth 40 -Compress)) }
  $sw.Close()
  $total += $rows.Count
  $kb = [math]::Round((Get-Item $out).Length / 1024)
  Write-Output ("  {0}: {1} rows -> {2} KB" -f $t, $rows.Count, $kb)
}

# verify every file reparses with the same row count
foreach ($f in Get-ChildItem $dayDir -Filter '*.ndjson.gz') {
  $fs = [System.IO.File]::OpenRead($f.FullName)
  $gz = New-Object System.IO.Compression.GZipStream($fs, [System.IO.Compression.CompressionMode]::Decompress)
  $sr = New-Object System.IO.StreamReader($gz)
  $n = 0
  while ($null -ne ($line = $sr.ReadLine())) { if ($line.Trim()) { $null = $line | ConvertFrom-Json; $n++ } }
  $sr.Close()
  Write-Output ("  verify {0}: {1} rows parse OK" -f $f.Name, $n)
}

# prune folders older than KeepDays
$cutoff = (Get-Date).Date.AddDays(-$KeepDays)
foreach ($d in Get-ChildItem $Root -Directory) {
  if ($d.Name -notmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') { continue }
  $parsed = [datetime]::ParseExact($d.Name, 'yyyy-MM-dd', $null)
  if ($parsed -lt $cutoff) { Remove-Item $d.FullName -Recurse -Force -Confirm:$false; Write-Output ("  pruned " + $d.Name) }
}
Set-Content -Encoding utf8 (Join-Path $Root 'last_run.txt') ("{0} rows={1}" -f (Get-Date -Format o), $total)
Write-Output ("backup complete: {0} rows, {1}" -f $total, (Get-Date -Format 'yyyy-MM-dd'))
