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

    # Try python3 first, then python
    if command -v python3 &> /dev/null; then
        PYTHON_CMD="python3"
    elif command -v python &> /dev/null; then
        PYTHON_CMD="python"
    else
        print_error "Python not found"
        echo
        echo "Please install Python 3.10 or higher:"
        echo "  macOS:   brew install python@3.11"
        echo "  Ubuntu:  sudo apt install python3.11"
        echo
        exit 1
    fi

    # Get Python version
    PYTHON_VERSION=$($PYTHON_CMD -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
    PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

    # Check if version is 3.10+
    if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]); then
        print_error "Python $PYTHON_VERSION found, but 3.10+ required"
        echo
        echo "Please upgrade Python:"
        echo "  Current: Python $PYTHON_VERSION"
        echo "  Required: Python 3.10 or higher"
        echo
        echo "Install instructions:"
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
        read -r response
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
        read -r response
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

# Function to create virtual environment
create_venv() {
    local venv_path=$1
    print_step "Creating virtual environment..."

    $PYTHON_CMD -m venv "$venv_path"
    print_success "Virtual environment created at $venv_path"
    echo
}

# Function to install Dango
install_dango() {
    local venv_path=$1
    print_step "Installing Dango from PyPI..."

    # Activate venv and install
    source "$venv_path/bin/activate"
    pip install --upgrade pip -q
    pip install getdango

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
    pip install --upgrade getdango -q

    DANGO_VERSION=$(dango --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")

    print_success "Dango upgraded to $DANGO_VERSION"
    echo
}

# Function to initialize new project
init_project() {
    local venv_path=$1
    print_step "Initializing Dango project..."
    echo

    source "$venv_path/bin/activate"
    dango init

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

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}Installation complete!${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo "To activate your environment:"
    echo -e "  ${YELLOW}cd $project_dir${NC}"
    echo -e "  ${YELLOW}source $venv_path/bin/activate${NC}"
    echo
    echo "Quick start commands:"
    echo -e "  ${YELLOW}dango source add${NC}    # Add a data source (CSV or Stripe)"
    echo -e "  ${YELLOW}dango sync${NC}          # Sync data"
    echo -e "  ${YELLOW}dango start${NC}         # Start platform (opens http://localhost:8800)"
    echo
    echo "Documentation:"
    echo "  https://github.com/getdango/dango"
    echo
    echo "Get help:"
    echo "  https://github.com/getdango/dango/issues"
    echo
}

# Function to print success message (when direnv is active)
print_success_message() {
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}Installation complete!${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo
    echo "Your environment is ready!"
    echo
    echo "Quick start commands:"
    echo -e "  ${YELLOW}dango source add${NC}    # Add a data source (CSV or Stripe)"
    echo -e "  ${YELLOW}dango sync${NC}          # Sync data"
    echo -e "  ${YELLOW}dango start${NC}         # Start platform (opens http://localhost:8800)"
    echo
    echo "Documentation:"
    echo "  https://github.com/getdango/dango"
    echo
    echo "Get help:"
    echo "  https://github.com/getdango/dango/issues"
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

            # Get project directory name
            echo -n "Enter project directory name (e.g., my-analytics): "
            read -r PROJECT_DIR

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

            # Create venv
            create_venv "venv"

            # Install Dango
            install_dango "venv"

            # Initialize project
            init_project "venv"

            # Setup direnv or show activation instructions
            if ! setup_direnv "venv"; then
                print_activation_instructions "venv" "$PROJECT_DIR"
            else
                print_success_message
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
            read -r action

            case $action in
                i|I)
                    install_dango "venv"
                    print_success_message
                    ;;
                u|U)
                    upgrade_dango "venv"
                    print_success_message
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
            read -r response

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

            # Create venv
            create_venv "venv"

            # Install Dango
            install_dango "venv"

            # Setup direnv or show activation instructions
            PROJECT_DIR=$(basename "$PWD")
            if ! setup_direnv "venv"; then
                print_activation_instructions "venv" "$PROJECT_DIR"
            else
                print_success_message
            fi
            ;;
    esac
}

# Run main function
main
