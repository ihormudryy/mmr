import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ai_review", Path(__file__).parents[1] / "scripts" / "ai_review.py")
ai_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_review)


def test_unknown_provider_fails_loudly():
    with pytest.raises(ai_review.ReviewError, match="unknown REVIEW_PROVIDER"):
        ai_review.complete("openai-direct", "m", "s", "p")


def test_parse_review_keeps_only_valid_findings():
    text = 'Sure:\n{"verdict": "not yet", "summary": "x", "findings": [' \
           '{"severity": "blocker", "body": "race"}, {"severity": "nit", "body": "drop"}, {"severity": "minor"}]}'
    verdict, summary, findings = ai_review.parse_review(text)
    assert verdict == "not yet"
    assert [f["body"] for f in findings] == ["race"]


def test_parse_review_rejects_bad_verdict():
    with pytest.raises(ai_review.ReviewError, match="bad verdict"):
        ai_review.parse_review('{"verdict": "maybe"}')


def test_review_rules_come_from_agents_md(monkeypatch):
    monkeypatch.chdir(Path(__file__).parents[1])
    assert "stopping rule" in ai_review.review_rules()


def test_body_says_when_diff_was_cut():
    body = ai_review.review_body("ready", "ok", [], "openrouter:m", truncated=True)
    assert "only the first part was reviewed" in body
