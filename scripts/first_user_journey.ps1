# scripts/first_user_journey.ps1
# One-command first-user check for a real Windows machine (PowerShell 5.1+, no admin rights).
# Creates a throwaway venv and project under $env:TEMP, installs Dango, runs the journey and
# writes everything (setup, pip, journey) to a timestamped log on the Desktop. Paste that log
# back. Nothing outside the temp dirs and that log file is changed.
# Keep first_user_journey.py, journey_support.py and journey_services.py in the same folder.
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\first_user_journey.ps1 `
#       [-Spec "getdango==1.0.15"] [-WheelPath C:\path\getdango.whl] [-WithDocker] [-LegacyEncoding]
param([string]$Spec = "getdango", [string]$WheelPath = "", [switch]$WithDocker, [switch]$LegacyEncoding)
$ErrorActionPreference = "Continue"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$root = Join-Path $env:TEMP "dango-journey-$stamp"
$log = Join-Path ([Environment]::GetFolderPath("Desktop")) "dango-journey-$stamp.log"
$journeyPy = Join-Path $PSScriptRoot "first_user_journey.py"
$min = 11; $max = 13   # default Python 3.11-3.13; replaced by requires-python of a pyproject.toml checkout
$pyproject = Join-Path $PSScriptRoot "..\pyproject.toml"
if (Test-Path $pyproject) {
    $req = Select-String -Path $pyproject -Pattern '^requires-python\s*=\s*"(.+)"'
    if ($req) {
        $reqText = $req.Matches[0].Groups[1].Value
        if ($reqText -match '>=\s*3\.(\d+)') { $min = [int]$Matches[1] }
        if ($reqText -match '<\s*3\.(\d+)') { $max = [int]$Matches[1] - 1 }
    }
}
function Log([string]$Line) { Write-Host $Line; Add-Content -Path $log -Value $Line -Encoding UTF8 }
function RunLogged([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments 2>&1 | ForEach-Object { Log "$_" }
    return $LASTEXITCODE
}
function PythonVersion([string]$Exe, [string[]]$Pre) {
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return "" }
    $v = & $Exe @Pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    return "$v".Trim()
}
$code = 1
try {
    New-Item -ItemType Directory -Path $root, "$root\work" | Out-Null
    Log "Dango first-user journey, log: $log"
    $candidates = @()
    for ($m = $max; $m -ge $min; $m--) { $candidates += , @("py", @("-3.$m")) }
    $candidates += , @("python", @())
    $exe = $null
    foreach ($c in $candidates) {
        $v = PythonVersion $c[0] $c[1]
        if ($v -match '^3\.(\d+)$' -and [int]$Matches[1] -ge $min -and [int]$Matches[1] -le $max) { $exe = $c; Log "Using $($c[0]) $($c[1]) (Python $v)"; break }
    }
    if (-not $exe) { throw "No Python 3.$min to 3.$max found (tried py -3.x and python). Install one from python.org and run again." }
    if ((RunLogged $exe[0] ($exe[1] + @("-m", "venv", "$root\venv"))) -ne 0) { throw "Creating the virtual environment failed." }
    $vpy = "$root\venv\Scripts\python.exe"
    $target = if ($WheelPath) { $WheelPath } else { $Spec }
    if ((RunLogged $vpy @("-m", "pip", "install", $target)) -ne 0) { throw "pip could not install $target." }
    $extra = @(); if ($WithDocker) { $extra += "--with-docker" }; if ($LegacyEncoding) { $extra += "--legacy-encoding" }
    $code = RunLogged $vpy (@($journeyPy, "--dango", "$root\venv\Scripts\dango.exe", "--workdir", "$root\work", "--log", $log) + $extra)
    Log "Journey exit code: $code"
} catch {
    Log "Journey setup failed: $_"
}
Log "Log file (paste this back): $log"
Log "Temp files (safe to delete): $root"
exit $code
