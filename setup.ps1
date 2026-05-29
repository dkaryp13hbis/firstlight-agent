param()
# FirstLight — Hotel Setup
# Run once on the hotel server after copying this folder here.
# Creates .env with hotel credentials and registers the daily scheduled task.

Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  FirstLight Hotel Setup" -ForegroundColor Cyan
Write-Host "  ----------------------" -ForegroundColor Cyan
Write-Host ""

function Ask($label, $default) {
    Write-Host "  $label" -NoNewline
    if ($default -ne "") { Write-Host " [$default]" -NoNewline -ForegroundColor DarkGray }
    Write-Host " : " -NoNewline
    $v = Read-Host
    if ($v -eq "") { return $default }
    return $v
}
function AskPwd($label) {
    Write-Host "  $label : " -NoNewline
    return Read-Host
}

$SQL_SERVER   = Ask  "SQL Server IP"           "192.168.1.100"
$SQL_DATABASE = Ask  "Database name"           "bidata"
$SQL_USER     = Ask  "SQL username"            "sa"
$SQL_PASSWORD = AskPwd "SQL password"
$HOTEL_ID     = Ask  "Protel Hotel ID"         "1"
$HOTEL_NAME   = Ask  "Hotel name"              "My Hotel"
$TOTAL_ROOMS  = Ask  "Total rooms"             "100"
$RECIP_EMAIL  = Ask  "Report email"            ""
$SMTP_USER    = Ask  "SMTP username (Gmail)"   ""
$SMTP_PASS    = AskPwd "SMTP password"
$ANTH_KEY     = Ask  "Anthropic API key"       ""
$FL_KEY       = AskPwd "FirstLight API key"
$RUN_TIME     = Ask  "Daily run time (HH:MM)"  "06:30"

# Write .env
@"
SQL_SERVER=$SQL_SERVER
SQL_DATABASE=$SQL_DATABASE
SQL_TRUSTED=no
SQL_USER=$SQL_USER
SQL_PASSWORD=$SQL_PASSWORD

HOTEL_ID=$HOTEL_ID
HOTEL_NAME=$HOTEL_NAME
HOTEL_TOTAL_ROOMS=$TOTAL_ROOMS

RECIPIENT_EMAIL=$RECIP_EMAIL
RECIPIENT_NAME=General Manager
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=$SMTP_USER
SMTP_PASSWORD=$SMTP_PASS

ANTHROPIC_API_KEY=$ANTH_KEY

FIRSTLIGHT_API_URL=https://web-production-61c4d.up.railway.app
FIRSTLIGHT_API_KEY=$FL_KEY
"@ | Out-File -FilePath "$PSScriptRoot\.env" -Encoding utf8 -Force

Write-Host ""
Write-Host "  .env saved." -ForegroundColor Green

# Install Python packages
Write-Host "  Installing packages..." -ForegroundColor Yellow
python -m pip install -q -r "$PSScriptRoot\requirements.txt"
Write-Host "  Packages ready." -ForegroundColor Green

# Task 1: daily morning briefing
$action    = New-ScheduledTaskAction -Execute "python" -Argument "main.py" -WorkingDirectory $PSScriptRoot
$trigger   = New-ScheduledTaskTrigger -Daily -At $RUN_TIME
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "FirstLight Morning Briefing" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

# Task 2: refresh daemon — starts at boot, listens for PWA refresh commands
$action2   = New-ScheduledTaskAction -Execute "python" -Argument "server.py --daemon" -WorkingDirectory $PSScriptRoot
$trigger2  = New-ScheduledTaskTrigger -AtStartup
$settings2 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 365)
Register-ScheduledTask -TaskName "FirstLight Refresh Daemon" -Action $action2 -Trigger $trigger2 -Settings $settings2 -Principal $principal -Force | Out-Null

Write-Host "  Scheduled task set: daily at $RUN_TIME" -ForegroundColor Green
Write-Host "  Refresh daemon set: starts at boot" -ForegroundColor Green
Write-Host ""
Write-Host "  Setup complete for $HOTEL_NAME" -ForegroundColor Cyan
Write-Host "  Test now: python main.py --preview" -ForegroundColor DarkGray
Write-Host ""
