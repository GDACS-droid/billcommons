from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.request

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.schemas import FeedbackRequest, FeedbackResponse
from billcommons_schema.models import Feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_NOTIFY_TO = "alberto@gdacs.net"
_NOTIFY_FROM = "Bill Commons <alerts@billcommons.org>"


def _notify(message: str, email: str | None, page: str | None) -> None:
    """Best-effort email so feedback is seen the day it arrives, not whenever
    the table is next queried. Runs in a daemon thread and swallows every
    failure: the row is already committed, and a mail outage must never turn
    into a 500 for the person trying to report a problem."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return
    body = f"{message}\n\n---\nfrom: {email or '(no email left)'}\npage: {page or '(unknown)'}"
    payload = {
        "from": _NOTIFY_FROM,
        "to": [_NOTIFY_TO],
        "subject": "Bill Commons feedback",
        "text": body,
        **({"reply_to": [email]} if email else {}),
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend's edge 403s the default Python-urllib user agent.
            "User-Agent": "billcommons-api/1.0",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        logger.exception("feedback notification email failed")


@router.post("", response_model=FeedbackResponse, status_code=201)
def submit_feedback(
    body: FeedbackRequest,
    request: Request,
    db: OrmSession = Depends(get_db),
) -> FeedbackResponse:
    """Accept a feedback message from the site (or any API consumer).

    `website` is a honeypot: the form never shows it, so a filled value means
    a bot. Those get the same 201 as everyone else -- returning an error just
    teaches the bot which field to skip -- but nothing is stored.
    """
    ok = FeedbackResponse(
        received=True,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )
    if body.website:
        return ok

    message = body.message.strip()
    email = (body.email or "").strip().lower() or None
    if email and not _EMAIL_RE.match(email):
        email = None

    db.add(
        Feedback(
            message=message,
            email=email,
            page=(body.page or "").strip()[:2000] or None,
            user_agent=(request.headers.get("user-agent") or "")[:1000] or None,
        )
    )
    db.commit()

    threading.Thread(target=_notify, args=(message, email, body.page), daemon=True).start()
    return ok
