# Installs autopilot on Windows.
# Usage: powershell -ExecutionPolicy ByPass -c "irm https://github.com/kafle1/autopilot/releases/latest/download/install.ps1 | iex"
$ErrorActionPreference = 'Stop'

$Repo = "kafle1/autopilot"

Write-Host "Setting up autopilot..."

$UvPath = "$env:USERPROFILE\.local\bin\uv.exe"
if (-not (Get-Command uv -ErrorAction SilentlyContinue) -and -not (Test-Path $UvPath)) {
    Write-Host "Installing uv (the tool that runs autopilot)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
}

$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

if ($env:AUTOPILOT_REF) {
    $Ref = $env:AUTOPILOT_REF
} else {
    Write-Host "Looking up the latest release..."
    try {
        $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest"
        $Ref = $Release.tag_name
    } catch {
        Write-Host "Could not reach GitHub to find the latest release. Check your internet connection and try again."
        exit 1
    }
    if (-not $Ref) {
        Write-Host "Could not reach GitHub to find the latest release. Check your internet connection and try again."
        exit 1
    }
}
Write-Host "Installing autopilot $Ref..."

uv tool install --force --managed-python --python 3.12 "https://github.com/$Repo/archive/refs/tags/$Ref.tar.gz"

Write-Host ""
Write-Host "autopilot is installed."
Write-Host ""
& "$env:USERPROFILE\.local\bin\autopilot.exe" setup
