from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.routers.topics import TOPICS
from billcommons_api.schemas import AlertSubscribeRequest, AlertSubscribeResponse
from billcommons_schema.models import AlertSubscription, Jurisdiction

router = APIRouter(prefix="/alerts", tags=["alerts"])

# Deliberately loose: real validation is that the digest either arrives or it
# doesn't. This only rejects strings that cannot possibly be an address, so a
# typo'd-but-shaped address is accepted rather than second-guessed.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/subscribe", response_model=AlertSubscribeResponse, status_code=201)
def subscribe(
    body: AlertSubscribeRequest,
    request: Request,
    db: OrmSession = Depends(get_db),
) -> AlertSubscribeResponse:
    """Subscribe an email to a topic's change digest.

    Idempotent on (email, kind, target): re-subscribing an existing address
    reactivates it rather than erroring, so a user who unsubscribed and
    changed their mind is not told "already subscribed".
    """
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="that does not look like an email address")
    if body.kind != "topic":
        raise HTTPException(status_code=422, detail="only kind='topic' alerts exist today")
    if body.target not in TOPICS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown topic {body.target!r}; see /api/v1/topics",
        )

    # Validate the scope against real jurisdictions rather than accepting any
    # two-character string. An unrecognised code would otherwise be stored,
    # match nothing forever, and present as "subscribed, but the digest never
    # arrives" -- indistinguishable from a broken sender.
    jurisdiction: str | None = None
    if body.jurisdiction is not None and body.jurisdiction.strip():
        jurisdiction = body.jurisdiction.strip().upper()
        known = db.execute(
            select(Jurisdiction.abbreviation).where(
                Jurisdiction.abbreviation == jurisdiction
            )
        ).scalar_one_or_none()
        if known is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown jurisdiction {jurisdiction!r}; use a two-letter "
                    "state abbreviation, or omit for a national digest"
                ),
            )

    existing = db.execute(
        select(AlertSubscription).where(
            AlertSubscription.email == email,
            AlertSubscription.kind == body.kind,
            AlertSubscription.target == body.target,
            AlertSubscription.jurisdiction.is_(None)
            if jurisdiction is None
            else AlertSubscription.jurisdiction == jurisdiction,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.active = True
        db.commit()
        return AlertSubscribeResponse(
            subscribed=True,
            kind=existing.kind,
            target=existing.target,
            jurisdiction=existing.jurisdiction,
            meta={"api_version": "v1", "request_id": request.state.request_id},
        )

    db.add(
        AlertSubscription(
            email=email,
            kind=body.kind,
            target=body.target,
            jurisdiction=jurisdiction,
            unsubscribe_token=secrets.token_urlsafe(32),
        )
    )
    db.commit()
    return AlertSubscribeResponse(
        subscribed=True,
        kind=body.kind,
        target=body.target,
        jurisdiction=jurisdiction,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(
    token: str = Query(..., min_length=8),
    db: OrmSession = Depends(get_db),
) -> HTMLResponse:
    """One-click unsubscribe from the link in every digest.

    A GET that mutates is deliberate here: the link must work from any mail
    client with no login and no form. Idempotent -- clicking twice lands on
    the same page. Returns HTML, not JSON: the person clicking is standing in
    their inbox, not in an API client.
    """
    sub = db.execute(
        select(AlertSubscription).where(AlertSubscription.unsubscribe_token == token)
    ).scalar_one_or_none()
    if sub is None:
        return HTMLResponse(
            "<h1>Unknown link</h1><p>This unsubscribe link is not recognized.</p>",
            status_code=404,
        )
    sub.active = False
    db.commit()
    scope = f" ({sub.jurisdiction})" if sub.jurisdiction else ""
    return HTMLResponse(
        "<h1>Unsubscribed</h1>"
        f"<p>{sub.email} will no longer receive the {sub.target}{scope} digest. "
        'Changed your mind? Re-subscribe any time at '
        f'<a href="https://billcommons.org/topics/{sub.target}">billcommons.org</a>.</p>'
    )
