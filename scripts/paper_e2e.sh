#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
if [[ -z "${DASHBOARD_TOKEN_FILE:-}" && -f "$HOME/.config/mmr/dashboard.token" ]]; then
  export DASHBOARD_TOKEN_FILE="$HOME/.config/mmr/dashboard.token"
fi
export MMR_PAPER_E2E=1
exec "${PYTHON:-.venv/bin/python}" -m pytest tests/paper_e2e -m paper_e2e -v --timeout=120 "$@"
