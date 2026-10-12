"""SP3 D4: the trader validates its get_active_ai_deployments reply with this model, so an authored row
must pass it, and nothing else new may."""
import pytest
from pydantic import ValidationError

from trader.messaging.ai_deployment_wire import ActiveAiDeployment, GetActiveAiDeploymentsResponse
from trader.research.strategy_key import authored_strategy_path

DIGEST = "sha256:" + "c" * 64


def row(path, digest=DIGEST):
    return {"version_digest": "sha256:" + "d" * 64, "base_digest": "sha256:" + "b" * 64, "strategy_path": path,
            "strategy_digest": digest, "class_name": "OpeningRangeBreakout", "params": {}, "conids": [265598],
            "bar_size": "1 min", "expiry_session": "2026-11-06"}


def test_the_reply_carries_a_catalogue_and_an_authored_row():
    reply = GetActiveAiDeploymentsResponse.model_validate(
        {"account_mode": "paper",
         "deployments": [row("strategies/opening_range_breakout.py"), row(authored_strategy_path(DIGEST))]})
    assert [d.strategy_path for d in reply.deployments] == [
        "strategies/opening_range_breakout.py", authored_strategy_path(DIGEST)]


@pytest.mark.parametrize("path", ["authored:sha256:abc", "authored/" + "c" * 64 + ".py", "strategies/../x.py",
                                  "/etc/x.py", "authored:sha256:" + "C" * 64])
def test_other_paths_stay_refused(path):
    with pytest.raises(ValidationError):
        ActiveAiDeployment.model_validate(row(path))
