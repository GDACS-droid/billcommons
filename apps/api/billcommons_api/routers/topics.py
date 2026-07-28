from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.labels import attach_bill_labels
from billcommons_api.pagination import (
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    Page,
    clamp_per_page,
    paginate,
)
from billcommons_api.schemas import BillSummary, TopicListEnvelope, TopicOut
from billcommons_schema.models import Bill, BillSubject

router = APIRouter(prefix="/topics", tags=["topics"])


@dataclass(frozen=True)
class Topic:
    """A curated cross-state slice of the corpus.

    Membership is TITLE substring + subject-tag match, tuned for precision
    over recall: a topic hub that includes a stray bill misleads louder than
    one that misses an edge case, because the hub is presented as "every X
    bill in the country". Subject tags alone are useless here -- 31k distinct
    values of state-specific vocabulary tag only ~150 of the 650+ bills whose
    own title says "artificial intelligence".
    """

    slug: str
    name: str
    description: str
    title_patterns: tuple[str, ...]
    subject_patterns: tuple[str, ...] = field(default=())


TOPICS: dict[str, Topic] = {
    t.slug: t
    for t in (
        Topic(
            slug="artificial-intelligence",
            name="Artificial Intelligence",
            description=(
                "State legislation regulating, deploying, or studying artificial "
                "intelligence -- algorithmic decision systems, generative AI, "
                "deepfakes, and AI in government use."
            ),
            title_patterns=("%artificial intelligence%",),
            subject_patterns=("%artificial intelligence%",),
        ),
        Topic(
            slug="data-privacy",
            name="Data Privacy",
            description=(
                "State legislation on consumer data privacy, personal data "
                "protection, and biometric identifiers."
            ),
            title_patterns=(
                "%data privacy%",
                "%consumer privacy%",
                "%personal data%",
                "%biometric%",
            ),
            subject_patterns=("%data privacy%",),
        ),
        Topic(
            slug="cryptocurrency",
            name="Cryptocurrency & Digital Assets",
            description=(
                "State legislation on cryptocurrency, blockchain, and digital "
                "asset regulation."
            ),
            title_patterns=(
                "%cryptocurrency%",
                "%digital asset%",
                "%blockchain%",
                "%virtual currency%",
            ),
            subject_patterns=("%cryptocurrency%", "%blockchain%"),
        ),
    )
}


def _membership_clause(topic: Topic):
    branches = [func.lower(Bill.title).like(p) for p in topic.title_patterns]
    for pattern in topic.subject_patterns:
        branches.append(
            select(BillSubject.id)
            .where(
                BillSubject.bill_id == Bill.id,
                func.lower(BillSubject.subject).like(pattern),
            )
            .exists()
        )
    return or_(*branches)


@router.get("", response_model=TopicListEnvelope)
def list_topics(request: Request, db: OrmSession = Depends(get_db)) -> TopicListEnvelope:
    items = []
    for topic in TOPICS.values():
        count = db.execute(
            select(func.count()).select_from(Bill).where(_membership_clause(topic))
        ).scalar_one()
        items.append(
            TopicOut(
                slug=topic.slug,
                name=topic.name,
                description=topic.description,
                bill_count=count,
            )
        )
    return TopicListEnvelope(
        data=items,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )


@router.get("/{slug}", response_model=Page[BillSummary])
def topic_bills(
    request: Request,
    slug: str,
    page: int = Query(DEFAULT_PAGE, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[BillSummary]:
    topic = TOPICS.get(slug)
    if topic is None:
        raise HTTPException(status_code=404, detail=f"unknown topic {slug!r}")

    clause = _membership_clause(topic)
    per_page = clamp_per_page(per_page)
    total = db.execute(
        select(func.count()).select_from(Bill).where(clause)
    ).scalar_one()
    rows = (
        db.execute(
            select(Bill)
            .where(clause)
            .order_by(Bill.latest_action_date.desc().nullslast(), Bill.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    items = attach_bill_labels(db, [BillSummary.model_validate(r) for r in rows])
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
