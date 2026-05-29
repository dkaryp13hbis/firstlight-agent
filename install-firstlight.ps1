param()
$ErrorActionPreference = "Stop"
$INSTALL_DIR = "C:\FirstLight"
$TASK_NAME   = "FirstLight Morning Briefing"
$AGENT_ZIP   = "https://github.com/dkaryp13hbis/firstlight-agent/archive/refs/heads/main.zip"

New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path "$INSTALL_DIR\install.log" -Value "$ts  $msg" -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Host "  $msg"
}

function Ask($prompt, $def) {
    Write-Host "  $prompt [$def] : " -NoNewline
    $v = Read-Host
    if ($v -eq "") { return $def }
    return $v
}

function AskSecret($prompt) {
    Write-Host "  $prompt : " -NoNewline
    $ss = Read-Host -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss)
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
}

Clear-Host
Write-Host "================================================="
Write-Host "  FirstLight Hotel Agent - Setup v1.0"
Write-Host "================================================="
Write-Host ""

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")
if (-not $isAdmin) {
    Write-Host "ERROR: Run as Administrator." -ForegroundColor Red
    exit 1
}

# ── Step 1: Python ────────────────────────────────────────────────────
Write-Host ""
Write-Host "[1] Checking Python..." -ForegroundColor Cyan
$python = $null
$cmds = @("py", "python", "python3")
foreach ($cmd in $cmds) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.[89]|Python 3\.1[0-9]") {
            $python = $cmd
            Write-Host "    Found: $ver" -ForegroundColor Green
            break
        }
    } catch { }
}
if (-not $python) {
    Write-Host "    Downloading Python 3.11..." -ForegroundColor Yellow
    $pyExe = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $pyExe -UseBasicParsing
    Start-Process -FilePath $pyExe -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $python = "python"
    Write-Host "    Python 3.11 installed." -ForegroundColor Green
}
Log "Python OK: $python"

# ── Step 2: ODBC Driver ───────────────────────────────────────────────
Write-Host ""
Write-Host "[2] Checking ODBC Driver 17..." -ForegroundColor Cyan
$odbc = Get-ItemProperty "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server" -ErrorAction SilentlyContinue
if ($odbc) {
    Write-Host "    Already installed." -ForegroundColor Green
} else {
    Write-Host "    Downloading ODBC Driver 17..." -ForegroundColor Yellow
    $msi = "$env:TEMP\msodbcsql17.msi"
    Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/?linkid=2249004" -OutFile $msi -UseBasicParsing
    Start-Process msiexec -ArgumentList "/i `"$msi`" /quiet IACCEPTMSODBCSQLLICENSETERMS=YES" -Wait
    Write-Host "    ODBC Driver 17 installed." -ForegroundColor Green
}
Log "ODBC OK"

# ── Step 3: Configuration ─────────────────────────────────────────────
Write-Host ""
Write-Host "[3] Hotel configuration" -ForegroundColor Cyan
Write-Host "    (Press Enter to accept the default shown in brackets)"
Write-Host ""

$SQL_SERVER   = Ask "PMS SQL Server IP" "192.168.1.100"
$SQL_DATABASE = Ask "Database name" "bidata"
$SQL_TRUSTED  = Ask "Windows auth (yes/no)" "no"
if ($SQL_TRUSTED -eq "yes") {
    $SQL_USER = ""
    $SQL_PASSWORD = ""
} else {
    $SQL_USER     = Ask "SQL username" "sa"
    $SQL_PASSWORD = AskSecret "SQL password"
}
$HOTEL_ID    = Ask "Protel Hotel ID" "1"
$HOTEL_NAME  = Ask "Hotel display name" "My Hotel"
$TOTAL_ROOMS = Ask "Total rooms" "100"
$RECIP_EMAIL = Ask "Report recipient email" ""
$RECIP_NAME  = Ask "Recipient name" "General Manager"
$SMTP_HOST   = Ask "SMTP server" "smtp.gmail.com"
$SMTP_PORT   = Ask "SMTP port" "587"
$SMTP_USER   = Ask "SMTP username" ""
$SMTP_PASS   = AskSecret "SMTP password"
$ANTH_KEY    = Ask "Anthropic API key (Enter to skip)" ""
$FL_URL      = Ask "FirstLight API URL" "https://web-production-61c4d.up.railway.app"
$FL_KEY      = AskSecret "FirstLight API key"
$RUN_TIME    = Ask "Daily run time (HH:MM)" "06:30"

Log "Config collected: $HOTEL_NAME"

# ── Step 4: Download agent ────────────────────────────────────────────
Write-Host ""
Write-Host "[4] Downloading agent files..." -ForegroundColor Cyan
$zipPath = "$env:TEMP\firstlight-agent.zip"
$extractPath = "$env:TEMP\firstlight-extract"

$localZip = Join-Path $PSScriptRoot "firstlight-agent.zip"
if (Test-Path $localZip) {
    $zipPath = $localZip
    Write-Host "    Using local bundle." -ForegroundColor Green
} else {
    Invoke-WebRequest -Uri $AGENT_ZIP -OutFile $zipPath -UseBasicParsing
    Write-Host "    Downloaded from GitHub." -ForegroundColor Green
}

if (Test-Path $extractPath) { Remove-Item $extractPath -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
$srcDir = (Get-ChildItem $extractPath -Directory | Select-Object -First 1).FullName
Copy-Item "$srcDir\*" -Destination $INSTALL_DIR -Recurse -Force
Write-Host "    Installed to $INSTALL_DIR" -ForegroundColor Green
Log "Agent files installed"

# ── Step 5: Write .env ────────────────────────────────────────────────
Write-Host ""
Write-Host "[5] Writing configuration..." -ForegroundColor Cyan
$envLines = @(
    "SQL_SERVER=$SQL_SERVER",
    "SQL_DATABASE=$SQL_DATABASE",
    "SQL_TRUSTED=$SQL_TRUSTED",
    "SQL_USER=$SQL_USER",
    "SQL_PASSWORD=$SQL_PASSWORD",
    "",
    "HOTEL_ID=$HOTEL_ID",
    "HOTEL_NAME=$HOTEL_NAME",
    "HOTEL_TOTAL_ROOMS=$TOTAL_ROOMS",
    "",
    "RECIPIENT_EMAIL=$RECIP_EMAIL",
    "RECIPIENT_NAME=$RECIP_NAME",
    "SMTP_HOST=$SMTP_HOST",
    "SMTP_PORT=$SMTP_PORT",
    "SMTP_USER=$SMTP_USER",
    "SMTP_PASSWORD=$SMTP_PASS",
    "",
    "ANTHROPIC_API_KEY=$ANTH_KEY",
    "",
    "FIRSTLIGHT_API_URL=$FL_URL",
    "FIRSTLIGHT_API_KEY=$FL_KEY"
)
$envLines | Out-File -FilePath "$INSTALL_DIR\.env" -Encoding utf8 -Force
Write-Host "    .env written." -ForegroundColor Green
Log ".env written"

# ── Step 6: Install packages ──────────────────────────────────────────
Write-Host ""
Write-Host "[6] Installing Python packages..." -ForegroundColor Cyan
& $python -m pip install --quiet --upgrade pip 2>&1 | Out-Null
& $python -m pip install --quiet -r "$INSTALL_DIR\requirements.txt"
Write-Host "    Packages installed." -ForegroundColor Green
Log "pip install OK"

# ── Step 7: Test DB connection ─────────────────────────────────────────
Write-Host ""
Write-Host "[7] Testing database connection..." -ForegroundColor Cyan
$testPy = "import sys,os; sys.path.insert(0,r'$INSTALL_DIR'); os.chdir(r'$INSTALL_DIR'); from dotenv import load_dotenv; load_dotenv(); from db.connection import get_connection; conn=get_connection(); conn.close(); print('OK')"
$result = & $python -c $testPy 2>&1
if ($result -match "OK") {
    Write-Host "    Database connected!" -ForegroundColor Green
    Log "DB test OK"
} else {
    Write-Host "    WARNING: $result" -ForegroundColor Yellow
    Write-Host "    Check credentials and firewall. Agent will retry at scheduled time." -ForegroundColor Yellow
    Log "DB test WARN: $result"
}

# ── Step 8: Scheduled task ─────────────────────────────────────────────
Write-Host ""
Write-Host "[8] Registering scheduled task..." -ForegroundColor Cyan
$pythonFull = & $python -c "import sys; print(sys.executable)" 2>&1
$action    = New-ScheduledTaskAction -Execute $pythonFull -Argument "main.py" -WorkingDirectory $INSTALL_DIR
$trigger   = New-ScheduledTaskTrigger -Daily -At $RUN_TIME
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "    Task registered: daily at $RUN_TIME" -ForegroundColor Green
Log "Scheduled task OK at $RUN_TIME"

# ── Done ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================="
Write-Host "  Done! FirstLight installed for $HOTEL_NAME"
Write-Host "  Runs daily at $RUN_TIME"
Write-Host "  Folder: $INSTALL_DIR"
Write-Host "================================================="
Write-Host ""
Write-Host "  To run manually now:" -ForegroundColor Gray
Write-Host "  cd $INSTALL_DIR && $pythonFull main.py --preview" -ForegroundColor Gray
Write-Host ""
Log "Install complete"
