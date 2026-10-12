#!/usr/bin/env python3
"""Refuse a sandbox full-stack run whose rendered compose config could touch a live stack (SP3 Plan 2 ruling 30).

Reads `docker compose ... config --format json` on stdin. Exit 0 when safe, 2 with one line per problem otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

LIVE_PORTS = frozenset({7496, 7497, 5901, 7424, 8081, *range(42001, 42010), *range(42101, 42110)})
PROJECT_PREFIX = "mmr-sbxtest"
PRODUCTION_IMAGE = "mmr:latest"
# The only services the sandbox test starts (review r2 #2). Plan 3 Task 10 adds research here.
ALLOWED_SERVICES = frozenset({"sandbox", "fullstack-tests"})
# Any of these on a rendered service's environment, with a non-empty value, means live broker/provider access.
CREDENTIAL_PREFIXES = ("TWS_", "IB_", "TWS", "ALPACA_", "MASSIVE_", "TWELVEDATA_", "OPENROUTER_", "AWS_", "AZURE_")
CREDENTIAL_NAMES = frozenset({"TRADING_MODE"})
FORBIDDEN_SERVICES = frozenset({"ib-gateway"})


def _bind_sources(service: dict) -> Iterable[str]:
    for volume in service.get("volumes", []) or []:
        if isinstance(volume, dict) and volume.get("type") == "bind":
            yield str(volume.get("source", ""))


def _environment(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):                           # compose may render it as ["K=v", ...]
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return env


def _is_credential(name: str) -> bool:
    return name in CREDENTIAL_NAMES or name.startswith(CREDENTIAL_PREFIXES)


def _is_under(source: str, roots: list[Path]) -> bool:
    path = Path(source)
    return any(path == root or root in path.parents for root in roots)


def _service_problems(name: str, service: dict, *, forbidden_roots: list[Path]) -> list[str]:
    found = []
    if name in FORBIDDEN_SERVICES:
        found.append(f"{name} is in the rendered config; the sandbox test needs no broker gateway")
    elif name not in ALLOWED_SERVICES:
        found.append(f"{name} is in the rendered config, not one of {sorted(ALLOWED_SERVICES)}")
    image = str(service.get("image", ""))
    if image == PRODUCTION_IMAGE:
        found.append(f"{name} uses the production image {image!r}; use a {PROJECT_PREFIX}:<run id> tag")
    if service.get("build") and not image.startswith(f"{PROJECT_PREFIX}:"):
        found.append(f"{name} builds image {image!r}, not a {PROJECT_PREFIX}:<run id> tag")
    for key, value in _environment(service).items():
        if _is_credential(str(key)) and value is not None and str(value).strip():
            found.append(f"{name} carries credential/mode variable {key} with a value")
    for port in service.get("ports", []) or []:
        published = port.get("published") if isinstance(port, dict) else port
        note = " (a live port)" if str(published).isdigit() and int(published) in LIVE_PORTS else ""
        found.append(f"{name} publishes host port {published}{note}")
    for source in _bind_sources(service):
        if _is_under(source, forbidden_roots):
            found.append(f"{name} binds {source}, under the operator's live config or data")
    for env_file in service.get("env_file", []) or []:
        path = env_file.get("path") if isinstance(env_file, dict) else env_file
        found.append(f"{name} reads env file {path} (it could carry live credentials)")
    return found


def problems(config: dict[str, Any], *, operator_home: str, repo_root: str) -> list[str]:
    """Every way the rendered ``config`` could reach a live stack; [] when it cannot.

    ``repo_root`` is kept for the caller's contract; any env file at all is refused, the repo's .env included."""
    found = []
    if not str(config.get("name", "")).startswith(PROJECT_PREFIX):
        found.append(f"project {config.get('name')!r} is not {PROJECT_PREFIX}*")
    forbidden_roots = [Path(operator_home, ".config", "mmr"), Path(operator_home, ".local", "share", "mmr")]
    built_images: dict[str, str] = {}             # image tag -> the first built service that claims it
    for name, service in sorted((config.get("services") or {}).items()):
        if service.get("build"):
            image = str(service.get("image", ""))
            if image in built_images:              # two built services, one tag: `up --build` overwrites one
                found.append(f"{name} and {built_images[image]} both build image {image!r}; use distinct tags")
            built_images[image] = name
        found.extend(_service_problems(name, service, forbidden_roots=forbidden_roots))
    for name, volume in sorted((config.get("volumes") or {}).items()):
        if not str((volume or {}).get("name", "")).startswith(f"{PROJECT_PREFIX}_"):
            found.append(f"volume {name} is named {(volume or {}).get('name')!r}, outside the test project")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_sbxtest_config.py OPERATOR_HOME REPO_ROOT < rendered.json", file=sys.stderr)
        return 2
    operator_home, repo_root = argv[1], argv[2]
    try:
        config = json.load(sys.stdin)
    except ValueError as exc:
        print(f"refusing: the rendered config is not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(config, dict):
        print("refusing: the rendered config is not a JSON object", file=sys.stderr)
        return 2
    found = problems(config, operator_home=operator_home, repo_root=repo_root)
    for line in found:
        print(f"refusing: {line}", file=sys.stderr)
    return 2 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
