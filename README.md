# 🍡 Dango

**Open source data platform built with production-grade tools**

Works on your laptop today. Production deployment coming soon.

Dango deploys a complete data stack (dlt + dbt + DuckDB + Metabase) to your laptop with one command.

## Installation

### Prerequisites

#### Python 3.10-3.12 (Required)

**Recommended:** Python 3.11 or 3.12

**Supported versions:** Python 3.10, 3.11, 3.12

**Check if you have Python:**
```bash
# Try these commands in order:
python3.12 --version  # Check for Python 3.12
python3.11 --version  # Check for Python 3.11
python3.10 --version  # Check for Python 3.10

# If any show "3.10" or higher, you're good!
```

**If you don't have Python 3.10+, install it:**

**macOS:**
1. Install Homebrew (if you don't have it):
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
2. Install Python:
   ```bash
   brew install python@3.11
   ```
3. Verify: `python3.11 --version`

**Linux (Ubuntu/Debian):**
```bash
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip
```

**Linux (Fedora):**
```bash
sudo dnf install python3.11
```

**Windows:**
- Download from [python.org](https://www.python.org/downloads/)
- OR install from Microsoft Store (search "Python 3.11")
- **Important:** Check "Add Python to PATH" during installation

#### Docker Desktop (Required)

**Required for:** Metabase dashboards, Web UI, dbt docs visualization

**All platforms:** [Download Docker Desktop](https://docs.docker.com/desktop/)

After installing, start Docker Desktop and verify:
```bash
docker --version
```

#### Disk Space Requirements

**Installation:** ~5GB
- Docker Desktop: ~4.5GB
- Python packages (dlt, dbt, DuckDB, etc.): ~400MB
- Dango platform: ~100MB

**Data Storage:** Varies by data volume
- Small datasets (< 100K rows): < 100MB
- Medium datasets (100K - 1M rows): 100MB - 1GB
- Large datasets (> 1M rows): 1GB+

**Recommendation:** Have at least 10GB free space before installing.

#### Supported Platforms
- macOS (Intel and Apple Silicon)
- Linux (Ubuntu 20.04+, Debian 11+, Fedora 35+)
- Windows 10/11

---

### Verify Prerequisites

Before installing, check you have everything:

**macOS / Linux:**
```bash
# Check Python (any of these should work):
python3.12 --version  # 3.12.x ✓
python3.11 --version  # 3.11.x ✓
python3.10 --version  # 3.10.x ✓

# Check Docker (required):
docker --version

# Check disk space:
df -h .
```

**Windows:**
```powershell
python --version     # Should show 3.10 or higher
docker --version
```

---

### Quick Install (Recommended)

**macOS / Linux:**

```bash
curl -sSL https://getdango.dev/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://getdango.dev/install.ps1 | iex
```

This will:
- Create a project directory
- Set up an isolated virtual environment
- Install Dango from PyPI
- Initialize your project interactively

**For security-conscious users (inspect first):**

**macOS / Linux:**
```bash
# Download the installer
curl -sSL https://getdango.dev/install.sh -o install.sh

# Review what it does
cat install.sh

# Run when ready
bash install.sh
```

**Windows (PowerShell):**
```powershell
# Download the installer
Invoke-WebRequest -Uri https://getdango.dev/install.ps1 -OutFile install.ps1

# Review what it does
Get-Content install.ps1

# Run when ready
.\install.ps1
```

View the installer source: [install.sh](https://github.com/getdango/dango/blob/main/install.sh) | [install.ps1](https://github.com/getdango/dango/blob/main/install.ps1)

### Manual Installation

If you prefer to set things up yourself:

**macOS / Linux:**
```bash
# Create project directory
mkdir my-analytics
cd my-analytics

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Dango
pip install getdango

# Initialize project
dango init
```

**Windows (PowerShell):**
```powershell
# Create project directory
New-Item -ItemType Directory -Path my-analytics
Set-Location my-analytics

# Create virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install Dango
pip install getdango

# Initialize project
dango init
```

## Quick Start

**macOS / Linux:**
```bash
# If you used the bootstrap installer, activate your environment
cd my-analytics
source venv/bin/activate

# Add a data source (CSV or Stripe)
dango source add

# Sync your data
dango sync

# Verify installation
dango --version
# Should show: dango, version X.X.X

# Start the platform (Web UI + Metabase + dbt docs)
dango start

# Open dashboard
open http://localhost:8800
```

**Windows (PowerShell):**
```powershell
# If you used the bootstrap installer, activate your environment
cd my-analytics
.\venv\Scripts\Activate.ps1

# Add a data source (CSV or Stripe)
dango source add

# Sync your data
dango sync

# Verify installation
dango --version
# Should show: dango, version X.X.X

# Start the platform (Web UI + Metabase + dbt docs)
dango start

# Open dashboard
Start-Process http://localhost:8800
```

**What you get:**
- **Web UI** at `http://localhost:8800` - Monitor your data pipeline
- **dlt** for data ingestion (29+ verified sources)
- **dbt** for SQL transformations and modeling
- **DuckDB** as your analytics database
- **Metabase** for dashboards and SQL queries

## Features (v0.1.0)

**✅ What Works Now:**
- ✅ Full CLI with 10+ commands
- ✅ CSV data sources (upload and auto-sync)
- ✅ Stripe integration (tested and working)
- ✅ **Google Sheets** with OAuth authentication
- ✅ **Google Analytics (GA4)** with OAuth authentication
- ✅ **Facebook Ads** with OAuth authentication (60-day token)
- ✅ **Google Ads** with OAuth authentication
- ✅ dbt auto-generation for staging models
- ✅ Web UI with live monitoring
- ✅ Metabase dashboards (auto-configured)
- ✅ File watcher with auto-triggers
- ✅ DuckDB as embedded analytics database
- ✅ Token expiry warnings and validation
- ✅ Custom sources via `dlt_native` type

**📝 v0.1.0 MVP Release**
- First stable release for early adopters
- Google Ads OAuth support fully tested
- Shorter install URLs via getdango.dev
- Windows support fully tested

**📝 Previous releases**
- v0.0.5: `--dry-run` flag, unreferenced sources warning
- v0.0.3: OAuth authentication for Google and Facebook sources

**🔮 Coming Soon:**
- Demo project with sample data
- Full documentation website at docs.getdango.dev

**🔮 Beyond v0.1.0:**
- Cloud deployment guides and infrastructure templates
- Advanced scheduling and orchestration
- Team collaboration features
- Enhanced monitoring and alerting

## Architecture

**Data Layers:**
- `raw` - Immutable source of truth (with metadata)
- `staging` - Clean, deduplicated data
- `intermediate` - Reusable business logic
- `marts` - Final business metrics

**Tech Stack:**
- **DuckDB** - Analytics database (embedded, fast)
- **dbt** - SQL transformations
- **dlt** - API integrations (29 sources: 27 verified + CSV + REST)
- **Metabase** - BI dashboards
- **Docker** - Service orchestration
- **FastAPI** - Web UI backend
- **nginx** - Reverse proxy with domain routing

## Target Users

- Solo data professionals
- Fractional consultants
- SMEs needing analytics fast
- Anyone who wants a "real" data stack without the complexity

## Why Dango?

**Most tools force you to choose:**
- ❌ Simple setup (limited features) OR Enterprise platforms (expensive, complex)
- ❌ No-code (inflexible) OR Full-code (steep learning curve)
- ❌ Fast setup (toy project) OR Production-grade (weeks of work)

**Dango gives you both:**
- ✅ Runs on your laptop now. Production infrastructure support planned.
- ✅ Wizard-driven AND fully customizable
- ✅ Fast setup AND best practices built-in

## Troubleshooting

### Installation Issues

**"Python version too old"**
```bash
# Check your version
python3 --version

# Install Python 3.11 (recommended)
# macOS:
brew install python@3.11

# Ubuntu:
sudo apt install python3.11
```

**"Docker not found" or "Docker not running"**
- Install Docker Desktop: https://docs.docker.com/desktop/
- Make sure Docker is running (check system tray icon)
- You can continue without Docker, but Metabase won't work

**"pip install getdango failed"**
```bash
# Check internet connection
ping pypi.org

# Upgrade pip
pip install --upgrade pip

# Try again
pip install getdango
```

**"Permission denied"**
- Don't use `sudo` with pip in a virtual environment
- Check directory permissions: `ls -la`

### Runtime Issues

**"dango: command not found" (virtual environment)**
```bash
# Make sure venv is activated
source venv/bin/activate

# Or use full path
./venv/bin/dango --version
```

**"dango: command not found" (after global install)**

If you just installed Dango globally, your terminal needs to reload:

**Option 1: Restart terminal (recommended)**

Close and reopen your terminal window, then verify:
```bash
dango --version
```

**Option 2: Reload shell config**
```bash
# For zsh users:
source ~/.zshrc

# For bash on macOS:
source ~/.bash_profile

# For bash on Linux:
source ~/.bashrc

# Then verify:
dango --version
```

**"Port 8800 already in use"**
```bash
# Option 1: Kill the process using the port
lsof -ti:8800 | xargs kill -9

# Option 2: Change the port in .dango/project.yml
# Edit platform.port to a different value
```

**"Metabase not starting"**
```bash
# Check Docker is running
docker ps

# Check container logs
docker ps  # Get container ID
docker logs <container-id>

# Restart everything
dango stop
dango start
```

**"Sync failed" or "Source connection error"**
- Check your API credentials in `.dango/sources.yml`
- Verify internet connection
- Check source-specific documentation

### Getting Help

- **GitHub Issues:** https://github.com/getdango/dango/issues
- **View Documentation:** https://github.com/getdango/dango
- **Check Changelog:** [CHANGELOG.md](CHANGELOG.md)

---

## Upgrading Dango

### Automatic Upgrade (Recommended)

If you installed with the bootstrap script:

```bash
cd your-project
curl -sSL https://getdango.dev/install.sh | bash
# Select [u] to upgrade when prompted
```

### Manual Upgrade

```bash
cd your-project
source venv/bin/activate

# Upgrade to latest version
pip install --upgrade getdango

# Verify new version
dango --version
```

### After Upgrading

```bash
# Validate project still works
dango validate

# Restart the platform
dango stop
dango start
```

### Breaking Changes

Check [CHANGELOG.md](CHANGELOG.md) for breaking changes between versions.

---

## Uninstall

### Which Installation Method Did I Use?

**Virtual Environment (venv):**
- You have a `venv/` folder in your project directory
- Need to activate with `source venv/bin/activate` (macOS/Linux) or `.\venv\Scripts\Activate.ps1` (Windows)

**Global Install:**
- No `venv/` folder in your project
- `dango` command works from anywhere without activation

### Uninstall Virtual Environment Installation

**macOS / Linux:**
```bash
# Simply delete the project directory
rm -rf my-analytics/
```

**Windows:**
```powershell
# Simply delete the project directory
Remove-Item -Recurse -Force my-analytics
```

That's it! Everything (venv, data, config) is contained in the project directory.

### Uninstall Global Installation

**Step 1: Find which Python has Dango**

**macOS / Linux:**
```bash
# Check each Python version
python3.11 -m pip list | grep getdango
python3.10 -m pip list | grep getdango
python3 -m pip list | grep getdango

# Or find the command location
which dango
# Example output: /Users/you/Library/Python/3.11/bin/dango
# This means use python3.11
```

**Windows:**
```powershell
# Check Python versions
python -m pip list | findstr getdango
py -3.11 -m pip list | findstr getdango
```

**Step 2: Uninstall Dango**

**macOS / Linux:**
```bash
# Use the Python version that has it (e.g., python3.11)
python3.11 -m pip uninstall getdango
```

**Windows:**
```powershell
python -m pip uninstall getdango
```

**Step 3: Clean up PATH (if you added it)**

**macOS / Linux:**
```bash
# Edit your shell config file
# zsh users:
nano ~/.zshrc

# bash users (macOS):
nano ~/.bash_profile

# bash users (Linux):
nano ~/.bashrc

# Remove the line that looks like:
# export PATH="/Users/you/Library/Python/3.11/bin:$PATH"

# Save and reload
source ~/.zshrc  # or ~/.bash_profile or ~/.bashrc
```

**Windows:**
1. Search for "Environment Variables" in Windows search
2. Click "Edit the system environment variables"
3. Click "Environment Variables" button
4. Under "User variables", select "Path" and click "Edit"
5. Find and remove the entry with `Python\...\Scripts`
6. Click OK to save
7. Restart PowerShell

### Remove Docker Containers (Optional)

If you're done with Dango entirely, clean up Docker:

**macOS / Linux:**
```bash
# List running containers
docker ps

# Stop Metabase container
docker stop <metabase-container-id>

# Remove Metabase image (saves disk space)
docker rmi metabase/metabase
```

**Windows:**
```powershell
# List running containers
docker ps

# Stop Metabase container
docker stop <metabase-container-id>

# Remove Metabase image (saves disk space)
docker rmi metabase/metabase
```

---

## Contributing

We're in active MVP development! Contributions welcome after v0.1.0 releases.

## License

Apache 2.0 - See [LICENSE](LICENSE) for details.

## Links

- **PyPI:** https://pypi.org/project/getdango/
- **GitHub:** https://github.com/getdango/dango
- **Issues:** https://github.com/getdango/dango/issues
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)

---

Built with ❤️ for solo data professionals and small teams
