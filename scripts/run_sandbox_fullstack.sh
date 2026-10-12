#!/bin/bash
# Build the sandbox and run its container checks (tests/fullstack/test_sandbox_service.py), SP3 Plan 2 ruling 30.
#
# WARNING: never run this against the live stack. It is built to share nothing with one on the same host:
#   - its own compose project (mmr-sbxtest), so its own containers, network and named volumes (mmr-sbxtest_*);
#   - it starts ONLY `sandbox` and `fullstack-tests` (review r2 #2), so ib-gateway, trader, strategy, data,
#     dashboard and scheduler never start -- no second IB login, no live service touched;
#   - a test-only image tag (mmr-sbxtest:<run id>), never the production mmr:latest;
#   - no published host port (docker-compose.sbxtest.override.yml);
#   - ${HOME} points at a throwaway directory with a copy of config_defaults and keys generated for this run,
#     never the operator's ~/.config/mmr; .env is neither interpolated (--env-file is an empty file) nor injected;
#   - every docker and uv call runs under `env -i` (only PATH, HOME, DOCKER_CONFIG, UV_CACHE_DIR, TERM, the
#     MMR_* vars it sets, and DOCKER_HOST/DOCKER_CONTEXT when set), so an exported TWS_USERID, TWS_PASSWORD,
#     TRADING_MODE, IB_*, ALPACA_*, ... never interpolates into any service;
#   - the rendered config is checked (scripts/check_sbxtest_config.py) and the run refuses before `up` otherwise.
# It removes its containers, volumes and the throwaway directory when it ends. Exit status: pytest's.
set -o errexit -o pipefail -o nounset
PROJECT="${MMR_SBXTEST_PROJECT-mmr-sbxtest}"
if [[ "$PROJECT" != mmr-sbxtest* ]]; then
  echo "refusing: compose project '${PROJECT}' could address the live stack; it must start with mmr-sbxtest" >&2
  exit 2
fi
BUILDDIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$BUILDDIR"   # compose resolves seccomp=./config_defaults/... from here (ruling 17)
OPERATOR_HOME="$HOME"
DOCKER_CONFIG="${DOCKER_CONFIG:-$OPERATOR_HOME/.docker}"    # docker keeps its context when HOME changes
UV_CACHE_DIR="${UV_CACHE_DIR:-$OPERATOR_HOME/.cache/uv}"
TEST_HOME=$(mktemp -d "${TMPDIR:-/tmp}/mmr-sbxtest-home.XXXXXX")
MMR_BUILD_ID="${MMR_BUILD_ID:-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)-test}"
MMR_SBXTEST_IMAGE="mmr-sbxtest:${MMR_BUILD_ID}"             # a test-only tag, never mmr:latest
# The exact services the sandbox tests need; nothing else is rendered or started (review r2 #2).
SERVICES=(sandbox fullstack-tests)
compose=(docker compose -p "$PROJECT" --env-file "$TEST_HOME/compose.env" -f docker-compose.yml
         -f docker-compose.test.override.yml -f docker-compose.sbxtest.override.yml --profile test)
# A clean environment for every call: only what docker, compose and uv need. An operator's exported broker or
# provider variables (TWS_*, IB_*, ALPACA_*, ...) are not present, so none can interpolate into a service.
clean=(env -i "PATH=$PATH" "HOME=$TEST_HOME" "DOCKER_CONFIG=$DOCKER_CONFIG" "UV_CACHE_DIR=$UV_CACHE_DIR"
       "MMR_BUILD_ID=$MMR_BUILD_ID" "MMR_SBXTEST_IMAGE=$MMR_SBXTEST_IMAGE" "TERM=${TERM:-dumb}")
for var in DOCKER_HOST DOCKER_CONTEXT; do                       # which daemon to talk to, not a credential
  if [[ -n "${!var:-}" ]]; then
    clean+=("$var=${!var}")
  fi
done
cleanup() {
  "${clean[@]}" "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TEST_HOME"
}
trap cleanup EXIT

mkdir -p "$TEST_HOME/.config/mmr" "$TEST_HOME/.local/share/mmr/logs" "$TEST_HOME/.local/share/mmr/artifacts" \
         "$TEST_HOME/.local/share/mmr/tws_settings"
cp -R config_defaults/. "$TEST_HOME/.config/mmr/"
: > "$TEST_HOME/compose.env"
# Keys for this run only, written under the throwaway home and never the operator's.
"${clean[@]}" uv run --frozen --project "$BUILDDIR" mmr keys init --keys-dir "$TEST_HOME/.config/mmr/keys/rpc" \
  >/dev/null

"${clean[@]}" "${compose[@]}" config --format json "${SERVICES[@]}" \
  | python3 scripts/check_sbxtest_config.py "$OPERATOR_HOME" "$BUILDDIR" || exit 2
"${clean[@]}" "${compose[@]}" up --build --exit-code-from fullstack-tests "${SERVICES[@]}"
