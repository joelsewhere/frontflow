"""contact_form — bare FastAPI version, no frontflow.

A two-screen contact form with a conditional follow-up that asks
for a different contact detail (email / phone / mailing address)
based on the user's preferred contact method.

Functionally equivalent on the user path to the frontflow example
at ../frontflow_version/contact_form.py.

Deliberately does NOT replicate: an admin UI, submission listing,
analytics, form versioning, auth, theming, resumable URL handles,
or edit-cascade. Adding any of these would substantially grow the
bare version. See ../README.md for the full feature-parity table.
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
from sqlalchemy import (
    Boolean, Column, DateTime, String, Text, create_engine, select,
)
from sqlalchemy.orm import DeclarativeBase, Session

# --- Config -----------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./contact.db")
CONTACT_METHODS = ["Email", "Phone", "Mail"]

# --- DB ---------------------------------------------------------------------

engine = create_engine(DATABASE_URL)


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submission"
    submission_id = Column(String, primary_key=True)
    full_name = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    contact_method = Column(String, nullable=False)
    # Method-specific fields. Only the one matching the chosen
    # contact_method is populated; the others are NULL.
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    ok_to_text = Column(Boolean, default=False)
    mailing_address = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)

# --- Helpers ----------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[+\d][\d\s().\-]{6,}$")


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s or "contact"


def _save(sub: Submission) -> None:
    with Session(engine) as session:
        session.add(sub)
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
async def show_form(request: Request):
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "errors": {},
            "values": {},
            "contact_methods": CONTACT_METHODS,
        },
    )


@app.post("/submit")
async def submit(
    request: Request,
    full_name: str = Form(""),
    message: str = Form(""),
    contact_method: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    ok_to_text: Optional[str] = Form(None),
    mailing_address: str = Form(""),
):
    # Validate the always-required fields.
    errors: dict[str, str] = {}
    if not full_name.strip():
        errors["full_name"] = "Your name is required"
    if not message.strip():
        errors["message"] = "Message is required"
    if contact_method not in CONTACT_METHODS:
        errors["contact_method"] = "Pick how we should reach you"

    # Validate the method-specific field. Mirrors the conditional
    # reveal in the frontflow version: only the matching field is
    # required.
    if contact_method == "Email":
        if not email.strip():
            errors["email"] = "Email address is required"
        elif not EMAIL_RE.match(email.strip()):
            errors["email"] = "That doesn't look like an email address"
    elif contact_method == "Phone":
        if not phone.strip():
            errors["phone"] = "Phone number is required"
        elif not PHONE_RE.match(phone.strip()):
            errors["phone"] = "That doesn't look like a phone number"
    elif contact_method == "Mail":
        if not mailing_address.strip():
            errors["mailing_address"] = "Mailing address is required"

    if errors:
        return templates.TemplateResponse(
            "form.html",
            {
                "request": request,
                "errors": errors,
                "values": {
                    "full_name": full_name,
                    "message": message,
                    "contact_method": contact_method,
                    "email": email,
                    "phone": phone,
                    "ok_to_text": bool(ok_to_text),
                    "mailing_address": mailing_address,
                },
                "contact_methods": CONTACT_METHODS,
            },
        )

    submission_id = f"{slugify(full_name)}-{uuid4().hex[:8]}"
    sub = Submission(
        submission_id=submission_id,
        full_name=full_name.strip(),
        message=message.strip(),
        contact_method=contact_method,
        email=email.strip() if contact_method == "Email" else None,
        phone=phone.strip() if contact_method == "Phone" else None,
        ok_to_text=bool(ok_to_text) if contact_method == "Phone" else False,
        mailing_address=(
            mailing_address.strip() if contact_method == "Mail" else None
        ),
    )
    _save(sub)
    return RedirectResponse(f"/thanks/{submission_id}", status_code=303)


@app.get("/thanks/{submission_id}", response_class=HTMLResponse)
async def thanks(request: Request, submission_id: str):
    sub = _load(submission_id)
    return templates.TemplateResponse(
        "thanks.html",
        {
            "request": request,
            "full_name": sub.full_name,
            "contact_method": sub.contact_method.lower(),
        },
    )
