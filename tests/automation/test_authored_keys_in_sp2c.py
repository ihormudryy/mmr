"""SP3 Plan 3 Task 3: authored keys inside SP2c's request, case, binding and deployment (rulings 1-2)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.automation.backtest_judge_fixtures import case_body, case_keys, make_case, request_body, write_signed_body
from trader.authored.keys import authored_key
from trader.automation.ai_deployments import AiDeployment, DeploymentRefused
from trader.automation.ai_judgment_port import strategy_key
from trader.automation.backtest_judge_config import BacktestJudgeConfigError, load_backtest_judge_config
from trader.automation.backtest_judgments import _binding
from trader.research.evaluation_case import CaseRefused, load_case_verify_keys, load_verified_case
from trader.research.strategy_key import (
    authored_digest_of_path, authored_strategy_path, is_authored_path, is_evaluation_key, split_evaluation_key,
)
from trader.research.strategy_paths import normalize_strategy_path

DIGEST = "sha256:" + "c" * 64
AUTHORED = authored_key(DIGEST, "RangeBreak")
CATALOGUE = "strategies/opening_range_breakout.py:OpeningRangeBreakout"


def test_an_authored_key_splits_into_its_path_and_class_and_joins_back():
    path, class_name = split_evaluation_key(AUTHORED)
    assert (path, class_name) == ("authored:" + DIGEST, "RangeBreak")
    assert strategy_key(path, class_name) == AUTHORED
    assert is_authored_path(path) and authored_digest_of_path(path) == DIGEST
    assert split_evaluation_key(CATALOGUE) == ("strategies/opening_range_breakout.py", "OpeningRangeBreakout")
    assert is_evaluation_key(AUTHORED) and is_evaluation_key(CATALOGUE)


@pytest.mark.parametrize("value", ["authored:sha256:abc:X", "authored:" + DIGEST, "strategies/x.py", 7, None])
def test_anything_else_is_not_an_evaluation_key(value):
    assert not is_evaluation_key(value)
    if isinstance(value, str):
        with pytest.raises(ValueError):
            split_evaluation_key(value)


def test_the_allowlist_stays_catalogue_only():                                         # ruling 1
    with pytest.raises(BacktestJudgeConfigError):
        load_backtest_judge_config({"strategy_allowlist": [AUTHORED]})


def test_the_request_body_and_the_case_accept_an_authored_key():
    body = request_body(strategy_key=AUTHORED)
    assert body.strategy_key == AUTHORED
    assert make_case(body, strategy_file_hash=DIGEST).strategy_key == AUTHORED


def test_an_authored_case_must_hash_the_keys_own_file():                               # ruling 2
    with pytest.raises(ValidationError, match="strategy_file_hash must equal"):
        make_case(request_body(strategy_key=AUTHORED), strategy_file_hash="sha256:" + "d" * 64)


def test_a_signed_authored_case_with_other_bytes_is_malformed_on_load(tmp_path):   # ruling 2
    keys = case_keys(tmp_path)
    digest = write_signed_body(keys, case_body(request_body(strategy_key=AUTHORED),
                                               strategy_file_hash="sha256:" + "d" * 64))
    with pytest.raises(CaseRefused) as refused:
        load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))
    assert refused.value.code == "CASE_MALFORMED"


def test_the_judgment_binding_names_the_authored_path():
    case = make_case(request_body(strategy_key=AUTHORED), strategy_file_hash=DIGEST)
    binding = _binding(case, "sha256:" + "e" * 64)
    assert (binding["strategy_path"], binding["class_name"], binding["strategy_file_hash"]) == (
        "authored:" + DIGEST, "RangeBreak", DIGEST)


def deployment(**changes) -> AiDeployment:
    raw = {"strategy_path": authored_strategy_path(DIGEST), "strategy_digest": DIGEST, "class_name": "RangeBreak",
           "params": {"RANGE_BARS": 15}, "conids": [265598, 272093], "bar_size": "5 mins",
           "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
           "evidence_ref": "sha256:" + "1" * 64, "evidence_order_notional": 2000.0, **changes}
    return AiDeployment.from_json(raw)


def test_a_deployment_may_name_an_authored_path():
    assert deployment().strategy_path == "authored:" + DIGEST
    assert normalize_strategy_path(deployment().strategy_path) == "authored:" + DIGEST


@pytest.mark.parametrize("path", ["authored:sha256:abc", "authored/" + "c" * 64 + ".py",
                                  "authored:" + DIGEST + "/../x"])
def test_a_malformed_authored_path_is_invalid(path):
    with pytest.raises(DeploymentRefused) as refused:
        deployment(strategy_path=path)
    assert refused.value.code == "DEPLOYMENT_INVALID"
