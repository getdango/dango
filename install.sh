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
#                                  install (a wheel path, "getdango==1.0.15", ...). One token
#                                  of letters, digits and . _ / : @ + = ~ , \ - (no spaces or
#                                  shell metacharacters, must not start with "-"); it is
#                                  validated, always quoted and never evaluated.
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
#   DANGO_INSTALL_PROJECT_NAME       new project directory name (required for SETUP=2; must not
#                                    already exist)
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
    local allowed='^[A-Za-z0-9._/:@+=~,\\-]+$'
    if [ -z "$spec" ]; then
        return 0
    fi
    if [[ "$spec" == -* ]] || ! [[ "$spec" =~ $allowed ]]; then
        print_error "DANGO_INSTALL_SPEC must be one token of letters, digits and . _ / : @ + = ~ , \\ - (not starting with -); got: $spec" >&2
        exit 1
    fi
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

# Function to check Python version
check_python() {
    print_step "Checking Python version..."

    # Try multiple Python commands in order of preference
    PYTHON_CMD=""
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
            fi
        fi
    done

    # If no suitable Python found
    if [ -z "$PYTHON_CMD" ]; then
        print_error "Python 3.11-3.13 not found"
        echo
        echo "Dango requires Python 3.11-3.13 (3.12 recommended)"
        echo
        echo "Install options:"
        echo "  macOS:   brew install python@3.12"
        echo "  Ubuntu:  sudo apt install python3.12"
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
    local venv_path=$1
    print_step "Creating virtual environment at $venv_path..."

    $PYTHON_CMD -m venv "$venv_path"
    print_success "Virtual environment created"
    echo
}

# Function to install Dango
install_dango() {
    local venv_path=$1
    print_step "Installing Dango..."

    # Activate venv and install
    source "$venv_path/bin/activate"
    $PYTHON_CMD -m pip install --upgrade pip -q

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
    $PYTHON_CMD -m pip install --upgrade "$(dango_pip_spec)" -q

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

# Function to check for package conflicts before global install
check_conflicts() {
    print_step "Checking for potential package conflicts..."
    echo

    # Run dry-run to see what will be installed/upgraded
    dry_run_output=$($PYTHON_CMD -m pip install --dry-run --user "$(dango_pip_spec)" 2>&1)

    # Check if any packages will be upgraded
    if echo "$dry_run_output" | grep -q "Would upgrade"; then
        print_warning "The following packages will be upgraded:"
        echo
        echo "$dry_run_output" | grep "Would upgrade" | sed 's/Would upgrade: /  • /' | sed 's/ to / → /'
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

# Function to install Dango globally
install_dango_global() {
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
        echo -e "  ${YELLOW}export PATH=\"$USER_BIN_DIR:\$PATH\"${NC}"
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
                # Add to shell config
                echo "export PATH=\"$USER_BIN_DIR:\$PATH\"" >> "$SHELL_CONFIG"
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
                return 0
            else
                print_error "Failed to add dango to PATH"
                echo
                echo "Please restart your terminal (close and reopen), or run:"
                echo -e "  ${YELLOW}source $SHELL_CONFIG${NC}"
                echo
                return 1
            fi
        else
            echo
            print_info "Skipped automatic configuration"
            echo
            echo "To use 'dango', run this command in your terminal:"
            echo -e "  ${YELLOW}export PATH=\"$USER_BIN_DIR:\$PATH\"${NC}"
            echo
            echo "Or add it permanently to $SHELL_CONFIG"
            echo
            return 1
        fi
    fi
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
        echo "You can try initializing manually later:"
        echo "  source $venv_path/bin/activate"
        echo "  dango init"
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

                if [ -z "$PROJECT_DIR" ]; then
                    print_error "Project name cannot be empty"
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

            # Sanitize: lowercase, spaces → dashes, remove special chars
            PROJECT_DIR=$(echo "$PROJECT_DIR" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')

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
                if ! install_dango_global; then
                    print_error "Global installation failed"
                    echo
                    echo "Please restart your terminal (close and reopen), or run:"
                    echo -e "  ${YELLOW}source $(detect_shell_config)${NC}"
                    echo
                    echo "Then navigate to your project:"
                    echo -e "  ${YELLOW}cd $PROJECT_DIR${NC}"
                    echo -e "  ${YELLOW}dango init${NC}"
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
                    echo "You can try initializing manually later:"
                    echo -e "  ${YELLOW}cd $PROJECT_DIR${NC}"
                    echo -e "  ${YELLOW}dango init${NC}"
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
