param()
$ErrorActionPreference = "Stop"
$INSTALL_DIR = "C:\FirstLight"
$AGENT_ZIP   = "https://github.com/dkaryp13hbis/firstlight-agent/archive/refs/heads/main.zip"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path "$INSTALL_DIR\install.log" -Value "$ts  [UPDATE] $msg" -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Host "  $msg"
}

Clear-Host
Write-Host "================================================="
Write-Host "  FirstLight Hotel Agent - Updater"
Write-Host "================================================="
Write-Host ""

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")
if (-not $isAdmin) {
    Write-Host "ERROR: Run as Administrator." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $INSTALL_DIR)) {
    Write-Host "ERROR: $INSTALL_DIR not found. Run the installer first." -ForegroundColor Red
    exit 1
}

# Download latest zip
Write-Host "[1] Downloading latest agent from GitHub..." -ForegroundColor Cyan
$zipPath     = "$env:TEMP\firstlight-agent-update.zip"
$extractPath = "$env:TEMP\firstlight-extract-update"
Invoke-WebRequest -Uri $AGENT_ZIP -OutFile $zipPath -UseBasicParsing
Write-Host "    Downloaded." -ForegroundColor Green
Log "Downloaded latest zip"

# Extract
if (Test-Path $extractPath) { Remove-Item $extractPath -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
$srcDir = (Get-ChildItem $extractPath -Directory | Select-Object -First 1).FullName

# Copy files, preserving .env and install.log
Write-Host "[2] Updating files (preserving .env)..." -ForegroundColor Cyan
Get-ChildItem "$srcDir" -Recurse | ForEach-Object {
    $rel  = $_.FullName.Substring($srcDir.Length + 1)
    $dest = Join-Path $INSTALL_DIR $rel
    if ($rel -eq ".env" -or $rel -eq "install.log") { return }
    if ($_.PSIsContainer) {
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
    } else {
        Copy-Item $_.FullName -Destination $dest -Force
    }
}
Write-Host "    Files updated." -ForegroundColor Green
Log "Files updated from GitHub"

# Upgrade pip packages
Write-Host "[3] Upgrading Python packages..." -ForegroundColor Cyan
$python = & python -c "import sys; print(sys.executable)" 2>&1
& python -m pip install --quiet -r "$INSTALL_DIR\requirements.txt"
Write-Host "    Packages up to date." -ForegroundColor Green
Log "pip install OK"

Write-Host ""
Write-Host "================================================="
Write-Host "  Update complete!"
Write-Host "  Restart the scheduled task or run manually:"
Write-Host "  cd $INSTALL_DIR && python main.py --preview"
Write-Host "================================================="
Log "Update complete"
