#!/bin/bash
# Build the sandbox and run one container test file against it, SP3 Plan 2 ruling 30 and Plan 5 ruling G12.
#
#   scripts/run_sandbox_fullstack.sh                      # tests/fullstack/test_sandbox_service.py
#   scripts/run_sandbox_fullstack.sh --test tests/fullstack/test_prove_isolation_in_research.py
#
# WARNING: never run this against the live stack. It is built to share nothing with one on the same host:
#   - its own compose project (mmr-sbxtest), so its own containers, network and named volumes (mmr-sbxtest_*);
#   - it starts ONLY `sandbox`, `research` and `fullstack-tests` (review r2 #2; research only sleeps, with the
#     research socket and nothing else), so ib-gateway, trader, strategy, data, dashboard and scheduler never
#     start -- no second IB login, no live service touched;
#   - a test-only image tag (mmr-sbxtest:<run id>), never the production mmr:latest;
#   - no published host port (docker-compose.sbxtest.override.yml);
#   - ${HOME} points at a throwaway directory with a copy of config_defaults and keys generated for this run,
#     never the operator's ~/.config/mmr; .env is neither interpolated (--env-file is an empty file) nor injected;
#   - every docker and uv call runs under `env -i` (only PATH, HOME, DOCKER_CONFIG, UV_CACHE_DIR, TERM, the
#     MMR_* vars it sets, and DOCKER_HOST/DOCKER_CONTEXT when set), so an exported TWS_USERID, TWS_PASSWORD,
#     TRADING_MODE, IB_*, ALPACA_*, ... never interpolates into any service;
#   - the rendered config is checked (scripts/check_sbxtest_config.py) and the run refuses before `up` otherwise.
# It removes its containers, volumes and the throwaway directory when it ends. Exit status: pytest's;
# on a failure it also prints the sandbox's and research's logs.
set -o errexit -o pipefail -o nounset
TEST_PATH=tests/fullstack/test_sandbox_service.py
while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      if [[ $# -lt 2 ]]; then
        echo "usage: $0 [--test tests/fullstack/test_<name>.py]" >&2
        exit 2
      fi
      TEST_PATH="$2"
      shift 2
      ;;
    *)
      echo "usage: $0 [--test tests/fullstack/test_<name>.py]" >&2
      exit 2
      ;;
  esac
done
if [[ ! "$TEST_PATH" =~ ^tests/fullstack/test_[a-z0-9_]+\.py$ ]]; then
  echo "refusing: --test '${TEST_PATH}' is not one tests/fullstack/test_<name>.py file" >&2
  exit 2
fi
PROJECT="${MMR_SBXTEST_PROJECT-mmr-sbxtest}"
if [[ "$PROJECT" != mmr-sbxtest* ]]; then
  echo "refusing: compose project '${PROJECT}' could address the live stack; it must start with mmr-sbxtest" >&2
  exit 2
fi
BUILDDIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$BUILDDIR"   # compose resolves seccomp=./config_defaults/... from here (ruling 17)
if [[ ! -f "$TEST_PATH" ]]; then
  echo "refusing: --test '${TEST_PATH}' does not exist in ${BUILDDIR}" >&2
  exit 2
fi
OPERATOR_HOME="$HOME"
DOCKER_CONFIG="${DOCKER_CONFIG:-$OPERATOR_HOME/.docker}"    # docker keeps its context when HOME changes
UV_CACHE_DIR="${UV_CACHE_DIR:-$OPERATOR_HOME/.cache/uv}"
TEST_HOME=$(mktemp -d "${TMPDIR:-/tmp}/mmr-sbxtest-home.XXXXXX")
MMR_BUILD_ID="${MMR_BUILD_ID:-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)-test}"
MMR_SBXTEST_IMAGE="mmr-sbxtest:${MMR_BUILD_ID}"             # a test-only tag, never mmr:latest
# The exact services the container tests need; nothing else is rendered or started (review r2 #2).
SERVICES=(sandbox research fullstack-tests)
BUILT_SERVICES=(sandbox fullstack-tests)   # research runs sandbox's image, so it is never built on its own
STARTED_SERVICES=(sandbox research)        # fullstack-tests runs as a one-off container below
WAIT_TIMEOUT_S=180   # the sandbox healthcheck has a 30 s start period and a 30 s interval
compose=(docker compose -p "$PROJECT" --env-file "$TEST_HOME/compose.env" -f docker-compose.yml
         -f docker-compose.test.override.yml -f docker-compose.sbxtest.override.yml --profile test)
# A clean environment for every call: only what docker, compose and uv need. An operator's exported broker or
# provider variables (TWS_*, IB_*, ALPACA_*, ...) are not present, so none can interpolate into a service.
clean=(env -i "PATH=$PATH" "HOME=$TEST_HOME" "DOCKER_CONFIG=$DOCKER_CONFIG" "UV_CACHE_DIR=$UV_CACHE_DIR"
       "MMR_BUILD_ID=$MMR_BUILD_ID" "MMR_SBXTEST_IMAGE=$MMR_SBXTEST_IMAGE" "MMR_SBXTEST_TEST=$TEST_PATH"
       "TERM=${TERM:-dumb}")
for var in DOCKER_HOST DOCKER_CONTEXT; do                       # which daemon to talk to, not a credential
  if [[ -n "${!var:-}" ]]; then
    clean+=("$var=${!var}")
  fi
done
# Only this run's own two tags: never mmr:latest or any other image.
TEST_IMAGES=("$MMR_SBXTEST_IMAGE" "$MMR_SBXTEST_IMAGE-test")
cleanup() {
  "${clean[@]}" "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  "${clean[@]}" docker image rm "${TEST_IMAGES[@]}" >/dev/null 2>&1 || true
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
"${clean[@]}" "${compose[@]}" build "${BUILT_SERVICES[@]}"
# Sandbox and research run detached, so no compose watcher aborts the run when the restart test stops the
# sandbox; the tests run in a one-off container (`run` publishes no port without --service-ports).
"${clean[@]}" "${compose[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT_S" "${STARTED_SERVICES[@]}"
PYTEST_OUT="$TEST_HOME/pytest.out"
if "${clean[@]}" "${compose[@]}" run --rm --no-deps -T fullstack-tests | tee "$PYTEST_OUT"; then
  status=0
else
  status=$?
fi
# pytest exits 0 when every test skipped or none ran (review): only "<N> passed in <time>" with nothing else counts.
ALL_PASSED='^=+ [0-9]+ passed in [0-9.]+s( \([0-9:]+\))? =+$'
summary=$(grep -E '^=+ .* in [0-9.]+s' "$PYTEST_OUT" | tail -n 1 || true)
if [[ "$status" -eq 0 && ! "$summary" =~ $ALL_PASSED ]]; then
  echo "refusing to report success: every test must pass, none may skip; pytest said: '${summary}'" >&2
  status=3
fi
if [[ "$status" -eq 0 ]]; then
  exit 0
fi
echo "sandbox full-stack tests failed (exit ${status}); the sandbox and research logs follow:" >&2
"${clean[@]}" "${compose[@]}" logs --no-color "${STARTED_SERVICES[@]}" >&2 || true
exit "$status"
