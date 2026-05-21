#!/bin/bash
# fileswatch.sh - Bash wrapper for fileswatch.py
# Activates virtual environment and runs the file watch script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/fileswatch.py"

# Check if Python script exists
if [[ ! -f "$PYTHON_SCRIPT" ]]; then
    echo "Error: fileswatch.py not found in ${SCRIPT_DIR}"
    exit 1
fi

# Find and activate virtual environment (look locally first)
if [[ -d "${SCRIPT_DIR}/.venv" ]]; then
    source "${SCRIPT_DIR}/.venv/bin/activate"
elif [[ -d "${SCRIPT_DIR}/venv" ]]; then
    source "${SCRIPT_DIR}/venv/bin/activate"
else
    # Look in parent directories for venv
    PARENT_DIR="$(dirname "$SCRIPT_DIR")"
    if [[ -d "${PARENT_DIR}/.venv" ]]; then
        source "${PARENT_DIR}/.venv/bin/activate"
    elif [[ -d "${PARENT_DIR}/venv" ]]; then
        source "${PARENT_DIR}/venv/bin/activate"
    else
        echo "Warning: No virtual environment found in ${SCRIPT_DIR} or parent directories."
        echo "Using system Python. Ensure watchdog is installed: pip install watchdog"
    fi
fi

# Run the script with all arguments
exec python3 "$PYTHON_SCRIPT" "$@"
