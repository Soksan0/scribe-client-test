$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "Starting Scribe for local Windows testing..." -ForegroundColor Cyan
Write-Host "Your datasets stay on this computer. Scribe will open in your browser at localhost." -ForegroundColor Gray
Write-Host ""

Set-Location -Path $PSScriptRoot

function Assert-Command {
    param(
        [string]$Name,
        [string]$InstallUrl
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "$Name is required but was not found." -ForegroundColor Red
        Write-Host "Install it here: $InstallUrl"
        Write-Host "Then close and reopen PowerShell and run this starter again."
        exit 1
    }
}

Assert-Command -Name "py" -InstallUrl "https://www.python.org/downloads/"
Assert-Command -Name "npm" -InstallUrl "https://nodejs.org/"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating local Python environment..."
    py -m venv .venv
}

Write-Host "Installing Python packages..."
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

if (-not (Test-Path "node_modules")) {
    Write-Host "Installing website packages..."
    npm ci
}

Write-Host ""
Write-Host "Launching Scribe. Leave this window open while testing." -ForegroundColor Green
Write-Host ""

.\.venv\Scripts\python.exe scripts\start_scribe.py

