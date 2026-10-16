# Setup

How to install and run MMR. Start with **paper trading**; the [Disclaimer](../README.md#disclaimer) applies.

## Requirements

- Python 3.12 (CI and the image pin 3.12.13; see `.python-version`)
- An Interactive Brokers account with [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php) or TWS
- Market data subscriptions for the exchanges you trade
- Docker Desktop with 28 GB RAM or more for the full stack (`./docker.sh -u` warns below that)

## Docker (recommended)

```bash
git clone https://github.com/ihormudryy/mmr.git
cd mmr
./docker.sh -g
```

`./docker.sh -g` builds the shared image, asks for your IB username, password and account, writes them to `.env`
(gitignored), starts IB Gateway and the split MMR services, and opens a shell in the `trader` service. Compose starts
each service itself: do not run `start_mmr.sh` inside that shell.

| Command | What it does |
|---|---|
| `./docker.sh -u` | Start the services |
| `./docker.sh -d` | Stop the services (data is kept) |
| `./docker.sh -b` | Build the shared image |
| `./docker.sh -b -u` | Rebuild and restart after code changes (images are read-only) |
| `./docker.sh -e [service]` | Open a shell in `trader` or another service |
| `./docker.sh -l` | Tail the logs |
| `./docker.sh -r` | Restart IB Gateway |
| `./docker.sh -k` / `-K` | Create the RPC keys / check the key mounts |
| `./docker.sh -B [name]` | Back up the DuckDB files |
| `./docker.sh -c` | Remove images and volumes (deletes data) |

The AI services (`ai`, `research`, `sandbox`) are in the compose profile `ai` and start separately. The ordered
checklist is [SP2_PAPER_DEPLOY.md](SP2_PAPER_DEPLOY.md).

### Prebuilt image

Each [release](https://github.com/ihormudryy/mmr/releases) publishes the shared image to GitHub Container Registry for
`linux/amd64` and `linux/arm64`. To skip the local build, pull it and tag it as the image Compose expects:

```bash
docker pull ghcr.io/ihormudryy/mmr:latest      # or a version, e.g. :1.0.0
docker tag ghcr.io/ihormudryy/mmr:latest mmr:latest
./docker.sh -u
```

## Local install (no Docker)

```bash
uv sync --python 3.12.13 --frozen --extra test
./start_mmr.sh --setup       # wizard: IB connection, accounts, API keys
./start_mmr.sh --paper       # or --live
```

The wizard writes `~/.config/mmr/trader.yaml`: IB Gateway host and port, accounts, trading mode, the Alpaca keys
(free; the default US data source) and an optional Massive.com key. Run it again at any time with `--setup`.

## Configuration

User config lives in `~/.config/mmr/`. The files are copied from `config_defaults/` on the first run only, so editing
`config_defaults/` does not change a running system.

| File | Purpose |
|---|---|
| `trader.yaml` | IB connection, DuckDB paths, typed RPC ports, `default_data_source`, AI paper limits, API keys |
| `ai.yaml` | Models, prices and the research cycle of the AI paper controller |
| `position_sizing.yaml` | Base size, risk level, ATR parameters, hard limits |
| `trading_filters.yaml` | Symbol and exchange denylist and allowlist |
| `strategy_runtime.yaml` | Strategy definitions (module, class, conids, bar size) |
| `data_refresh.yaml` | Declarative history refresh jobs |
| `pycron.yaml` | Scheduler cron jobs |
| `logging.yaml` | Console and rotating file logs |

Environment variables override config values (the upper-cased key). `TRADER_CONFIG` overrides the config file path.
Secrets (API keys, the IB login) belong in `.env` or `~/.config/mmr/secrets/`, never in a file you commit.

### IB Gateway container

The gateway runs `ghcr.io/gnzsnz/ib-gateway`, configured in `docker-compose.yml` and `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `TWS_SETTINGS_PATH` | `/home/ibgateway/tws_settings` | Keep runtime settings on a bind mount |
| `BYPASS_WARNING` | `yes` | Dismiss dialogs IBC cannot handle |
| `SAVE_TWS_SETTINGS` | `"08:00 17:00"` | Save settings regularly, not only on a clean shutdown |
| `AUTO_RESTART_TIME` | `11:59 PM` | Daily gateway restart (avoids a new 2FA login) |
| `VNC_SERVER_PASSWORD` | from `.env` | VNC access to the gateway at `vnc://localhost:5901` |

See the [ib-gateway-docker README](https://github.com/gnzsnz/ib-gateway-docker) for every variable. If `mmr status`
says IB is connected but `ib_responding: false`, the gateway stopped answering: `./docker.sh -r`.

## Reproducing a research artifact

A published strategy comes as a read-only artifact bundle. Take the values from its `attestation.json` and run, on the
machine that ran `mmr research attest bundle` (it reads the local research DB):

```bash
mmr research attest verify --digest <payload_digest> --public-key-file <public_key> \
    --artifact-digest <artifact_digest> --allowlist-digest <allowlist_digest> \
    --ruleset-digest <ruleset_digest> --account-mode paper \
    --max-gross-allocation <max_gross_allocation> --instruments <conid> [<conid> ...]
```

`scripts/reproduce_experiment.py --bundle <path>` only prints the path for now; it does not replay yet. A strategy
stays `CANDIDATE` until a qualified dataset earns a signed paper attestation, because vendors restate data over time.

## Project layout

```
trader/            core library and service entry points (trader, strategy, data, ai, research, sandbox)
web/               FastAPI dashboard and command center
strategies/        strategy files (subclass trader.trading.strategy.Strategy)
research/          specs for `mmr research evaluate`
config_defaults/   config templates, copied to ~/.config/mmr/ on the first run
scripts/           operational scripts, release gates, drills
skills/            Claude skills
tests/             pytest suite (no IB connection needed)
docs/              reference docs and runbooks
AGENTS.md          rules for AI coding agents; CLAUDE.md imports it
```
