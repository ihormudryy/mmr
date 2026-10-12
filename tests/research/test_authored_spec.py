"""SP3 Plan 4 Task 4: the evaluation spec, cohort, family and run environment of an authored key."""
from dataclasses import replace

import pytest

from tests.authored.fake_sandbox import FakeSandbox
from tests.research.authored_fixtures import CATALOGUE_KEY, research_world, time_of_day_key
from trader.authored.keys import parse_authored_key
from trader.research.cohort import RequestRefused
from trader.research.evaluation import AUTHORED_COMMIT, _commit_of, _family, run_environment
from trader.research.evaluation_spec import EvaluationSpecError, build_evaluation_spec
from trader.research.strategy_key import authored_strategy_path
from trader.sandbox.protocol import RESEARCH_SOCKET

KEY = time_of_day_key(FakeSandbox())
DIGEST = parse_authored_key(KEY)[0]


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    return research_world(tmp_path, tmp_duckdb_path)


def test_an_authored_key_needs_no_allowlist_no_file_and_no_import(world):
    spec = world.build(KEY)
    assert spec.base.authored_digest == DIGEST
    assert spec.base.strategy_path == authored_strategy_path(DIGEST)
    assert spec.file_hash == DIGEST


def test_a_point_naming_another_tunable_is_refused(world):
    with pytest.raises(RequestRefused) as refused:
        world.build(KEY, cohort=({"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615, "OTHER": 1}))
    assert refused.value.code == "COHORT_POINT_INVALID"


def test_a_point_leaving_out_a_tunable_of_point_0_is_refused(world):
    # The sandbox gets exactly the OPEN params (no class defaults), so every point names point 0's tunables.
    with pytest.raises(RequestRefused) as refused:
        world.build(KEY, cohort=({"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}, {"ENTRY_MINUTE": 615}))
    assert refused.value.code == "COHORT_POINT_INVALID"
    assert "EXIT_MINUTE" in refused.value.detail


def test_the_family_names_the_digest_and_no_commit(world):
    base = world.build(KEY).base
    assert _commit_of(base, world.root) == AUTHORED_COMMIT
    assert _family(base, "m", AUTHORED_COMMIT, world.paths).source_tree_digest == DIGEST.removeprefix("sha256:")


def test_the_run_environment_names_the_research_socket(world):
    base = world.build(KEY).base
    env = run_environment(base, world.paths)
    assert (env.authored_digest, env.sandbox_socket) == (DIGEST, RESEARCH_SOCKET)
    assert env.strategy_file == authored_strategy_path(DIGEST)
    moved = run_environment(base, replace(world.paths, sandbox_socket="/tmp/s/research.sock"))
    assert moved.sandbox_socket == "/tmp/s/research.sock"


def test_a_catalogue_environment_names_no_sandbox(world):
    env = run_environment(world.build(CATALOGUE_KEY).base, world.paths)
    assert (env.authored_digest, env.sandbox_socket) == (None, None)


def test_a_spec_whose_path_is_not_the_digest_is_refused(world):
    raw = {"name": "x", "strategy": "authored:sha256:" + "0" * 64, "class": "TimeOfDayPort",
           "params": {"ENTRY_MINUTE": 600}}
    with pytest.raises(EvaluationSpecError, match="not the authored file"):
        build_evaluation_spec(raw, universe_accessor=None, costs_config=None, repo_root=world.root,
                              authored_digest=DIGEST)
