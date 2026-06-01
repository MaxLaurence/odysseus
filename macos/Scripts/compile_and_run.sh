#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

swift build --package-path "${ROOT}/macos"
ODYSSEUS_BACKEND_ROOT="$ROOT" "${ROOT}/macos/.build/debug/Odysseus"
