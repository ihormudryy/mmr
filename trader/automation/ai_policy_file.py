"""The operator's AI risk policy file for ``mmr ai-policy publish`` (SP2 spec 6.7).

Format: one mapping with exactly the key ``limits``, whose value has exactly the
``RiskLimits`` fields. Loaded with ``yaml.safe_load``; checked here so a typo
fails before anything is sent. The trader checks it again.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from trader.automation.risk_limits import RiskLimits, RiskLimitsError


class PolicyFileError(ValueError):
    pass


def load_policy_file(path: str | Path) -> dict:
    try:
        document = yaml.safe_load(Path(path).expanduser().read_text())
    except (OSError, yaml.YAMLError) as ex:
        raise PolicyFileError(f"cannot read policy file {path}: {ex}") from None
    if not isinstance(document, dict) or set(document) != {"limits"}:
        raise PolicyFileError("policy file must be a mapping with exactly the key 'limits'")
    try:
        return RiskLimits.from_json(document["limits"]).to_json()
    except RiskLimitsError as ex:
        raise PolicyFileError(str(ex)) from None
