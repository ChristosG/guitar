import uuid

from sqlalchemy import JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin


class GenerationJob(Base, PkMixin, TimestampMixin):
    """A generic async background-generation job record (Plan 8).

    Curriculum generation is a blocking LLM call measured at 49-179s
    (`routers/curriculum.py`'s `generate_curriculum_endpoint` docstring) —
    too slow for a synchronous request/response cycle behind a browser tab
    or a load balancer's timeout. This table is the enqueue/poll record for
    making that (and any future slow generation) asynchronous: an endpoint
    inserts a row here and returns immediately, a background runner flips
    `status` pending -> running -> succeeded|failed, and a poll endpoint
    reads this row back. `params` holds the original generation request
    verbatim so the runner is the only thing that needs to reconstruct and
    execute it.

    `result_root_id` deliberately has NO `ForeignKey` to `block.id` (unlike
    e.g. `Progress.block_id`/`Artifact.block_id`): the job row and its
    eventual result `Block` have independent lifecycles — the job is an
    audit/poll record that must stay readable (with whatever id it recorded)
    even if that `Block` is later edited or deleted, so a dangling id here is
    expected and validated at read time (the poll endpoint), not enforced by
    the DB.

    `error_kind` mirrors the 502 ("upstream")/504 ("timeout") split already
    established in `generate_curriculum_endpoint` for synchronous callers,
    plus "internal" for anything else unexpected the runner catches, so a
    poll response can carry the same distinction without depending on HTTP
    status codes.
    """

    __tablename__ = "generation_job"
    kind: Mapped[str] = mapped_column(String(30))  # e.g. "curriculum"
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | running | succeeded | failed
    params: Mapped[dict] = mapped_column(JSON)
    result_root_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # upstream | timeout | internal | auth | rate_limit
    #
    # Free-form job progress, for the ONE job that has any: the curriculum draft
    # fan-out writes `{"phase": "drafting"}` here. Note what it does NOT hold —
    # the drafted/total counts. Those are a GROUP BY over `block.meta ->
    # draft_status` (see `app.curriculum.progress_report`), because the blocks are
    # materialized at CONFIRM and are the truth about what is drafted. A count
    # cached on this row would be a second truth, and the two would disagree the
    # first time a lesson was deleted mid-draft.
    #
    # Plain sa.JSON, no MutableDict — reassign the whole dict, never mutate.
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
