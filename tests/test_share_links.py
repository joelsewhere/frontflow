"""Anonymous read-only share links for a submission.

Submission visibility is independent of form visibility — a form can
be public to FILL while its submissions stay private to their
contributors — so sharing a result with someone who has no login needs
a token whose possession IS the credential. These links authenticate
as nobody, are bound to one submission, are read-only, and expire.

The security boundary under test: a read-only link must never
authorize a write.
"""
from __future__ import annotations

import time

import pytest

from frontflow.dsl import signed_links
from frontflow.main import _token_bears_submission


HANDLE = "abc123"


# --- minting guards --------------------------------------------------------


def test_share_token_round_trips():
    tok = signed_links.mint_for_share(submission_handle=HANDLE)
    payload = signed_links.verify(tok, submission_handle=HANDLE)
    assert payload is not None
    assert payload["user_id"] is None      # authenticates as nobody
    assert payload["scope"] == "read"
    assert payload["issuer"] == "share"


def test_share_token_is_bound_to_one_submission():
    tok = signed_links.mint_for_share(submission_handle=HANDLE)
    assert signed_links.verify(tok, submission_handle="other") is None


def test_share_tokens_must_be_anonymous_and_read_only():
    with pytest.raises(ValueError):
        signed_links.mint(
            user_id=7, submission_handle=HANDLE,
            scope="read", issuer="share",
        )
    with pytest.raises(ValueError):
        signed_links.mint(
            user_id=None, submission_handle=HANDLE,
            scope="fill", issuer="share",
        )


def test_other_issuers_still_require_a_user():
    with pytest.raises(ValueError):
        signed_links.mint(
            user_id=None, submission_handle=HANDLE,
            scope="read", issuer="admin",
        )


def test_tampered_token_is_refused():
    tok = signed_links.mint_for_share(submission_handle=HANDLE)
    body, sig = tok.split(".")
    assert signed_links.verify(f"{body}x.{sig}", submission_handle=HANDLE) is None
    assert signed_links.verify(f"{body}.{sig}x", submission_handle=HANDLE) is None


def test_expired_token_is_refused(monkeypatch):
    tok = signed_links.mint_for_share(
        submission_handle=HANDLE, ttl_seconds=60,
    )
    assert signed_links.verify(tok, submission_handle=HANDLE) is not None
    # Jump past the expiry — the grant is time-boxed, not permanent.
    later = time.time() + 3600
    monkeypatch.setattr(signed_links.time, "time", lambda: later)
    assert signed_links.verify(tok, submission_handle=HANDLE) is None


# --- the read/write boundary ----------------------------------------------


def test_share_token_grants_read_but_not_write():
    tok = signed_links.mint_for_share(submission_handle=HANDLE)
    # Read endpoints pass no scope requirement.
    assert _token_bears_submission(tok, HANDLE) is True
    # A mutating endpoint demands "fill" — possession of a read-only
    # link must not confer write access.
    assert _token_bears_submission(tok, HANDLE, require_scope="fill") is False


def test_assignee_fill_token_may_write():
    tok = signed_links.mint(
        user_id=1, submission_handle=HANDLE,
        scope="fill", issuer="assign_operator",
    )
    assert _token_bears_submission(tok, HANDLE) is True
    assert _token_bears_submission(tok, HANDLE, require_scope="fill") is True


def test_absent_or_junk_token_bears_nothing():
    assert _token_bears_submission(None, HANDLE) is False
    assert _token_bears_submission("", HANDLE) is False
    assert _token_bears_submission("not-a-token", HANDLE) is False
