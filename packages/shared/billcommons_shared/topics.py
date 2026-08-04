"""The curated cross-state topic registry, shared by every consumer that
needs to answer "what are the topic trackers and which bills are in them".

Single source of truth: this used to live only in
billcommons_api.routers.topics, duplicated in two places -- billcommons_mcp
had no shared registry to import at all, and workers/alerts/send_alerts.py
carried a second, hand-duplicated copy of the same membership predicates (as
raw SQL strings, its own `topic_membership_sql()`) with a comment promising
"the contract test in apps/api/tests keeps the two in sync" -- a promise a
contract test can only ever catch AFTER a drift, not prevent one. Moving the
registry here (a package both billcommons_api and billcommons_mcp already
depend on) removed the first duplicate and gave the MCP tool a home to
import from.

`workers/alerts/send_alerts.py`'s `topic_membership_sql()` is the ONE
remaining deliberate copy, and stays that way on purpose: that script runs
straight from the working tree via a systemd timer (see its own docstring),
not from an installed environment, so it cannot `import billcommons_shared`
without carrying this whole package's dependency chain into a script that
exists specifically to avoid needing a deploy step. Anyone tuning a topic's
membership rule here must update `topic_membership_sql()` there in the same
change, or the nightly digest and the API/MCP will silently disagree about
which bills are "in" a topic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, or_, select

from billcommons_schema.models import Bill, BillSubject


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
        # The four topics below were tuned against the live corpus rather than
        # guessed. Patterns that read plausibly but matched badly were dropped:
        # "%section 230%" matches "section 2307 of the public health law", and
        # "%parental consent%" is mostly minors' mental-health and abortion
        # bills, not platform consent. Both would have polluted a hub that is
        # presented as "every X bill in the country".
        Topic(
            slug="youth-online-safety",
            name="Youth Online Safety",
            description=(
                "State legislation on minors' access to online services -- age "
                "verification, app-store and parental-consent duties, "
                "age-appropriate design codes, and social media rules aimed "
                "specifically at children."
            ),
            title_patterns=(
                "%age verification%",
                "%age-verification%",
                "%app store accountability%",
                "%social media%minor%",
                "%minor%social media%",
                "%social media%child%",
                "%child%social media%",
                "%child%online safety%",
                "%age-appropriate design%",
            ),
            subject_patterns=("%age verification%",),
        ),
        Topic(
            slug="platform-accountability",
            name="Platform Accountability & Content Moderation",
            description=(
                "State legislation governing how online platforms moderate, "
                "rank, and disclose content -- moderation mandates, "
                "transparency reporting, algorithmic disclosure, and platform "
                "duties that are not specific to minors."
            ),
            title_patterns=(
                "%content moderation%",
                "%social media platform%",
                "%online platform%",
                "%social media compan%",
                "%social networking%",
            ),
            subject_patterns=("%social media%",),
        ),
        Topic(
            slug="cybersecurity",
            name="Cybersecurity",
            description=(
                "State legislation on cybersecurity programs, breach "
                "notification, ransomware, and encryption requirements across "
                "government and private systems."
            ),
            title_patterns=(
                "%cybersecurity%",
                "%cyber security%",
                "%data breach%",
                "%ransomware%",
                "%encryption%",
            ),
            subject_patterns=("%cybersecurity%",),
        ),
        Topic(
            slug="local-government",
            name="Local Government & Preemption",
            description=(
                "State legislation that governs cities and counties -- home "
                "rule, state preemption of local ordinances, municipal "
                "finance, and local authority. Deliberately broad: a city "
                "affairs office cares about municipal bonds, utilities and "
                "courts as much as about preemption fights, so the hub errs "
                "toward including anything a municipality is subject to."
            ),
            title_patterns=(
                "%local government%",
                "%home rule%",
                "%preemption%",
                "%municipal%",
            ),
            subject_patterns=(
                "%local government%",
                "%municipalit%",
                "%municipal government%",
            ),
        ),
    )
}


def membership_clause(topic: Topic):
    """SQLAlchemy WHERE clause for "bill b is in this topic", built the same
    way for every caller (the API's /topics endpoints and the MCP's
    list_topics tool) so a query change here can't drift between them."""
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


def topic_bill_count(db, topic: Topic) -> int:
    """COUNT of bills currently matching a topic -- shared so /topics and
    list_topics report the identical number for the identical query."""
    return db.execute(
        select(func.count()).select_from(Bill).where(membership_clause(topic))
    ).scalar_one()
