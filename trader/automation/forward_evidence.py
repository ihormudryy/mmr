"""``get_deployment_forward_evidence`` (SP2c spec 5.2 item 7): a port that SP2c Plan 5 fills.

Plan 5 reads Plan 2's version, its sessions and its paper trips, and Plan 3's
shadow rows. Until then the default refuses every read.
"""
from __future__ import annotations

from typing import Protocol


class ForwardEvidenceRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ForwardEvidenceSource(Protocol):
    def read(self, deployment_version: str) -> dict: ...


class NoDeploymentVersions:
    def read(self, deployment_version: str) -> dict:
        raise ForwardEvidenceRefused("DEPLOYMENT_VERSION_UNKNOWN", "forward evidence arrives with SP2c Plan 5")
