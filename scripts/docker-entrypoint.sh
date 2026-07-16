#!/bin/bash
# MMR container entrypoint
#
# Sets IB Gateway connection env vars based on TRADING_MODE, does best-effort
# directory setup, then hands off to whatever command the Compose service
# declared (`command:` in docker-compose.yml -- e.g. `python -m
# trader.trader_service`) via "$@". Falls back to start_mmr.sh (the
# all-in-one legacy/manual-debug launcher) when invoked with no arguments,
# e.g. a bare `docker run <image>`.
#
# G0 Task 5: each Compose service now runs `user: trader` (non-root) and
# `read_only: true` (root filesystem read-only; writable paths are explicit
# volumes/tmpfs — see docker-compose.yml). This script therefore runs AS
# trader from the start under Compose, not just for the final exec'd
# command, so it can no longer chown/fix ownership at startup the way it
# used to when everything ran as root. The chown/chmod recovery steps below
# are now conditional on actually being root (true for a manual `docker run
# --user root` debug session, or if the image's default USER is ever
# overridden back to root) — under the normal Compose path they're skipped,
# and we rely on: (1) named volumes (e.g. mmr_db_data) inheriting the
# image's baked-in ownership on first mount, and (2) host bind-mounted
# directories (~/.config/mmr, ~/.local/share/mmr/{logs,backups}) already
# being writable by the pinned trader UID (1000) — see the Dockerfile
# comment above the `useradd` line for the operator-facing note.

# Default IB Gateway connection (overridable via env vars)
IB_SERVER_ADDRESS="${IB_SERVER_ADDRESS:-ib-gateway}"

# Set the API port based on trading mode
# IB Gateway internal ports: 4003 = live, 4004 = paper
if [ "${TRADING_MODE:-paper}" = "paper" ]; then
    IB_SERVER_PORT="${IB_SERVER_PORT:-4004}"
else
    IB_SERVER_PORT="${IB_SERVER_PORT:-4003}"
fi

echo "MMR starting: IB_SERVER_ADDRESS=$IB_SERVER_ADDRESS IB_SERVER_PORT=$IB_SERVER_PORT TRADING_MODE=${TRADING_MODE:-paper}"

IS_ROOT=false
if [ "$(id -u)" -eq 0 ]; then
    IS_ROOT=true
fi

# Write env vars to a file that .bash_profile can source (interactive
# `docker compose exec ... bash` sessions -- IB_SERVER_PORT in particular is
# *derived* here from TRADING_MODE, so it isn't otherwise available to a
# freshly-exec'd shell). This used to live at /home/trader/.mmr_env, which
# was fine when the whole image ran as root with a writable root fs; under
# G0's read_only:true + user:trader, /home/trader itself is NOT a mounted
# path (only specific subdirectories are — see docker-compose.yml), so a
# write there always fails with EROFS. /tmp IS tmpfs-mounted on every
# service (see the `tmpfs:` list in docker-compose.yml), so write it there
# instead — it doesn't need to survive a restart, just the container's
# lifetime. (Redirect ordering note: `2>/dev/null` must come BEFORE the `>`
# that might fail, e.g. `: 2>/dev/null > file` not `: > file 2>/dev/null` —
# bash sets up redirects left-to-right, so if the failing one is set up
# first, its error goes to the original stderr before the later
# `2>/dev/null` ever takes effect. Confirmed by hand: the original ordering
# leaked "bash: .../.mmr_env: Read-only file system" straight to the
# container log despite the trailing `2>/dev/null`.)
MMR_ENV_FILE=/tmp/.mmr_env
if : 2>/dev/null > "$MMR_ENV_FILE"; then
    cat > "$MMR_ENV_FILE" <<EOF
export IB_SERVER_ADDRESS="$IB_SERVER_ADDRESS"
export IB_SERVER_PORT="$IB_SERVER_PORT"
export TRADING_MODE="${TRADING_MODE:-paper}"
export IB_ACCOUNT="${IB_ACCOUNT:-}"
export TRADER_CONFIG="${TRADER_CONFIG:-/home/trader/.config/mmr/trader.yaml}"
export ZMQ_RPC_SERVER_ADDRESS="${ZMQ_RPC_SERVER_ADDRESS:-tcp://127.0.0.1}"
export ZMQ_PUBSUB_SERVER_ADDRESS="${ZMQ_PUBSUB_SERVER_ADDRESS:-tcp://127.0.0.1}"
export ZMQ_STRATEGY_RPC_SERVER_ADDRESS="${ZMQ_STRATEGY_RPC_SERVER_ADDRESS:-tcp://127.0.0.1}"
export ZMQ_MESSAGEBUS_SERVER_ADDRESS="${ZMQ_MESSAGEBUS_SERVER_ADDRESS:-tcp://127.0.0.1}"
export ZMQ_DATA_RPC_SERVER_ADDRESS="${ZMQ_DATA_RPC_SERVER_ADDRESS:-tcp://127.0.0.1}"
EOF
fi

if [ "$IS_ROOT" = true ]; then
    # Legacy/manual-debug path (root): fix up ownership the way this script
    # always has, so a `docker run --user root` (or an image whose default
    # USER was overridden back to root) still self-heals a fresh bind mount.
    mkdir -p /home/trader/.config/mmr
    cp -n /home/trader/mmr/config_defaults/*.yaml /home/trader/.config/mmr/ 2>/dev/null || true
    chown -R trader:trader /home/trader/.config/mmr 2>/dev/null || true

    mkdir -p /home/trader/.local/share/mmr/data
    mkdir -p /home/trader/.local/share/mmr/logs

    # Fix permissions — use chmod to avoid chown failures in podman rootless
    chown -R trader:trader /home/trader/.local/share/mmr 2>/dev/null || chmod -R 777 /home/trader/.local/share/mmr
else
    # Normal Compose path (non-root, read_only root fs): best-effort mkdir
    # (a no-op if the volume/bind mount already provides the directory,
    # which it always should for the paths each service actually needs —
    # see docker-compose.yml). No chown attempt (we have no privilege to
    # do one) — if a mounted directory's ownership genuinely doesn't allow
    # trader to write, fail loudly with a clear, early diagnostic rather
    # than a silent no-op or an opaque Python traceback three layers down.
    mkdir -p /home/trader/.config/mmr 2>/dev/null || true
    mkdir -p /home/trader/.local/share/mmr/data 2>/dev/null || true
    mkdir -p /home/trader/.local/share/mmr/logs 2>/dev/null || true

    for probe_dir in /home/trader/.config/mmr /home/trader/.local/share/mmr/logs; do
        # Redirect order matters here too (see the .mmr_env comment above):
        # 2>/dev/null must precede the potentially-failing `>` redirect.
        if [ -d "$probe_dir" ] && ! ( : 2>/dev/null > "$probe_dir/.mmr_write_probe" && rm -f "$probe_dir/.mmr_write_probe" ); then
            echo "WARNING: $probe_dir is not writable by $(id -u):$(id -g) (trader)." >&2
            echo "         Host-bind-mounted directories must be owned by (or" >&2
            echo "         group-writable by) UID 1000 under read_only:true + user:" >&2
            echo "         trader. Run: chown -R 1000:1000 \"<host path for $probe_dir>\"" >&2
        fi
    done
fi

# Source the env vars we just wrote (if the file was writable) and hand off
# to the per-service command. Each Compose service supplies its own
# `command:` (e.g. ["python", "-m", "trader.trader_service"]) which becomes
# "$@" here — this is what makes each service its own supervised process
# with its own PID 1 and exit code instead of everything living inside
# start_mmr.sh's monitor loop. Falls back to start_mmr.sh only when no
# command was given at all (bare `docker run <image>`).
[ -f "$MMR_ENV_FILE" ] && . "$MMR_ENV_FILE"
export TRADER_CONFIG="${TRADER_CONFIG:-/home/trader/.config/mmr/trader.yaml}"

if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec /home/trader/mmr/start_mmr.sh
fi
