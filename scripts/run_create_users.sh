#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

# Create virtual environment if it doesn't exist
if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating virtual environment in ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi

# Install dependencies
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet boto3

# Run the script, propagating all arguments
exec "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/create_users_sm_domain.py" "$@"
