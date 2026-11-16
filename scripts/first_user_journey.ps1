# scripts/first_user_journey.ps1
# One-command first-user check for a real Windows machine (PowerShell 5.1+, no admin rights).
# Creates a throwaway venv and project under $env:TEMP, installs Dango, runs the journey and
# writes two timestamped logs to the Desktop: a setup log (setup, pip, journey summary) and the
# journey's own log. Paste both back. Without -WithDocker nothing outside the temp dirs and those
# two logs is changed. -WithDocker starts the real Dango platform: Dango may store credentials in
# your user keychain and in ~/.dango, and the Metabase image it builds is left behind. Use it only
# on a machine where that is acceptable.
# The journey keeps file encodings at the platform default on purpose so encoding defects
# surface; its console output is UTF-8 as in a real terminal (-LegacyStdio turns that off,
# -Utf8Mode runs Python in UTF-8 mode, which hides file-encoding defects).
# Keep first_user_journey.py, journey_support.py and journey_services.py in the same folder.
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\first_user_journey.ps1 `
#       [-Spec "getdango==1.0.15"] [-WheelPath C:\path\getdango.whl] [-WithDocker] [-Utf8Mode] [-LegacyStdio]
param([string]$Spec = "getdango", [string]$WheelPath = "", [switch]$WithDocker, [switch]$Utf8Mode, [switch]$LegacyStdio)
$ErrorActionPreference = "Continue"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$root = Join-Path $env:TEMP "dango-journey-$stamp"
$desktop = [Environment]::GetFolderPath("Desktop")
$log = Join-Path $desktop "dango-journey-$stamp-setup.log"
$journeyLog = Join-Path $desktop "dango-journey-$stamp.log"
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
    Log "Dango first-user journey, setup log: $log"
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
    $extra = @(); if ($WithDocker) { $extra += "--with-docker" }; if ($Utf8Mode) { $extra += "--utf8-mode" }; if ($LegacyStdio) { $extra += "--legacy-stdio" }
    $code = RunLogged $vpy (@($journeyPy, "--dango", "$root\venv\Scripts\dango.exe", "--workdir", "$root\work", "--log", $journeyLog) + $extra)
    Log "Journey exit code: $code"
} catch {
    Log "Journey setup failed: $_"
}
Log "Paste both logs back: $log and $journeyLog"
Log "Temp files (safe to delete): $root"
exit $code
