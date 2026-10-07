import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("ci_test_shard", ROOT / "scripts" / "ci_test_shard.py")
ci_test_shard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_test_shard)


def test_every_work_item_runs_in_exactly_one_shard():
    items = [item for item, _ in ci_test_shard.work_items(ROOT)]
    flat = [item for shard in ci_test_shard.assign(ROOT, 8) for item in shard]
    assert sorted(flat) == sorted(items)
    assert len(flat) == len(set(flat))


def test_every_test_file_is_covered():
    items = {item for item, _ in ci_test_shard.work_items(ROOT)}
    for name in ci_test_shard.test_files(ROOT):
        assert name in items or f"REST:{name}" in items


def test_quarantined_async_file_is_left_to_its_own_job():
    assert "tests/test_ibrx_async.py" not in ci_test_shard.test_files(ROOT)


def test_a_split_file_runs_its_new_tests_through_the_rest_item(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text("def test_a(): pass\ndef test_new(): pass\n")
    (tmp_path / "tests" / ".test_file_durations.json").write_text(json.dumps(
        {"files": {"tests/test_slow.py": 100.0}, "tests": {"tests/test_slow.py::test_a": 100.0}}))
    args = ci_test_shard.pytest_args(["REST:tests/test_slow.py"], tmp_path)
    assert args == ["tests/test_slow.py", "--deselect=tests/test_slow.py::test_a"]


def test_a_file_without_recorded_time_falls_back_to_its_size(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text("x" * 1000)
    assert ci_test_shard.work_items(tmp_path) == [("tests/test_new.py", 1000 * ci_test_shard.SECONDS_PER_BYTE_FALLBACK)]


def test_a_shard_with_the_rest_and_a_named_test_of_the_same_file_keeps_that_test(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text("def test_a(): pass\ndef test_b(): pass\n")
    (tmp_path / "tests" / ".test_file_durations.json").write_text(json.dumps(
        {"files": {"tests/test_slow.py": 100.0},
         "tests": {"tests/test_slow.py::test_a": 60.0, "tests/test_slow.py::test_b": 40.0}}))
    args = ci_test_shard.pytest_args(["REST:tests/test_slow.py", "tests/test_slow.py::test_a"], tmp_path)
    assert args == ["tests/test_slow.py", "--deselect=tests/test_slow.py::test_b"]
