#!/usr/bin/env python3
"""Seal or replay a forensic trading-day bundle (P3 Task 8).

Usage:
    python3 scripts/replay_trading_day.py seal --session xnys-2026-07-18 \\
        --evidence-dir ~/.local/share/mmr/replay_evidence --output-dir ./bundles
    python3 scripts/replay_trading_day.py replay --bundle ./bundles/xnys-2026-07-18
    python3 scripts/replay_trading_day.py replay --bundle ./bundles/xnys-2026-07-18 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from trader.automation.replay import TradingDayReplay
from trader.automation.replay_bundle import ReplayBundle, ReplayBundleError
from trader.research.canonical import canonical_json_bytes


class DirectoryEvidenceStore:
    """Load session evidence from ``<root>/<session_id>.json`` files."""

    def __init__(self, root: Path):
        self._root = Path(root)

    def load_session(self, session_id: str) -> Mapping[str, Any]:
        path = self._root / f"{session_id}.json"
        if not path.is_file():
            raise KeyError(session_id)
        return json.loads(path.read_text(encoding="utf-8"))


def _cmd_seal(args: argparse.Namespace) -> int:
    store = DirectoryEvidenceStore(args.evidence_dir)
    bundle = ReplayBundle(store, output_dir=args.output_dir)
    digest = bundle.seal(args.session)
    payload = {
        "success": True,
        "session_id": args.session,
        "manifest_digest": digest.manifest_digest,
        "path": str(digest.path),
    }
    if args.json:
        sys.stdout.buffer.write(canonical_json_bytes(payload) + b"\n")
    else:
        print(f"sealed {args.session} -> {digest.path}")
        print(f"manifest_digest={digest.manifest_digest}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    verified = ReplayBundle.verify(args.bundle)
    result = TradingDayReplay().run(verified)
    payload = {
        "success": result.matched,
        "session_id": result.session_id,
        "matched": result.matched,
        "decision_trace_digest": result.decision_trace_digest,
        "recomputed_trace_digest": result.recomputed_trace_digest,
        "divergences": [asdict(d) for d in result.divergences],
    }
    if args.json:
        sys.stdout.buffer.write(canonical_json_bytes(payload) + b"\n")
    else:
        status = "MATCH" if result.matched else "DIVERGENCE"
        print(f"replay {result.session_id}: {status}")
        print(f"sealed_trace={result.decision_trace_digest}")
        print(f"recomputed_trace={result.recomputed_trace_digest}")
        for div in result.divergences:
            print(f"  - {div.kind} @ {div.path}: expected={div.expected!r} actual={div.actual!r}")
    return 0 if result.matched else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seal or replay a forensic trading day.")
    sub = parser.add_subparsers(dest="command", required=True)

    seal = sub.add_parser("seal", help="Seal a complete session into a read-only bundle")
    seal.add_argument("--session", required=True, help="Session id (safe path component)")
    seal.add_argument("--evidence-dir", type=Path, required=True,
                      help="Directory of <session_id>.json evidence files")
    seal.add_argument("--output-dir", type=Path, required=True,
                      help="Directory that receives the sealed bundle")
    seal.add_argument("--json", action="store_true")

    replay = sub.add_parser("replay", help="Replay a sealed bundle and compare traces")
    replay.add_argument("--bundle", type=Path, required=True, help="Sealed bundle directory")
    replay.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "seal":
            return _cmd_seal(args)
        return _cmd_replay(args)
    except ReplayBundleError as exc:
        if getattr(args, "json", False):
            sys.stdout.buffer.write(canonical_json_bytes({
                "success": False, "message": str(exc),
            }) + b"\n")
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
