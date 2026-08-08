# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

Use GitHub's private reporting form:
[Report a vulnerability](https://github.com/muhzuhaib/ci-triage/security/advisories/new). It opens a
thread visible only to you and the maintainer.

You can expect an acknowledgement within a week. If a report is valid, the fix and an advisory are
published together, and you are credited unless you would rather not be.

## Supported versions

This project is pre-1.0 and only the latest release is supported. Fixes land on `main` and go out in
the next release rather than being backported.

| Version | Supported |
|---|---|
| 0.5.x | yes |
| < 0.5 | no |

## What is in scope

The parts of this service that exist to hold a boundary:

- **Webhook signature verification.** The HMAC is computed over the raw request bytes, compared in
  constant time, and an empty secret raises rather than being treated as "no security". The legacy
  SHA-1 `X-Hub-Signature` header is rejected rather than accepted for compatibility. Anything that
  lets an unsigned or wrongly signed delivery be processed is a vulnerability.
- **The spend ceiling.** The guarantee is precise: reservations never exceed the configured ceiling.
  Anything that allows a reservation past it, including through a race between concurrent workers, is
  a vulnerability.
- **Exactly-once side effects.** Anything that causes the same failure to be commented on twice.

## What is not in scope

- **The provider call.** This package chooses no vendor and holds no API key. What you call, and how
  you store its credentials, is yours.
- **Anything the ceiling explicitly does not cover.** The ledger bounds what it *authorises*. A crash
  between the provider answering and the commit landing can cost up to one call beyond the ceiling,
  which is why a reclaimed hold is kept as an audit row rather than deleted. That is a documented
  limit, not a defect.
- **Deployment configuration.** The shipped `compose.yaml` is a demonstration stack with development
  credentials in it. It is not a hardened deployment and does not claim to be.
