# Dango Bootstrap Installer for Windows
# Version: 0.0.5
# Purpose: Install Dango with per-project virtual environment
# Platform: Windows 10/11 with PowerShell 5.1+

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
    $response = Read-Host "Allow script execution for your user? [Y/n]"

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
    $commands = @("python3.12", "python3.11", "python3.10", "python3", "python", "py")

    foreach ($cmd in $commands) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            try {
                $version = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($version) {
                    $versionParts = $version -split '\.'
                    $major = [int]$versionParts[0]
                    $minor = [int]$versionParts[1]

                    # Check if version is 3.10-3.12
                    if ($major -eq 3 -and $minor -ge 10 -and $minor -le 12) {
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
        Write-Error-Message "Python 3.10-3.12 not found"
        Write-Host ""
        Write-Host "Dango requires Python 3.10, 3.11, or 3.12 (recommended)"
        Write-Host ""
        Write-Host "Install options:"
        Write-Host "  winget install Python.Python.3.12"
        Write-Host "  Or download: https://www.python.org/downloads/"
        Write-Host ""
        Write-Host "Note: Python 3.13+ not yet supported due to dependency compatibility"
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

        $response = Read-Host "Continue without Docker? (y/N)"
        if ($response -notmatch '^[Yy]$') {
            Write-Info "Installation cancelled"
            Write-Host ""
            Write-Host "Install Docker and run this script again:"
            Write-Host "  irm get.getdango.dev | iex"
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

        $response = Read-Host "Continue? (y/N)"
        if ($response -notmatch '^[Yy]$') {
            Write-Info "Installation cancelled"
            Write-Host ""
            Write-Host "Start Docker and run this script again:"
            Write-Host "  irm get.getdango.dev | iex"
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
        $choice = Read-Host "Choose [1] or [2]"
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
        $choice = Read-Host "Choose [1] or [2]"
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
    & pip install getdango

    if ($LASTEXITCODE -ne 0) {
        Write-Error-Message "Failed to install Dango"
        exit 1
    }

    # Get installed version
    $version = & dango --version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+' | ForEach-Object { $_.Matches.Value }
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
    & pip install --upgrade getdango --quiet

    if ($LASTEXITCODE -ne 0) {
        Write-Error-Message "Failed to upgrade Dango"
        exit 1
    }

    # Get installed version
    $version = & dango --version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+' | ForEach-Object { $_.Matches.Value }
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
    & $PythonCmd -m pip install --user getdango

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
        $version = & dango --version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+' | ForEach-Object { $_.Matches.Value }
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

        $response = Read-Host "Would you like me to add it automatically? [y/N]"

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

    & dango init

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
    Write-Host "    # Add a data source (CSV or Stripe)"
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
    Write-Host "       # Add a data source (CSV or Stripe)"
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
                $projectDir = Read-Host "Enter project directory name (e.g., my-analytics)"

                if ([string]::IsNullOrWhiteSpace($projectDir)) {
                    Write-Error-Message "Project name cannot be empty"
                    Write-Host ""
                    continue
                }

                # Check if directory exists
                if (Test-Path $projectDir) {
                    Write-Error-Message "Directory '$projectDir' already exists"
                    Write-Host "Please choose a different name."
                    Write-Host ""
                    continue
                }

                # Valid input, break the loop
                break
            } while ($true)

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

            $action = Read-Host "Choice"

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

            $response = Read-Host "Create virtual environment now? [Y/n]"

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
