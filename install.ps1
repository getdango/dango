# Dango Bootstrap Installer for Windows
# Version: 0.0.1
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

                    # Check if version is 3.10+
                    if ($major -eq 3 -and $minor -ge 10) {
                        $pythonCmd = $cmd
                        $pythonVersion = $version
                        break
                    }
                    elseif ($major -gt 3) {
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
        Write-Error-Message "Python 3.10+ not found"
        Write-Host ""
        Write-Host "Please install Python 3.10 or higher:"
        Write-Host "  Download: https://www.python.org/downloads/"
        Write-Host "  Or use: winget install Python.Python.3.11"
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

    # Install getdango
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

# Function to initialize new project
function Initialize-Project {
    param([string]$VenvPath)

    Write-Step "Initializing Dango project..."
    Write-Host ""

    # Activate venv and run init
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    & $activateScript

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
    Write-Host "⚠️  IMPORTANT: Activate your environment first!" -ForegroundColor Yellow
    Write-Host ""

    if ($CreatedSubdir) {
        Write-Host "Run these commands to get started:"
        Write-Host ""
        Write-Host "  cd $ProjectDir" -ForegroundColor Yellow
        Write-Host "  .\$VenvPath\Scripts\Activate.ps1" -ForegroundColor Yellow
    }
    else {
        Write-Host "Run this command to activate:"
        Write-Host ""
        Write-Host "  .\$VenvPath\Scripts\Activate.ps1" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "You need to activate the environment EVERY TIME you work on this project." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Once activated, try these commands:"
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

# Function to print success message
function Write-SuccessMessage {
    param(
        [string]$VenvPath,
        [bool]$IsActivated
    )

    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "Installation complete!" -ForegroundColor Green
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""

    if ($IsActivated) {
        Write-Host "✓ Your environment is already activated!" -ForegroundColor Green
    }
    else {
        Write-Host "⚠️  Don't forget to activate your environment:" -ForegroundColor Yellow
        Write-Host "  .\$VenvPath\Scripts\Activate.ps1" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Quick start commands:"
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

# Main installation logic
function Main {
    Write-Header

    # Check prerequisites
    $pythonInfo = Test-PythonVersion
    Test-Docker

    # Detect scenario
    $scenario = Get-InstallScenario

    switch ($scenario) {
        "new_project" {
            Write-Info "Scenario: New Dango project"
            Write-Host ""

            # Get project directory name
            $projectDir = Read-Host "Enter project directory name (e.g., my-analytics)"

            if ([string]::IsNullOrWhiteSpace($projectDir)) {
                Write-Error-Message "Project name cannot be empty"
                exit 1
            }

            # Check if directory exists
            if (Test-Path $projectDir) {
                Write-Error-Message "Directory '$projectDir' already exists"
                Write-Host ""
                Write-Host "Please choose a different name or remove the existing directory."
                exit 1
            }

            Write-Host ""
            Write-Step "Creating project directory: $projectDir"
            New-Item -ItemType Directory -Path $projectDir | Out-Null
            Set-Location $projectDir
            Write-Success "Directory created"
            Write-Host ""

            # Create venv
            New-VirtualEnvironment -Path "venv" -PythonCmd $pythonInfo.Command

            # Install Dango
            Install-Dango -VenvPath "venv"

            # Initialize project
            Initialize-Project -VenvPath "venv"

            # Show activation instructions
            Write-ActivationInstructions -VenvPath "venv" -ProjectDir $projectDir -CreatedSubdir $true
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
                    Write-SuccessMessage -VenvPath "venv" -IsActivated $true
                }
                "u" {
                    Update-Dango -VenvPath "venv"
                    Write-SuccessMessage -VenvPath "venv" -IsActivated $true
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
