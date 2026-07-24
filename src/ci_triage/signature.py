"""Verify that a webhook body really came from GitHub.

GitHub signs every delivery with an HMAC-SHA256 of the *raw request body*, keyed
by the webhook's shared secret, and sends it in the ``X-Hub-Signature-256``
header as ``sha256=<hex digest>`` (confirmed against GitHub's "Validating
webhook deliveries" documentation). Verification recomputes that HMAC and
compares.

Three details here are load-bearing, and each is a real vulnerability if got
wrong rather than a style preference:

* **The signature is over the raw bytes.** Parse-then-reserialise changes the
  bytes -- key order, whitespace, unicode escaping -- and the recomputed HMAC no
  longer matches. So verification must run on the body exactly as received,
  *before* anything parses it. This is also why the receiver authenticates
  before it calls ``json.loads``: never hand attacker-controlled bytes to a
  parser you have not yet authenticated.
* **The comparison must be constant-time.** A byte-by-byte ``==`` returns as
  soon as it finds a difference, so the time it takes leaks how many leading
  bytes were correct -- enough to forge a signature one byte at a time. This is
  the textbook timing-oracle attack, and ``hmac.compare_digest`` exists
  precisely to close it.
* **An empty secret fails closed.** An unconfigured secret is not "no security",
  it is *forgeable* security: anyone can compute an HMAC keyed by the empty
  string. Refusing to verify at all is safer than verifying against nothing.
"""

from __future__ import annotations

import hashlib
import hmac

PREFIX = "sha256="


class SignatureError(Exception):
    """Base class for signature-verification failures."""


class MissingSignature(SignatureError):
    """No usable ``X-Hub-Signature-256`` header was present.

    Treated as distinct from an *invalid* signature because the two want
    different handling: a missing header is usually a misrouted request or a
    misconfigured hook, an invalid one is a body that failed authentication.
    """


class InvalidSignature(SignatureError):
    """The header was present but did not match the body."""


def compute_signature(secret: str | bytes, body: bytes) -> str:
    """Return the ``sha256=...`` header value GitHub would send for ``body``.

    Exposed so tests and any local delivery-signing helper produce the header
    the same way the verifier expects, rather than reimplementing it.
    """
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not key:
        raise ValueError("webhook secret is empty; refusing to sign with no key")
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    return PREFIX + digest


def verify_signature(secret: str | bytes, body: bytes, signature_header: str | None) -> None:
    """Check that ``body`` was signed with ``secret``; raise if not.

    Returns ``None`` on success and raises a :class:`SignatureError` subclass on
    failure, so a caller cannot accidentally treat a falsy return as "verified".

    :raises ValueError: the secret is empty (a configuration error, not a
        request error -- see the module docstring on failing closed).
    :raises MissingSignature: no header, or one without the ``sha256=`` prefix.
    :raises InvalidSignature: the header did not match the recomputed HMAC.
    """
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not key:
        raise ValueError("webhook secret is empty; refusing to verify against no key")

    if not signature_header:
        raise MissingSignature("no X-Hub-Signature-256 header on the request")

    # Only SHA-256 is accepted. GitHub still sends the legacy SHA-1
    # ``X-Hub-Signature`` for backwards compatibility, but SHA-1 is broken for
    # this purpose and accepting it would let a caller downgrade the check.
    if not signature_header.startswith(PREFIX):
        raise MissingSignature(
            f"signature header is not SHA-256 (expected a {PREFIX!r} prefix)"
        )

    expected = compute_signature(key, body)
    if not hmac.compare_digest(expected, signature_header):
        raise InvalidSignature("signature does not match the request body")
