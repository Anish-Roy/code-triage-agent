from __future__ import annotations

from pathlib import Path

import pytest

from code_triage_agent.collector.redact import Redactor

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CANARIES = [
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "hunter2-super-secret",
    "json-secret-value",
    "s3cr3t-pass",
    "abcdef0123456789abcdef0123456789",
    "MIIEowIBAAKCAQEA0exampleexampleexampleexampleexample",
]


def test_malicious_log_has_no_canaries_after_redaction() -> None:
    text = (FIXTURES / "logs" / "malicious_job.log").read_text(encoding="utf-8")

    result = Redactor().redact(text)

    for canary in CANARIES:
        assert canary not in result.text
    assert "[REDACTED:github_token]" in result.text
    assert "[REDACTED:private_key]" in result.text
    assert "[REDACTED:url_credentials]" in result.text
    assert result.total > 0


def test_preserves_non_secret_context() -> None:
    text = (
        "DB_PASSWORD=hunter2-super-secret\n"
        "Authorization: Bearer abcdef0123456789abcdef0123456789\n"
        "AssertionError: expected 5 items but got 4\n"
    )

    result = Redactor().redact(text)

    assert "DB_PASSWORD=" in result.text
    assert "Authorization: Bearer " in result.text
    assert "AssertionError: expected 5 items but got 4" in result.text
    assert "hunter2" not in result.text


def test_url_credentials_keep_scheme_and_host() -> None:
    result = Redactor().redact("clone https://ci-bot:s3cr3t-pass@example.com/acme/widgets")

    assert "https://ci-bot:[REDACTED:url_credentials]@example.com/acme/widgets" in result.text


def test_custom_pattern() -> None:
    redactor = Redactor(extra_patterns=[r"acme-secret-[0-9]{4}"])

    result = redactor.redact("value acme-secret-1234 here")

    assert result.text == "value [REDACTED:custom_0] here"
    assert result.counts == {"custom_0": 1}


def test_invalid_custom_pattern_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid extra redaction pattern"):
        Redactor(extra_patterns=["("])


def test_counts_by_pattern() -> None:
    result = Redactor().redact(
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
    )

    assert result.counts.get("github_token") == 2
