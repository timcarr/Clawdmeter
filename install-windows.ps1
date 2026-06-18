# install-windows.ps1 - Clawdmeter Windows turnkey bootstrap
#
# Creates a Python virtual environment, installs dependencies, builds
# Clawdmeter.exe with PyInstaller, registers it to launch at login
# (HKCU\...\Run, no admin required), and starts it immediately.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1
#
# To disable autostart later: right-click the tray icon -> uncheck "Start at login"
# Or remove manually: reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Clawdmeter /f
#
# Security: this script downloads nothing from the internet. It installs only
# the packages listed in the in-repo daemon\requirements-windows.txt.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Log {
    param([string]$Msg)
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $Msg"
}

$RepoRoot = $PSScriptRoot
if (-not $RepoRoot) {
    $RepoRoot = (Get-Location).Path
}

Log "=== Clawdmeter Windows Install ==="
Log "Repository root: $RepoRoot"

# ------------------------------------------------------------------
# Guard: refuse to install from a WSL path (APP-02 / SC#4 / SC#5)
# ------------------------------------------------------------------
if ($RepoRoot -match '\\\\wsl(\$|\.localhost)\\') {
    throw @"
Refusing to install from a WSL path:
  $RepoRoot

The Clawdmeter daemon must be WSL-independent. Installing from the WSL share
would make the virtual environment and login-autostart entry point at a path
that is unreachable once WSL shuts down.

Fix: copy this repository to a native Windows location and run the installer
there, e.g.

  Copy-Item -Recurse '$RepoRoot' "$env:USERPROFILE\Clawdmeter"
  cd "$env:USERPROFILE\Clawdmeter"
  powershell -ExecutionPolicy Bypass -File install-windows.ps1
"@
}

# ------------------------------------------------------------------
# Step 1: Create virtual environment
# ------------------------------------------------------------------
$VenvDir = Join-Path $RepoRoot ".venv"
if (Test-Path $VenvDir) {
    Log "Virtual environment already exists at .venv - skipping creation"
} else {
    Log "Creating virtual environment at .venv ..."
    & python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment (exit $LASTEXITCODE)" }
    Log "Virtual environment created"
}

# ------------------------------------------------------------------
# Step 2: Install dependencies (includes pyinstaller)
# ------------------------------------------------------------------
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsFile = Join-Path $RepoRoot "daemon\requirements-windows.txt"

Log "Installing dependencies from daemon\requirements-windows.txt ..."
& $PythonExe -m pip install --quiet -r $RequirementsFile
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
Log "Dependencies installed"

# ------------------------------------------------------------------
# Step 3: Build Clawdmeter.exe
# ------------------------------------------------------------------
$SpecFile = Join-Path $RepoRoot "Clawdmeter.spec"
$ExePath  = Join-Path $RepoRoot "dist\Clawdmeter.exe"

Log "Building Clawdmeter.exe ..."
& $PythonExe -m PyInstaller --clean --noconfirm $SpecFile
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }
if (-not (Test-Path $ExePath)) { throw "Build succeeded but dist\Clawdmeter.exe not found" }
Log "Build complete: $ExePath"

# ------------------------------------------------------------------
# Step 4: Register autostart (HKCU\Run, per-user, no admin needed)
# ------------------------------------------------------------------
Log "Registering autostart ..."
& $PythonExe -c @"
import sys
sys.path.insert(0, r'$RepoRoot')
import daemon.autostart_windows as a
a.enable(tray_script=r'$ExePath')
"@
if ($LASTEXITCODE -ne 0) { throw "Autostart registration failed (exit $LASTEXITCODE)" }
Log "Autostart registered - Clawdmeter will launch automatically at next logon"

# ------------------------------------------------------------------
# Step 5: Launch
# ------------------------------------------------------------------
Log "Launching Clawdmeter ..."
Start-Process $ExePath -WorkingDirectory $RepoRoot
Log "Clawdmeter started - look for the icon in your notification area"
Log "=== Install complete ==="
