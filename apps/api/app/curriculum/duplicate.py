"""Duplicate a curriculum: a second template, identical to the first, that the
tutor can let the AI loose on without risking the original.

Chris asked for this as a BACKUP, in those words — "so that the tutor can make
changes and always have a 'backup' or a version more general which needs to be a
base for similar curricula". Both readings are the same object: a full-fidelity
fork. So this copies the prose, not a skeleton. A copy that came back without
its drafted lessons would be a template, and he already has a way to make one of
those; it would not be a thing to fall back to.

IT DOES NOT OWN A SECOND DEEP-CLONE. `assign.clone_content_subtree` is the one
walk, shared with per-student assignment, and that module's docstring exists to
argue exactly this point — it was extracted from two call sites so that a new
`Block` field or a plane-filter change could not silently diverge them. A third
copy here would reintroduce the drift it was written to prevent. The two callers
differ only in what they STAMP, so the stamp is a parameter.

Caller-owned session, same as its siblings under `app/curriculum/`: this flushes
(the recursion needs ids) but does not commit.

NO MIGRATION, DELIBERATELY. Everything new lives in the existing JSON `meta`
column. On the desktop a migration has to survive first-run `alembic upgrade
head` AND the seed/backup restore path, and not having one removes that entire
class of risk from a release whose point is a bug fix.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.curriculum.assign import clone_content_subtree
from app.models.artifact import Artifact
from app.models.block import Block

# `Block.title` is String(300). Duplicating a duplicate of a duplicate is a real
# thing a tutor does, and the suffix is what tells them apart, so when something
# has to give it is the ORIGINAL title that gets cut — never the marker.
TITLE_MAX = 300

_SUFFIX = {"el": " (αντίγραφο)", "en": " (copy)"}


class DuplicateError(ValueError):
    """The root is not a duplicable curriculum. Router turns this into a 404."""


def copy_title(title: str, language: str | None) -> str:
    """«Ήχος και Ενισχυτές» -> «Ήχος και Ενισχυτές (αντίγραφο)».

    In the COURSE'S OWN language, not the UI locale. A Greek course duplicated
    from an English-language session is still a Greek course, and its title is
    read in the board next to twenty Greek module names; a lone "(copy)" in that
    column is the tutor's own content being talked about in the wrong language.
    Matched on the `el`/`en` prefix rather than the exact tag, since
    `Block.language` is String(5) and has held both forms.
    """
    suffix = _SUFFIX["el"] if (language or "el").lower().startswith("el") else _SUFFIX["en"]
    # Unconditional slice + rstrip: a short title is unchanged by the slice, and
    # the rstrip is wanted either way — a title the tutor left a trailing space
    # on would otherwise read "Ήχος  (αντίγραφο)".
    return f"{title[:TITLE_MAX - len(suffix)].rstrip()}{suffix}"


def duplicate_curriculum(db: Session, root_id: UUID) -> Block:
    """Fork the whole curriculum. Returns the new root; the caller commits."""
    course = db.get(Block, root_id)
    if course is None or course.kind != "course" or course.parent_id is not None:
        raise DuplicateError(f"not a curriculum root: {root_id}")

    # THE WALK. `is_template=True` + `student_id=None` is the opposite stamp
    # from assignment's, and is what makes the copy show up in `GET /curricula`
    # (which selects `is_template.is_(True), parent_id.is_(None)`) rather than
    # becoming an invisible orphan.
    id_map: dict[UUID, UUID] = {}
    copy = clone_content_subtree(
        db, course, parent_id=None, student_id=None, is_template=True, id_map=id_map,
    )

    copy.title = copy_title(course.title, course.language)

    # WHOLE-DICT REASSIGNMENT. `Block.meta` is plain sa.JSON with no
    # MutableDict, so in-place mutation appears to work in dev (the identity map
    # hands the same dict back) and silently no-ops in production.
    #
    # Nothing reads these two keys yet. They cost one key each and are what lets
    # a later "compare with the original" exist without a migration — which, on
    # an app that ships as a .deb, is the difference between a feature and a
    # release-day risk.
    copy.meta = {
        **(copy.meta or {}),
        "copied_from": str(root_id),
        "copied_at": datetime.now(timezone.utc).isoformat(),
    }

    _settle_draft_markers(db, id_map)
    _clone_artifacts(db, id_map)
    db.flush()
    return copy


def _settle_draft_markers(db: Session, id_map: dict[UUID, UUID]) -> None:
    """`drafting` means "a worker is part-way through writing this". The copy has
    no worker, so on the copy it is a lie — and an expensive one: the board shows
    a spinner that never resolves, and `draft_progress` reads the curriculum as
    in-flight. `jobs/sweep.py` would fix it at the next boot, but the copy has to
    be honest the moment it exists, not the morning after.

    Only ever `drafting` -> `queued`. `ready` and `failed` are real states about
    real content and are copied verbatim; a `queued` lesson stays queued and the
    copy's own Resume finishes it, which is the correct behaviour for a fork
    taken mid-draft.
    """
    for clone_id in id_map.values():
        clone = db.get(Block, clone_id)
        if clone is None or not clone.meta:
            continue
        if clone.meta.get("draft_status") == "drafting":
            clone.meta = {**clone.meta, "draft_status": "queued"}


def _clone_artifacts(db: Session, id_map: dict[UUID, UUID]) -> None:
    """Bring the attached chord diagrams and visuals along.

    A copy whose lessons came back without their diagrams is visibly poorer than
    the original, which undermines the one thing it is for. `Artifact` is
    `{kind, spec}` JSON with no filesystem backing (see its own docstring), so
    this is rows only — no media copy, no disk, and no half-copied state to
    recover from.

    One query for the whole tree rather than one per block: a 24-lesson course
    with segments is several hundred blocks, and this is the difference between
    one statement and several hundred.
    """
    if not id_map:
        return
    attached = db.scalars(
        select(Artifact).where(Artifact.block_id.in_(list(id_map.keys())))
    ).all()
    for art in attached:
        db.add(Artifact(
            kind=art.kind,
            # Independent dict/list copies, the same anti-aliasing instinct as
            # `clone_content_subtree`'s own `meta`/`target_profile` handling —
            # `spec` is a JSON column and two rows must never share one object.
            spec=dict(art.spec) if art.spec else {},
            title=art.title,
            tags=list(art.tags) if art.tags else [],
            source=art.source,
            block_id=id_map[art.block_id],
        ))
