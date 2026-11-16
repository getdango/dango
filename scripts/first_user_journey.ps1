# scripts/first_user_journey.ps1
# One-command first-user check for a real Windows machine (PowerShell 5.1+, no admin rights).
# Creates a throwaway venv and project under $env:TEMP, installs Dango, runs the journey and
# writes the log to the Desktop. Paste that log back. Nothing outside the temp dirs is changed.
# Keep first_user_journey.py, journey_support.py and journey_services.py in the same folder.
#   .\first_user_journey.ps1 [-Spec "getdango==1.0.15"] [-WheelPath C:\path\getdango.whl] [-WithDocker]
param([string]$Spec = "getdango", [string]$WheelPath = "", [switch]$WithDocker)
$ErrorActionPreference = "Continue"
$root = Join-Path $env:TEMP ("dango-journey-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$log = Join-Path ([Environment]::GetFolderPath("Desktop")) "dango-journey.log"
$script = Join-Path $PSScriptRoot "first_user_journey.py"
try {
    New-Item -ItemType Directory -Path $root, "$root\work" | Out-Null
    if (Test-Path $log) { Remove-Item $log }
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv "$root\venv" } else { & python -m venv "$root\venv" }
    $target = if ($WheelPath) { $WheelPath } else { $Spec }
    & "$root\venv\Scripts\python.exe" -m pip install $target
    $extra = @(); if ($WithDocker) { $extra += "--with-docker" }
    & "$root\venv\Scripts\python.exe" $script --dango "$root\venv\Scripts\dango.exe" `
        --workdir "$root\work" --log $log @extra
    Write-Host "Exit code: $LASTEXITCODE"
} catch {
    Write-Host "Journey setup failed: $_"
} finally {
    Write-Host "Log file: $log"
    Write-Host "Temp files (safe to delete): $root"
}
