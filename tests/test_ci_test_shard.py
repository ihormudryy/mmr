import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("ci_test_shard", ROOT / "scripts" / "ci_test_shard.py")
ci_test_shard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_test_shard)


def test_every_test_file_runs_in_exactly_one_shard():
    files = ci_test_shard.test_files(ROOT)
    shards = ci_test_shard.assign(files, ROOT, 4)
    flat = [path for shard in shards for path in shard]
    assert sorted(flat) == sorted(files)
    assert len(flat) == len(set(flat))


def test_quarantined_async_file_is_left_to_its_own_job():
    assert Path("tests/test_ibrx_async.py") not in ci_test_shard.test_files(ROOT)
