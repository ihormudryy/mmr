#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export MMR_PAPER_E2E=1
exec "${PYTHON:-.venv/bin/python}" -m pytest -m paper_e2e -v --timeout=120 "$@"
