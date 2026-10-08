"""A served SP1 stack with one ACTIVE judged deployment version (SP2c Plan 2 rulings 17 and 18).

The acceptance harness registers nothing: it trades under an operator-given
judged version. Tests seed that version straight into the stores; the
registration chain has its own tests.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from tests.automation.judged_deployment import install_seeded_judgments, seed_judged_deployment
from tests.sp1_fixtures import served_stack
from trader.acceptance.scenario import deployment_record


def judged_served_stack(tmp_path, loop_thread, monkeypatch, **options):
    """``served_stack`` plus ``seeded``, ``deployment_digest``, ``deployment_version`` and ``source_digest``."""
    from tests.sp1_acceptance.test_acceptance_run import settings as acceptance_settings
    seeded = install_seeded_judgments(monkeypatch)          # before the stack builds its judgment reader
    served = served_stack(tmp_path, loop_thread, monkeypatch, **options)
    record = deployment_record(acceptance_settings())
    today = served.now().astimezone(ZoneInfo("America/New_York")).date()
    served.seeded = seeded
    served.deployment_digest, served.deployment_version = seed_judged_deployment(
        served.stack.ai_paper, seeded, record, today=today)
    served.source_digest = record["strategy_digest"]
    return served
