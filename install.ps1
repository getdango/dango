# Dango Bootstrap Installer for Windows
# Version: see getdango on PyPI
# Purpose: Install Dango with per-project virtual environment
# Platform: Windows 10/11 with PowerShell 5.1+

# Test hooks (opt-in; with none of these set the installer behaves exactly as before):
#
#   DANGO_INSTALL_SPEC             pip argument that replaces "getdango" in every Dango pip
#                                  install (a wheel path, "getdango==1.0.15", ...). One pip
#                                  argument of letters, digits, spaces and . _ / : @ + = ~ , \ -
#                                  (no shell metacharacters or newlines, no leading "-", no
#                                  leading or trailing space); it is validated and passed to pip
#                                  as a single argument.
#   DANGO_INSTALL_NONINTERACTIVE   1 = read every answer from the variables below instead of
#                                  Read-Host. A prompt whose variable is unset (and that has no
#                                  default) stops the installer with that variable's name; an
#                                  invalid value does the same. Never prompts, never guesses.
#
# Answer variables (used only when DANGO_INSTALL_NONINTERACTIVE=1):
#
#   DANGO_INSTALL_ALLOW_EXECUTION_POLICY  y|n  set the CurrentUser execution policy to
#                                    RemoteSigned when it is too restrictive (required then)
#   DANGO_INSTALL_DOCKER_CONTINUE    y|n  continue when Docker is missing or not running (required)
#   DANGO_INSTALL_MODE               1|2  1 = virtual environment, 2 = global (required)
#   DANGO_INSTALL_SETUP              1|2  1 = install only, 2 = install + new project (required)
#   DANGO_INSTALL_ADD_TO_PATH        y|n  add the user Scripts directory to PATH (default n)
#   DANGO_INSTALL_PROJECT_NAME       new project directory name (required for SETUP=2). It is
#                                    cleaned as when typed (lowercase, spaces to dashes, other
#                                    characters dropped) and must then match ^[a-z0-9][a-z0-9-]*$
#                                    and not already exist as a directory
#   DANGO_INSTALL_EXISTING_ACTION    i|u|c  install, upgrade or cancel, for an existing project
#                                    with a venv (required)
#   DANGO_INSTALL_CREATE_VENV        y|n  create the missing venv of an existing project (default y)
#
# `dango init` runs as `dango init --skip-wizard` in non-interactive mode.

# Requires PowerShell to be run as Administrator for Docker check
#Requires -Version 5.1

# Color output functions
function Write-Header {
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  Dango Installer - Open Source Data Platform" -ForegroundColor Cyan
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Success {
    param([string]$Message)
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-Error-Message {
    param([string]$Message)
    Write-Host "✗ $Message" -ForegroundColor Red
}

function Write-Warning-Message {
    param([string]$Message)
    Write-Host "⚠ $Message" -ForegroundColor Yellow
}

function Write-Info {
    param([string]$Message)
    Write-Host "ℹ $Message" -ForegroundColor Blue
}

function Write-Step {
    param([string]$Message)
    Write-Host "▶ $Message" -ForegroundColor Cyan
}

# Validate DANGO_INSTALL_SPEC (one plain token, checked before anything runs) and resolve the
# pip argument used for every Dango install: the spec, or "getdango" when it is unset.
function Test-InstallSpec {
    $spec = $env:DANGO_INSTALL_SPEC
    $script:DangoSpec = "getdango"
    if ([string]::IsNullOrEmpty($spec)) {
        return
    }
    if ($spec.StartsWith("-") -or $spec.StartsWith(" ") -or $spec.EndsWith(" ") -or ($spec -notmatch '^[A-Za-z0-9._/:@+=~, \\-]+\z')) {
        Write-Error-Message "DANGO_INSTALL_SPEC must be one pip argument of letters, digits, spaces and . _ / : @ + = ~ , \ - (no leading dash or edge spaces); got: $spec"
        exit 1
    }
    $script:DangoSpec = $spec
}

# Stop with a message naming the variable when a non-interactive run cannot continue.
function Stop-IfNonInteractive {
    param([string]$Message)

    if ($env:DANGO_INSTALL_NONINTERACTIVE -eq "1") {
        Write-Error-Message $Message
        exit 1
    }
}

# Ask a question. Interactive: Read-Host with the prompt. Non-interactive
# (DANGO_INSTALL_NONINTERACTIVE=1): the answer comes from the environment variable $Name, else
# $Default ("" = none); with no answer, or one that does not match $Pattern, stop naming $Name.
# This is the only place install.ps1 calls Read-Host.
function Read-Answer {
    param([string]$Name, [string]$Prompt, [string]$Default = "", [string]$Pattern = "")

    if ($env:DANGO_INSTALL_NONINTERACTIVE -ne "1") {
        return (Read-Host $Prompt)
    }
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) {
        $value = $Default
    }
    if ([string]::IsNullOrEmpty($value)) {
        Write-Error-Message "Non-interactive install: set $Name (no answer given, no default)"
        exit 1
    }
    if ($Pattern -and ($value -notmatch $Pattern)) {
        Write-Error-Message "Non-interactive install: $Name='$value' is not valid (expected $Pattern)"
        exit 1
    }
    Write-Host "(non-interactive) $Name=$value"
    return $value
}

# Function to check and fix PowerShell execution policy
function Test-ExecutionPolicy {
    Write-Step "Checking PowerShell execution policy..."

    $policy = Get-ExecutionPolicy -Scope CurrentUser

    # If policy allows scripts, we're good
    if ($policy -eq "RemoteSigned" -or $policy -eq "Unrestricted" -or $policy -eq "Bypass") {
        Write-Success "Execution policy is already set: $policy"
        Write-Host ""
        return
    }

    # Policy is too restrictive
    Write-Warning-Message "PowerShell script execution is not enabled (current policy: $policy)"
    Write-Host ""
    Write-Host "To install Dango, we need to enable script execution."
    Write-Host "This is safe and only affects your user account (no admin needed)."
    Write-Host ""
    Write-Host "We will set the policy to 'RemoteSigned' which:"
    Write-Host "  • Allows local scripts to run"
    Write-Host "  • Requires downloaded scripts to be signed"
    Write-Host "  • Only affects your user (not system-wide)"
    Write-Host ""

    # Prompt for permission
    $response = Read-Answer -Name 'DANGO_INSTALL_ALLOW_EXECUTION_POLICY' -Prompt "Allow script execution for your user? [Y/n]" -Pattern '^[YyNn]\z'

    if ($response -match '^[Yy]$' -or [string]::IsNullOrWhiteSpace($response)) {
        try {
            Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force -ErrorAction Stop
            Write-Success "Execution policy updated to RemoteSigned"
            Write-Host ""
        }
        catch {
            Write-Error-Message "Failed to update execution policy: $_"
            Write-Host ""
            Write-Host "Please run this command manually:"
            Write-Host "  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser"
            Write-Host ""
            exit 1
        }
    }
    else {
        Write-Error-Message "Cannot proceed without script execution enabled"
        Write-Host ""
        Write-Host "To enable manually, run:"
        Write-Host "  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser"
        Write-Host ""
        Write-Host "Then run this installer again."
        Write-Host ""
        exit 1
    }
}

# Function to check Python version
function Test-PythonVersion {
    Write-Step "Checking Python version..."

    # Try multiple Python commands in order of preference
    $pythonCmd = $null
    $pythonVersion = $null
    $commands = @("python3.13", "python3.12", "python3.11", "python3", "python", "py")

    foreach ($cmd in $commands) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            try {
                $version = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($version) {
                    $versionParts = $version -split '\.'
                    $major = [int]$versionParts[0]
                    $minor = [int]$versionParts[1]

                    # Check if version is 3.11-3.13 (matches requires-python >=3.11,<3.14)
                    if ($major -eq 3 -and $minor -ge 11 -and $minor -le 13) {
                        $pythonCmd = $cmd
                        $pythonVersion = $version
                        break
                    }
                }
            }
            catch {
                # Skip this command and try next
                continue
            }
        }
    }

    # If no suitable Python found
    if (-not $pythonCmd) {
        Write-Error-Message "Python 3.11-3.13 not found"
        Write-Host ""
        Write-Host "Dango requires Python 3.11-3.13 (3.12 recommended)"
        Write-Host ""
        Write-Host "Install options:"
        Write-Host "  winget install Python.Python.3.12"
        Write-Host "  Or download: https://www.python.org/downloads/"
        Write-Host ""
        exit 1
    }

    Write-Success "Python $pythonVersion found ($pythonCmd)"
    Write-Host ""

    return @{
        Command = $pythonCmd
        Version = $pythonVersion
    }
}

# Function to check Docker
function Test-Docker {
    Write-Step "Checking Docker..."

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warning-Message "Docker not found"
        Write-Host ""
        Write-Host "Docker is required for Metabase (visualization)."
        Write-Host "Please install Docker Desktop:"
        Write-Host "  Download: https://docs.docker.com/desktop/install/windows-install/"
        Write-Host ""

        $response = Read-Answer -Name 'DANGO_INSTALL_DOCKER_CONTINUE' -Prompt "Continue without Docker? (y/N)" -Pattern '^[YyNn]\z'
        if ($response -notmatch '^[Yy]$') {
            Write-Info "Installation cancelled"
            Write-Host ""
            Write-Host "Install Docker and run this script again:"
            Write-Host "  irm https://getdango.dev/install.ps1 | iex"
            Write-Host ""
            exit 0
        }
        Write-Host ""
        return
    }

    # Check if Docker daemon is running
    $dockerRunning = $false
    try {
        docker info 2>&1 | Out-Null
        $dockerRunning = $LASTEXITCODE -eq 0
    }
    catch {
        $dockerRunning = $false
    }

    if (-not $dockerRunning) {
        Write-Warning-Message "Docker is installed but not running"
        Write-Host ""
        Write-Host "Please start Docker Desktop and try again."
        Write-Host ""
        Write-Host "Or continue without Docker (Metabase won't work):"

        $response = Read-Answer -Name 'DANGO_INSTALL_DOCKER_CONTINUE' -Prompt "Continue? (y/N)" -Pattern '^[YyNn]\z'
        if ($response -notmatch '^[Yy]$') {
            Write-Info "Installation cancelled"
            Write-Host ""
            Write-Host "Start Docker and run this script again:"
            Write-Host "  irm https://getdango.dev/install.ps1 | iex"
            Write-Host ""
            exit 0
        }
        Write-Host ""
        return
    }

    Write-Success "Docker is running"
    Write-Host ""
}

# Function to detect scenario
function Get-InstallScenario {
    if (Test-Path ".dango\project.yml") {
        # Existing Dango project
        if (Test-Path "venv") {
            return "existing_with_venv"
        }
        else {
            return "existing_without_venv"
        }
    }
    else {
        # New project
        return "new_project"
    }
}

# Function to prompt for installation mode
function Get-InstallMode {
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  Installation Options" -ForegroundColor Cyan
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "How would you like to install Dango?"
    Write-Host ""
    Write-Host "[1] Virtual Environment (Recommended)" -ForegroundColor Green
    Write-Host "    ✓ Keeps Dango separate from other Python programs"
    Write-Host "    ✓ Safe to experiment - won't affect anything else"
    Write-Host "    ✗ Requires one setup command each time (we'll show you)"
    Write-Host "    ✗ Easy to forget - you'll see an error if you do"
    Write-Host ""
    Write-Host "[2] Global Install (More convenient)" -ForegroundColor Yellow
    Write-Host "    ✓ Works immediately - no setup needed"
    Write-Host "    ✓ Just type 'dango' anywhere"
    Write-Host "    ⚠ May upgrade packages that other Python programs use"
    Write-Host "      (We'll check for conflicts before installing)"
    Write-Host ""
    Write-Host "Tip: Press Ctrl+C anytime to quit" -ForegroundColor DarkGray
    Write-Host ""

    do {
        $choice = Read-Answer -Name 'DANGO_INSTALL_MODE' -Prompt "Choose [1] or [2]" -Pattern '^[12]\z'
        if ($choice -notmatch '^[12]$') {
            Write-Host "✗ Please enter 1 or 2" -ForegroundColor Red
        }
    } while ($choice -notmatch '^[12]$')

    if ($choice -eq "1") {
        return "venv"
    } else {
        return "global"
    }
}

# Function to prompt for setup mode
function Get-SetupMode {
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  What would you like to do?" -ForegroundColor Cyan
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "[1] Just install Dango" -ForegroundColor Green -NoNewline
    Write-Host " (set up projects later)"
    Write-Host ""
    Write-Host "[2] Install Dango + create a new project now" -ForegroundColor Green
    Write-Host ""

    do {
        $choice = Read-Answer -Name 'DANGO_INSTALL_SETUP' -Prompt "Choose [1] or [2]" -Pattern '^[12]\z'
        if ($choice -notmatch '^[12]$') {
            Write-Host "✗ Please enter 1 or 2" -ForegroundColor Red
        }
    } while ($choice -notmatch '^[12]$')

    if ($choice -eq "1") {
        return "install_only"
    } else {
        return "install_and_project"
    }
}

# Function to create virtual environment
function New-VirtualEnvironment {
    param([string]$Path, [string]$PythonCmd)

    Write-Step "Creating virtual environment..."

    & $PythonCmd -m venv $Path

    if ($LASTEXITCODE -ne 0) {
        Write-Error-Message "Failed to create virtual environment"
        exit 1
    }

    Write-Success "Virtual environment created at $Path"
    Write-Host ""
}

# Function to install Dango
function Install-Dango {
    param([string]$VenvPath)

    Write-Step "Installing Dango from PyPI..."

    # Activate venv and install
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"

    if (-not (Test-Path $activateScript)) {
        Write-Error-Message "Virtual environment activation script not found"
        exit 1
    }

    & $activateScript

    # Upgrade pip silently
    & pip install --upgrade pip --quiet

    # Install getdango from PyPI
    & pip install $script:DangoSpec

    if ($LASTEXITCODE -ne 0) {
        Write-Error-Message "Failed to install Dango"
        exit 1
    }

    # Get installed version
    $version = & dango --version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+[a-zA-Z0-9]*' | ForEach-Object { $_.Matches.Value }
    if (-not $version) { $version = "unknown" }

    Write-Success "Dango $version installed"
    Write-Host ""
}

# Function to upgrade Dango
function Update-Dango {
    param([string]$VenvPath)

    Write-Step "Upgrading Dango..."

    # Activate venv and upgrade
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    & $activateScript

    # Upgrade from PyPI
    & pip install --upgrade $script:DangoSpec --quiet

    if ($LASTEXITCODE -ne 0) {
        Write-Error-Message "Failed to upgrade Dango"
        exit 1
    }

    # Get installed version
    $version = & dango --version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+[a-zA-Z0-9]*' | ForEach-Object { $_.Matches.Value }
    if (-not $version) { $version = "unknown" }

    Write-Success "Dango upgraded to $version"
    Write-Host ""
}

# Function to install Dango globally
function Install-DangoGlobal {
    param([string]$PythonCmd)

    Write-Step "Installing Dango globally..."
    Write-Host ""

    # Install from PyPI
    & $PythonCmd -m pip install --user $script:DangoSpec

    if ($LASTEXITCODE -ne 0) {
        Write-Error-Message "Failed to install Dango from PyPI"
        Write-Host ""
        Write-Host "Possible causes:"
        Write-Host "  • No internet connection"
        Write-Host "  • PyPI is down"
        Write-Host "  • Python version incompatible"
        Write-Host ""
        Write-Host "Check errors above and try again"
        exit 1
    }
    Write-Host ""

    # Get the user Scripts directory
    $userScriptsDir = & $PythonCmd -c "import site, os; print(os.path.join(site.USER_BASE, 'Scripts'))"

    # Check if dango command is accessible BEFORE we modify PATH
    $dangoWasInPath = $false
    if (Get-Command dango -ErrorAction SilentlyContinue) {
        $dangoWasInPath = $true
    }

    # If dango already in PATH, we're done
    if ($dangoWasInPath) {
        $version = & dango --version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+[a-zA-Z0-9]*' | ForEach-Object { $_.Matches.Value }
        if (-not $version) { $version = "unknown" }
        Write-Success "Dango $version installed and ready to use!"
        Write-Host ""
        return $true
    } else {
        # Not in PATH - need to add it
        Write-Warning-Message "Dango installed but not in PATH"
        Write-Host ""
        Write-Host "The 'dango' command is installed at:"
        Write-Host "  $userScriptsDir\dango.exe" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "To use 'dango' from anywhere, add this to your PATH:"
        Write-Host "  $userScriptsDir" -ForegroundColor Yellow
        Write-Host ""

        $response = Read-Answer -Name 'DANGO_INSTALL_ADD_TO_PATH' -Prompt "Would you like me to add it automatically? [y/N]" -Default 'n' -Pattern '^[YyNn]\z'

        if ($response -match '^[Yy]$') {
            # Add to user PATH
            $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
            if ($currentPath -notlike "*$userScriptsDir*") {
                [Environment]::SetEnvironmentVariable("Path", "$currentPath;$userScriptsDir", "User")
                Write-Success "Added to PATH"
            } else {
                Write-Info "Already in PATH"
            }
            Write-Host ""

            # CRITICAL: Refresh PATH in current session immediately
            $env:Path = [System.Environment]::GetEnvironmentVariable('Path','User') + ';' +
                        [System.Environment]::GetEnvironmentVariable('Path','Machine')

            # Set script-level flag so we know to show restart warning later
            $script:PathWasAdded = $true

            # Verify dango is now accessible
            if (Get-Command dango -ErrorAction SilentlyContinue) {
                Write-Success "dango command is now available!"
                Write-Host ""
                return $true
            } else {
                Write-Warning-Message "Failed to add dango to PATH"
                Write-Host ""
                Write-Host "Please restart PowerShell and try again."
                Write-Host ""
                return $false
            }
        } else {
            Write-Host ""
            Write-Info "Skipped automatic configuration"
            Write-Host ""
            Write-Host "To add manually:"
            Write-Host "  1. Search for 'Environment Variables' in Windows"
            Write-Host "  2. Edit 'Path' under User variables"
            Write-Host "  3. Add: $userScriptsDir"
            Write-Host ""
            return $false
        }
    }
}

# Function to initialize new project
function Initialize-Project {
    param([string]$VenvPath)

    Write-Step "Initializing Dango project..."
    Write-Host ""

    if ($VenvPath) {
        # Activate venv and run init
        $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
        & $activateScript
    }

    if ($env:DANGO_INSTALL_NONINTERACTIVE -eq "1") {
        & dango init --skip-wizard
    } else {
        & dango init
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Error-Message "Failed to initialize project"
        exit 1
    }

    Write-Success "Project initialized"
    Write-Host ""
}

# Function to print activation instructions
function Write-ActivationInstructions {
    param(
        [string]$VenvPath,
        [string]$ProjectDir,
        [bool]$CreatedSubdir
    )

    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "Installation complete!" -ForegroundColor Green
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "✓ Your environment is activated and ready!" -ForegroundColor Green
    Write-Host ""
    Write-Host "💡 For future sessions, activate your environment with:" -ForegroundColor Cyan
    Write-Host "  .\$VenvPath\Scripts\Activate.ps1" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Try these commands now:"
    Write-Host "  dango source add" -ForegroundColor Yellow -NoNewline
    Write-Host "    # Add a data source"
    Write-Host "  dango sync" -ForegroundColor Yellow -NoNewline
    Write-Host "          # Sync data"
    Write-Host "  dango start" -ForegroundColor Yellow -NoNewline
    Write-Host "         # Start platform (opens http://localhost:8800)"
    Write-Host ""
    Write-Host "Documentation: https://github.com/getdango/dango"
    Write-Host "Get help: https://github.com/getdango/dango/issues"
    Write-Host ""
}

# Function to print success message (for venv existing scenarios)
function Write-SuccessMessage {
    param([string]$VenvPath)

    Write-Host "✓ Dango is ready to use!" -ForegroundColor Green
    Write-Host "Make sure to activate your venv: " -NoNewline
    Write-Host ".\$VenvPath\Scripts\Activate.ps1" -ForegroundColor Yellow
    Write-Host ""
}

# Function to print global install success message
function Write-GlobalSuccess {
    param([string]$ProjectDir)

    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "Installation complete!" -ForegroundColor Green
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "✓ Dango is installed globally!" -ForegroundColor Green
    Write-Host ""

    # If we added PATH during install, user needs to restart PowerShell
    # (Even though dango works in THIS script, it won't work in user's terminal after script exits)
    if ($script:PathWasAdded -eq $true) {
        Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
        Write-Host "⚠  ONE MORE STEP" -ForegroundColor Yellow
        Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "'dango' is installed, but your current PowerShell can't see it yet."
        Write-Host ""
        Write-Host "To fix this: Restart PowerShell (close and reopen this window)"
        Write-Host ""
        Write-Host "After that, 'dango' will work from anywhere!"
        Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
        Write-Host ""
    }

    if ($ProjectDir) {
        Write-Host "Your project is ready at: " -NoNewline
        Write-Host $ProjectDir -ForegroundColor Green
        Write-Host ""
        Write-Host "Next steps:"
        Write-Host "  cd $ProjectDir" -ForegroundColor Yellow
    } else {
        Write-Host "Next steps:"
        Write-Host "  dango init my-project" -ForegroundColor Yellow -NoNewline
        Write-Host "  # Create a new project"
        Write-Host "  cd my-project" -ForegroundColor Yellow
    }

    Write-Host "  dango source add" -ForegroundColor Yellow -NoNewline
    Write-Host "       # Add a data source"
    Write-Host "  dango sync" -ForegroundColor Yellow -NoNewline
    Write-Host "             # Sync data"
    Write-Host "  dango start" -ForegroundColor Yellow -NoNewline
    Write-Host "            # Start platform (opens http://localhost:8800)"
    Write-Host ""

    # Only show "no activation needed" if PATH didn't need to be added
    if ($script:PathWasAdded -ne $true) {
        Write-Host "No activation needed - 'dango' command works from anywhere!" -ForegroundColor Green
        Write-Host ""
    }

    Write-Host "Documentation: https://github.com/getdango/dango"
    Write-Host "Get help: https://github.com/getdango/dango/issues"
    Write-Host ""
}

# Main installation logic
function Main {
    Write-Header

    # Opt-in test hook: refuse a malformed DANGO_INSTALL_SPEC before anything runs
    Test-InstallSpec

    # Check prerequisites
    Test-ExecutionPolicy
    $pythonInfo = Test-PythonVersion
    Test-Docker

    # Detect scenario
    $scenario = Get-InstallScenario

    switch ($scenario) {
        "new_project" {
            Write-Info "Scenario: New Dango project"
            Write-Host ""

            # Prompt for installation mode
            $installMode = Get-InstallMode
            Write-Host ""

            # Prompt for setup mode
            $setupMode = Get-SetupMode
            Write-Host ""

            # Handle install_only mode
            if ($setupMode -eq "install_only") {
                if ($installMode -eq "venv") {
                    # Venv install only
                    New-VirtualEnvironment -Path "venv" -PythonCmd $pythonInfo.Command
                    Install-Dango -VenvPath "venv"

                    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
                    Write-Host "Installation complete!" -ForegroundColor Green
                    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
                    Write-Host ""
                    Write-Host "✓ Dango is installed!" -ForegroundColor Green
                    Write-Host ""
                    Write-Host "To create your first project:"
                    Write-Host "  .\venv\Scripts\Activate.ps1" -ForegroundColor Yellow
                    Write-Host "  dango init my-project" -ForegroundColor Yellow
                    Write-Host "  cd my-project" -ForegroundColor Yellow
                    Write-Host "  dango source add" -ForegroundColor Yellow
                    Write-Host "  dango sync" -ForegroundColor Yellow
                    Write-Host "  dango start" -ForegroundColor Yellow
                    Write-Host ""
                    Write-Host "Documentation: https://github.com/getdango/dango"
                    Write-Host "Get help: https://github.com/getdango/dango/issues"
                    Write-Host ""
                } else {
                    # Global install only
                    Install-DangoGlobal -PythonCmd $pythonInfo.Command

                    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
                    Write-Host "Installation complete!" -ForegroundColor Green
                    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
                    Write-Host ""
                    Write-Host "✓ Dango is installed!" -ForegroundColor Green
                    Write-Host ""

                    # Show restart warning if PATH was added
                    if ($script:PathWasAdded -eq $true) {
                        Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
                        Write-Host "⚠  ONE MORE STEP" -ForegroundColor Yellow
                        Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
                        Write-Host ""
                        Write-Host "'dango' is installed, but your current PowerShell can't see it yet."
                        Write-Host ""
                        Write-Host "To fix this: Restart PowerShell (close and reopen this window)"
                        Write-Host ""
                        Write-Host "After that, 'dango' will work from anywhere!"
                        Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
                        Write-Host ""
                    }

                    Write-Host "To create your first project:"
                    Write-Host "  dango init my-project" -ForegroundColor Yellow
                    Write-Host "  cd my-project" -ForegroundColor Yellow
                    Write-Host "  dango source add" -ForegroundColor Yellow
                    Write-Host "  dango sync" -ForegroundColor Yellow
                    Write-Host "  dango start" -ForegroundColor Yellow
                    Write-Host ""
                    Write-Host "Documentation: https://github.com/getdango/dango"
                    Write-Host "Get help: https://github.com/getdango/dango/issues"
                    Write-Host ""
                }

                exit 0
            }

            # Handle install_and_project mode (existing behavior)
            # Get project directory name
            do {
                $projectDir = Read-Answer -Name 'DANGO_INSTALL_PROJECT_NAME' -Prompt "Enter project directory name (e.g., my-analytics)" -Pattern '.'

                # Non-interactive: clean the name as below first, so it is checked as it is used
                if ($env:DANGO_INSTALL_NONINTERACTIVE -eq "1") {
                    $typedName = $projectDir
                    $projectDir = ($projectDir.ToLower() -replace '\s+', '-' -replace '[^a-z0-9-]', '')
                    if ($projectDir -cnotmatch '^[a-z0-9][a-z0-9-]*\z') {
                        Stop-IfNonInteractive "DANGO_INSTALL_PROJECT_NAME='$typedName' is not usable as a project directory name (after cleanup: lowercase letters, digits and dashes, starting with a letter or digit)"
                    }
                }

                if ([string]::IsNullOrWhiteSpace($projectDir)) {
                    Write-Error-Message "Project name cannot be empty"
                    Write-Host ""
                    continue
                }

                # Check if directory exists
                if (Test-Path $projectDir) {
                    Stop-IfNonInteractive "DANGO_INSTALL_PROJECT_NAME='$projectDir' already exists as a directory"
                    Write-Error-Message "Directory '$projectDir' already exists"
                    Write-Host "Please choose a different name."
                    Write-Host ""
                    continue
                }

                # Valid input, break the loop
                break
            } while ($true)

            # Sanitize: lowercase, spaces → dashes, remove special chars
            $projectDir = ($projectDir.ToLower() -replace '\s+', '-' -replace '[^a-z0-9-]', '')

            Write-Host ""
            Write-Step "Creating project directory: $projectDir"
            New-Item -ItemType Directory -Path $projectDir | Out-Null
            Set-Location $projectDir
            Write-Success "Directory created"
            Write-Host ""

            # Install based on mode
            if ($installMode -eq "venv") {
                # Create venv
                New-VirtualEnvironment -Path "venv" -PythonCmd $pythonInfo.Command

                # Install Dango
                Install-Dango -VenvPath "venv"

                # Initialize project
                Initialize-Project -VenvPath "venv"

                # Show activation instructions
                Write-ActivationInstructions -VenvPath "venv" -ProjectDir $projectDir -CreatedSubdir $true
            } else {
                # Global install
                $success = Install-DangoGlobal -PythonCmd $pythonInfo.Command

                if ($success) {
                    # Initialize project (no venv needed)
                    Initialize-Project -VenvPath $null

                    # Show global success message
                    Write-GlobalSuccess -ProjectDir $projectDir
                } else {
                    Write-Warning-Message "Installation completed but PATH setup may be needed"
                    Write-Host "Please restart PowerShell and run 'dango init' in your project directory."
                }
            }
        }

        "existing_with_venv" {
            Write-Info "Scenario: Existing Dango project with venv"
            Write-Host ""

            Write-Host "Found existing Dango project with virtual environment."
            Write-Host ""
            Write-Host "What would you like to do?"
            Write-Host "  [i] Install Dango (if not installed)"
            Write-Host "  [u] Upgrade Dango to latest version"
            Write-Host "  [c] Cancel"
            Write-Host ""

            $action = Read-Answer -Name 'DANGO_INSTALL_EXISTING_ACTION' -Prompt "Choice" -Pattern '^[iIuUcC]\z'

            switch ($action.ToLower()) {
                "i" {
                    Install-Dango -VenvPath "venv"
                    Write-SuccessMessage -VenvPath "venv"
                }
                "u" {
                    Update-Dango -VenvPath "venv"
                    Write-SuccessMessage -VenvPath "venv"
                }
                default {
                    Write-Info "Cancelled"
                    exit 0
                }
            }
        }

        "existing_without_venv" {
            Write-Warning-Message "Detected Dango project without virtual environment"
            Write-Host ""

            Write-Host "This project needs a virtual environment for Dango."
            Write-Host ""

            $response = Read-Answer -Name 'DANGO_INSTALL_CREATE_VENV' -Prompt "Create virtual environment now? [Y/n]" -Default 'y' -Pattern '^[YyNn]\z'

            if ($response -match '^[Nn]$') {
                Write-Info "Cancelled"
                Write-Host ""
                Write-Host "To create venv manually:"
                Write-Host "  $($pythonInfo.Command) -m venv venv"
                Write-Host "  .\venv\Scripts\Activate.ps1"
                Write-Host "  pip install getdango"
                Write-Host ""
                exit 0
            }

            Write-Host ""

            # Create venv
            New-VirtualEnvironment -Path "venv" -PythonCmd $pythonInfo.Command

            # Install Dango
            Install-Dango -VenvPath "venv"

            # Show activation instructions
            $currentDir = Split-Path -Leaf (Get-Location)
            Write-ActivationInstructions -VenvPath "venv" -ProjectDir $currentDir -CreatedSubdir $false
        }
    }
}

# Run main function
try {
    Main
}
catch {
    Write-Host ""
    Write-Error-Message "An error occurred: $_"
    Write-Host ""
    Write-Host "Please report this issue at:"
    Write-Host "  https://github.com/getdango/dango/issues"
    Write-Host ""
    exit 1
}
