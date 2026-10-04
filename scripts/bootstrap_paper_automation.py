#!/usr/bin/env python3
"""Verify existing public research material and print disabled activation config.

No normal-path key generation, evidence fabrication or activation. Explicit
--offline-fixture writes isolated, non-authorizing material for offline drills;
it never prints activation snippets and cannot qualify a strategy for promotion.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
    verify_qualified_paper_bundle,
)

DEFAULT_CONFIG = Path(os.path.expanduser("~/.config/mmr"))
DEFAULT_SHARE = Path(os.path.expanduser("~/.local/share/mmr"))


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
    print("   A signature verifies integrity, not that performance was measured.")
    print("   Other strategies may keep auto_execute: propose (human approve).")
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
        description="Verify existing paper research material; offline fixtures require explicit opt-in",
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
        help="Overwrite isolated offline-fixture key files (never research keys)",
    )
    parser.add_argument("--offline-fixture", action="store_true",
                        help="Export non-authorizing fixture for offline drills only")
    parser.add_argument("--artifact-bundle-path", type=Path,
                        help="Existing signed research bundle (no new evidence is created)")
    parser.add_argument("--public-key-ring-path", type=Path,
                        help="Existing trusted research public keys")
    parser.add_argument("--expected-artifact-id", help="Exact existing research artifact id")
    args = parser.parse_args()
    research_args = (args.artifact_bundle_path, args.public_key_ring_path, args.expected_artifact_id)
    if not args.offline_fixture and not all(research_args):
        parser.error("existing research requires --artifact-bundle-path, --public-key-ring-path "
                     "and --expected-artifact-id; use --offline-fixture only for offline drills")

    try:
        if args.offline_fixture:
            private_key, key_ring, public_key = default_key_paths(args.config_dir / "offline-fixtures")
            signer, _reused = ensure_signing_keypair(
                private_key_path=private_key, public_key_path=public_key, force=args.force,
            )
            artifacts_root = args.share_dir / "offline-fixtures" / "artifacts"
            artifact_id = export_fixture_paper_eligible_bundle(
                signer=signer, artifacts_root=artifacts_root, offline_fixture=True,
            )
            print("OFFLINE FIXTURE — not qualification or promotion evidence")
            print("CANDIDATE; permitted_account_mode=none; never activate this bundle")
            print(f"Fixture bundle: {artifacts_root / artifact_id}")
            print(f"Isolated fixture public key ring: {key_ring}")
            return 0

        bundle_path = args.artifact_bundle_path.expanduser()
        key_ring = args.public_key_ring_path.expanduser()
        verified = verify_qualified_paper_bundle(
            bundle_path=bundle_path, public_key_ring_path=key_ring,
            expected_artifact_id=args.expected_artifact_id, now=dt.datetime.now(dt.timezone.utc),
        )
        artifact_id = verified.artifact_id
    except (PaperMaterialsError, FileExistsError) as exc:
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
