"""Signature verification: the gate that proves a body came from GitHub."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from ci_triage.signature import (
    InvalidSignature,
    MissingSignature,
    compute_signature,
    verify_signature,
)

SECRET = "it's a secret to everybody"
BODY = b'{"action":"completed","workflow_run":{"id":42}}'


def _sign(secret: str, body: bytes) -> str:
    """Sign the way GitHub documents it, independently of the module under test."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def test_a_correct_signature_verifies():
    verify_signature(SECRET, BODY, _sign(SECRET, BODY))  # returns None, does not raise


def test_compute_signature_matches_an_independent_hmac():
    assert compute_signature(SECRET, BODY) == _sign(SECRET, BODY)


def test_a_tampered_body_is_rejected():
    header = _sign(SECRET, BODY)
    with pytest.raises(InvalidSignature):
        verify_signature(SECRET, BODY + b" ", header)


def test_the_wrong_secret_is_rejected():
    with pytest.raises(InvalidSignature):
        verify_signature("wrong secret", BODY, _sign(SECRET, BODY))


def test_a_missing_header_is_missing_not_invalid():
    with pytest.raises(MissingSignature):
        verify_signature(SECRET, BODY, None)


def test_an_empty_header_is_missing():
    with pytest.raises(MissingSignature):
        verify_signature(SECRET, BODY, "")


def test_a_legacy_sha1_header_is_refused():
    # GitHub still sends the SHA-1 ``X-Hub-Signature`` for compatibility; SHA-1
    # is broken here and accepting it would be a downgrade.
    sha1 = "sha1=" + hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()
    with pytest.raises(MissingSignature):
        verify_signature(SECRET, BODY, sha1)


def test_an_empty_secret_fails_closed():
    # A configuration error, not a request error: refuse rather than verify
    # against a key anyone can guess.
    with pytest.raises(ValueError):
        verify_signature("", BODY, _sign("", BODY))


def test_bytes_and_str_secrets_agree():
    assert compute_signature(SECRET, BODY) == compute_signature(SECRET.encode(), BODY)
    verify_signature(SECRET.encode(), BODY, _sign(SECRET, BODY))


def test_verification_uses_a_constant_time_compare(monkeypatch):
    # We cannot measure timing reliably in a unit test, so instead assert the
    # mechanism: the comparison goes through hmac.compare_digest. If a refactor
    # replaced it with ``==``, this catches it.
    calls: list[tuple] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("ci_triage.signature.hmac.compare_digest", spy)
    verify_signature(SECRET, BODY, _sign(SECRET, BODY))
    assert calls, "verify_signature must compare through hmac.compare_digest"
