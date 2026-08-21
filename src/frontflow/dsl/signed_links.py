"""Signed-link infrastructure — Phase 5.

A signed link grants access to one specific submission as one
specific user, without requiring a frontflow login. Used for:

  - Notification delivery: the URL embedded in a Slack/email
    notification from the on_assigned hook.
  - Authenticated iframe embedding (Phase 6).

Token shape (JWT-style envelope, HS256-signed):

    {
      "user_id":            <int>,
      "submission_handle":  <str>,
      "scope":              "fill" | "read",
      "exp":                <unix-seconds>,
      "iat":                <unix-seconds>,
      "issuer":             "assign_operator" | "admin" | "embed"
    }

Signed with the install's FRONTFLOW_SECRET_KEY (HS256). Opaque
to recipients.

Verification (`verify(token, submission_handle)`):

  1. Decode + verify signature. Invalid → return None (caller
     responds 404 to avoid leaking validity).
  2. Verify `exp > now`. Expired → return None.
  3. Verify `submission_handle` matches. Mismatch → return None.
  4. Verify the user still has an active assignment on the
     submission. Revoked → return None.
  5. Return the verified `(user_id, scope, issuer)` tuple.

Failures collapse to `None` rather than distinct errors — leaking
"this token is expired" vs "this token has a bad signature" gives
attackers a probing oracle.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional


# Token lifetime cap — refuse to mint tokens longer than this even
# if a caller passes a larger TTL. 90 days is the practical ceiling;
# anything longer is a real session, not a link.
_MAX_TTL_SECONDS = 90 * 24 * 3600

# Valid `scope` values.
_VALID_SCOPES = frozenset({"fill", "read"})

# Valid `issuer` tags.
_VALID_ISSUERS = frozenset({
    "assign_operator", "admin", "embed", "share",
})


def _secret() -> bytes:
    """Resolve the signing key. The same secret used to encrypt
    auth cookies — rotating it invalidates every outstanding
    signed link, which is the intended behavior (the design doc
    accepts invalidation on key rotation as a locked decision)."""
    key = os.environ.get("FRONTFLOW_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "FRONTFLOW_SECRET_KEY is not set; signed links cannot "
            "be minted or verified"
        )
    return key.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # Pad to a multiple of 4.
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def mint(
    *,
    user_id: Optional[int],
    submission_handle: str,
    scope: str = "fill",
    issuer: str = "assign_operator",
    ttl_seconds: int = 7 * 24 * 3600,
) -> str:
    """Mint a signed token. Returns the encoded string.

    Args:
      user_id: the frontflow User.id the token authenticates as, or
        None for an anonymous `share` token (see `mint_for_share`) —
        a link that grants read access to one submission to whoever
        holds it, without authenticating as anybody.
      submission_handle: the submission the token grants access to.
      scope: "fill" (write + read) or "read" (read-only). Defaults
        to "fill" since most callers want the assignee to actually
        do something.
      issuer: where the token came from — drives audit and lets
        verification refuse tokens with a wrong issuer for the
        consuming endpoint.
      ttl_seconds: lifetime in seconds. Capped at _MAX_TTL_SECONDS;
        a longer value is silently reduced to the cap.

    Raises:
      ValueError if scope/issuer aren't recognized.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(_VALID_SCOPES)!r}; "
            f"got {scope!r}"
        )
    if issuer not in _VALID_ISSUERS:
        raise ValueError(
            f"issuer must be one of {sorted(_VALID_ISSUERS)!r}; "
            f"got {issuer!r}"
        )
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive int")
    ttl_seconds = min(ttl_seconds, _MAX_TTL_SECONDS)
    # Anonymity is confined to `share` tokens, and a share token is
    # read-only. Pairing any other issuer with a null user — or a
    # share token with write scope — would turn a link into an
    # unauthenticated writer.
    if issuer == "share":
        if user_id is not None:
            raise ValueError("share tokens are anonymous; pass user_id=None")
        if scope != "read":
            raise ValueError("share tokens must be read-only")
    elif user_id is None:
        raise ValueError(f"issuer {issuer!r} requires a user_id")

    now = int(time.time())
    payload = {
        "user_id": None if user_id is None else int(user_id),
        "submission_handle": str(submission_handle),
        "scope": scope,
        "issuer": issuer,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    payload_b = json.dumps(
        payload, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    payload_b64 = _b64url_encode(payload_b)
    sig = hmac.new(
        _secret(), payload_b64.encode("ascii"), hashlib.sha256,
    ).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def verify(
    token: str,
    *,
    submission_handle: str,
    require_issuer: Optional[str] = None,
) -> Optional[dict]:
    """Verify a token. Returns the decoded payload dict on success,
    None on any failure. Callers that distinguish failure modes
    must do so by checking for None; the function deliberately
    collapses every error path to keep the response unambiguous
    from the outside.

    `submission_handle` must match the token's claim — pass the
    handle from the URL path. If `require_issuer` is set, the
    token's issuer must equal it (useful for endpoints that only
    accept embed-scoped tokens, etc.).

    On success the caller must additionally check that the user
    still has an active assignment on the submission. This
    function checks the token's integrity + expiry + binding;
    the role-state check lives at the call site since it shares
    machinery with the runtime auth check.
    """
    if not isinstance(token, str) or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts

    # Verify signature first — fail-closed on tampering.
    try:
        expected = hmac.new(
            _secret(), payload_b64.encode("ascii"), hashlib.sha256,
        ).digest()
        actual = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(expected, actual):
        return None

    # Decode the payload.
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    # Shape check.
    required_keys = {
        "user_id", "submission_handle", "scope", "issuer", "iat", "exp",
    }
    if not isinstance(payload, dict) or not required_keys <= payload.keys():
        return None
    if payload["scope"] not in _VALID_SCOPES:
        return None
    if payload["issuer"] not in _VALID_ISSUERS:
        return None
    # Same pairing rules as mint(), re-checked on the way in: a null
    # user is only ever a read-only `share` link, and no other issuer
    # may be anonymous.
    if payload["issuer"] == "share":
        if payload["user_id"] is not None or payload["scope"] != "read":
            return None
    elif not isinstance(payload["user_id"], int):
        return None

    # Binding + expiry.
    # Embed-scope tokens carry submission_handle="*" — they
    # authenticate a user across the install (for /my-tasks
    # embedding), not a specific submission. Skip the binding
    # check in that case; every other token must match the
    # caller-supplied handle exactly.
    if payload["submission_handle"] != "*":
        if payload["submission_handle"] != submission_handle:
            return None
    now = int(time.time())
    if not isinstance(payload["exp"], int) or payload["exp"] <= now:
        return None
    if require_issuer is not None and payload["issuer"] != require_issuer:
        return None

    return payload


def mint_for_share(
    *,
    submission_handle: str,
    ttl_seconds: int = 7 * 24 * 3600,
) -> str:
    """Mint an ANONYMOUS read-only link to one submission.

    Unlike `mint`'s assignee tokens, this authenticates as nobody: the
    token itself is the credential, so whoever holds the URL may read
    that one submission and nothing else. It is the answer to "share
    this result with someone who has no frontflow login" — safer than
    making a form's submissions public, because possession is required
    and the grant expires.

    Bound to a single submission handle and always read-only; the
    minting endpoint is admin/manage-gated.
    """
    return mint(
        user_id=None,
        submission_handle=submission_handle,
        scope="read",
        issuer="share",
        ttl_seconds=ttl_seconds,
    )


def mint_for_embed(
    *,
    user_id: int,
    ttl_seconds: int = 7 * 24 * 3600,
) -> str:
    """Mint a token for the install-wide /my-tasks embed.

    Embed tokens are NOT bound to a specific submission — they
    authenticate a user across their whole inbox. The
    `submission_handle` field is set to the wildcard "*" so the
    verify-time binding check skips for embed tokens; per-route
    enforcement decides what the token's bearer can reach.

    `issuer` is fixed to "embed"; consuming endpoints should
    require this via `verify(..., require_issuer="embed")` to
    prevent confused-deputy use of assign_operator tokens on
    embed routes (and vice versa).
    """
    return mint(
        user_id=user_id,
        submission_handle="*",
        scope="read",
        issuer="embed",
        ttl_seconds=ttl_seconds,
    )


def build_link(
    *,
    base_url: str,
    form_id: str,
    submission_handle: str,
    token: str,
) -> str:
    """Construct the user-facing URL carrying a signed token.

    Layout matches the existing form-render route:
        {base_url}/forms/{form_id}/form/submission/{handle}?token=<...>

    Callers (notification handlers, the assign_demo, future iframe
    embeds) build links via this helper so the structure stays
    consistent and trailing-slash-safe.
    """
    base = base_url.rstrip("/")
    return (
        f"{base}/forms/{form_id}/form/submission/"
        f"{submission_handle}?token={token}"
    )
