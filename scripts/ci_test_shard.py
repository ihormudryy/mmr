"""Print the pytest arguments for one CI shard.

Usage:
    python scripts/ci_test_shard.py SHARD_INDEX SHARD_COUNT
    python scripts/ci_test_shard.py --record JUNIT_XML   (refresh the timings file)

Work is spread over the shards greedily by recorded run time
(tests/.test_file_durations.json), so the shards finish at about the same
time. A file with no recorded time (a new file) is weighted by its size.

A file slower than SPLIT_SECONDS is split by test: its recorded tests are
spread like files, and one shard also runs the file with those tests
deselected, so a test added later still runs exactly once. Every test runs in
exactly one shard. The quarantined async IB file runs in its own job.
"""
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

EXCLUDED = {"tests/test_ibrx_async.py"}
DURATIONS_FILE = Path("tests/.test_file_durations.json")
SECONDS_PER_BYTE_FALLBACK = 0.0001
SPLIT_SECONDS = 60.0


def test_files(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in (root / "tests").rglob("test_*.py")
        if str(path.relative_to(root)) not in EXCLUDED
    )


def recorded(root: Path) -> dict:
    path = root / DURATIONS_FILE
    if not path.exists():
        return {"files": {}, "tests": {}}
    return json.loads(path.read_text())


def work_items(root: Path) -> list[tuple[str, float]]:
    """Return (item, seconds). An item is a file, a node id, or 'REST:<file>'."""
    timings = recorded(root)
    split_tests = defaultdict(list)
    for node_id, seconds in timings["tests"].items():
        split_tests[node_id.split("::", 1)[0]].append((node_id, seconds))
    items = []
    for name in test_files(root):
        if name in split_tests:
            items.extend(split_tests[name])
            items.append((f"REST:{name}", 0.0))
        else:
            seconds = timings["files"].get(name)
            if seconds is None:
                seconds = (root / name).stat().st_size * SECONDS_PER_BYTE_FALLBACK
            items.append((name, seconds))
    return items


def assign(root: Path, count: int) -> list[list[str]]:
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for item, seconds in sorted(work_items(root), key=lambda pair: (pair[1], pair[0]), reverse=True):
        lightest = loads.index(min(loads))
        shards[lightest].append(item)
        loads[lightest] += seconds
    return shards


def pytest_args(items: list[str], root: Path) -> list[str]:
    timings = recorded(root)
    args = []
    for item in sorted(items):
        if item.startswith("REST:"):
            name = item[len("REST:"):]
            args.append(name)
            # A recorded test that this shard also runs by node id must not be
            # deselected: --deselect would drop it from the explicit run too.
            args.extend(
                f"--deselect={node}"
                for node in sorted(timings["tests"])
                if node.startswith(f"{name}::") and node not in items
            )
        elif not any(rest == f"REST:{item.split('::', 1)[0]}" for rest in items):
            args.append(item)
    return args


def node_id(case: ET.Element, root: Path) -> tuple[str, str] | None:
    parts = [part for part in case.get("classname", "").split(".") if part]
    for end in range(len(parts), 0, -1):
        candidate = str(Path(*parts[:end]).with_suffix(".py"))
        if (root / candidate).exists():
            return candidate, "::".join([candidate, *parts[end:], case.get("name", "")])
    return None


def record(junit_xml: Path, root: Path) -> None:
    per_file: dict[str, float] = defaultdict(float)
    per_test: dict[str, float] = {}
    for case in ET.parse(junit_xml).getroot().iter("testcase"):
        found = node_id(case, root)
        if found is None:
            continue
        name, node = found
        seconds = float(case.get("time") or 0)
        per_file[name] += seconds
        per_test[node] = per_test.get(node, 0.0) + seconds
    slow_files = {name for name, seconds in per_file.items() if seconds > SPLIT_SECONDS}
    data = {
        "files": {name: round(seconds, 2) for name, seconds in sorted(per_file.items())},
        "tests": {node: round(seconds, 2) for node, seconds in sorted(per_test.items())
                  if node.split("::", 1)[0] in slow_files and "[" not in node},
    }
    (root / DURATIONS_FILE).write_text(json.dumps(data, indent=1) + "\n")
    print(f"recorded {len(data['files'])} files ({sum(per_file.values()):.0f} s), split {sorted(slow_files)}")


def main() -> None:
    root = Path.cwd()
    if sys.argv[1] == "--record":
        record(Path(sys.argv[2]), root)
        return
    index, count = int(sys.argv[1]), int(sys.argv[2])
    if not 0 <= index < count:
        raise SystemExit(f"shard index {index} is outside 0..{count - 1}")
    print("\n".join(pytest_args(assign(root, count)[index], root)))


if __name__ == "__main__":
    main()
