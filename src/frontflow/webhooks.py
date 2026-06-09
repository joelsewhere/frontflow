"""HMAC signing + verification for outbound webhooks.

frontflow's lifecycle hooks (`on_submitted`, `on_failed`, `on_revoked`,
`on_assigned`) are Python callables. Authors who want to forward those
events over HTTP to an external system need a way to sign the payload
so the receiver can verify it actually came from the form. This module
provides that.

The shape mirrors Stripe / GitHub / Vercel:

  signed event = HMAC-SHA256(secret, f"{timestamp}.{canonical_body}")
  header value = "t={timestamp},v1={signed_event_hex}"

The timestamp is part of the signed input (replay defense) and is
embedded in the header (so the receiver doesn't need a separate
date header). Versioning the algorithm with `v1=` lets us add `v2`
later without breaking deployed verifiers.

Typical author usage::

    import json
    import os
    import frontflow.webhooks as webhooks

    SECRET = os.environ["RELEASE_HOOK_SECRET"]

    def on_submitted(event):
        body = json.dumps(event, sort_keys=True, separators=(",", ":"))
        sig = webhooks.sign(body, secret=SECRET)
        requests.post(
            "https://example.com/hooks/release",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Frontflow-Signature": sig,
            },
        )

Receiver verification::

    body = await request.body()
    sig_header = request.headers["X-Frontflow-Signature"]
    if not webhooks.verify(body, sig_header, secret=SECRET):
        return 401
    event = json.loads(body)
    ...

Notes:

  - `sign`/`verify` operate on raw bytes (or a UTF-8 str) — the
    canonical encoding (e.g. JSON with sorted keys) is the caller's
    decision; the signed message is whatever bytes go on the wire.
  - `verify` uses constant-time comparison via `hmac.compare_digest`.
  - `verify` rejects timestamps outside `tolerance_seconds` (default
    300 / five minutes) — a stale signature could be replayed by a
    network attacker who saw the original, even though the signature
    itself is valid forever.
  - `verify` does NOT raise on a bad signature — returns False. It
    raises only on a malformed header (programmer error / sender
    misconfiguration). This separation lets the receiver log
    "verification failed" vs "we got garbage" distinctly.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional, Union


__all__ = ["sign", "verify", "MalformedSignatureHeader"]


# Default replay window for verify(). Wide enough that NTP skew and
# slow networks don't trip; narrow enough that a leaked signature
# isn't reusable a day later. Five minutes matches the major SaaS
# webhook implementations.
DEFAULT_TOLERANCE_SECONDS = 300


class MalformedSignatureHeader(ValueError):
    """Raised by `verify` when the signature header is structurally
    broken — missing required parts, non-integer timestamp, unknown
    version. Use to distinguish "the sender is misconfigured" from
    "the message is forged"; the latter just returns False."""


def _as_bytes(x: Union[bytes, str]) -> bytes:
    """Normalize the signed body to bytes. Strings are encoded as
    UTF-8 (the only sane default for JSON / form payloads). Callers
    that need a different encoding should pre-encode."""
    if isinstance(x, bytes):
        return x
    return x.encode("utf-8")


def sign(
    payload: Union[bytes, str],
    *,
    secret: str,
    timestamp: Optional[int] = None,
) -> str:
    """Produce a webhook signature header for `payload`.

    Returns a string of the form `t={ts},v1={hexdigest}`. `payload`
    is the exact bytes that will go on the wire — sign the SAME
    bytes the receiver will read, including any whitespace; canonical
    JSON is the caller's concern.

    `timestamp` defaults to the current wall clock; pass an explicit
    value only for tests or for re-signing pre-stamped events.
    """
    if not secret:
        raise ValueError("sign(secret=...) must be a non-empty string")
    if timestamp is None:
        timestamp = int(time.time())
    body = _as_bytes(payload)
    signed_input = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(
        secret.encode("utf-8"), signed_input, hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(
    payload: Union[bytes, str],
    signature_header: str,
    *,
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Return True iff `signature_header` is a valid signature of
    `payload` under `secret`, and its timestamp is within
    `tolerance_seconds` of the wall clock.

    Returns False (not raises) on signature mismatch or stale
    timestamp — those are forgery / replay events the caller will
    want to count + log. Raises `MalformedSignatureHeader` on a
    structurally-broken header (the sender is misconfigured rather
    than malicious — handle this case separately).

    Constant-time comparison via `hmac.compare_digest`.
    """
    if not secret:
        raise ValueError("verify(secret=...) must be a non-empty string")
    parts = _parse_signature_header(signature_header)
    ts = parts["t"]
    received = parts["v1"]

    # Replay window.
    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        return False

    body = _as_bytes(payload)
    signed_input = f"{ts}.".encode("utf-8") + body
    expected = hmac.new(
        secret.encode("utf-8"), signed_input, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received)


def _parse_signature_header(header: str) -> dict:
    """Parse `t=...,v1=...` into a dict. Tolerates extra unknown
    keys (future-proofing) but requires `t` and `v1` to be present
    and `t` to be a parseable integer. Raises
    `MalformedSignatureHeader` on any structural problem.
    """
    if not isinstance(header, str) or not header.strip():
        raise MalformedSignatureHeader(
            "signature header is empty or not a string"
        )
    parts = {}
    for piece in header.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise MalformedSignatureHeader(
                f"signature header piece {piece!r} has no '='"
            )
        k, v = piece.split("=", 1)
        parts[k.strip()] = v.strip()
    if "t" not in parts:
        raise MalformedSignatureHeader(
            "signature header missing 't=' (timestamp)"
        )
    if "v1" not in parts:
        raise MalformedSignatureHeader(
            "signature header missing 'v1=' (signature)"
        )
    try:
        ts = int(parts["t"])
    except ValueError as e:
        raise MalformedSignatureHeader(
            f"signature header timestamp {parts['t']!r} is not an integer"
        ) from e
    parts["t"] = ts
    return parts
