<#
.SYNOPSIS
    FirstLight Hotel Agent — One-time installer
    Run on the hotel server (or any always-on Windows machine on the hotel network).
    Installs the agent to C:\FirstLight and schedules a daily run via Task Scheduler.
#>

$ErrorActionPreference = "Stop"
$INSTALL_DIR  = "C:\FirstLight"
$TASK_NAME    = "FirstLight Morning Briefing"
$LOG_FILE     = "$INSTALL_DIR\install.log"
$AGENT_ZIP    = "https://github.com/dkaryp13hbis/firstlight-agent/archive/refs/heads/main.zip"

# ── Helpers ───────────────────────────────────────────────────────────
function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Add-Content -Path $LOG_FILE -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Host "  $msg"
}
function Step($n, $label) {
    Write-Host ""
    Write-Host "  [$n] $label" -ForegroundColor Cyan
}
function Ok($msg)   { Write-Host "      OK — $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "      WARN — $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "      FAIL — $msg" -ForegroundColor Red; Log "FAIL: $msg" }

function Ask($prompt, $default = "", [switch]$Secret) {
    $hint = if ($default) { " (default: $default)" } else { "" }
    Write-Host "      $prompt$hint : " -NoNewline -ForegroundColor White
    if ($Secret) {
        $ss = Read-Host -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                     [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss))
        return $plain
    }
    $v = Read-Host
    if ($v) { return $v } else { return $default }
}

# ── Banner ────────────────────────────────────────────────────────────
Clear-Host
Write-Host ""
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host "    FirstLight Hotel Agent  —  Setup" -ForegroundColor Cyan
Write-Host "    Hotel Morning Briefing v1.0" -ForegroundColor Cyan
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This will install the FirstLight agent on this machine" -ForegroundColor DarkGray
Write-Host "  and schedule it to run every morning automatically." -ForegroundColor DarkGray
Write-Host ""

New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null
Log "Install started"

# ── Step 1: Python ────────────────────────────────────────────────────
Step 1 "Checking Python 3..."
$python = $null
foreach ($cmd in @("py -3", "python3", "python")) {
    try {
        $ver = Invoke-Expression "$cmd --version 2>&1"
        if ($ver -match "Python 3\.(\d+)\." -and [int]$Matches[1] -ge 8) {
            $python = $cmd
            Ok "$ver found"
            break
        }
    } catch {}
}

if (-not $python) {
    Warn "Python 3.8+ not found — downloading Python 3.11..."
    $pyUrl  = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    $pyExe  = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri $pyUrl -OutFile $pyExe -UseBasicParsing
    Start-Process -FilePath $pyExe -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
    $python = "python"
    Ok "Python 3.11 installed"
}
Log "Python: $python"

# ── Step 2: ODBC Driver ───────────────────────────────────────────────
Step 2 "Checking ODBC Driver 17 for SQL Server..."
$odbcInstalled = Get-ItemProperty "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server" -ErrorAction SilentlyContinue
if ($odbcInstalled) {
    Ok "Already installed"
} else {
    Warn "Not found — downloading ODBC Driver 17..."
    $odbcUrl = "https://go.microsoft.com/fwlink/?linkid=2249004"
    $odbcMsi = "$env:TEMP\msodbcsql17.msi"
    Invoke-WebRequest -Uri $odbcUrl -OutFile $odbcMsi -UseBasicParsing
    Start-Process msiexec -ArgumentList "/i `"$odbcMsi`" /quiet IACCEPTMSODBCSQLLICENSETERMS=YES" -Wait
    Ok "ODBC Driver 17 installed"
}
Log "ODBC Driver: OK"

# ── Step 3: Hotel configuration ───────────────────────────────────────
Step 3 "Hotel configuration"
Write-Host "      (Your FirstLight API key was provided when you signed up)" -ForegroundColor DarkGray
Write-Host ""

$SQL_SERVER    = Ask "PMS SQL Server IP or hostname"    "192.168.1.100"
$SQL_DATABASE  = Ask "SQL database name"                "bidata"
$SQL_TRUSTED   = Ask "Windows authentication? (yes/no)" "no"
if ($SQL_TRUSTED -eq "yes") {
    $SQL_USER = ""; $SQL_PASSWORD = ""
} else {
    $SQL_USER     = Ask "SQL username"     "sa"
    $SQL_PASSWORD = Ask "SQL password"     "" -Secret
}
$HOTEL_ID      = Ask "Protel Hotel ID (number)"         "1"
$HOTEL_NAME    = Ask "Hotel display name"               "My Hotel"
$TOTAL_ROOMS   = Ask "Total rooms"                      "100"
$RECIP_EMAIL   = Ask "Morning report recipient email"
$RECIP_NAME    = Ask "Recipient name"                   "General Manager"
$SMTP_HOST     = Ask "SMTP server"                      "smtp.gmail.com"
$SMTP_PORT     = Ask "SMTP port"                        "587"
$SMTP_USER     = Ask "SMTP username (sender email)"
$SMTP_PASS     = Ask "SMTP password / app password"     "" -Secret
$ANTHROPIC_KEY = Ask "Anthropic API key (leave blank to skip AI)"
$FL_API_URL    = Ask "FirstLight API URL"               "https://web-production-61c4d.up.railway.app"
$FL_API_KEY    = Ask "FirstLight API key"               "" -Secret
$RUN_TIME      = Ask "Daily run time (24h format)"      "06:30"

Log "Config collected for hotel: $HOTEL_NAME"

# ── Step 4: Download agent ────────────────────────────────────────────
Step 4 "Installing FirstLight agent files..."
$zipPath     = "$env:TEMP\firstlight-agent.zip"
$extractPath = "$env:TEMP\firstlight-extract"

$useLocal = $false
if (Test-Path "$PSScriptRoot\firstlight-agent.zip") {
    # Prefer a local ZIP if bundled alongside this installer
    $zipPath  = "$PSScriptRoot\firstlight-agent.zip"
    $useLocal = $true
    Ok "Using bundled agent ZIP"
} else {
    Write-Host "      Downloading from GitHub..." -ForegroundColor DarkGray
    try {
        Invoke-WebRequest -Uri $AGENT_ZIP -OutFile $zipPath -UseBasicParsing
        Ok "Downloaded"
    } catch {
        Fail "Could not download agent: $_"
        Write-Host ""
        Write-Host "  Place firstlight-agent.zip next to this script and re-run." -ForegroundColor Yellow
        exit 1
    }
}

if (Test-Path $extractPath) { Remove-Item $extractPath -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
$srcDir = (Get-ChildItem $extractPath -Directory | Select-Object -First 1).FullName
Copy-Item "$srcDir\*" -Destination $INSTALL_DIR -Recurse -Force
Ok "Agent files installed to $INSTALL_DIR"
$src_label = if ($useLocal) { 'local zip' } else { 'GitHub' }
Log "Agent files extracted from: $src_label"

# ── Step 5: Write .env ────────────────────────────────────────────────
Step 5 "Writing configuration..."
@"
# FirstLight Agent — Hotel Configuration
# Generated by installer on $(Get-Date -Format 'yyyy-MM-dd HH:mm')

SQL_SERVER=$SQL_SERVER
SQL_DATABASE=$SQL_DATABASE
SQL_TRUSTED=$SQL_TRUSTED
SQL_USER=$SQL_USER
SQL_PASSWORD=$SQL_PASSWORD

HOTEL_ID=$HOTEL_ID
HOTEL_NAME=$HOTEL_NAME
HOTEL_TOTAL_ROOMS=$TOTAL_ROOMS

RECIPIENT_EMAIL=$RECIP_EMAIL
RECIPIENT_NAME=$RECIP_NAME
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_USER=$SMTP_USER
SMTP_PASSWORD=$SMTP_PASS

ANTHROPIC_API_KEY=$ANTHROPIC_KEY

FIRSTLIGHT_API_URL=$FL_API_URL
FIRSTLIGHT_API_KEY=$FL_API_KEY
"@ | Out-File -FilePath "$INSTALL_DIR\.env" -Encoding utf8 -Force
Ok ".env written"
Log ".env written"

# ── Step 6: Python packages ───────────────────────────────────────────
Step 5 "Installing Python packages..."  # intentional re-use of label for display
try {
    Invoke-Expression "$python -m pip install --quiet --upgrade pip" | Out-Null
    Invoke-Expression "$python -m pip install --quiet -r `"$INSTALL_DIR\requirements.txt`""
    Ok "All packages installed"
    Log "pip install: OK"
} catch {
    Fail "pip install failed: $_"
    exit 1
}

# ── Step 7: Test DB connection ─────────────────────────────────────────
Step 6 "Testing database connection..."
$testScript = @"
import sys, os
sys.path.insert(0, r'$INSTALL_DIR')
os.chdir(r'$INSTALL_DIR')
from dotenv import load_dotenv
load_dotenv()
from db.connection import get_connection
try:
    conn = get_connection()
    conn.close()
    print('OK')
except Exception as e:
    print(f'FAIL:{e}')
"@
$testResult = Invoke-Expression "$python -c `"$($testScript -replace '"','\"')`"" 2>&1
if ("$testResult" -match "^OK") {
    Ok "Database connected successfully"
    Log "DB test: OK"
} else {
    Warn "Database test failed: $testResult"
    Warn "Check SQL_SERVER, credentials, and Windows Firewall."
    Warn "You can still continue — the agent will retry at the scheduled time."
    Log "DB test FAILED: $testResult"
}

# ── Step 8: Register scheduled task ───────────────────────────────────
Step 6 "Registering Windows scheduled task..."
$timeParts = $RUN_TIME.Split(":")
$taskHour  = [int]$timeParts[0]
$taskMin   = [int]$timeParts[1]
$taskTime  = "{0:D2}:{1:D2}" -f $taskHour, $taskMin

# Resolve full python path
$pythonFull = Invoke-Expression "$python -c `"import sys; print(sys.executable)`"" 2>&1

$action   = New-ScheduledTaskAction `
    -Execute $pythonFull `
    -Argument "main.py" `
    -WorkingDirectory $INSTALL_DIR
$trigger  = New-ScheduledTaskTrigger -Daily -At "$taskTime"
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable   # catches missed runs (e.g. server was off at 6:30)
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName  $TASK_NAME `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

Ok "Task registered — runs daily at $taskTime"
Log "Scheduled task: $TASK_NAME at $taskTime"

# ── Done ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host "    Installation complete!" -ForegroundColor Green
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Hotel    : $HOTEL_NAME" -ForegroundColor White
Write-Host "  Runs at  : $taskTime every day" -ForegroundColor White
Write-Host "  Location : $INSTALL_DIR" -ForegroundColor White
Write-Host "  Log      : $LOG_FILE" -ForegroundColor White
Write-Host ""
Write-Host "  To run manually right now:" -ForegroundColor DarkGray
Write-Host "    cd $INSTALL_DIR && $pythonFull main.py --preview" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  To update the agent in future, re-run this installer." -ForegroundColor DarkGray
Write-Host ""
Log "Install complete. Hotel=$HOTEL_NAME RunTime=$taskTime"
