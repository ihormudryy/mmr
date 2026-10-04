#!/usr/bin/env python3
"""Print the paper automation config snippets for a bundle from `mmr research attest bundle`.

Read-only: it writes nothing and creates no keys or evidence. It refuses a bundle
that the key ring under ``~/.config/mmr/keys/verify`` did not sign, then verifies
the whole bundle (signature, expiry, qualified paper-v1 evidence, no fixture
provenance) and prints **disabled** ``trader.yaml`` + strategy YAML snippets.
It does not enable automation; Activate in the dashboard does, after it checks
the strategy binding.

Usage:
    python3 scripts/bootstrap_paper_automation.py --bundle ~/.local/share/mmr/artifacts/sha256_<digest> --strategy-name <name>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from trader.automation.paper_materials import (
    PaperMaterialsError,
    default_key_paths,
    verify_qualified_paper_bundle,
)
from trader.research.signing import InvalidKeyType, MalformedKey, load_verify_key, public_key_id

DEFAULT_CONFIG = Path(os.path.expanduser("~/.config/mmr"))


def _print_snippets(
    *,
    strategy_name: str,
    artifact_id: str,
    bundle_path: Path,
    key_ring: Path,
) -> None:
    print()
    print("=== Hybrid paper automation — next steps ===")
    print()
    print(f"Public key ring:            {key_ring}")
    print(f"Artifact bundle:            {bundle_path}")
    print(f"Artifact id:                {artifact_id}")
    print()
    print("Note: Activate in /cc picks the newest eligible bundle bound to the strategy;")
    print("it may arm a newer one than this and rewrites these keys itself.")
    print()
    print("1) Append to ~/.config/mmr/trader.yaml (paper only):")
    print()
    print("command_authority:")
    print("  enabled: true")
    print("  live_enabled: false")
    print("automation:")
    print("  enabled: false  # Activate only after strategy/evidence preflight")
    print("  live_enabled: false")
    print(f"  artifact_bundle_path: {bundle_path}")
    print(f"  public_key_ring_path: {key_ring}")
    print(f"  expected_artifact_id: {artifact_id}")
    print(f"  strategy_name: {strategy_name}")
    print()
    print("2) Strategy YAML for the ONE automated strategy:")
    print("   - set params.artifact_bundle_path to the bundle path above")
    print("   - do NOT set auto_execute: propose while automation is armed (R1)")
    print()
    print("strategies:")
    print(f"  - name: {strategy_name}")
    print("    # ... module / class / conids / bar_size ...")
    print("    params:")
    print(f"      artifact_bundle_path: {bundle_path}")
    print()
    print("3) Activate via the dashboard after configuring the matching strategy.")
    print("   Activate checks that the bundle attests this strategy's file, class,")
    print("   params, conids and bar size. A signature verifies integrity, not that")
    print("   performance was measured. Other strategies may keep auto_execute: propose.")
    print()
    print("4) Release gates (P1 then P3):")
    print("   python3 scripts/p1_release_gate.py --synthetic-only")
    print("   python3 scripts/p3_release_gate.py --synthetic-only")
    print("   # During XNYS RTH with Docker+IB paper:")
    print("   python3 scripts/p1_release_gate.py --ib-paper --watch-minutes 390")
    print("   python3 scripts/p3_release_gate.py --ib-paper --watch-minutes 390")
    print()


def _read_field(bundle: Path, file_name: str, field: str) -> str:
    path = bundle / file_name
    if not path.is_file():
        raise ValueError(f"not a bundle (no {file_name}): {bundle}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))[field]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"cannot read {field!r} from {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field!r} in {path} is not a non-empty string")
    return value


def _read_bundle(bundle: Path) -> tuple[str, str]:
    """Return ``(artifact_id, public_key_id)`` of an exported bundle, or raise ValueError."""
    return (
        _read_field(bundle, "manifest.json", "artifact_id"),
        _read_field(bundle, "attestation.json", "public_key_id"),
    )


def _read_ring_key_id(public_key_path: Path) -> str:
    if not public_key_path.is_file():
        raise ValueError(
            f"no signing key under {public_key_path.parent}; "
            "run `mmr research attest bundle <artifact_id>` first"
        )
    try:
        return public_key_id(load_verify_key(str(public_key_path)))
    except (OSError, MalformedKey, InvalidKeyType) as exc:
        raise ValueError(
            f"cannot read signing key {public_key_path}: {type(exc).__name__}: {exc}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print paper automation config snippets for a real research bundle",
    )
    parser.add_argument(
        "--bundle", type=Path, required=True,
        help="Bundle directory from `mmr research attest bundle`",
    )
    parser.add_argument(
        "--strategy-name", required=True,
        help="Exact automation_strategy_name to print in YAML snippets",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=DEFAULT_CONFIG,
        help="Config root (default ~/.config/mmr)",
    )
    args = parser.parse_args(argv)

    _, key_ring, public_key = default_key_paths(args.config_dir)
    bundle_path = args.bundle.expanduser().resolve()

    try:
        artifact_id, signed_by = _read_bundle(bundle_path)
        ring_key_id = _read_ring_key_id(public_key)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if signed_by != ring_key_id:
        print(f"bundle signed by key {signed_by}, ring holds {ring_key_id}", file=sys.stderr)
        return 1

    try:
        verify_qualified_paper_bundle(
            bundle_path=bundle_path, public_key_ring_path=key_ring,
            expected_artifact_id=artifact_id, now=dt.datetime.now(dt.timezone.utc),
        )
    except PaperMaterialsError as exc:
        print(exc, file=sys.stderr)
        return 1

    _print_snippets(
        strategy_name=args.strategy_name,
        artifact_id=artifact_id,
        bundle_path=bundle_path,
        key_ring=key_ring,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
