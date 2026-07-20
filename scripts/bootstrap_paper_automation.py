#!/usr/bin/env python3
"""Bootstrap paper automation keys + one fixture PAPER_ELIGIBLE artifact.

Writes operator-local material only (never committed):

* ``~/.config/mmr/keys/private/signing.pem`` — Ed25519 PKCS8 private (0o600)
* ``~/.config/mmr/keys/verify/*.pem`` — public verify ring for trader/strategy
* ``~/.local/share/mmr/artifacts/<artifact_id>/`` — exported research bundle

Prints the exact nested ``trader.yaml`` + strategy YAML snippets for hybrid
paper-auto activation. Does not enable automation itself.

Usage:
    python3 scripts/bootstrap_paper_automation.py
    python3 scripts/bootstrap_paper_automation.py --strategy-name orb_googl
    python3 scripts/bootstrap_paper_automation.py --force
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from trader.automation.paper_materials import (
    PaperMaterialsError,
    default_key_paths,
    ensure_signing_keypair,
    export_fixture_paper_eligible_bundle,
)

DEFAULT_CONFIG = Path(os.path.expanduser("~/.config/mmr"))
DEFAULT_SHARE = Path(os.path.expanduser("~/.local/share/mmr"))


def _print_snippets(
    *,
    strategy_name: str,
    artifact_id: str,
    bundle_path: Path,
    key_ring: Path,
    private_key: Path,
) -> None:
    print()
    print("=== Hybrid paper automation — next steps ===")
    print()
    print(f"Private key (never commit): {private_key}")
    print(f"Public key ring:            {key_ring}")
    print(f"Artifact bundle:            {bundle_path}")
    print(f"Artifact id:                {artifact_id}")
    print()
    print("1) Append to ~/.config/mmr/trader.yaml (paper only):")
    print()
    print("command_authority:")
    print("  enabled: true")
    print("  live_enabled: false")
    print("automation:")
    print("  enabled: true")
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
    print("3) Other strategies may keep auto_execute: propose (human approve).")
    print()
    print("4) Release gates (P1 then P3):")
    print("   python3 scripts/p1_release_gate.py --synthetic-only")
    print("   python3 scripts/p3_release_gate.py --synthetic-only")
    print("   # During XNYS RTH with Docker+IB paper:")
    print("   python3 scripts/p1_release_gate.py --ib-paper --watch-minutes 390")
    print("   python3 scripts/p3_release_gate.py --ib-paper --watch-minutes 390")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap paper automation keys + fixture PAPER_ELIGIBLE artifact",
    )
    parser.add_argument(
        "--strategy-name", default="orb_googl",
        help="Exact automation_strategy_name to print in YAML snippets",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=DEFAULT_CONFIG,
        help="Config root (default ~/.config/mmr)",
    )
    parser.add_argument(
        "--share-dir", type=Path, default=DEFAULT_SHARE,
        help="Share root (default ~/.local/share/mmr)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing key files",
    )
    args = parser.parse_args()

    private_key, key_ring, public_key = default_key_paths(args.config_dir)
    artifacts_root = args.share_dir / "artifacts"

    try:
        signer, _reused = ensure_signing_keypair(
            private_key_path=private_key,
            public_key_path=public_key,
            force=args.force,
        )

        artifact_id = export_fixture_paper_eligible_bundle(
            signer=signer,
            artifacts_root=artifacts_root,
        )
    except (PaperMaterialsError, FileExistsError) as exc:
        print(exc, file=sys.stderr)
        return 1

    bundle_path = artifacts_root / artifact_id

    _print_snippets(
        strategy_name=args.strategy_name,
        artifact_id=artifact_id,
        bundle_path=bundle_path,
        key_ring=key_ring,
        private_key=private_key,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
