"""publish_article — bare FastAPI app, no frontflow.

A user-facing form that submits an article, triggers a publishing
DAG in Airflow, polls until the editor reviews it via the HITL task,
then branches to one of three terminal pages based on the editor's
decision. Approve → page with a button that pulls the published URL
and shows it. Request-changes / Reject → their own terminal pages.

Functionally equivalent on the USER-facing path to the frontflow
example `publish_article.py`. Deliberately does NOT replicate
frontflow's admin UI, edit-cascade clearing, form-versioning,
auth/access controls, or resumable handles — those would add several
files of their own.
"""
import os
import re
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, Column, DateTime, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Session

import httpx

# --- Config -----------------------------------------------------------------

DAG_ID = "publish_article"
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./publish.db")
AIRFLOW_BASE = os.environ["AIRFLOW_API_URL"]
AIRFLOW_AUTH = (os.environ["AIRFLOW_USER"], os.environ["AIRFLOW_PASS"])

CHANNELS = ["Blog", "Newsletter", "Press release"]
HITL_DECISIONS = {
    "Approve": "approved",
    "Request changes": "changes_requested",
    "Reject": "rejected",
}

# --- DB ---------------------------------------------------------------------

engine = create_engine(DATABASE_URL)


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submission"
    submission_id = Column(String, primary_key=True)
    headline = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    channel = Column(String, nullable=False)
    feature = Column(Boolean, default=False)
    dag_run_id = Column(String, nullable=False)
    # State machine: building -> reviewing -> {approved, changes_requested, rejected, failed}
    state = Column(String, nullable=False, default="building")
    article_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)


# --- Airflow REST client ----------------------------------------------------

def trigger_dag(conf: dict) -> str:
    run_id = f"manual__{uuid4().hex}"
    r = httpx.post(
        f"{AIRFLOW_BASE}/dags/{DAG_ID}/dagRuns",
        json={"dag_run_id": run_id, "conf": conf},
        auth=AIRFLOW_AUTH, timeout=30,
    )
    r.raise_for_status()
    return run_id


def get_task_state(run_id: str, task_id: str) -> str:
    r = httpx.get(
        f"{AIRFLOW_BASE}/dags/{DAG_ID}/dagRuns/{run_id}/taskInstances/{task_id}",
        auth=AIRFLOW_AUTH, timeout=30,
    )
    if r.status_code == 404:
        return "queued"
    r.raise_for_status()
    return r.json().get("state") or "queued"


def get_dag_state(run_id: str) -> str:
    r = httpx.get(
        f"{AIRFLOW_BASE}/dags/{DAG_ID}/dagRuns/{run_id}",
        auth=AIRFLOW_AUTH, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("state") or "queued"


def pull_xcom(run_id: str, task_id: str, key: str) -> Optional[str]:
    r = httpx.get(
        f"{AIRFLOW_BASE}/dags/{DAG_ID}/dagRuns/{run_id}"
        f"/taskInstances/{task_id}/xcomEntries/{key}",
        auth=AIRFLOW_AUTH, timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("value")


def get_hitl_decision(run_id: str, task_id: str) -> Optional[str]:
    """The HITL task xcom-pushes its chosen option under "hitl_choice"
    once a reviewer responds. Returns None while still pending."""
    if get_task_state(run_id, task_id) != "success":
        return None
    return pull_xcom(run_id, task_id, "hitl_choice")


# --- Helpers ----------------------------------------------------------------

def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s or "article"


def _save(sub: Submission) -> None:
    with Session(engine) as session:
        session.merge(sub)
        session.commit()


def _load(submission_id: str) -> Submission:
    with Session(engine) as session:
        sub = session.execute(
            select(Submission).where(
                Submission.submission_id == submission_id
            )
        ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(404)
    return sub


# --- App + routes -----------------------------------------------------------

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def draft_form(request: Request):
    return templates.TemplateResponse(
        "draft.html",
        {"request": request, "errors": {}, "values": {}, "channels": CHANNELS},
    )


@app.post("/submit")
async def submit(
    request: Request,
    headline: str = Form(""),
    body: str = Form(""),
    channel: str = Form(""),
    feature: Optional[str] = Form(None),
):
    errors: dict[str, str] = {}
    if not headline.strip():
        errors["headline"] = "Headline is required"
    if not body.strip():
        errors["body"] = "Article body is required"
    if channel not in CHANNELS:
        errors["channel"] = "Pick a channel"

    if errors:
        return templates.TemplateResponse(
            "draft.html",
            {
                "request": request,
                "errors": errors,
                "values": {
                    "headline": headline, "body": body,
                    "channel": channel, "feature": bool(feature),
                },
                "channels": CHANNELS,
            },
        )

    headline_clean = headline.strip()
    submission_id = (
        f"{slugify(headline_clean)}-"
        f"{int(datetime.utcnow().timestamp())}"
    )
    try:
        run_id = trigger_dag(
            {"headline": headline_clean, "channel": channel}
        )
    except Exception as e:
        raise HTTPException(500, f"Could not trigger DAG: {e}")

    sub = Submission(
        submission_id=submission_id,
        headline=headline_clean,
        body=body, channel=channel, feature=bool(feature),
        dag_run_id=run_id,
        created_at=datetime.utcnow(),
        state="building",
    )
    _save(sub)
    return RedirectResponse(f"/status/{submission_id}", status_code=303)


@app.get("/status/{submission_id}", response_class=HTMLResponse)
async def status(request: Request, submission_id: str):
    sub = _load(submission_id)

    # Inline state machine — poll Airflow as needed and advance.
    if sub.state == "building":
        st = get_task_state(sub.dag_run_id, "build_content")
        if st == "success":
            sub.state = "reviewing"
            _save(sub)
        elif st == "failed":
            sub.state = "failed"
            _save(sub)

    if sub.state == "reviewing":
        decision = get_hitl_decision(sub.dag_run_id, "editor_review")
        if decision in HITL_DECISIONS:
            sub.state = HITL_DECISIONS[decision]
            _save(sub)

    # Render the page for the current state.
    if sub.state in ("building", "reviewing"):
        msg = (
            "Building content..." if sub.state == "building"
            else "Editor reviewing..."
        )
        return templates.TemplateResponse(
            "waiting.html",
            {"request": request, "message": msg, "auto_refresh": True},
        )
    if sub.state == "approved":
        if sub.article_url:
            return templates.TemplateResponse(
                "article_live.html",
                {"request": request, "url": sub.article_url},
            )
        return templates.TemplateResponse(
            "approved.html",
            {"request": request, "submission_id": submission_id},
        )
    if sub.state == "changes_requested":
        return templates.TemplateResponse(
            "changes_requested.html", {"request": request}
        )
    if sub.state == "rejected":
        return templates.TemplateResponse(
            "rejected.html", {"request": request}
        )
    if sub.state == "failed":
        return templates.TemplateResponse(
            "waiting.html",
            {
                "request": request,
                "message": "The publishing pipeline failed.",
                "auto_refresh": False,
            },
        )
    raise HTTPException(500, f"Unknown state: {sub.state}")


@app.post("/publish/{submission_id}")
async def publish_fetch(submission_id: str):
    sub = _load(submission_id)
    if sub.state != "approved":
        raise HTTPException(400, "Not in approved state")
    if get_dag_state(sub.dag_run_id) != "success":
        return RedirectResponse(f"/status/{submission_id}", status_code=303)
    url = pull_xcom(sub.dag_run_id, "publish", "return_value")
    sub.article_url = url
    _save(sub)
    return RedirectResponse(f"/status/{submission_id}", status_code=303)
