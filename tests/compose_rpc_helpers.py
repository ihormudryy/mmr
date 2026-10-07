"""Static readers for docker-compose.yml's RPC key mounts (no Docker needed)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
CONTAINER_CONFIG = "/home/trader/.config/mmr"
CONTAINER_RPC_DIR = f"{CONTAINER_CONFIG}/keys/rpc"
RETIRED_HMAC_TARGET = f"{CONTAINER_CONFIG}/service_hmac.key"


def load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def parse_volume(volume) -> dict:
    """Normalise short and long volume syntax to {type, source, target, read_only}."""
    if isinstance(volume, dict):
        return {"type": volume.get("type", "bind"), "source": str(volume.get("source", "")),
                "target": str(volume.get("target", "")),
                "read_only": bool(volume.get("read_only", False))}
    parts = str(volume).split(":")
    source, target = parts[0], parts[1] if len(parts) > 1 else parts[0]
    options = parts[2].split(",") if len(parts) > 2 else []
    kind = "bind" if source.startswith(("/", "$", ".", "~")) else "volume"
    return {"type": kind, "source": source, "target": target, "read_only": "ro" in options}


def volumes(service: dict) -> list[dict]:
    return [parse_volume(v) for v in service.get("volumes", [])]


def mounts_config_dir(service: dict) -> bool:
    """True if the host's ~/.config/mmr is mounted; a tmpfs mask over it (the ai service) exposes nothing."""
    return any(v["target"] == CONTAINER_CONFIG and v["type"] != "tmpfs" for v in volumes(service))


def visible_rpc_files(service: dict) -> set[str] | None:
    """Files under keys/rpc a container can read; None if it sees the whole host dir.

    A service with no ~/.config/mmr mount and no key binds sees nothing.
    """
    vols = volumes(service)
    hidden = any(v["type"] == "tmpfs" and v["target"] == CONTAINER_RPC_DIR for v in vols)
    if mounts_config_dir(service) and not hidden:
        return None
    return {v["target"].rsplit("/", 1)[1] for v in vols
            if v["type"] == "bind" and v["target"].startswith(CONTAINER_RPC_DIR + "/")}
