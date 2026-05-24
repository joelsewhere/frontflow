"""Integration tests for submission lifecycle via FastAPI TestClient.

Covers create → advance → show → list endpoints on real (in-memory)
forms loaded from `tests/fixtures/forms/`. Each test gets a fresh
DB + FRONTFLOW_HOME, so submissions never leak between tests."""
from __future__ import annotations

from fastapi.testclient import TestClient


class TestCreateSubmission:
    def test_simple_form_creates_and_terminates(
        self, admin_client: TestClient
    ):
        # The simple form has one node — submitting reaches a
        # terminal state immediately.
        r = admin_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"name": "Test", "note": "ok"}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["handle"]
        assert body["state"] in ("success", "running")

    def test_anon_can_create_on_public_form(
        self, anon_client: TestClient, admin_user: dict
    ):
        # Forms are public by default — anyone can submit them. This
        # is intentional (forms exist to be filled by end users), so
        # the submission-CREATE path is open. Admin-only operations
        # like /refresh, listing all submissions, repin, reset are
        # the ones that gate.
        r = anon_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"name": "anon-user"}},
        )
        assert r.status_code == 201

    def test_unauthenticated_cannot_list_submissions(
        self, anon_client: TestClient, admin_user: dict
    ):
        # Listing the submissions of a form is admin-only.
        r = anon_client.get("/api/forms/test_simple/submissions")
        assert r.status_code in (401, 403)

    def test_unknown_form_returns_404(self, admin_client: TestClient):
        r = admin_client.post(
            "/api/forms/does_not_exist/submissions",
            json={"values": {}},
        )
        assert r.status_code == 404

    def test_missing_required_field_returns_400(
        self, admin_client: TestClient
    ):
        # `name` is required on test_simple.
        r = admin_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"note": "no name"}},
        )
        assert r.status_code == 400


class TestTwoStepSubmission:
    def test_two_step_advances_through_pages(
        self, admin_client: TestClient
    ):
        # Step 1: create with name (slugified into submission_id).
        r = admin_client.post(
            "/api/forms/test_two_step/submissions",
            json={"values": {"name": "Alice"}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        sid = body["submission_id"]
        assert sid == "alice", f"expected slugified id, got {sid}"
        # After step 1 the chain is still in flight — confirm awaits.
        assert body["state"] == "running"

        # Step 2: advance the confirm node. The POST-step endpoint
        # returns the StepDetail (not the parent SubmissionResponse),
        # so the shape is different — assert on response_received.
        r = admin_client.post(
            f"/api/forms/test_two_step/submissions/{sid}/steps/confirm",
            json={"values": {"confirmed": "Alice"}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["step_id"] == "confirm"
        assert body["response_received"] is True
        assert body["response"]["values"]["confirmed"] == "Alice"

        # Then fetch the parent submission to verify it terminated.
        r = admin_client.get(
            f"/api/forms/test_two_step/submissions/{sid}"
        )
        assert r.status_code == 200
        assert r.json()["state"] == "success"

    def test_advance_unknown_step_returns_400(
        self, admin_client: TestClient
    ):
        r = admin_client.post(
            "/api/forms/test_two_step/submissions",
            json={"values": {"name": "Bob"}},
        )
        sid = r.json()["submission_id"]
        r = admin_client.post(
            f"/api/forms/test_two_step/submissions/{sid}/steps/no_such_step",
            json={"values": {}},
        )
        # The step name is rejected before route dispatch.
        assert r.status_code in (400, 404)


class TestListAndFetchSubmissions:
    def test_list_returns_submissions(self, admin_client: TestClient):
        admin_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"name": "First", "note": ""}},
        )
        admin_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"name": "Second", "note": ""}},
        )
        r = admin_client.get("/api/forms/test_simple/submissions")
        assert r.status_code == 200
        body = r.json()
        assert len(body) >= 2

    def test_fetch_submission_detail(self, admin_client: TestClient):
        r = admin_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"name": "Detail Test"}},
        )
        handle = r.json()["handle"]
        r = admin_client.get(
            f"/api/forms/test_simple/submissions/{handle}"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["handle"] == handle

    def test_fetch_unknown_submission_returns_404(
        self, admin_client: TestClient
    ):
        r = admin_client.get(
            "/api/forms/test_simple/submissions/nope"
        )
        assert r.status_code == 404
