#!/bin/bash
set -e

# Dango Bootstrap Installer
# Version: see getdango on PyPI
# Purpose: Install Dango with per-project virtual environment
# Platform: macOS / Linux / Windows
#
# Test hooks (opt-in; with none of these set the installer behaves exactly as before):
#
#   DANGO_INSTALL_SPEC             pip argument that replaces "getdango" in every Dango pip
#                                  install (a wheel path, "getdango==1.0.15", ...). One pip
#                                  argument of letters, digits, spaces and . _ / : @ + = ~ , \ -
#                                  (no shell metacharacters or newlines, no leading "-", no
#                                  leading or trailing space); it is validated, always quoted
#                                  and never evaluated.
#   DANGO_INSTALL_NONINTERACTIVE   1 = read every answer from the variables below instead of
#                                  /dev/tty. A prompt whose variable is unset (and that has no
#                                  default) stops the installer with that variable's name; an
#                                  invalid value does the same. Never prompts, never guesses.
#
# Answer variables (used only when DANGO_INSTALL_NONINTERACTIVE=1):
#
#   DANGO_INSTALL_DOCKER_CONTINUE    y|n  continue when Docker is missing or not running (required)
#   DANGO_INSTALL_MODE               1|2  1 = virtual environment, 2 = global (required)
#   DANGO_INSTALL_SETUP              1|2  1 = install only, 2 = install + new project (required)
#   DANGO_INSTALL_VENV_LOCATION      1|2  1 = default location, 2 = custom (default 1)
#   DANGO_INSTALL_VENV_PATH          path of the custom virtual environment (required when
#                                    DANGO_INSTALL_VENV_LOCATION=2)
#   DANGO_INSTALL_CONFLICTS_CONTINUE y|n  continue a global install that would upgrade other
#                                    packages (required when that happens)
#   DANGO_INSTALL_ADD_TO_PATH        y|n  add the user bin directory to the shell config (default n)
#   DANGO_INSTALL_PROJECT_NAME       new project directory name (required for SETUP=2). It is
#                                    cleaned as when typed (lowercase, spaces to dashes, other
#                                    characters dropped) and must then match ^[a-z0-9][a-z0-9-]*$
#                                    and not already exist as a directory
#   DANGO_INSTALL_EXISTING_ACTION    i|u|c  install, upgrade or cancel, for an existing project
#                                    with a venv (required)
#   DANGO_INSTALL_CREATE_VENV        y|n  create the missing venv of an existing project (default y)
#
# `dango init` runs as `dango init --skip-wizard` in non-interactive mode.

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m' # No Color

# Print functions
print_header() {
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  Dango Installer - Open Source Data Platform${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

print_step() {
    echo -e "${CYAN}▶${NC} $1"
}

# The pip argument for every Dango install: DANGO_INSTALL_SPEC, or "getdango" when unset.
dango_pip_spec() {
    echo "${DANGO_INSTALL_SPEC:-getdango}"
}

# Reject a DANGO_INSTALL_SPEC that is not a single plain token (checked before anything runs).
validate_install_spec() {
    local spec="${DANGO_INSTALL_SPEC:-}"
    local allowed='^[A-Za-z0-9._/:@+=~, \\-]+$'
    if [ -z "$spec" ]; then
        return 0
    fi
    if [[ "$spec" == -* || "$spec" == " "* || "$spec" == *" " ]] || ! [[ "$spec" =~ $allowed ]]; then
        print_error "DANGO_INSTALL_SPEC must be one pip argument of letters, digits, spaces and . _ / : @ + = ~ , \\ - (no leading dash or edge spaces); got: $spec" >&2
        exit 1
    fi
}

# Project directory name from what was typed: lowercase, spaces to dashes, other characters dropped.
sanitize_project_name() {
    echo "$1" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-'
}

# Stop with a message naming the variable when a non-interactive run cannot continue.
fail_if_noninteractive() {
    if [ "${DANGO_INSTALL_NONINTERACTIVE:-}" = "1" ]; then
        print_error "$1" >&2
        exit 1
    fi
}

# read_answer <result_var> <ANSWER_ENV_VAR> <default> <pattern>
# Interactive: read one line from the terminal into <result_var>. Non-interactive
# (DANGO_INSTALL_NONINTERACTIVE=1): take it from <ANSWER_ENV_VAR>, else <default> ("" = none), and
# stop naming the variable when there is no answer or it does not match <pattern>.
# This is the only place install.sh reads from /dev/tty.
read_answer() {
    local __var=$1 __env=$2 __default=${3-} __pattern=${4-} __value
    if [ "${DANGO_INSTALL_NONINTERACTIVE:-}" != "1" ]; then
        read -r "$__var" < /dev/tty
        return
    fi
    __value="${!__env:-}"
    if [ -z "$__value" ]; then
        __value=$__default
    fi
    if [ -z "$__value" ]; then
        print_error "Non-interactive install: set $__env (no answer given, no default)" >&2
        exit 1
    fi
    if [ -n "$__pattern" ] && ! [[ "$__value" =~ $__pattern ]]; then
        print_error "Non-interactive install: $__env='$__value' is not valid (expected $__pattern)" >&2
        exit 1
    fi
    echo "(non-interactive) $__env=$__value" >&2
    printf -v "$__var" '%s' "$__value"
}

# Run `dango init`: the wizard on the terminal, or --skip-wizard when non-interactive.
run_dango_init() {
    if [ "${DANGO_INSTALL_NONINTERACTIVE:-}" = "1" ]; then
        dango init --skip-wizard < /dev/null
    else
        dango init < /dev/tty
    fi
}

# Platform family for install hints: macos, debian (Debian, Ubuntu and derivatives), fedora (Fedora,
# RHEL and derivatives) or other.
detect_distro_family() {
    local release_file=${1:-/etc/os-release}
    # An explicit file (the tests give one) is read as it is; the default also covers macOS
    if [ -z "${1:-}" ] && [ "$(uname -s)" = "Darwin" ]; then
        echo "macos"
        return
    fi
    local id="" like=""
    if [ -r "$release_file" ]; then
        id=$(sed -n 's/^ID=//p' "$release_file" | tr -d "\"'" | tr '[:upper:]' '[:lower:]')
        like=$(sed -n 's/^ID_LIKE=//p' "$release_file" | tr -d "\"'" | tr '[:upper:]' '[:lower:]')
    fi
    case " $id $like " in
        *" debian "*|*" ubuntu "*) echo "debian" ;;
        *" fedora "*|*" rhel "*|*" centos "*) echo "fedora" ;;
        *) echo "other" ;;
    esac
}

# How to get a supported Python (3.11-3.13) on this platform family.
python_install_hint() {
    case "$1" in
        macos)
            echo "  macOS:   brew install python@3.12"
            ;;
        debian)
            echo "  Debian / Ubuntu 24.04: sudo apt install python3 python3-venv python3-pip"
            echo "           (Debian 12 has Python 3.11; Ubuntu 24.04 has Python 3.12)"
            echo "  Ubuntu 22.04: its Python 3.10 is too old and Python 3.12 is not in its default"
            echo "           repositories. Install Python 3.12 from python.org, with pyenv or uv,"
            echo "           or from the deadsnakes PPA (sudo add-apt-repository ppa:deadsnakes/ppa)"
            echo "  Any release whose repositories have no Python 3.11-3.13 (for example Ubuntu 26.04, which"
            echo "           has 3.14): install Python 3.13 from python.org, or with uv (uv python install 3.13),"
            echo "           and make sure 'python3.13' is on your PATH"
            ;;
        fedora)
            echo "  Fedora:  sudo dnf install python3.13"
            ;;
        *)
            echo "  Install Python 3.12 from https://www.python.org/downloads/ or with your package manager,"
            echo "  or with uv (uv python install 3.13), and make sure 'python3.12' or 'python3.13' is on your PATH"
            ;;
    esac
}

# The command that adds the missing venv module for python command $2 on family $1.
venv_install_hint() {
    local family=$1 py=${2:-python3} pkg="python3-venv"
    case "$family" in
        debian)
            case "$py" in
                python3.[0-9]*) pkg="${py}-venv" ;;
            esac
            echo "  sudo apt install $pkg"
            ;;
        fedora)
            echo "  sudo dnf install python3"
            ;;
        *)
            echo "  Install the venv module for $py with your package manager (check it with: $py -m venv <folder>)"
            ;;
    esac
}

# Function to check Python version
check_python() {
    print_step "Checking Python version..."

    # Try multiple Python commands in order of preference
    PYTHON_CMD=""
    unsupported=""
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if command -v $cmd &> /dev/null; then
            # Check if this version meets requirements
            version=$($cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
            if [ $? -eq 0 ]; then
                major=$(echo $version | cut -d. -f1)
                minor=$(echo $version | cut -d. -f2)

                # Check if version is 3.11-3.13 (matches requires-python >=3.11,<3.14)
                if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ] && [ "$minor" -le 13 ]; then
                    PYTHON_CMD="$cmd"
                    PYTHON_VERSION="$version"
                    break
                fi
                if [ -z "$unsupported" ]; then
                    unsupported="$version ($cmd)"
                fi
            fi
        fi
    done

    # If no suitable Python found
    if [ -z "$PYTHON_CMD" ]; then
        print_error "Python 3.11-3.13 not found"
        echo
        if [ -n "$unsupported" ]; then
            echo "Found Python $unsupported, but Dango needs 3.11-3.13."
        fi
        echo "Dango requires Python 3.11-3.13 (3.12 recommended)"
        echo
        echo "Install options:"
        python_install_hint "$(detect_distro_family)"
        echo
        exit 1
    fi

    print_success "Python $PYTHON_VERSION found ($PYTHON_CMD)"
    echo
}

# Function to check Docker
check_docker() {
    print_step "Checking Docker..."

    if ! command -v docker &> /dev/null; then
        print_warning "Docker not found"
        echo
        echo "Docker is required for Metabase (visualization)."
        echo "Please install Docker Desktop:"
        echo "  macOS:   https://docs.docker.com/desktop/install/mac-install/"
        echo "  Linux:   https://docs.docker.com/engine/install/"
        echo
        echo -n "Continue without Docker? (y/N): "
        read_answer response DANGO_INSTALL_DOCKER_CONTINUE "" '^[YyNn]$'
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            print_info "Installation cancelled"
            echo
            echo "Install Docker and run this script again:"
            echo "  curl -sSL https://getdango.dev/install.sh | bash"
            echo
            exit 0
        fi
        echo
        return
    fi

    # Check if Docker daemon is running
    if ! docker info &> /dev/null 2>&1; then
        print_warning "Docker is installed but not running"
        echo
        echo "Please start Docker Desktop and try again."
        echo
        echo "Or continue without Docker (Metabase won't work):"
        echo -n "Continue? (y/N): "
        read_answer response DANGO_INSTALL_DOCKER_CONTINUE "" '^[YyNn]$'
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            print_info "Installation cancelled"
            echo
            echo "Start Docker and run this script again:"
            echo "  curl -sSL https://getdango.dev/install.sh | bash"
            echo
            exit 0
        fi
        echo
        return
    fi

    print_success "Docker is running"
    echo
}

# Function to detect scenario
detect_scenario() {
    if [ -f ".dango/project.yml" ]; then
        # Existing Dango project
        if [ -d "venv" ]; then
            echo "existing_with_venv"
        else
            echo "existing_without_venv"
        fi
    else
        # New project
        echo "new_project"
    fi
}

# Function to prompt for installation mode
prompt_install_mode() {
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
    echo -e "${CYAN}  Installation Options${NC}" >&2
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
    echo >&2
    echo "How would you like to install Dango?" >&2
    echo >&2
    echo -e "${GREEN}[1] Virtual Environment (Recommended)${NC}" >&2
    echo "    ✓ Keeps Dango separate from other Python programs" >&2
    echo "    ✓ Safe to experiment - won't affect anything else" >&2
    echo "    ✗ Requires one setup command each time (we'll show you)" >&2
    echo "    ✗ Easy to forget - you'll see an error if you do" >&2
    echo >&2
    echo -e "${YELLOW}[2] Global Install (More convenient)${NC}" >&2
    echo "    ✓ Works immediately - no setup needed" >&2
    echo "    ✓ Just type 'dango' anywhere" >&2
    echo "    ⚠ May upgrade packages that other Python programs use" >&2
    echo "      (We'll check for conflicts before installing)" >&2
    echo >&2
    echo -e "${DIM}Tip: Press Ctrl+C anytime to quit${NC}" >&2
    echo >&2
    echo -n "Choose [1] or [2]: " >&2

    while true; do
        read_answer choice DANGO_INSTALL_MODE "" '^[12]$'

        case $choice in
            1)
                echo "venv"
                return
                ;;
            2)
                echo "global"
                return
                ;;
            *)
                echo -e "${RED}✗ Please enter 1 or 2${NC}" >&2
                echo -n "Choose [1] or [2]: " >&2
                ;;
        esac
    done
}

# Function to prompt for setup mode
prompt_setup_mode() {
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
    echo -e "${CYAN}  What would you like to do?${NC}" >&2
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
    echo >&2
    echo -e "${GREEN}[1] Just install Dango${NC} (set up projects later)" >&2
    echo >&2
    echo -e "${GREEN}[2] Install Dango + create a new project now${NC}" >&2
    echo >&2
    echo -n "Choose [1] or [2]: " >&2

    while true; do
        read_answer choice DANGO_INSTALL_SETUP "" '^[12]$'

        case $choice in
            1)
                echo "install_only"
                return
                ;;
            2)
                echo "install_and_project"
                return
                ;;
            *)
                echo -e "${RED}✗ Please enter 1 or 2${NC}" >&2
                echo -n "Choose [1] or [2]: " >&2
                ;;
        esac
    done
}

# Function to prompt for venv location
prompt_venv_location() {
    local default_location=$1
    local scenario=$2

    echo "Virtual environment location:" >&2
    if [ "$scenario" == "new_project" ]; then
        echo "  [1] Default: $default_location (inside project directory)" >&2
    else
        echo "  [1] Default: $default_location (current directory)" >&2
    fi
    echo "  [2] Custom location" >&2
    echo >&2
    echo -n "Choose [1] or [2]: " >&2

    while true; do
        read_answer choice DANGO_INSTALL_VENV_LOCATION 1 '^[12]$'

        case $choice in
            1)
                echo "$default_location"
                return
                ;;
            2)
                while true; do
                    echo -n "Enter path for virtual environment: " >&2
                    read_answer custom_path DANGO_INSTALL_VENV_PATH "" '.'
                    if [ -z "$custom_path" ]; then
                        print_error "Path cannot be empty" >&2
                        continue
                    else
                        echo "$custom_path"
                        return
                    fi
                done
                ;;
            *)
                echo -e "${RED}✗ Please enter 1 or 2${NC}" >&2
                echo -n "Choose [1] or [2]: " >&2
                ;;
        esac
    done
}

# Function to create virtual environment
create_venv() {
    local venv_path=$1 existed=false
    if [ -e "$venv_path" ] || [ -L "$venv_path" ]; then
        existed=true
    fi
    print_step "Creating virtual environment at $venv_path..."

    if ! $PYTHON_CMD -m venv "$venv_path"; then
        # Remove only what this run started to create, so the next attempt starts clean
        if [ "$existed" = false ] && [ -n "$venv_path" ]; then
            rm -rf "$venv_path"
        fi
        echo
        print_error "Failed to create virtual environment"
        echo
        echo "Python could not create the environment at $venv_path. Install what is missing,"
        echo "then run this installer again:"
        venv_install_hint "$(detect_distro_family)" "$PYTHON_CMD"
        echo
        exit 1
    fi
    print_success "Virtual environment created"
    echo
}

# Function to install Dango
install_dango() {
    local venv_path=$1
    print_step "Installing Dango..."

    # Activate venv and install
    source "$venv_path/bin/activate"
    if ! $PYTHON_CMD -m pip install --upgrade pip -q; then
        print_warning "Could not upgrade pip (no network access, or a proxy in the way?); continuing with the pip that came with Python"
    fi

    if ! $PYTHON_CMD -m pip install "$(dango_pip_spec)"; then
        print_error "Failed to install Dango"
        echo
        echo "Possible causes:"
        echo "  • No internet connection"
        echo "  • PyPI is down"
        echo "  • Python version incompatible"
        echo
        echo "Check errors above and try again"
        exit 1
    fi

    # Get installed version
    DANGO_VERSION=$(dango --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9]*' || echo "unknown")

    print_success "Dango $DANGO_VERSION installed"
    echo
}

# Function to upgrade Dango
upgrade_dango() {
    local venv_path=$1
    print_step "Upgrading Dango..."

    source "$venv_path/bin/activate"
    if ! $PYTHON_CMD -m pip install --upgrade "$(dango_pip_spec)" -q; then
        print_error "Failed to upgrade Dango"
        echo
        echo "Possible causes:"
        echo "  • No internet connection"
        echo "  • PyPI is down"
        echo "  • A proxy or firewall is blocking pip"
        echo
        echo "Check errors above and try again"
        exit 1
    fi

    DANGO_VERSION=$(dango --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9]*' || echo "unknown")

    print_success "Dango upgraded to $DANGO_VERSION"
    echo
}

# Function to detect user bin directory (works on macOS + Linux)
get_user_bin_dir() {
    echo "$($PYTHON_CMD -m site --user-base)/bin"
}

# Function to detect shell config file
detect_shell_config() {
    # Use $SHELL variable (works even when piped)
    if [[ "$SHELL" == *"zsh"* ]]; then
        echo "$HOME/.zshrc"
    elif [[ "$SHELL" == *"bash"* ]]; then
        # macOS uses .bash_profile, Linux uses .bashrc
        if [ "$(uname -s)" = "Darwin" ]; then
            echo "$HOME/.bash_profile"
        else
            echo "$HOME/.bashrc"
        fi
    elif [[ "$SHELL" == *"fish"* ]]; then
        echo "$HOME/.config/fish/config.fish"
    else
        # Fallback: check shell version variables (when SHELL not set)
        if [ -n "$ZSH_VERSION" ]; then
            echo "$HOME/.zshrc"
        elif [ -n "$BASH_VERSION" ]; then
            if [ "$(uname -s)" = "Darwin" ]; then
                echo "$HOME/.bash_profile"
            else
                echo "$HOME/.bashrc"
            fi
        else
            # Final fallback to .profile
            echo "$HOME/.profile"
        fi
    fi
}

# True when the chosen Python runs inside a virtual environment
python_in_virtualenv() {
    [ "$($PYTHON_CMD -c 'import sys; print(int(sys.prefix != getattr(sys, "base_prefix", sys.prefix)))' 2>/dev/null)" = "1" ]
}

# True when the operating system marks the chosen Python as externally managed (PEP 668)
python_externally_managed() {
    [ "$($PYTHON_CMD -c 'import os, sysconfig; print(int(os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED"))))' 2>/dev/null)" = "1" ]
}

# True when the chosen Python is the distribution's own python3 (the one its pip package is for)
python_is_distro_python3() {
    case "$PYTHON_CMD" in
        python3|python) return 0 ;;
    esac
    local mine theirs
    local code='import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
    mine=$($PYTHON_CMD -c "$code" 2>/dev/null) || return 1
    theirs=$(python3 -c "$code" 2>/dev/null) || return 1
    [ -n "$mine" ] && [ "$mine" = "$theirs" ]
}

# Stop with advice when a per-user package install cannot work here (it would otherwise fail later,
# or silently, with a message about the network)
check_global_install_possible() {
    if python_in_virtualenv; then
        print_error "Cannot install globally from inside an active virtual environment"
        echo
        echo "pip's --user option does not work in a virtual environment."
        echo "Run 'deactivate', then run this installer again, or choose option [1]"
        echo "(virtual environment) to keep Dango in its own environment."
        exit 1
    fi

    if python_externally_managed; then
        print_error "This Python is managed by your operating system"
        echo
        echo "Your system's Python refuses global pip installs (PEP 668), so a global"
        echo "install would fail. Run this installer again and choose option [1] (virtual"
        echo "environment), or install Dango with pipx (get pipx with your package manager,"
        echo "for example: brew install pipx, or sudo apt install pipx):"
        echo "  pipx install getdango"
        exit 1
    fi

    if ! $PYTHON_CMD -m pip --version &> /dev/null; then
        print_error "pip is not available for $PYTHON_CMD"
        echo
        case "$PYTHON_CMD:$(detect_distro_family)" in
            # Debian and Ubuntu: their pip package serves the distribution's own python3 only (it
            # does not run on a newer Python such as deadsnakes 3.12 on Ubuntu 22.04), and
            # ensurepip is there only once the venv package is installed
            *:debian)
                if python_is_distro_python3; then
                    echo "Install it with:  sudo apt install python3-pip"
                else
                    echo "Install it with:  sudo apt install ${PYTHON_CMD}-venv"
                    echo "then:             $PYTHON_CMD -m ensurepip --user --upgrade"
                fi
                ;;
            python3:fedora) echo "Install it with:  sudo dnf install python3-pip" ;;
            *) echo "Install it with:  $PYTHON_CMD -m ensurepip --user" ;;
        esac
        echo "Or run this installer again and choose option [1] (virtual environment)."
        exit 1
    fi
}

# Function to check for package conflicts before global install
check_conflicts() {
    print_step "Checking for potential package conflicts..."
    echo

    # Run dry-run to see what will be installed (pip prints "Would install name-version ...",
    # for upgrades as well as new packages)
    local dry_run_output
    if ! dry_run_output=$($PYTHON_CMD -m pip install --dry-run --user "$(dango_pip_spec)" 2>&1); then
        if [[ "$dry_run_output" == *"no such option: --dry-run"* ]]; then
            # pip before 22.2 cannot preview an install; the install itself may still work
            print_warning "This pip is too old (22.2 or newer is needed) to check for package conflicts first"
            echo
            echo "Continuing without the check: packages other Python programs use may be upgraded."
            echo "To get the check, run '$PYTHON_CMD -m pip install --user --upgrade pip' and this installer"
            echo "again, or choose option [1] (virtual environment) to leave other programs alone."
            echo
            return 0
        fi
        print_error "Could not check for package conflicts"
        echo
        echo "$dry_run_output" | tail -n 15
        echo
        echo "The installation would fail the same way. Fix the error above, or run this"
        echo "installer again and choose option [1] (virtual environment)."
        exit 1
    fi

    local would tok name old_version new_version item upgrades=""
    local -a names=()
    would=$(echo "$dry_run_output" | grep '^Would install ' | tail -n 1 | sed 's/^Would install //')
    for tok in $would; do
        names+=("${tok%-*}")
    done

    # An upgrade is a package that is already installed at a different version than pip would install
    if [ ${#names[@]} -gt 0 ]; then
        local installed
        installed=$($PYTHON_CMD -c 'import importlib.metadata as m, sys
for n in sys.argv[1:]:
    try:
        print(n, m.version(n))
    except m.PackageNotFoundError:
        pass' "${names[@]}" 2>/dev/null) || installed=""
        # $installed is "name version" pairs, one per line: walk it as alternating words
        name=""
        for item in $installed; do
            if [ -z "$name" ]; then
                name=$item
                continue
            fi
            old_version=$item
            for tok in $would; do
                if [ "${tok%-*}" = "$name" ]; then
                    new_version=${tok##*-}
                    # Upgrading Dango itself is the point of the install, not a conflict
                    if [ "$new_version" != "$old_version" ] && [ "$(echo "$name" | tr '[:upper:]' '[:lower:]')" != "getdango" ]; then
                        upgrades="${upgrades}  • ${name}: ${old_version} → ${new_version}"$'\n'
                    fi
                fi
            done
            name=""
        done
    fi

    if [ -n "$upgrades" ]; then
        print_warning "The following packages will be upgraded:"
        echo
        printf '%s' "$upgrades"
        echo
        print_warning "This may affect other Python applications on your computer."
        echo
        echo "Do you want to continue? This could break other tools."
        echo -n "[y/N]: "
        read_answer response DANGO_INSTALL_CONFLICTS_CONTINUE "" '^[YyNn]$'

        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            print_info "Installation cancelled"
            echo
            echo "Consider using Virtual Environment instead for complete isolation."
            echo "Run the installer again and choose option [1]."
            exit 0
        fi
        echo
    else
        print_success "No conflicts detected"
        echo
    fi
}

# The line that puts $USER_BIN_DIR on PATH, in the syntax of the person's shell (fish differs)
path_export_line() {
    if [[ "${SHELL:-}" == *"fish"* ]]; then
        echo "fish_add_path \"$USER_BIN_DIR\""
    else
        echo "export PATH=\"$USER_BIN_DIR:\$PATH\""
    fi
}

# Function to install Dango globally. Sets GLOBAL_ON_PATH to true when 'dango' can be run from this
# terminal afterwards, false when it cannot (the install itself worked). It returns 0 either way, so
# set -e stays in force throughout (an `if` around the call would turn it off).
install_dango_global() {
    GLOBAL_ON_PATH=false

    # Stop early, with advice, where a global install cannot work
    check_global_install_possible

    # Check for conflicts first
    check_conflicts

    print_step "Installing Dango globally..."
    echo

    if ! $PYTHON_CMD -m pip install --user "$(dango_pip_spec)"; then
        print_error "Failed to install Dango"
        echo
        echo "Possible causes:"
        echo "  • No internet connection"
        echo "  • PyPI is down"
        echo "  • Python version incompatible"
        echo
        echo "Check errors above and try again"
        exit 1
    fi
    echo

    # Get the actual user bin directory
    USER_BIN_DIR=$(get_user_bin_dir)

    # Check if dango command is accessible BEFORE we modify PATH
    DANGO_WAS_IN_PATH=false
    if command -v dango &> /dev/null; then
        DANGO_WAS_IN_PATH=true
    fi

    # If dango already in PATH, we're done
    if [ "$DANGO_WAS_IN_PATH" = true ]; then
        DANGO_VERSION=$(dango --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9]*' || echo "unknown")
        print_success "Dango $DANGO_VERSION installed and ready to use!"
        echo
        GLOBAL_ON_PATH=true
        return 0
    else
        # Not in PATH - need to add it
        print_warning "Dango installed but not in PATH"
        echo
        echo "The 'dango' command is installed at:"
        echo -e "  ${CYAN}$USER_BIN_DIR/dango${NC}"
        echo

        SHELL_CONFIG=$(detect_shell_config)

        echo "To use 'dango' from anywhere, add this to your PATH:"
        echo -e "  ${YELLOW}$(path_export_line)${NC}"
        if [[ "${SHELL:-}" == *"fish"* ]]; then
            echo "  (fish_add_path needs fish 3.2 or newer)"
        fi
        echo
        echo -e "This line should be added to: ${CYAN}$SHELL_CONFIG${NC}"
        echo
        echo -n "Would you like me to add it automatically? [y/N]: " >&2
        read_answer response DANGO_INSTALL_ADD_TO_PATH n '^[YyNn]$'

        if [[ "$response" =~ ^[Yy]$ ]]; then
            # Check if already in config file (avoid duplicates)
            if grep -q "$USER_BIN_DIR" "$SHELL_CONFIG" 2>/dev/null; then
                print_info "Already in $SHELL_CONFIG"
            else
                # Add to shell config (a fish config may not exist yet)
                if ! { mkdir -p "${SHELL_CONFIG%/*}" && path_export_line >> "$SHELL_CONFIG"; } 2> /dev/null; then
                    print_warning "Could not write to $SHELL_CONFIG"
                    echo
                    echo "Add this line to it yourself:"
                    echo -e "  ${YELLOW}$(path_export_line)${NC}"
                    echo
                    return 0
                fi
                print_success "Added to $SHELL_CONFIG"
            fi
            echo

            # CRITICAL: Export in current session immediately
            export PATH="$USER_BIN_DIR:$PATH"

            # Set flag so we know to show restart warning later
            PATH_WAS_ADDED=true

            # Verify dango is now accessible
            if command -v dango &> /dev/null; then
                print_success "dango command is now available!"
                echo
                GLOBAL_ON_PATH=true
                return 0
            else
                print_error "Failed to add dango to PATH"
                echo
                echo "Please restart your terminal (close and reopen), or run:"
                echo -e "  ${YELLOW}source $SHELL_CONFIG${NC}"
                echo
                return 0
            fi
        else
            echo
            print_info "Skipped automatic configuration"
            echo
            echo "To use 'dango', run this command in your terminal:"
            echo -e "  ${YELLOW}$(path_export_line)${NC}"
            echo
            echo "Or add it permanently to $SHELL_CONFIG"
            echo
            return 0
        fi
    fi
}

# Where to look when `dango init` fails
print_init_log_hint() {
    echo "More detail: the output above and, if it was created, $PROJECT_DIR/.dango/logs/dango.log"
    echo "Get help: https://github.com/getdango/dango/issues"
}

# Printed when a global install worked but 'dango' is not on PATH in this terminal
print_global_not_on_path() {
    local what=$1
    print_warning "Dango is installed, but 'dango' is not on your PATH yet${what}"
    echo
}

# Function to initialize new project
init_project() {
    local venv_path=$1
    print_step "Initializing Dango project..."
    echo

    source "$venv_path/bin/activate"
    if run_dango_init; then
        print_success "Project initialized"
        echo
    else
        print_error "Failed to initialize project"
        echo
        echo "Dango is installed, but 'dango init' did not finish. Fix the error shown above,"
        echo "then run it again:"
        echo -e "  ${YELLOW}cd $PROJECT_DIR${NC}"
        echo -e "  ${YELLOW}source $venv_path/bin/activate${NC}"
        echo -e "  ${YELLOW}dango init${NC}"
        echo
        print_init_log_hint
        exit 1
    fi
}

# Function to print activation instructions
print_activation_instructions() {
    local venv_path=$1
    local project_dir=$2
    local created_subdir=$3

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}Installation complete!${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo -e "${YELLOW}⚠️  IMPORTANT: Activate your environment first!${NC}"
    echo

    if [ "$created_subdir" = "true" ]; then
        echo "Run these commands to get started:"
        echo
        echo -e "  ${YELLOW}cd $project_dir${NC}"
        echo -e "  ${YELLOW}source $venv_path/bin/activate${NC}"
    else
        echo "Run this command to activate:"
        echo
        echo -e "  ${YELLOW}source $venv_path/bin/activate${NC}"
    fi

    echo
    echo -e "${YELLOW}You need to activate the environment every time you work on this project.${NC}"
    echo
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo "Once activated, try these commands:"
    echo -e "  ${YELLOW}dango source add${NC}    # Add a data source"
    echo -e "  ${YELLOW}dango sync${NC}          # Sync data"
    echo -e "  ${YELLOW}dango start${NC}         # Start platform (opens http://localhost:8800)"
    echo
    echo "Documentation: https://github.com/getdango/dango"
    echo "Get help: https://github.com/getdango/dango/issues"
    echo
}

# Function to print global install success message
print_global_success() {
    local project_dir=$1

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}Installation complete!${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo -e "${GREEN}✓${NC} Dango is installed globally!"
    echo

    # If we added PATH during install, user needs to restart terminal
    # (Even though dango works in THIS script, it won't work in user's terminal after script exits)
    if [ "${PATH_WAS_ADDED:-false}" = true ]; then
        SHELL_CONFIG=$(detect_shell_config)
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${YELLOW}⚠  ONE MORE STEP${NC}"
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo
        echo "'dango' is installed, but your current terminal can't see it yet."
        echo
        echo "To fix this, choose ONE of these options:"
        echo
        echo -e "  ${GREEN}Option 1:${NC} Restart your terminal (close and reopen this window)"
        echo
        echo -e "  ${GREEN}Option 2:${NC} Run this command now:"
        echo -e "            ${YELLOW}source $SHELL_CONFIG${NC}"
        echo
        echo "After that, 'dango' will work from anywhere!"
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo
    fi

    if [ -n "$project_dir" ]; then
        echo -e "Your project is ready at: ${GREEN}$project_dir${NC}"
        echo
        echo "Next steps:"
        echo -e "  ${YELLOW}cd $project_dir${NC}"
    else
        echo "Next steps:"
        echo -e "  ${YELLOW}dango init my-project${NC}  # Create a new project"
        echo -e "  ${YELLOW}cd my-project${NC}"
    fi

    echo -e "  ${YELLOW}dango source add${NC}       # Add a data source"
    echo -e "  ${YELLOW}dango sync${NC}             # Sync data"
    echo -e "  ${YELLOW}dango start${NC}            # Start platform (opens http://localhost:8800)"
    echo

    # Only show "no activation needed" if PATH didn't need to be added
    if [ "${PATH_WAS_ADDED:-false}" != true ]; then
        echo -e "${GREEN}No activation needed - 'dango' command works from anywhere!${NC}"
        echo
    fi

    echo "Documentation: https://github.com/getdango/dango"
    echo "Get help: https://github.com/getdango/dango/issues"
    echo
}

# Main installation logic
main() {
    print_header

    # Opt-in test hook: refuse a malformed DANGO_INSTALL_SPEC before anything runs
    validate_install_spec

    # Check prerequisites
    check_python
    check_docker

    # Detect scenario
    SCENARIO=$(detect_scenario)

    case $SCENARIO in
        "new_project")
            print_info "Scenario: New Dango project"
            echo

            # Prompt for installation mode
            INSTALL_MODE=$(prompt_install_mode)
            echo

            # Prompt for setup mode
            SETUP_MODE=$(prompt_setup_mode)
            echo

            # Handle install_only mode
            if [ "$SETUP_MODE" == "install_only" ]; then
                if [ "$INSTALL_MODE" == "venv" ]; then
                    # Venv install only
                    VENV_PATH="venv"
                    create_venv "$VENV_PATH"
                    install_dango "$VENV_PATH"

                    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                    echo -e "${GREEN}Installation complete!${NC}"
                    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                    echo
                    echo -e "${GREEN}✓${NC} Dango is installed!"
                    echo
                    echo "To create your first project:"
                    echo -e "  ${YELLOW}source venv/bin/activate${NC}"
                    echo -e "  ${YELLOW}dango init my-project${NC}"
                    echo -e "  ${YELLOW}cd my-project${NC}"
                    echo -e "  ${YELLOW}dango source add${NC}"
                    echo -e "  ${YELLOW}dango sync${NC}"
                    echo -e "  ${YELLOW}dango start${NC}"
                    echo
                    echo "Documentation: https://github.com/getdango/dango"
                    echo "Get help: https://github.com/getdango/dango/issues"
                    echo
                else
                    # Global install only
                    install_dango_global
                    if [ "$GLOBAL_ON_PATH" != true ]; then
                        # pip succeeded; only this terminal cannot find the command yet
                        print_global_not_on_path ""
                        echo "To create your first project, run Dango by its full path:"
                        echo -e "  ${YELLOW}$USER_BIN_DIR/dango init my-project${NC}"
                        echo
                        echo "Or make 'dango' available in this terminal first:"
                        echo -e "  ${YELLOW}$(path_export_line)${NC}"
                        if [ "${PATH_WAS_ADDED:-false}" != true ]; then
                            echo "and add that line to $(detect_shell_config) to keep it."
                        fi
                        echo
                        exit 0
                    fi

                    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                    echo -e "${GREEN}Installation complete!${NC}"
                    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                    echo
                    echo -e "${GREEN}✓${NC} Dango is installed!"
                    echo

                    # Show restart warning if PATH was added
                    if [ "${PATH_WAS_ADDED:-false}" = true ]; then
                        SHELL_CONFIG=$(detect_shell_config)
                        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                        echo -e "${YELLOW}⚠  ONE MORE STEP${NC}"
                        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                        echo
                        echo "'dango' is installed, but your current terminal can't see it yet."
                        echo
                        echo "To fix this, choose ONE of these options:"
                        echo
                        echo -e "  ${GREEN}Option 1:${NC} Restart your terminal (close and reopen this window)"
                        echo
                        echo -e "  ${GREEN}Option 2:${NC} Run this command now:"
                        echo -e "            ${YELLOW}source $SHELL_CONFIG${NC}"
                        echo
                        echo "After that, 'dango' will work from anywhere!"
                        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                        echo
                    fi

                    echo "To create your first project:"
                    echo -e "  ${YELLOW}dango init my-project${NC}"
                    echo -e "  ${YELLOW}cd my-project${NC}"
                    echo -e "  ${YELLOW}dango source add${NC}"
                    echo -e "  ${YELLOW}dango sync${NC}"
                    echo -e "  ${YELLOW}dango start${NC}"
                    echo
                    echo "Documentation: https://github.com/getdango/dango"
                    echo "Get help: https://github.com/getdango/dango/issues"
                    echo
                fi

                exit 0
            fi

            # Handle install_and_project mode (existing behavior)
            # Get project directory name
            while true; do
                echo -n "Enter project directory name (e.g., my-analytics): "
                read_answer PROJECT_DIR DANGO_INSTALL_PROJECT_NAME "" '.'

                # Clean the name first (lowercase, spaces to dashes, other characters dropped), so
                # every check below sees the folder name that would be created
                typed_name=$PROJECT_DIR
                PROJECT_DIR=$(sanitize_project_name "$PROJECT_DIR")
                name_ok='^[a-z0-9][a-z0-9-]*$'
                if ! [[ "$PROJECT_DIR" =~ $name_ok ]]; then
                    fail_if_noninteractive "DANGO_INSTALL_PROJECT_NAME='$typed_name' is not usable as a project directory name (after cleanup: lowercase letters, digits and dashes, starting with a letter or digit)"
                    if [ -z "$typed_name" ]; then
                        print_error "Project name cannot be empty"
                    elif [ -z "$PROJECT_DIR" ]; then
                        print_error "Project name cannot be empty (only letters, digits, spaces and dashes are kept)"
                    else
                        print_error "Project name must start with a letter or digit"
                    fi
                    echo
                    continue
                fi

                # Check if directory exists
                if [ -d "$PROJECT_DIR" ]; then
                    fail_if_noninteractive "DANGO_INSTALL_PROJECT_NAME='$PROJECT_DIR' already exists as a directory"
                    print_error "Directory '$PROJECT_DIR' already exists"
                    echo "Please choose a different name."
                    echo
                    continue
                fi

                # Valid input, break the loop
                break
            done

            echo
            print_step "Creating project directory: $PROJECT_DIR"
            mkdir -p "$PROJECT_DIR"
            cd "$PROJECT_DIR"
            print_success "Directory created"
            echo

            # Install based on mode
            if [ "$INSTALL_MODE" == "venv" ]; then
                # Prompt for venv location
                VENV_PATH=$(prompt_venv_location "venv" "new_project")
                echo

                # Create venv
                create_venv "$VENV_PATH"

                # Install Dango
                install_dango "$VENV_PATH"

                # Initialize project
                init_project "$VENV_PATH"

                # Show activation instructions
                print_activation_instructions "$VENV_PATH" "$PROJECT_DIR" "true"
            else
                # Global install
                install_dango_global
                if [ "$GLOBAL_ON_PATH" != true ]; then
                    print_global_not_on_path ", so the project was not initialised"
                    echo "To finish setting up the project, run:"
                    echo -e "  ${YELLOW}cd $PROJECT_DIR${NC}"
                    echo -e "  ${YELLOW}$USER_BIN_DIR/dango init${NC}"
                    echo
                    echo "Or make 'dango' available in this terminal first, then run 'dango init':"
                    echo -e "  ${YELLOW}$(path_export_line)${NC}"
                    echo
                    exit 1
                fi

                # Initialize project (no venv needed)
                print_step "Initializing Dango project..."
                echo
                if run_dango_init; then
                    print_success "Project initialized"
                    echo

                    # Show global success message
                    print_global_success "$PROJECT_DIR"
                else
                    print_error "Failed to initialize project"
                    echo
                    echo "Dango is installed, but 'dango init' did not finish. Fix the error shown above,"
                    echo "then run it again:"
                    echo -e "  ${YELLOW}cd $PROJECT_DIR${NC}"
                    echo -e "  ${YELLOW}dango init${NC}"
                    echo
                    print_init_log_hint
                    exit 1
                fi
            fi
            ;;

        "existing_with_venv")
            print_info "Scenario: Existing Dango project with venv"
            echo

            echo "Found existing Dango project with virtual environment."
            echo
            echo "What would you like to do?"
            echo "  [i] Install Dango (if not installed)"
            echo "  [u] Upgrade Dango to latest version"
            echo "  [c] Cancel"
            echo
            echo -n "Choice: "
            read_answer action DANGO_INSTALL_EXISTING_ACTION "" '^[iIuUcC]$'

            case $action in
                i|I)
                    install_dango "venv"
                    echo -e "${GREEN}✓ Dango is ready to use!${NC}"
                    echo -e "Make sure to activate your venv: ${YELLOW}source venv/bin/activate${NC}"
                    echo
                    ;;
                u|U)
                    upgrade_dango "venv"
                    echo -e "${GREEN}✓ Dango upgraded!${NC}"
                    echo -e "Make sure to activate your venv: ${YELLOW}source venv/bin/activate${NC}"
                    echo
                    ;;
                *)
                    print_info "Cancelled"
                    exit 0
                    ;;
            esac
            ;;

        "existing_without_venv")
            print_warning "Detected Dango project without virtual environment"
            echo

            echo "This project needs a virtual environment for Dango."
            echo
            echo -n "Create virtual environment now? [Y/n]: "
            read_answer response DANGO_INSTALL_CREATE_VENV y '^[YyNn]$'

            if [[ "$response" =~ ^[Nn]$ ]]; then
                print_info "Cancelled"
                echo
                echo "To create venv manually:"
                echo "  $PYTHON_CMD -m venv venv"
                echo "  source venv/bin/activate"
                echo "  pip install getdango"
                echo
                exit 0
            fi

            echo

            # Prompt for venv location
            VENV_PATH=$(prompt_venv_location "venv" "existing")
            echo

            # Create venv
            create_venv "$VENV_PATH"

            # Install Dango
            install_dango "$VENV_PATH"

            # Show activation instructions
            PROJECT_DIR=$(basename "$PWD")
            print_activation_instructions "$VENV_PATH" "$PROJECT_DIR" "false"
            ;;
    esac
}

# Run main function (skipped when sourced for testing)
if [ -z "${DANGO_INSTALL_SOURCED:-}" ]; then
    main
fi
