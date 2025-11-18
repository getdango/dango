#!/bin/bash
set -e

# Dango Bootstrap Installer
# Version: 0.0.1
# Purpose: Install Dango with per-project virtual environment
# Platform: macOS / Linux (Windows support coming in v0.1.0)

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
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

# Function to check Python version
check_python() {
    print_step "Checking Python version..."

    # Try multiple Python commands in order of preference
    PYTHON_CMD=""
    for cmd in python3.12 python3.11 python3.10 python3 python; do
        if command -v $cmd &> /dev/null; then
            # Check if this version meets requirements
            version=$($cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
            if [ $? -eq 0 ]; then
                major=$(echo $version | cut -d. -f1)
                minor=$(echo $version | cut -d. -f2)

                # Check if version is 3.10+
                if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
                    PYTHON_CMD="$cmd"
                    PYTHON_VERSION="$version"
                    break
                elif [ "$major" -gt 3 ]; then
                    PYTHON_CMD="$cmd"
                    PYTHON_VERSION="$version"
                    break
                fi
            fi
        fi
    done

    # If no suitable Python found
    if [ -z "$PYTHON_CMD" ]; then
        print_error "Python 3.10+ not found"
        echo
        echo "Please install Python 3.10 or higher:"
        echo "  macOS:   brew install python@3.11"
        echo "  Ubuntu:  sudo apt install python3.11"
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
        read -r response < /dev/tty
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            print_info "Installation cancelled"
            echo
            echo "Install Docker and run this script again:"
            echo "  curl -sSL get.getdango.dev | bash"
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
        read -r response < /dev/tty
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            print_info "Installation cancelled"
            echo
            echo "Start Docker and run this script again:"
            echo "  curl -sSL get.getdango.dev | bash"
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
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  Installation Options${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo "How would you like to install Dango?"
    echo
    echo -e "${GREEN}[1] Virtual Environment (Recommended for beginners)${NC}"
    echo "    ✓ Keeps Dango separate from other Python programs"
    echo "    ✓ Won't break anything else on your computer"
    echo "    ✓ Safe for experimenting"
    echo "    ✗ Must run 'source venv/bin/activate' before using Dango"
    echo "    ✗ Needs activation EVERY TIME you open a new terminal"
    echo
    echo -e "${YELLOW}[2] Global Install (Simpler but less safe)${NC}"
    echo "    ✓ Works immediately, no activation needed"
    echo "    ✓ Run 'dango' from anywhere"
    echo "    ✗ Might update Python packages that other programs use"
    echo "    ✗ Could break other Python tools on your computer"
    echo
    echo -n "Choose [1] or [2]: "
    read -r choice < /dev/tty

    case $choice in
        1)
            echo "venv"
            ;;
        2)
            echo "global"
            ;;
        *)
            print_error "Invalid choice. Please run the installer again."
            exit 1
            ;;
    esac
}

# Function to prompt for venv location
prompt_venv_location() {
    local default_location=$1
    local scenario=$2

    echo "Virtual environment location:"
    if [ "$scenario" == "new_project" ]; then
        echo "  [1] Default: $default_location (inside project directory)"
    else
        echo "  [1] Default: $default_location (current directory)"
    fi
    echo "  [2] Custom location"
    echo
    echo -n "Choose [1] or [2]: "
    read -r choice < /dev/tty

    case $choice in
        1)
            echo "$default_location"
            ;;
        2)
            echo -n "Enter path for virtual environment: "
            read -r custom_path < /dev/tty
            if [ -z "$custom_path" ]; then
                print_error "Path cannot be empty, using default"
                echo "$default_location"
            else
                echo "$custom_path"
            fi
            ;;
        *)
            print_warning "Invalid choice, using default"
            echo "$default_location"
            ;;
    esac
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
    print_step "Installing Dango from PyPI..."

    # Activate venv and install
    source "$venv_path/bin/activate"
    $PYTHON_CMD -m pip install --upgrade pip -q
    $PYTHON_CMD -m pip install getdango

    # Get installed version
    DANGO_VERSION=$(dango --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")

    print_success "Dango $DANGO_VERSION installed"
    echo
}

# Function to upgrade Dango
upgrade_dango() {
    local venv_path=$1
    print_step "Upgrading Dango..."

    source "$venv_path/bin/activate"
    $PYTHON_CMD -m pip install --upgrade getdango -q

    DANGO_VERSION=$(dango --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")

    print_success "Dango upgraded to $DANGO_VERSION"
    echo
}

# Function to check for package conflicts before global install
check_conflicts() {
    print_step "Checking for potential package conflicts..."
    echo

    # Run dry-run to see what will be installed/upgraded
    dry_run_output=$($PYTHON_CMD -m pip install --dry-run --user getdango 2>&1)

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
        read -r response < /dev/tty

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

    $PYTHON_CMD -m pip install --user getdango

    # Get installed version
    DANGO_VERSION=$(dango --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")

    if [ "$DANGO_VERSION" != "unknown" ]; then
        print_success "Dango $DANGO_VERSION installed globally"
        echo
        return 0
    else
        print_error "Installation completed but 'dango' command not found"
        echo
        echo "You may need to add ~/.local/bin to your PATH:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo
        return 1
    fi
}

# Function to initialize new project
init_project() {
    local venv_path=$1
    print_step "Initializing Dango project..."
    echo

    source "$venv_path/bin/activate"
    dango init < /dev/tty

    print_success "Project initialized"
    echo
}

# Function to check for direnv
setup_direnv() {
    local venv_path=$1

    if command -v direnv &> /dev/null; then
        print_step "direnv detected - setting up auto-activation..."

        echo "source $venv_path/bin/activate" > .envrc
        direnv allow . 2>/dev/null || true

        print_success "direnv configured - venv will auto-activate when entering directory"
        echo
        return 0
    else
        return 1
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
    echo -e "${YELLOW}You need to activate the environment EVERY TIME you work on this project.${NC}"
    echo
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo "Once activated, try these commands:"
    echo -e "  ${YELLOW}dango source add${NC}    # Add a data source (CSV or Stripe)"
    echo -e "  ${YELLOW}dango sync${NC}          # Sync data"
    echo -e "  ${YELLOW}dango start${NC}         # Start platform (opens http://localhost:8800)"
    echo
    echo "Optional: Auto-activate with direnv (advanced users)"
    echo "  Install direnv: https://direnv.net/"
    echo "  It will auto-activate the venv when you cd into this directory"
    echo
    echo "Documentation: https://github.com/getdango/dango"
    echo "Get help: https://github.com/getdango/dango/issues"
    echo
}

# Function to print success message (when direnv is active)
print_success_message() {
    local created_subdir=$1
    local project_dir=$2

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}Installation complete!${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo -e "${GREEN}✓${NC} Your environment is ready with direnv auto-activation!"
    echo

    if [ "$created_subdir" = "true" ]; then
        echo "Next step:"
        echo -e "  ${YELLOW}cd $project_dir${NC}"
        echo
        echo "The virtual environment will activate automatically."
        echo
    else
        echo "Your virtual environment is already activated."
        echo
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo "Quick start commands:"
    echo -e "  ${YELLOW}dango source add${NC}    # Add a data source (CSV or Stripe)"
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
    echo -e "${GREEN}✓${NC} Dango is installed globally and ready to use!"
    echo

    if [ -n "$project_dir" ]; then
        echo "Your project is ready at: ${GREEN}$project_dir${NC}"
        echo
        echo "Next steps:"
        echo -e "  ${YELLOW}cd $project_dir${NC}"
    else
        echo "Next steps:"
        echo -e "  ${YELLOW}dango init my-project${NC}  # Create a new project"
        echo -e "  ${YELLOW}cd my-project${NC}"
    fi

    echo -e "  ${YELLOW}dango source add${NC}       # Add a data source (CSV or Stripe)"
    echo -e "  ${YELLOW}dango sync${NC}             # Sync data"
    echo -e "  ${YELLOW}dango start${NC}            # Start platform (opens http://localhost:8800)"
    echo
    echo -e "${GREEN}No activation needed - 'dango' command works from anywhere!${NC}"
    echo
    echo "Documentation: https://github.com/getdango/dango"
    echo "Get help: https://github.com/getdango/dango/issues"
    echo
}

# Main installation logic
main() {
    print_header

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

            # Get project directory name
            echo -n "Enter project directory name (e.g., my-analytics): "
            read -r PROJECT_DIR < /dev/tty

            if [ -z "$PROJECT_DIR" ]; then
                print_error "Project name cannot be empty"
                exit 1
            fi

            # Check if directory exists
            if [ -d "$PROJECT_DIR" ]; then
                print_error "Directory '$PROJECT_DIR' already exists"
                echo
                echo "Please choose a different name or remove the existing directory."
                exit 1
            fi

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

                # Setup direnv or show activation instructions
                if ! setup_direnv "$VENV_PATH"; then
                    print_activation_instructions "$VENV_PATH" "$PROJECT_DIR" "true"
                else
                    print_success_message "true" "$PROJECT_DIR"
                fi
            else
                # Global install
                install_dango_global

                # Initialize project (no venv needed)
                print_step "Initializing Dango project..."
                echo
                dango init < /dev/tty
                print_success "Project initialized"
                echo

                # Show global success message
                print_global_success "$PROJECT_DIR"
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
            read -r action < /dev/tty

            case $action in
                i|I)
                    install_dango "venv"
                    print_success_message "false" ""
                    ;;
                u|U)
                    upgrade_dango "venv"
                    print_success_message "false" ""
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
            read -r response < /dev/tty

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

            # Setup direnv or show activation instructions
            PROJECT_DIR=$(basename "$PWD")
            if ! setup_direnv "$VENV_PATH"; then
                print_activation_instructions "$VENV_PATH" "$PROJECT_DIR" "false"
            else
                print_success_message "false" ""
            fi
            ;;
    esac
}

# Run main function
main
