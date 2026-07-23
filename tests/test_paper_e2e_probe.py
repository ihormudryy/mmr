import pytest
from tests.paper_e2e._probe import (
    capability_from_remote_error,
    derive_html_form_csrf,
)


def test_method_not_allowed_means_absent():
    assert capability_from_remote_error("METHOD_NOT_ALLOWED") == "absent"


@pytest.mark.parametrize("code", [
    "VALIDATION_ERROR", "PROPOSAL_NOT_FOUND", "COMMAND_NOT_FOUND", "INTERNAL_ERROR",
])
def test_handler_errors_mean_present(code):
    assert capability_from_remote_error(code) == "present"


def test_form_csrf_matches_dashboard_derivation():
    secret = "x" * 32
    import hashlib, hmac
    expected = hmac.new(secret.encode(), b"mmr-dashboard-html-form-csrf-v1", hashlib.sha256).hexdigest()
    assert derive_html_form_csrf(secret) == expected


def test_form_csrf_rejects_short_secret():
    with pytest.raises(ValueError):
        derive_html_form_csrf("short")
