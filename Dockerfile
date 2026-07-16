FROM python:3.12.13-slim-bookworm AS runtime
WORKDIR /home/trader/mmr
ENV container=docker
ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive

# Pin a fixed UID/GID (rather than whatever `useradd` picks next) so the
# G0 Compose split's `user: trader` + `read_only: true` hardening has a
# stable, documented identity: host-bind-mounted directories
# (~/.config/mmr, ~/.local/share/mmr/{logs,backups}) must be readable/
# writable by this UID since the container can no longer chown them at
# startup once it's running as a non-root user (see
# scripts/docker-entrypoint.sh). Operators on a fresh host should
# `chown -R 1000:1000` those directories once (or rely on Docker Desktop's
# UID passthrough, which usually already matches on macOS).
RUN groupadd -g 1000 trader \
    && useradd -u 1000 -g 1000 -m -d /home/trader -s /bin/bash -G sudo trader \
    && mkdir -p /tmp

# System packages (no TWS/VNC/X11 — IB Gateway runs in a separate container).
# Python 3.12.13 is provided by the python:3.12.13-slim-bookworm base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    dialog apt-utils ca-certificates \
    git wget vim dpkg build-essential \
    curl locales-all sudo unzip tmux \
    iproute2 net-tools rsync iputils-ping lnav jq \
    # required for native Python packages that compile C extensions
    libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# set to New York timezone, can override with docker run -e TZ=Europe/London etc.
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ports: pycron, zmq
EXPOSE 8081

# spin up the directories required
RUN mkdir -p /home/trader/.local/share/mmr/data \
    && mkdir -p /home/trader/.local/share/mmr/logs \
    && mkdir -p /home/trader/.config /home/trader/.tmp /home/trader/.cache /home/trader/.local/bin \
    && chown -R trader:trader /home/trader

RUN touch /home/trader/.hushlogin

# create Python virtualenv (changes rarely — cached layer)
USER trader

ENV HOME=/home/trader
ENV TMPDIR=$HOME/.tmp
ENV PATH=$HOME/.venv/bin:$HOME/.local/bin:$PATH

WORKDIR /home/trader/mmr

RUN python3 -m venv $HOME/.venv

# Copy ONLY requirements.txt first — this layer is cached unless deps change
COPY --chown=trader:trader requirements.txt /home/trader/mmr/requirements.txt

# pip install packages (cached unless requirements.txt changes)
RUN --mount=type=cache,target=/home/trader/.cache/pip \
    pip3 install -r /home/trader/mmr/requirements.txt

# NOW copy the rest of the source (this layer busts on every code change,
# but everything above is cached)
USER root
COPY --chown=trader:trader ./ /home/trader/mmr/

# Register the mmr package + console entry points (mmr, trader-service, ...)
# defined in pyproject.toml. --no-deps because requirements.txt covered them.
USER trader
RUN pip install --no-deps -e /home/trader/mmr

USER root
# .bash_profile sets env for interactive exec-ins. Services are launched
# by docker-entrypoint.sh — interactive shells must NOT re-launch them or
# they'll collide on ZMQ ports + IB client id.
RUN printf '%s\n' \
    'export HOME=/home/trader' \
    'export PATH="$HOME/.venv/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"' \
    'export TMPDIR="$HOME/.tmp"' \
    'export TRADER_CONFIG="$HOME/.config/mmr/trader.yaml"' \
    '' \
    '# Source IB Gateway env vars written by docker-entrypoint.sh (lives in' \
    '# /tmp, not $HOME -- see docker-entrypoint.sh for why: $HOME itself is' \
    '# not a writable path under G0'"'"'s read_only:true root filesystem)' \
    '[ -f /tmp/.mmr_env ] && . /tmp/.mmr_env' \
    '' \
    'cd $HOME/mmr' \
    > /home/trader/.bash_profile \
    && chown trader:trader /home/trader/.bash_profile

# Pre-populate user config from bundled defaults so TRADER_CONFIG resolves
RUN mkdir -p /home/trader/.config/mmr \
    && cp /home/trader/mmr/config_defaults/*.yaml /home/trader/.config/mmr/ \
    && chown -R trader:trader /home/trader/.config/mmr

RUN chmod +x /home/trader/mmr/scripts/docker-entrypoint.sh

# Default the image itself to the unprivileged user (defense-in-depth for
# anyone who `docker run`s the image directly without Compose's explicit
# `user: trader`); docker-entrypoint.sh detects this and skips the
# root-only permission-fixing steps it still supports for a manual
# `docker run --user root` debug session.
USER trader
WORKDIR /home/trader
ENTRYPOINT ["/home/trader/mmr/scripts/docker-entrypoint.sh"]

# ---------------------------------------------------------------------------
# `test` stage (G0 Task 6) -- ONLY used by docker-compose.yml's `test`-profile
# `fullstack-tests` runner (`build.target: test`). The five real, always-on
# services build the plain `runtime` stage above (docker-compose.yml's
# `x-mmr-build` anchor pins `target: runtime`), so production images never
# carry pytest/docker-py. `requirements.txt` intentionally does NOT list
# these -- they'd otherwise ship in every real deployment for no reason.
FROM runtime AS test
USER root
RUN --mount=type=cache,target=/home/trader/.cache/pip \
    pip3 install "pytest>=8.0" "pytest-timeout>=2.3" "docker>=7.0"
USER trader
