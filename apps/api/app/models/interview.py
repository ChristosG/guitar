"""`CurriculumInterview`: persisted state for the code-driven curriculum
authoring interview (Plan 12 Task 3, G2).

STATE PERSISTENCE CHOICE — a small, PURPOSE-BUILT table, not `GenerationJob.
params` — justified:

1. `GenerationJob.status` is a closed pending/running/succeeded/failed
   lifecycle for a fire-and-forget BACKGROUND task (see that model's own
   docstring) — it has no notion of "which of 5 steps are we on" or "what
   did the tutor already answer", and overloading its `params` JSON blob
   with an interview's running state would mean two completely different
   state machines (one enum-driven, one step-driven) sharing one column,
   read by two different runners (`app.jobs.runner` vs this interview).
   `app.jobs.runner.run_curriculum_job` already pattern-matches on `kind`
   for its OWN five job kinds; adding a sixth, non-background, synchronous
   "kind" to that same table blurs a distinction worth keeping: an
   interview answer is a plain request/response, not a scheduled task.
2. The interview needs to CACHE its `preview` step's output (a real LLM
   plan call + a real `ground_topic` retrieval per module) so `GET
   /curricula/interview/{id}` — a refresh — never re-runs either. Nothing
   on `GenerationJob` is meant to hold a re-servable, step-specific
   snapshot like that; adding one would be exactly the schema change this
   table already is, just bolted onto a model whose contract every other
   caller/test already depends on staying unchanged.
3. Once the tutor approves (the "confirm" step), this module creates a
   REAL `GenerationJob(kind="curriculum")` row and hands it to the
   UNMODIFIED `run_curriculum_job`/poll machinery — this table only ever
   stores that job's id (`job_id`, deliberately un-FK'd, same reasoning as
   `GenerationJob.result_root_id`'s own docstring: independent lifecycles,
   a dangling id here is expected and harmless). No new async
   infrastructure is added anywhere — only this one small state-holder for
   the SYNCHRONOUS, multi-turn part of the flow that happens before a job
   ever exists.

No foreign keys at all (deliberately): `answers`/`preview` hold plain JSON
(source ids, student ids/names as strings) rather than relational columns —
this row's job is to remember conversation state across HTTP round trips,
not to enforce referential integrity the underlying `Student`/
`KnowledgeSource`/`GenerationJob` tables already own. This also sidesteps the
"unnamed FK made downgrade() uncallable" pitfall this task's brief warns
about — there is no FK here to accidentally leave unnamed.
"""
import uuid

from sqlalchemy import JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin

# The fixed step order the state machine in `app.curriculum.interview` walks
# through, plus the terminal "done" state reached once "confirm" is approved
# and a real GenerationJob has been created. Exported here (not just as a
# local constant in `app.curriculum.interview`) since it describes what
# values `CurriculumInterview.step` can actually take.
# The six steps of the v2 interview, plus the terminal "done".
#
# "preview" IS GONE, REPLACED BY "outline" — and that rename is a data migration
# (`9c1e4a5d7b30`), not a constant edit. `answer_interview` does
# `STEP_ORDER.index(step)`, so a persisted row still sitting on "preview" when the
# tutor came back from lunch would raise ValueError -> a 500 on his next click.
#
# "scope" is new, and it is where `domain` died. Chris asked, of `domain`: "is it
# playing any role? is it used somewhere or only for tagging?" It was ONE line in
# one prompt ("Domain: {domain}") plus a retrieval filter that — after 5d77bd0 —
# could no longer exclude anything at all. It never tagged the curriculum and
# never rendered. A free-text COURSE BRIEF in its place is the thing he was
# reaching for: what this course is actually about, in his words.
#
# "structure" is Plan C's addition (Task 5): the OPTIONAL, SKIPPABLE lesson
# blueprint step, pre-filled with the settings default. It sits between "scope"
# and "sources" (Resolved design call #1) — it doesn't disturb the existing
# "sources -> outline" auto-regenerate handoff, and the blueprint shapes lesson
# DRAFTING, not the outline the "outline" step edits.
STEPS = ("who", "duration", "scope", "structure", "sources", "outline", "confirm", "done")


class CurriculumInterview(Base, PkMixin, TimestampMixin):
    __tablename__ = "curriculum_interview"

    # One of STEPS above. Column stays a plain String (not a DB enum) per
    # this codebase's established "soft, relabelable" precedent for
    # small-cardinality status-like strings (mirrors `Block.kind`,
    # `GenerationJob.status`).
    step: Mapped[str] = mapped_column(String(20), default="who")
    # The one piece of context NOT asked step-by-step — see
    # `app.curriculum.interview.start_interview`'s docstring for why.
    title: Mapped[str] = mapped_column(String(400))
    # The "scope" step's free-text course brief. This is what `domain` should
    # always have been: not a 30-character enum-ish tag the model was told to
    # respect, but the tutor describing the course he wants — which reaches the
    # outline prompt AND every lesson-draft prompt.
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "library_only" | "general_knowledge" | "web" — the ceiling the tutor sets on
    # how far a module may stray from his own material when his material doesn't
    # cover it. Per-module tiers (assigned by the model reading the book) are
    # clamped to this.
    gap_policy: Mapped[str] = mapped_column(String(20), default="general_knowledge")
    # Accumulated per-step answers, keyed by step name.
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    # The generated (and then TUTOR-EDITED) outline — the thing he approves before
    # a single expensive lesson is drafted. Cached here so a refresh never re-runs
    # the 90K-token outline call.
    outline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Set once "confirm" is approved: the materialized course Block, and the job
    # drafting into it. Deliberately un-FK'd — see this module's docstring, point 3.
    root_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
