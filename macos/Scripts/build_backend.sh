#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="${ROOT}/.venv-macos-build"

if [ ! -x "${VENV}/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi

"${VENV}/bin/python" -m pip install --upgrade pip
"${VENV}/bin/python" -m pip uninstall -y chromadb-client >/dev/null 2>&1 || true
"${VENV}/bin/python" -m pip install -r requirements.txt pyinstaller

ODYSSEUS_REPO_ROOT="$ROOT" "${VENV}/bin/pyinstaller" --clean --noconfirm macos/pyinstaller/odysseus_backend.spec

echo "Built backend at ${ROOT}/dist/odysseus_backend"
