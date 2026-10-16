# ---------------------------------------------------------------------------
# `build` stage: compilers and headers for Python packages with C extensions stay here and never reach the
# runtime image (#206). The runtime stage copies only the finished virtualenv (same base, same paths).
FROM python:3.12.13-slim-bookworm AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 1000 trader && useradd -u 1000 -g 1000 -m -d /home/trader -s /bin/bash trader
USER trader
ENV HOME=/home/trader
RUN python3 -m venv $HOME/.venv
COPY --chown=trader:trader requirements.txt /home/trader/requirements.txt
RUN --mount=type=cache,target=/home/trader/.cache/pip \
    $HOME/.venv/bin/pip3 install -r /home/trader/requirements.txt

# ---------------------------------------------------------------------------
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
    && useradd -u 1000 -g 1000 -m -d /home/trader -s /bin/bash trader \
    && mkdir -p /tmp

# Sandbox child identities (SP3 Plan 2, spec 7.5): one uid per session slot, in a
# group of their own, none in sudo or trader. The sandbox supervisor drops each
# forked child to one of these before any authored code loads.
RUN groupadd -g 20000 sbxchild \
    && for uid in $(seq 20001 20007); do \
         useradd -u "$uid" -g 20000 -M -N -s /usr/sbin/nologin "sbx$uid"; \
       done

# System packages (no TWS/VNC/X11 — IB Gateway runs in a separate container).
# Python 3.12.13 is provided by the python:3.12.13-slim-bookworm base image. Runtime only: no compilers, and
# no shell conveniences (vim, tmux, rsync, wget, ...) that nothing in the containers uses (#206).
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    ca-certificates git dpkg curl locales-all unzip iproute2 jq \
    && rm -rf /var/lib/apt/lists/*

# age: encrypted RPC key backups (`./docker.sh -k --backup`, SP1 Plan 2). The official release, not Debian's
# 1.1.1 built with Go 1.19.8 (7 critical, 43 high CVEs, #206). The sums are GitHub's release asset digests.
ARG AGE_VERSION=1.3.2
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
      amd64) sum=cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10 ;; \
      arm64) sum=6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4 ;; \
      *) echo "no pinned age build for architecture '${TARGETARCH}'" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/age.tgz \
       "https://github.com/FiloSottile/age/releases/download/v${AGE_VERSION}/age-v${AGE_VERSION}-linux-${TARGETARCH}.tar.gz" \
    && echo "${sum}  /tmp/age.tgz" | sha256sum -c - \
    && tar -xzf /tmp/age.tgz -C /tmp \
    && install -m 0755 /tmp/age/age /tmp/age/age-keygen /usr/local/bin/ \
    && rm -rf /tmp/age /tmp/age.tgz

# set to New York timezone, can override with docker run -e TZ=Europe/London etc.
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ports: pycron, zmq
EXPOSE 8081

# spin up the directories required
RUN mkdir -p /home/trader/.local/share/mmr/data \
    && mkdir -p /home/trader/.local/share/mmr/logs \
    && mkdir -p /home/trader/.local/share/mmr/research \
    && mkdir -p /home/trader/.local/share/mmr_ai \
    && mkdir -p /home/trader/.config /home/trader/.tmp /home/trader/.cache /home/trader/.local/bin \
    && chown -R trader:trader /home/trader

RUN touch /home/trader/.hushlogin

# create Python virtualenv (changes rarely — cached layer)
USER trader

ENV HOME=/home/trader
ENV TMPDIR=$HOME/.tmp
ENV PATH=$HOME/.venv/bin:$HOME/.local/bin:$PATH

WORKDIR /home/trader/mmr

# The virtualenv built in the `build` stage (cached unless requirements.txt changes).
COPY --from=build --chown=trader:trader /home/trader/.venv /home/trader/.venv

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
    && chown -R trader:trader /home/trader/.config/mmr \
    && chmod 0700 /home/trader/.config

RUN chmod +x /home/trader/mmr/scripts/docker-entrypoint.sh

# Build identity baked at image build time (SP3 Plan 2 ruling 1). docker.sh passes
# MMR_BUILD_ID; a plain `docker build` leaves it "unset" and the sandbox refuses to start.
# Declared here, after the apt, venv and pip layers, so a new build id does not rebuild them.
ARG MMR_BUILD_ID=unset
RUN printf '%s\n' "${MMR_BUILD_ID}" > /etc/mmr-build-id && chmod 0444 /etc/mmr-build-id
LABEL mmr.build_id="${MMR_BUILD_ID}"

# The authored-strategy store is mounted read-only here on the sandbox; create it so
# a fresh volume belongs to trader (the trader service, Plan 3, writes it).
RUN mkdir -p /store && chown trader:trader /store && chmod 0755 /store

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
