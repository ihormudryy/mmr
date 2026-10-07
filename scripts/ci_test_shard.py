"""Print the test files for one CI shard.

Usage: python scripts/ci_test_shard.py SHARD_INDEX SHARD_COUNT

Files are spread over the shards greedily by size (a rough proxy for run
time), so every file runs in exactly one shard and the shards finish at
about the same time. The quarantined async IB file runs in its own job.
"""
import sys
from pathlib import Path

EXCLUDED = {Path("tests/test_ibrx_async.py")}


def test_files(root: Path) -> list[Path]:
    return sorted(
        path.relative_to(root)
        for path in (root / "tests").rglob("test_*.py")
        if path.relative_to(root) not in EXCLUDED
    )


def assign(files: list[Path], root: Path, count: int) -> list[list[Path]]:
    shards: list[list[Path]] = [[] for _ in range(count)]
    loads = [0] * count
    for path in sorted(files, key=lambda p: ((root / p).stat().st_size, str(p)), reverse=True):
        smallest = loads.index(min(loads))
        shards[smallest].append(path)
        loads[smallest] += (root / path).stat().st_size
    return shards


def main() -> None:
    index, count = int(sys.argv[1]), int(sys.argv[2])
    if not 0 <= index < count:
        raise SystemExit(f"shard index {index} is outside 0..{count - 1}")
    root = Path.cwd()
    for path in sorted(assign(test_files(root), root, count)[index]):
        print(path)


if __name__ == "__main__":
    main()
