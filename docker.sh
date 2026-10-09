#!/bin/bash

set -o errexit -o pipefail -o noclobber -o nounset

# usually /home/trader/mmr
BUILDDIR=$(cd $(dirname "$0"); pwd)

# Sibling news scraper repo. Default to ~/dev/news so the standard checkout
# layout works out of the box; override with NEWS_DIR=/path/to/news.
NEWS_DIR="${NEWS_DIR:-$HOME/dev/news}"

echo_usage() {
    echo "usage: docker.sh -- helper script to manage split MMR services + IB Gateway"
    echo
    echo "  -b (build the shared MMR image)"
    echo "  -u (up: start all split services and IB Gateway)"
    echo "  -i (ib-only: start only IB Gateway for local development)"
    echo "  -d (down: stop and remove all containers)"
    echo "  -c (clean: remove all images and containers)"
    echo "  -f (force clean: images + containers + build cache + host data)"
    echo "  -s (retired: source is baked into read-only images; use -b -u)"
    echo "  -a (retired: source is baked into read-only images; use -b -u)"
    echo "  -g (go: build, start all containers, and exec into trader)"
    echo "  -l (logs: tail logs from all containers)"
    echo "  -r (restart-ib: restart the IB Gateway container)"
    echo "  -e [service] (exec: shell into trader by default; valid services:"
    echo "      trader, data, strategy, dashboard, scheduler, ib-gateway)"
    echo "  -n (news: bring up the news scraper stack at \$NEWS_DIR)"
    echo "  -B [name] (backup DuckDB files to ~/.local/share/mmr/backups/)"
    echo "  -k [--rotate P] (RPC keys: mmr keys init in the one-shot keygen container)"
    echo "  -k --backup [--recipient FILE] (encrypt keys/rpc with age)"
    echo "  -k --restore FILE [--identity-file PATH [--delete-identity-file]]"
    echo "      (restore keys/rpc; the age identity is read from stdin by default)"
    echo "  -K (key check: cutover gate; in an isolated compose project, check each"
    echo "      container sees only its own key pair, its peers' .pub and an empty HMAC file;"
    echo "      runs alone, refused together with any other option)"
    echo
}

b=n c=n f=n u=n d=n s=n a=n g=n l=n e=n i=n r=n n=n B=n k=n K=n
BACKUP_NAME=""
KEYS_ACTION="init"
KEYS_ROTATE=""
KEYS_RECIPIENT=""
KEYS_ARCHIVE=""
KEYS_IDENTITY_FILE=""
KEYS_DELETE_IDENTITY_FILE=n
EXEC_SERVICE="trader"

# Parse and validate before probing Docker or Podman. An invalid requested
# service should not touch a container runtime at all.
while [[ $# -gt 0 ]]; do
  case $1 in
    -b|--build) b=y; shift ;;
    -u|--up) u=y; shift ;;
    -d|--down) d=y; shift ;;
    -c|--clean) c=y; shift ;;
    -f|--force) f=y; shift ;;
    -g|--go) g=y; shift ;;
    -s|--sync) s=y; shift ;;
    -a|--sync_all) a=y; shift ;;
    -l|--logs) l=y; shift ;;
    -e|--exec)
      e=y
      shift
      if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
          EXEC_SERVICE="$1"
          shift
      fi
      ;;
    -i|--ib-only) i=y; shift ;;
    -r|--restart-ib) r=y; shift ;;
    -n|--news) n=y; shift ;;
    -k|--keys) k=y; shift ;;
    -K|--key-check) K=y; shift ;;
    --rotate)
      [[ $# -ge 2 ]] || { echo "--rotate needs a principal"; exit 1; }
      KEYS_ROTATE="$2"; shift 2 ;;
    --recipient)
      [[ $# -ge 2 ]] || { echo "--recipient needs a file"; exit 1; }
      KEYS_RECIPIENT="$2"; shift 2 ;;
    --restore)
      [[ $# -ge 2 ]] || { echo "--restore needs a backup file"; exit 1; }
      KEYS_ACTION="restore"; KEYS_ARCHIVE="$2"; shift 2 ;;
    --identity-file)
      [[ $# -ge 2 ]] || { echo "--identity-file needs a path"; exit 1; }
      KEYS_IDENTITY_FILE="$2"; shift 2 ;;
    --delete-identity-file) KEYS_DELETE_IDENTITY_FILE=y; shift ;;
    -B|--backup)
      if [[ $k == "y" ]]; then
        KEYS_ACTION="backup"; shift; continue
      fi
      B=y
      shift
      if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
        BACKUP_NAME="$1"
        shift
      fi
      ;;
    -*|--*)
      echo "Unknown option $1"
      echo_usage
      exit 1
      ;;
    *) shift ;;
  esac
done

if [[ $b == "n" && $c == "n" && $f == "n" && $u == "n" && $d == "n" && $s == "n" && $a == "n" && $g == "n" && $l == "n" && $e == "n" && $i == "n" && $r == "n" && $n == "n" && $B == "n" && $k == "n" && $K == "n" ]]; then
    echo_usage
    exit 0
fi

# -K is a non-disruptive pre-cutover gate: it must never share a run with an
# action that touches the live project (e.g. -d would stop it first).
if [[ $K == "y" ]]; then
    for other in $b $c $f $u $d $s $a $g $l $e $i $r $n $B $k; do
        if [[ $other == "y" ]]; then
            echo "Error: -K (key check) runs alone; drop the other options and run them separately."
            exit 1
        fi
    done
fi

if [[ $e == "y" ]]; then
    case "$EXEC_SERVICE" in
        trader|data|strategy|dashboard|scheduler|ib-gateway) ;;
        *)
            echo "Unknown exec service: $EXEC_SERVICE"
            exit 1
            ;;
    esac
fi

# Detect container runtime.
# 1. Use whichever is already running.
# 2. If neither is running, try to start whichever is installed.
_setup_podman_compose() {
    if podman compose version &> /dev/null; then
        COMPOSE="podman compose"
    elif command -v podman-compose &> /dev/null; then
        COMPOSE="podman-compose"
    else
        echo "Error: podman found but no compose support."
        echo "Install it with: pip3 install podman-compose"
        exit 1
    fi
}

_try_start_runtime() {
    # Try Docker
    if command -v docker &> /dev/null; then
        echo "Docker found but not running. Attempting to start..."
        if [[ "$(uname)" == "Darwin" ]]; then
            open -a Docker 2>/dev/null || true
        elif command -v systemctl &> /dev/null; then
            sudo systemctl start docker 2>/dev/null || true
        fi
        for i in $(seq 1 30); do
            if docker info &> /dev/null; then
                echo "Docker started successfully."
                RUNTIME=docker; COMPOSE="docker compose"; return 0
            fi
            sleep 1
        done
    fi
    # Try Podman
    if command -v podman &> /dev/null; then
        echo "Podman found but not running. Attempting to start machine..."
        podman machine start 2>/dev/null || { podman machine init 2>/dev/null && podman machine start 2>/dev/null; } || true
        if podman info &> /dev/null; then
            echo "Podman machine started successfully."
            RUNTIME=podman; _setup_podman_compose; return 0
        fi
    fi
    return 1
}

# Optional explicit runtime pin. Set MMR_CONTAINER_RUNTIME=docker or
# MMR_CONTAINER_RUNTIME=podman to bypass auto-detection. Useful when both
# are installed simultaneously (e.g. you just installed Docker Desktop on
# top of an existing podman setup) — without a pin, the auto-detect picks
# whichever answers `info` first, which can flip between invocations and
# leaves you with a half-podman / half-docker stack where containers from
# one runtime hold ports the other tries to bind. The same env var is
# honoured by news/docker.sh so the entire stack can be pinned in one
# place via your shell rc.
if [[ -n "${MMR_CONTAINER_RUNTIME:-}" ]]; then
    case "$MMR_CONTAINER_RUNTIME" in
        docker)
            if ! command -v docker &> /dev/null; then
                echo "Error: MMR_CONTAINER_RUNTIME=docker but docker is not installed."
                exit 1
            fi
            if ! docker info &> /dev/null; then
                echo "Error: MMR_CONTAINER_RUNTIME=docker but docker is not running."
                echo "  Start Docker Desktop and re-run."
                exit 1
            fi
            RUNTIME=docker
            COMPOSE="docker compose"
            ;;
        podman)
            if ! command -v podman &> /dev/null; then
                echo "Error: MMR_CONTAINER_RUNTIME=podman but podman is not installed."
                exit 1
            fi
            if ! podman info &> /dev/null; then
                echo "Error: MMR_CONTAINER_RUNTIME=podman but the podman machine is not running."
                echo "  Run: podman machine start"
                exit 1
            fi
            RUNTIME=podman
            _setup_podman_compose
            ;;
        *)
            echo "Error: MMR_CONTAINER_RUNTIME='$MMR_CONTAINER_RUNTIME' (must be 'docker' or 'podman')"
            exit 1
            ;;
    esac
    _RUNTIME_PINNED=1
# Auto-detect — prefer docker when both are running. Cross-runtime collisions
# (docker container vs podman container with the same compose name / port)
# do NOT cross-cancel: docker's port-bind will fail with "address already
# in use" and you'll see this script error out at compose-up. If that
# happens, either tear down the other runtime's containers or pin via
# MMR_CONTAINER_RUNTIME above.
elif command -v docker &> /dev/null && docker info &> /dev/null; then
    RUNTIME=docker
    COMPOSE="docker compose"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        # Both runtimes are running. Surface this — auto-detect picked
        # docker, but the user might be sitting on podman state from a
        # previous setup. Soft warning, not an error: lots of users have
        # both installed and only ever use one at a time.
        echo "Note: both docker and podman are running; using docker."
        echo "      Set MMR_CONTAINER_RUNTIME=podman to override, or run"
        echo "      \`podman ps\` to see if you have leftover podman containers."
    fi
elif command -v podman &> /dev/null && podman info &> /dev/null; then
    RUNTIME=podman
    _setup_podman_compose
elif ! _try_start_runtime; then
    echo "Error: no working container runtime found."
    echo "  - docker: $(command -v docker &> /dev/null && echo 'installed' || echo 'not installed')"
    echo "  - podman: $(command -v podman &> /dev/null && echo 'installed' || echo 'not installed')"
    echo ""
    echo "Install Docker Desktop: https://docker.com/products/docker-desktop"
    echo "Or install podman: brew install podman"
    exit 1
fi

if [[ "${_RUNTIME_PINNED:-0}" == "1" ]]; then
    echo "Using container runtime: $RUNTIME (pinned via \$MMR_CONTAINER_RUNTIME)"
else
    echo "Using container runtime: $RUNTIME"
fi

# Where the host keeps DuckDBs, logs, TWS session state. Bind-mounted into
# the containers per docker-compose.yml — clobbering this dir wipes every
# sweep, backtest run, downloaded history bar, and IB session.
MMR_DATA_DIR="${HOME}/.local/share/mmr"

_human_size() {
    # Cross-platform (macOS/Linux) human-readable size for one path.
    # macOS's `du -h` doesn't have a `-d` flag with the same semantics as GNU,
    # so we just shell out to -sh which works identically on both.
    if [ -e "$1" ]; then
        du -sh "$1" 2>/dev/null | awk '{print $1}'
    else
        echo "-"
    fi
}

print_data_sizes() {
    # Show what's on disk right up front so anyone running a destructive
    # action knows the scope of what they could lose.
    local data_db="$MMR_DATA_DIR/data/mmr.duckdb"
    local hist_db="$MMR_DATA_DIR/data/mmr_history.duckdb"
    local logs_dir="$MMR_DATA_DIR/logs"
    local tws_dir="$MMR_DATA_DIR/tws_settings"
    echo "Host data at $MMR_DATA_DIR:"
    [ -e "$data_db" ] && echo "  mmr.duckdb:          $(_human_size "$data_db")  (sweeps, backtests, proposals, universes)"
    [ -e "$hist_db" ] && echo "  mmr_history.duckdb:  $(_human_size "$hist_db")  (1-min / 1-day OHLCV)"
    [ -e "$logs_dir" ] && echo "  logs/:               $(_human_size "$logs_dir")"
    [ -e "$tws_dir" ] && echo "  tws_settings/:       $(_human_size "$tws_dir")  (IB session state)"
    if [ ! -d "$MMR_DATA_DIR" ]; then
        echo "  (no data dir yet — first run will create it)"
    fi
    echo ""
}

print_data_sizes

# Required RAM (bytes) for the full MMR stack. Set by the mem_limit values
# in docker-compose.yml (24 GB mmr + 2 GB ib-gateway = 26 GB; leave a few
# GB for the VM/kernel, so 28 GB minimum / 32 GB target).
MIN_VM_RAM_MB=28672
RECOMMENDED_VM_RAM_MB=32768

check_vm_ram() {
    # On macOS, Docker Desktop + Podman both run a Linux VM with its own
    # RAM budget — the Mac has plenty but the VM cap is what the
    # containers actually see. A 2 GB default (Podman applehv's out-of-box
    # setting) has OOM-killed trader_service + strategy_service mid-order;
    # warn loudly when we find it. On Linux, check host RAM directly.
    local ram_mb=0
    local source=""
    if [[ "$(uname)" == "Darwin" ]]; then
        if [[ "$RUNTIME" == "podman" ]]; then
            ram_mb=$(podman machine inspect 2>/dev/null \
                | awk -F'[:,]' '/"Memory"/ { gsub(/[^0-9]/,"",$2); print $2; exit }')
            source="podman machine"
        elif [[ "$RUNTIME" == "docker" ]]; then
            local mem_bytes
            mem_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
            if [ -n "$mem_bytes" ] && [ "$mem_bytes" -gt 0 ]; then
                ram_mb=$((mem_bytes / 1024 / 1024))
            fi
            source="Docker Desktop VM"
        fi
    else
        if command -v free &> /dev/null; then
            ram_mb=$(free -m | awk '/^Mem:/ {print $2}')
            source="host"
        fi
    fi

    if [ -z "$ram_mb" ] || [ "$ram_mb" -le 0 ]; then
        echo "Warning: couldn't determine ${source:-VM} RAM — skipping sizing check."
        echo ""
        return 0
    fi

    if [ "$ram_mb" -lt "$MIN_VM_RAM_MB" ]; then
        echo "=============================================================="
        echo "  RAM WARNING: $source has ${ram_mb} MB (minimum ${MIN_VM_RAM_MB} MB)"
        echo "=============================================================="
        echo "  trader_service + strategy_service have been OOM-killed in"
        echo "  production at 2 GB. Recommended: ${RECOMMENDED_VM_RAM_MB} MB."
        if [[ "$(uname)" == "Darwin" && "$RUNTIME" == "podman" ]]; then
            echo ""
            echo "  To resize the Podman VM (requires restart):"
            echo "    podman machine stop"
            echo "    podman machine set --memory ${RECOMMENDED_VM_RAM_MB}"
            echo "    podman machine start"
        elif [[ "$(uname)" == "Darwin" && "$RUNTIME" == "docker" ]]; then
            echo ""
            echo "  Open Docker Desktop → Settings → Resources → Memory,"
            echo "  bump to ${RECOMMENDED_VM_RAM_MB} MB or more."
        fi
        echo ""
        if [ -t 0 ] && [ -t 1 ]; then
            read -p "Continue anyway? [y/N]: " answer
            case "${answer:-n}" in
                y|Y|yes) ;;
                *) echo "Aborting. Resize and re-run."; exit 1 ;;
            esac
        else
            echo "(non-interactive, continuing — rebuild is up to you)"
        fi
        echo ""
    else
        echo "$source RAM: ${ram_mb} MB (>= ${MIN_VM_RAM_MB} MB required) ✓"
        echo ""
    fi
}

check_vm_ram

# ─── Colors ──────────────────────────────────────────────────────────────────
if [ -n "${NO_COLOR:-}" ]; then
    DC_RESET="" DC_GREEN="" DC_RED="" DC_DIM=""
elif [ -t 1 ] || [ "${FORCE_COLOR:-0}" = "1" ]; then
    DC_RESET=$'\033[0m' DC_GREEN=$'\033[32m' DC_RED=$'\033[31m' DC_DIM=$'\033[2m'
else
    DC_RESET="" DC_GREEN="" DC_RED="" DC_DIM=""
fi

_api_key_status_d() {
    local label="$1" yaml_key="$2" env_var="$3" yaml_file="$4"
    local env_val="${!env_var:-}"
    local yaml_val=""
    if [ -f "$yaml_file" ]; then
        yaml_val=$(
            grep -E "^[[:space:]]*${yaml_key}[[:space:]]*:" "$yaml_file" 2>/dev/null \
                | head -n1 \
                | sed -E "s/^[[:space:]]*${yaml_key}[[:space:]]*:[[:space:]]*//" \
                | sed -E 's/^"//;  s/"$//'                                          \
                | sed -E "s/^'//;  s/'$//"                                          \
                | sed -E 's/[[:space:]]+$//' || true
        )
    fi
    if [ -n "$env_val" ]; then
        printf "  %-18s${DC_GREEN}✓ set via \$%s (...%s)${DC_RESET}\n" "$label" "$env_var" "${env_val: -4}"
    elif [ -n "$yaml_val" ]; then
        if [ ${#yaml_val} -gt 4 ]; then
            printf "  %-18s${DC_GREEN}✓ set (...%s)${DC_RESET}\n" "$label" "${yaml_val: -4}"
        else
            printf "  %-18s${DC_GREEN}✓ set${DC_RESET}\n" "$label"
        fi
    else
        printf "  %-18s${DC_RED}✗ not set (export \$%s or set %s in trader.yaml)${DC_RESET}\n" \
            "$label" "$env_var" "$yaml_key"
    fi
}

# Show whether data-feed API keys are configured. Reads from
# $HOME/.config/mmr/secrets.env (if present, sourced for KEY=VALUE
# lines), then env vars, then $HOME/.config/mmr/trader.yaml — first
# non-empty wins. Same priority order as start_mmr.sh.
print_api_keys() {
    local secrets_file="$HOME/.config/mmr/secrets.env"
    if [ -f "$secrets_file" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$secrets_file"
        set +a
    fi
    local yaml_file="${HOME}/.config/mmr/trader.yaml"
    echo "${DC_DIM}Data feed API keys${DC_RESET}"
    _api_key_status_d "Massive/Polygon" "massive_api_key"    "MASSIVE_API_KEY"    "$yaml_file"
    _api_key_status_d "TwelveData"      "twelvedata_api_key" "TWELVEDATA_API_KEY" "$yaml_file"
    _api_key_status_d "Alpaca"          "alpaca_api_key_id"  "ALPACA_API_KEY_ID"  "$yaml_file"
    echo ""
}

print_vnc_url() {
    # Print a clickable vnc:// URL for the IB Gateway desktop.
    # macOS Terminal and iTerm2 auto-link the vnc:// scheme (⌘-click opens
    # Screen Sharing). Password is not embedded in the URL — it would land
    # in scrollback/logs.
    local prefix="${1:-}"
    # Compose defaults VNC_SERVER_PASSWORD to "trader" when unset.
    local vnc_pass="trader"
    if [ -f "$BUILDDIR/.env" ]; then
        local from_env
        from_env=$(grep -E '^VNC_SERVER_PASSWORD=' "$BUILDDIR/.env" 2>/dev/null | cut -d= -f2- || echo "")
        if [ -n "$from_env" ]; then
            vnc_pass="$from_env"
        fi
    fi
    echo "${prefix}IB Gateway VNC: vnc://localhost:5901"
    if [ "$vnc_pass" = "trader" ]; then
        echo "${prefix}  (password: trader)"
    else
        echo "${prefix}  (password: see VNC_SERVER_PASSWORD in .env)"
    fi
}

# Prompt for IB credentials and write .env file
setup_credentials() {
    echo ""
    echo "============================================================"
    echo "  MMR First-Time Setup"
    echo "============================================================"
    echo ""

    local userid password account trading_mode vnc_password timezone

    read -p "Interactive Brokers username: " userid
    read -s -p "Interactive Brokers password: " password
    echo ""
    read -p "IB account number (U... for live, DU... for paper): " account

    echo ""
    echo "Trading mode:"
    echo "  1) paper (default)"
    echo "  2) live"
    read -p "Select [1]: " mode_choice
    case "${mode_choice:-1}" in
        2|live) trading_mode="live" ;;
        *) trading_mode="paper" ;;
    esac

    read -p "Timezone [America/New_York]: " timezone
    timezone="${timezone:-America/New_York}"

    read -p "VNC password for IB Gateway GUI [trader]: " vnc_password
    vnc_password="${vnc_password:-trader}"

    cat > "$BUILDDIR/.env" <<EOF
# MMR Configuration — generated by docker.sh
# Edit this file to change credentials or settings.

# IB Gateway credentials
TWS_USERID=${userid}
TWS_PASSWORD=${password}

# Trading mode: paper or live
TRADING_MODE=${trading_mode}

# IB account number
IB_ACCOUNT=${account}

# Timezone
TIME_ZONE=${timezone}

# VNC password for IB Gateway GUI (default: "trader"; bound to 127.0.0.1:5901 only)
VNC_SERVER_PASSWORD=${vnc_password}

# IB Gateway settings
TWS_ACCEPT_INCOMING=accept
READ_ONLY_API=no
TWOFA_TIMEOUT_ACTION=restart
RELOGIN_AFTER_TWOFA_TIMEOUT=yes
EXISTING_SESSION_DETECTED_ACTION=primaryoverride
AUTO_RESTART_TIME="11:59 PM"
ALLOW_BLIND_TRADING=no
EOF

    echo ""
    echo "Credentials saved to $BUILDDIR/.env"
    echo "You can edit this file later to change settings."
    echo ""
}

# check for .env file, prompt if missing or incomplete
check_env() {
    if [ ! -f "$BUILDDIR/.env" ]; then
        setup_credentials
    fi

    # Check if credentials are filled in
    source "$BUILDDIR/.env"
    if [ -z "${TWS_USERID:-}" ] || [ -z "${TWS_PASSWORD:-}" ]; then
        echo "IB credentials are not set in .env"
        read -p "Set up credentials now? [Y/n]: " answer
        case "${answer:-y}" in
            n|N|no) echo "Skipping — IB Gateway will not be able to log in." ;;
            *) setup_credentials ;;
        esac
    fi
}

# The shared HMAC key is retired (SP1 Plan 2, ruling 14). Remove the yaml
# line (keeping a .bak) and remind the operator about the key file. Nothing
# here ever deletes service_hmac.key: rollback to a pre-cutover commit needs it.
_retire_hmac_config() {
    local trader_config="$1"
    if grep -q -E '^[[:space:]]*service_hmac_key_file[[:space:]]*:' "$trader_config"; then
        sed -i.bak -E '/^[[:space:]]*service_hmac_key_file[[:space:]]*:/d' "$trader_config"
        echo "Removed the retired service_hmac_key_file line from $trader_config (backup: ${trader_config}.bak)"
    fi
    if [[ -e "$HOME/.config/mmr/service_hmac.key" ]]; then
        echo "Note: retired HMAC key left in place at ~/.config/mmr/service_hmac.key;"
        echo "      delete it when the cutover is verified (see docs/OPERATIONAL_STATE.md, RPC keys)."
    fi
}

# Every host key file docker-compose.yml binds, read from the compose file
# itself (no second principal list). A missing per-file bind source would make
# Docker create a directory in its place, so refuse before any compose call.
_compose_rpc_key_files() {
    grep -oE '\$\{HOME\}/\.config/mmr/keys/rpc/[a-z_]+\.(key|pub)' "$BUILDDIR/docker-compose.yml" \
        | sed -E 's|^.*/||' | sort -u
}

# Single-file binds under keys/private (the research signing key). Same trap as the RPC keys.
_compose_private_key_files() {
    grep -oE '\$\{HOME\}/\.config/mmr/keys/private/[a-z_.]+' "$BUILDDIR/docker-compose.yml" \
        | sed -E 's|^.*/||' | sort -u
}

_require_rpc_keys() {
    local keys_dir="$HOME/.config/mmr/keys/rpc"
    local missing="" name
    for name in $(_compose_rpc_key_files); do
        if [[ ! -f "$keys_dir/$name" || -L "$keys_dir/$name" ]]; then
            missing="$missing $name"
        fi
    done
    if [[ -n "$missing" ]]; then
        echo "Error: RPC key files missing or not regular files in $keys_dir:$missing"
        echo "Run ./docker.sh -k (mmr keys init in the keygen container) first."
        exit 1
    fi
}

# True when compose would start the research service (the opt-in ai profile). Compose itself decides, so
# environment, .env, quoting and "*" are honoured. If compose cannot say, assume it runs (fail closed).
_research_service_active() {
    local services
    services=$($COMPOSE -f "$BUILDDIR/docker-compose.yml" config --services 2>/dev/null) || return 0
    grep -qx research <<<"$services"
}

# The research service binds the host signing key file; a missing file makes Docker create a directory.
_require_signing_key() {
    local name
    for name in $(_compose_private_key_files); do
        if [[ ! -f "$HOME/.config/mmr/keys/private/$name" || -L "$HOME/.config/mmr/keys/private/$name" ]]; then
            echo "Error: $HOME/.config/mmr/keys/private/$name is missing or not a regular file."
            echo "Create it once on the host: mmr keys init-signing (or restore it from backup)."
            exit 1
        fi
    done
}

ensure_split_config() {
    local config_dir="$HOME/.config/mmr"
    local trader_config="$config_dir/trader.yaml"
    local default_config

    mkdir -p "$config_dir"
    for default_config in "$BUILDDIR"/config_defaults/*.yaml; do
        [[ -e "$default_config" ]] || continue
        [[ -e "$config_dir/$(basename "$default_config")" ]] || cp "$default_config" "$config_dir/"
    done

    if [[ ! -f "$trader_config" ]]; then
        echo "Error: missing trader configuration at $trader_config"
        exit 1
    fi

    _retire_hmac_config "$trader_config"
}

build() {
    echo "Building MMR image..."
    echo ""
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" build
    # Every rebuild retags mmr:latest and leaves the previous image as
    # <none> plus BuildKit intermediates. Without a reclaim pass, repeated
    # `./docker.sh -b -u` grows Docker Desktop disk without bound.
    # Non-`--all` builder prune keeps cache mounts (pip) for the next build.
    reclaim_build_cache
}

reclaim_build_cache() {
    echo ""
    echo "Reclaiming dangling images + unused build cache from this rebuild..."
    if [[ "$RUNTIME" == "docker" ]]; then
        $RUNTIME image prune --force >/dev/null
        $RUNTIME builder prune --force >/dev/null
    else
        # Podman: dangling images + unused build/storage leftovers.
        $RUNTIME image prune --force >/dev/null 2>&1 || true
        $RUNTIME system prune --force >/dev/null 2>&1 || true
    fi
}

up() {
    _require_rpc_keys
    if _research_service_active; then
        _require_signing_key
    fi
    ensure_split_config
    check_env
    print_api_keys
    echo "Pulling latest IB Gateway image..."
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" pull ib-gateway
    echo ""
    echo "Starting IB Gateway + split MMR services..."
    echo ""
    # Ensure bind-mount directories exist on host
    mkdir -p "$HOME/.local/share/mmr/data" "$HOME/.local/share/mmr/logs" "$HOME/.local/share/mmr/tws_settings"
    # Remove stale network to avoid label mismatch errors (podman/docker-compose compat).
    # Must remove containers using the network first, then the network itself.
    for cid in $($RUNTIME ps -a -q --filter network=mmr_default 2>/dev/null); do
        $RUNTIME rm -f "$cid" 2>/dev/null || true
    done
    $RUNTIME network rm mmr_default 2>/dev/null || true
    # Remove only stale legacy monolith containers before the split topology
    # starts. This avoids a leftover legacy dependent blocking ib-gateway.
    _remove_legacy_monolith
    # Disaster-recovery: if the DB volume is empty (lost / reset / fresh machine),
    # seed it from the newest host backup BEFORE the services start. No-op when the
    # volume already has data.
    seed_db_if_empty
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" up -d
    echo ""
    echo "Containers started. Use './docker.sh -e [service]' to exec in, or './docker.sh -l' for logs."
    print_vnc_url
}

# A stale legacy monolith can retain a depends_on edge to ib-gateway. Remove
# only those legacy containers before recreating the gateway.
_remove_legacy_monolith() {
    for cid in $($RUNTIME ps -aq --filter "name=mmr[-_]mmr" 2>/dev/null); do
        $RUNTIME rm -f "$cid" 2>/dev/null || true
    done
}

ib_only() {
    check_env
    print_api_keys
    echo "Pulling latest IB Gateway image..."
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" pull ib-gateway
    echo ""
    echo "Starting IB Gateway only (for local development)..."
    echo ""
    _remove_legacy_monolith
    # Force recreate so .env changes are always picked up
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" up -d --force-recreate ib-gateway
    echo ""
    source "$BUILDDIR/.env"
    # Host-side port the user connects to. docker-compose.yml maps
    # 7496->container:4003 (live) and 7497->container:4004 (paper).
    # IB Gateway itself listens on 4001/4002 internally; gnzsnz's socat
    # forwards 4003/4004 -> 4001/4002. Don't echo the internal ports —
    # nothing on the host can reach them.
    local ib_port
    if [ "${TRADING_MODE:-paper}" = "paper" ]; then
        ib_port=7497
    else
        ib_port=7496
    fi
    echo "IB Gateway running."
    echo "  API:  localhost:$ib_port (${TRADING_MODE:-paper})"
    print_vnc_url "  "
    echo ""
    echo "Configure your local trader.yaml:"
    echo "  ib_server_address: 127.0.0.1"
    echo "  ib_server_port: $ib_port"
    echo ""
    echo "Or run services directly:"
    echo "  python3 -m trader.trader_service"
    echo "  python3 -m trader.strategy_service"
    echo "  python3 -m trader.mmr_cli"
}

restart_ib() {
    echo "Restarting IB Gateway container..."
    # Same dependent-container guard as ib_only(): podman/docker won't
    # recreate ib-gateway while a container with `depends_on: ib-gateway`
    # exists, even if it's stopped.
    _remove_legacy_monolith
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" up -d --force-recreate ib-gateway
    echo ""
    echo "IB Gateway restarted."
}

# Bring up the sibling news scraper docker stack at $NEWS_DIR (default
# ~/dev/news). Used by the `news` skill (skills/news/) which talks to
# the news service over http://127.0.0.1:8089. Idempotent — leans on
# news/docker.sh -u, which itself reuses any already-running container.
#
# Why we don't call news/docker.sh -g here: that command tails compose
# logs (`compose logs -f`) at the end and never returns, which would
# block this script. If the user wants a clean rebuild they can run
# `cd $NEWS_DIR && ./docker.sh -g` directly.
news_up() {
    if [[ ! -d "$NEWS_DIR" ]]; then
        echo "Error: NEWS_DIR=$NEWS_DIR not found."
        echo "  Clone the news repo to ~/dev/news, or set NEWS_DIR=/path/to/news."
        exit 1
    fi
    if [[ ! -x "$NEWS_DIR/docker.sh" ]]; then
        echo "Error: $NEWS_DIR/docker.sh not found or not executable."
        exit 1
    fi
    echo ""
    echo "Bringing up news scraper stack at $NEWS_DIR..."
    ( cd "$NEWS_DIR" && ./docker.sh -u )
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "news startup failed (rc=$rc) — try: cd $NEWS_DIR && ./docker.sh -g"
        exit $rc
    fi
    echo ""
    echo "News service is up. Health check:"
    echo "  curl http://127.0.0.1:8089/v1/health"
    echo ""
    echo "Manage from $NEWS_DIR:"
    echo "  ./docker.sh -l   # logs"
    echo "  ./docker.sh -e   # shell"
    echo "  ./docker.sh -d   # stop"
}

down() {
    echo "Stopping all containers..."
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" down 2>/dev/null || true
    # Remove stale network to avoid label mismatch errors (podman/docker-compose compat)
    for cid in $($RUNTIME ps -a -q --filter network=mmr_default 2>/dev/null); do
        $RUNTIME rm -f "$cid" 2>/dev/null || true
    done
    $RUNTIME network rm mmr_default 2>/dev/null || true
}

# Compose declares volume ``mmr_db_data``; with top-level ``name: mmr`` Docker
# materializes it as ``mmr_mmr_db_data``. Seed/backup MUST target that live
# volume — the unprefixed ``mmr_db_data`` is a stale sibling that can still
# contain an old DB, which made seed_db_if_empty() skip restoring Portfolios
# after ``docker compose down --volumes`` / ``./docker.sh -c``.
db_data_volume() {
    local declared="mmr_db_data"
    local project
    # Prefer compose project name; fall back to the yaml ``name:`` default.
    project="$($COMPOSE -f "$BUILDDIR/docker-compose.yml" config --format json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name") or "mmr")' 2>/dev/null \
        || true)"
    if [[ -z "${project}" ]]; then
        project="mmr"
    fi
    local prefixed="${project}_${declared}"
    if [[ -n "${MMR_DB_VOLUME:-}" ]]; then
        echo "$MMR_DB_VOLUME"
        return
    fi
    if $RUNTIME volume inspect "$prefixed" >/dev/null 2>&1; then
        echo "$prefixed"
    elif $RUNTIME volume inspect "$declared" >/dev/null 2>&1; then
        echo "$declared"
    else
        # Not created yet (first ``-u``) — use the compose-prefixed name so
        # seed writes into the volume services will actually mount.
        echo "$prefixed"
    fi
}

backup() {
    # Snapshot the DuckDB files to the host-visible backups/ dir.
    local name="$BACKUP_NAME"

    # Preferred path: scheduler owns both mounts and runs a clean in-DB backup
    # without stopping the split services.
    if [[ -n "$($COMPOSE -f "$BUILDDIR/docker-compose.yml" ps -q scheduler 2>/dev/null)" ]]; then
        echo "Clean in-DB snapshot via running scheduler service (no downtime)..."
        if [[ -n "$name" ]]; then
            $COMPOSE -f "$BUILDDIR/docker-compose.yml" exec -T scheduler \
                python3 -m trader.mmr_cli data backup --name "$name"
        else
            $COMPOSE -f "$BUILDDIR/docker-compose.yml" exec -T scheduler \
                python3 -m trader.mmr_cli data backup --keep 30
        fi
        echo "Backup complete → $HOME/.local/share/mmr/backups/"
        return
    fi

    # Fallback: container down — the DB isn't being written, so a plain sidecar
    # copy is safe and needs no host duckdb tooling.
    if [[ -z "$name" ]]; then
        name="$(date +%Y-%m-%d_%H-%M-%S)"
    fi
    local dest_host="$HOME/.local/share/mmr/backups/$name"
    local vol
    vol="$(db_data_volume)"
    mkdir -p "$dest_host"
    echo "Scheduler is not running — plain-copy snapshot from volume '$vol' to $dest_host/"
    $RUNTIME run --rm \
        -v "$vol":/src:ro \
        -v "$dest_host":/dst \
        alpine sh -c 'cp -v /src/*.duckdb /dst/ 2>&1; ls -lh /dst/'
    echo "Backup complete: $dest_host/"
}

# Seed an EMPTY named volume from the newest host backup (backups/latest) before
# the containers start. Without this, a lost/reset volume would restart from
# whatever the image happened to seed — the "stale seed" trap. Never overwrites a
# volume that already has data; no-ops when there's no latest backup.
seed_db_if_empty() {
    local vol
    vol="$(db_data_volume)"
    local latest_link="$HOME/.local/share/mmr/backups/latest"
    local latest
    latest=$(cd -P "$latest_link" 2>/dev/null && pwd) || return 0
    [[ -n "$latest" && -d "$latest" ]] || return 0
    ls "$latest"/*.duckdb >/dev/null 2>&1 || return 0

    local has_db
    has_db=$($RUNTIME run --rm -v "$vol":/v alpine sh -c \
        'ls /v/*.duckdb >/dev/null 2>&1 && echo yes || echo no' 2>/dev/null || echo no)
    if [[ "$has_db" == "yes" ]]; then
        return 0   # volume already populated — NEVER overwrite live data
    fi

    echo "Named volume '$vol' is empty — seeding from newest backup $latest ..."
    $RUNTIME run --rm -v "$vol":/v -v "$latest":/seed:ro \
        alpine sh -c 'cp -v /seed/*.duckdb /v/ 2>&1; ls -lh /v/'
    echo "Seed complete."
}

clean() {
    echo "Stopping and removing all containers and images..."
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" down --rmi all --volumes 2>/dev/null || true
}

force_clean() {
    clean
    echo "Cleaning build cache..."
    if [[ "$RUNTIME" == "docker" ]]; then
        docker builder prune --force
    else
        podman system prune --force
    fi

    # Host data is bind-mounted, so docker-level cleanup NEVER touches it —
    # if the user asked for force-clean they probably want a true reset, so
    # confirm the scope and wipe. Skipping when MMR_FORCE_CLEAN_KEEP_DATA=1
    # lets CI/automation keep the "build-cache-only" behaviour if needed.
    if [ "${MMR_FORCE_CLEAN_KEEP_DATA:-0}" = "1" ]; then
        echo "MMR_FORCE_CLEAN_KEEP_DATA=1 — keeping host data at $MMR_DATA_DIR"
        return
    fi
    if [ ! -d "$MMR_DATA_DIR" ]; then
        return
    fi
    echo ""
    echo "=============================================================="
    echo "  WIPE HOST DATA?"
    echo "=============================================================="
    echo "  This will DELETE all sweeps, backtest runs, proposals,"
    echo "  universes, downloaded OHLCV history, IB session state, and"
    echo "  logs at:"
    echo ""
    echo "    $MMR_DATA_DIR  ($(_human_size "$MMR_DATA_DIR"))"
    echo ""
    echo "  This is NOT recoverable. Container images + build cache are"
    echo "  already gone at this point; skipping now leaves only the"
    echo "  data behind for a fresh rebuild on next start."
    echo ""
    read -p "Type 'DELETE' to confirm (anything else aborts): " confirm
    if [ "$confirm" = "DELETE" ]; then
        echo "Removing $MMR_DATA_DIR..."
        rm -rf "$MMR_DATA_DIR"
        echo "Host data wiped."
    else
        echo "Keeping host data. (Tip: MMR_FORCE_CLEAN_KEEP_DATA=1 skips this prompt.)"
    fi
}

logs() {
    $COMPOSE -f "$BUILDDIR/docker-compose.yml" logs -f
}

exec_in() {
    local user="trader"
    local workdir="/home/trader/mmr"
    if [[ "$EXEC_SERVICE" == "ib-gateway" ]]; then
        user="ibgateway"
        workdir="/home/ibgateway"
    fi
    echo "Exec'ing into MMR $EXEC_SERVICE service..."
    exec $COMPOSE -f "$BUILDDIR/docker-compose.yml" exec -it -u "$user" -w "$workdir" "$EXEC_SERVICE" bash -l
}

sync() {
    echo "Sync is unavailable: split services run baked, read-only images."
    echo "Rebuild and recreate them with: ./docker.sh -b -u"
    exit 1

}

sync_all() {
    echo "Sync is unavailable: split services run baked, read-only images."
    echo "Rebuild and recreate them with: ./docker.sh -b -u"
    exit 1

}

_known_principals() {
    _compose_rpc_key_files | sed -E 's/\.(key|pub)$//' | sort -u
}

_check_rpc_key_modes() {
    local keys_dir="$1" f mode
    for f in "$keys_dir"/*.key "$keys_dir"/*.pub; do
        [[ -e "$f" ]] || continue
        mode="$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f")"
        case "$f" in
            *.key) [[ "$mode" == "600" ]] || { echo "Error: $f has mode $mode, expected 600"; exit 1; } ;;
            *.pub) [[ "$mode" == "644" ]] || { echo "Error: $f has mode $mode, expected 644"; exit 1; } ;;
        esac
    done
}

keys() {
    local keys_dir="$HOME/.config/mmr/keys/rpc"
    local user_spec
    user_spec="$(id -u):$(id -g)"
    if [[ -n "$KEYS_ROTATE" ]] && ! _known_principals | grep -qx -- "$KEYS_ROTATE"; then
        echo "Error: unknown principal '$KEYS_ROTATE' (expected one of: $(_known_principals | tr '\n' ' '))"
        exit 1
    fi
    # Create the bind source first so Docker never creates it as root.
    mkdir -p "$keys_dir"
    chmod 700 "$keys_dir"
    case "$KEYS_ACTION" in
        init)
            if [[ -n "$KEYS_ROTATE" ]]; then
                $COMPOSE -f "$BUILDDIR/docker-compose.yml" run --rm --no-deps --user "$user_spec" \
                    keygen init --rotate "$KEYS_ROTATE"
            else
                $COMPOSE -f "$BUILDDIR/docker-compose.yml" run --rm --no-deps --user "$user_spec" \
                    keygen init
            fi
            _check_rpc_key_modes "$keys_dir"
            ;;
        backup)
            local recipient="${KEYS_RECIPIENT:-$HOME/.config/mmr/keys/rpc_backup_recipient.txt}"
            if [[ ! -f "$recipient" ]]; then
                echo "Error: no age recipient file at $recipient"
                echo "Create an age key pair, keep the identity in 1Password, and save only the"
                echo "public recipient: age-keygen | ... ; echo 'age1...' > $recipient"
                exit 1
            fi
            local backup_dir="$HOME/.local/share/mmr/backups/rpc_keys"
            mkdir -p "$backup_dir"
            chmod 700 "$backup_dir"
            local stamp
            stamp="$(date -u +%Y%m%dT%H%M%SZ)"
            $COMPOSE -f "$BUILDDIR/docker-compose.yml" run --rm --no-deps --user "$user_spec" \
                -v "$recipient:/recipient.txt:ro" -v "$backup_dir:/backups" \
                keygen backup --recipient /recipient.txt --out "/backups/rpc_keys_${stamp}.tar.age"
            ;;
        restore)
            if [[ ! -f "$KEYS_ARCHIVE" ]]; then
                echo "Error: backup file not found: $KEYS_ARCHIVE"
                exit 1
            fi
            local archive
            archive="$(cd "$(dirname "$KEYS_ARCHIVE")" && pwd)/$(basename "$KEYS_ARCHIVE")"
            if [[ -n "$KEYS_IDENTITY_FILE" ]]; then
                $COMPOSE -f "$BUILDDIR/docker-compose.yml" run --rm --no-deps -T --user "$user_spec" \
                    -v "$archive:/restore/archive.tar.age:ro" -v "$KEYS_IDENTITY_FILE:/identity:ro" \
                    keygen restore /restore/archive.tar.age --identity-file /identity
                if [[ $KEYS_DELETE_IDENTITY_FILE == "y" ]]; then
                    : >| "$KEYS_IDENTITY_FILE"
                    rm -f -- "$KEYS_IDENTITY_FILE"
                fi
            else
                # The identity streams from this script's stdin straight into
                # the container; it is never written to disk or put on argv.
                $COMPOSE -f "$BUILDDIR/docker-compose.yml" run --rm --no-deps -T --user "$user_spec" \
                    -v "$archive:/restore/archive.tar.age:ro" \
                    keygen restore /restore/archive.tar.age --identity-stdin
            fi
            _check_rpc_key_modes "$keys_dir"
            ;;
    esac
}

# Cutover gate (-K). Runs `mmr keys check-mount` as a one-shot container per
# service in its own compose project, with the test override (fake broker,
# --simulation True on trader). The entrypoint is replaced, so no service
# process starts, no port is published and the live `mmr` project is not
# touched. Each container decides pass/fail itself from the shared
# principals.rpc_files_for formula.
KEYCHECK_PROJECT="mmr-keycheck"
KEYCHECK_SERVICES="trader strategy dashboard cli scheduler data ai research"

key_check() {
    _require_rpc_keys
    _require_signing_key
    local compose_args=(-p "$KEYCHECK_PROJECT" -f "$BUILDDIR/docker-compose.yml"
                        -f "$BUILDDIR/docker-compose.test.override.yml")
    local failed="" svc
    for svc in $KEYCHECK_SERVICES; do
        if ! $COMPOSE "${compose_args[@]}" run --rm --no-deps -T --entrypoint python \
                "$svc" -m trader.messaging.keys_cli check-mount "$svc"; then
            failed="$failed $svc"
        fi
    done
    # No --volumes: only this project's containers and network go. Its empty
    # DB volume is removed by its project-prefixed name, never the live one.
    $COMPOSE "${compose_args[@]}" down --remove-orphans || true
    $RUNTIME volume rm "${KEYCHECK_PROJECT}_mmr_db_data" >/dev/null 2>&1 || true
    $RUNTIME volume rm "${KEYCHECK_PROJECT}_mmr_ai_data" >/dev/null 2>&1 || true
    $RUNTIME volume rm "${KEYCHECK_PROJECT}_mmr_research_data" >/dev/null 2>&1 || true
    if [[ -n "$failed" ]]; then
        echo "Key check FAILED for:$failed. Do not cut over."
        exit 1
    fi
    echo "Key check passed for every service (project $KEYCHECK_PROJECT)."
}

echo "action: build=$b clean=$c force=$f up=$u down=$d sync=$s sync_all=$a go=$g logs=$l exec=$e ib-only=$i restart-ib=$r news=$n backup=$B${BACKUP_NAME:+($BACKUP_NAME)} key-check=$K | runtime: $RUNTIME"

if [[ $b == "y" ]]; then
    build
fi
if [[ $d == "y" ]]; then
    down
fi
if [[ $c == "y" ]]; then
    clean
fi
if [[ $f == "y" ]]; then
    force_clean
fi
if [[ $u == "y" ]]; then
    up
fi
if [[ $i == "y" ]]; then
    ib_only
fi
if [[ $r == "y" ]]; then
    restart_ib
fi
if [[ $n == "y" ]]; then
    news_up
fi
if [[ $s == "y" ]]; then
    sync
fi
if [[ $a == "y" ]]; then
    sync_all
fi
if [[ $l == "y" ]]; then
    logs
fi
if [[ $e == "y" ]]; then
    exec_in
fi
if [[ $g == "y" ]]; then
    build
    down 2>/dev/null || true
    up
    echo ""
    echo "Waiting for containers to start..."
    sleep 3
    exec_in
fi
if [[ $B == "y" ]]; then
    backup
fi
if [[ $k == "y" ]]; then
    keys
fi
if [[ $K == "y" ]]; then
    key_check
fi
