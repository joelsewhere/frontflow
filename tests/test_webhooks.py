"""Tests for frontflow.webhooks — HMAC signing + verification of
outbound lifecycle-hook webhooks (Phase 15 / likely-next-ask #8)."""
from __future__ import annotations

import time

import pytest

from frontflow import webhooks
from frontflow.webhooks import MalformedSignatureHeader


SECRET = "test-secret-please-rotate"


class TestSignVerifyRoundtrip:
    def test_signed_payload_verifies(self):
        body = b'{"event":"submitted","handle":"abc"}'
        sig = webhooks.sign(body, secret=SECRET)
        assert webhooks.verify(body, sig, secret=SECRET) is True

    def test_signed_str_payload_verifies(self):
        # `sign`/`verify` accept str (UTF-8) as well as bytes.
        body = '{"event":"failed"}'
        sig = webhooks.sign(body, secret=SECRET)
        assert webhooks.verify(body, sig, secret=SECRET) is True

    def test_explicit_timestamp_passes_through_to_header(self):
        # An explicit timestamp lets the caller stamp pre-canonicalized
        # events; verify it lands in the header exactly.
        sig = webhooks.sign(b"x", secret=SECRET, timestamp=1700000000)
        assert "t=1700000000" in sig

    def test_signature_header_shape(self):
        # Defensive — the header layout is a public contract; if
        # the test breaks, deployed verifiers might break too.
        sig = webhooks.sign(b"x", secret=SECRET, timestamp=1700000000)
        parts = dict(p.split("=", 1) for p in sig.split(","))
        assert "t" in parts
        assert "v1" in parts
        # SHA-256 hexdigest is 64 hex chars.
        assert len(parts["v1"]) == 64
        assert all(c in "0123456789abcdef" for c in parts["v1"])


class TestForgeryRejection:
    def test_tampered_body_rejected(self):
        body = b'{"amount":10}'
        sig = webhooks.sign(body, secret=SECRET)
        # Attacker changes the amount; the original signature no
        # longer matches the new body.
        forged = b'{"amount":99999}'
        assert webhooks.verify(forged, sig, secret=SECRET) is False

    def test_wrong_secret_rejected(self):
        body = b"hello"
        sig = webhooks.sign(body, secret=SECRET)
        assert webhooks.verify(body, sig, secret="wrong-secret") is False

    def test_tampered_signature_rejected(self):
        body = b"hello"
        sig = webhooks.sign(body, secret=SECRET)
        # Flip a hex char in the v1 segment.
        bad = sig.replace("v1=", "v1=0", 1)
        # Don't break the length (the parser would still accept it
        # but the digest length will be off by one — verify still
        # returns False, not raises).
        assert webhooks.verify(body, bad, secret=SECRET) is False

    def test_tampered_timestamp_rejected(self):
        # If an attacker rewrites t= but not v1=, the recomputed
        # signed-input won't match.
        body = b"hello"
        sig = webhooks.sign(body, secret=SECRET, timestamp=int(time.time()))
        # Increment the timestamp segment.
        ts_segment = sig.split(",")[0]
        ts = int(ts_segment[2:])
        bumped = sig.replace(f"t={ts}", f"t={ts + 1}", 1)
        assert webhooks.verify(body, bumped, secret=SECRET) is False


class TestReplayWindow:
    def test_old_signature_rejected_outside_tolerance(self):
        # 1 hour old; default tolerance is 5 minutes. Reject.
        body = b"hello"
        ts = int(time.time()) - 3600
        sig = webhooks.sign(body, secret=SECRET, timestamp=ts)
        assert webhooks.verify(body, sig, secret=SECRET) is False

    def test_future_signature_rejected_outside_tolerance(self):
        # 1 hour in the future (clock-skew attack). Reject.
        body = b"hello"
        ts = int(time.time()) + 3600
        sig = webhooks.sign(body, secret=SECRET, timestamp=ts)
        assert webhooks.verify(body, sig, secret=SECRET) is False

    def test_custom_tolerance_window_honored(self):
        # Caller widens the window for a high-skew network — should
        # accept a 30-minute-old signature.
        body = b"hello"
        ts = int(time.time()) - 1800
        sig = webhooks.sign(body, secret=SECRET, timestamp=ts)
        assert (
            webhooks.verify(
                body, sig, secret=SECRET, tolerance_seconds=3600,
            )
            is True
        )


class TestMalformedHeader:
    def test_empty_header_raises(self):
        with pytest.raises(MalformedSignatureHeader, match="empty"):
            webhooks.verify(b"x", "", secret=SECRET)

    def test_missing_t_raises(self):
        with pytest.raises(MalformedSignatureHeader, match="missing 't="):
            webhooks.verify(b"x", "v1=deadbeef", secret=SECRET)

    def test_missing_v1_raises(self):
        with pytest.raises(MalformedSignatureHeader, match="missing 'v1="):
            webhooks.verify(b"x", "t=1700000000", secret=SECRET)

    def test_non_integer_timestamp_raises(self):
        with pytest.raises(MalformedSignatureHeader, match="not an integer"):
            webhooks.verify(
                b"x", "t=oh-no,v1=deadbeef", secret=SECRET,
            )

    def test_piece_without_equals_raises(self):
        with pytest.raises(MalformedSignatureHeader, match="no '='"):
            webhooks.verify(
                b"x", "t=1700000000,bare,v1=deadbeef", secret=SECRET,
            )

    def test_extra_unknown_keys_tolerated(self):
        # Future-proofing — a v2 verifier may emit `v2=...` alongside
        # `v1=...`. v1 receivers should still accept.
        body = b"hello"
        ts = int(time.time())
        sig = webhooks.sign(body, secret=SECRET, timestamp=ts)
        # Splice an unknown key into the header.
        spliced = sig + ",future_alg=zzz"
        assert webhooks.verify(body, spliced, secret=SECRET) is True


class TestSecretValidation:
    def test_empty_secret_to_sign_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            webhooks.sign(b"x", secret="")

    def test_empty_secret_to_verify_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            webhooks.verify(b"x", "t=1,v1=deadbeef", secret="")
