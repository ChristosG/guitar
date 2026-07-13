"""Pydantic request/response models for the curriculum-authoring interview
API (`routers/curriculum.py`'s `/curricula/interview...` routes, Plan 12
Task 3 / G2).

Kept separate from `app.models.interview.CurriculumInterview` per this
codebase's established model/schema split (mirrors `schemas/curriculum.py`
vs `app.models.block`).
"""
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class InterviewStartRequest(BaseModel):
    """`POST /curricula/interview`'s body — the one piece of context the
    interview does NOT ask about step-by-step (see
    `app.curriculum.interview.start_interview`'s docstring).
    """
    title: str
    domain: str | None = None


class InterviewAnswerRequest(BaseModel):
    """`POST /curricula/interview/{id}/answer`'s body. `answer`'s shape is
    STEP-DEPENDENT (a plain dict keyed per step — see
    `app.curriculum.interview`'s own per-step validators for the exact
    shape each step expects) rather than a rigid, step-specific schema: the
    interview's state machine lives in code, and each step's own validator
    is what decides "valid" vs. "re-ask", not FastAPI/pydantic request
    validation. `Any` (defaulting to `None`) is deliberate here — a
    genuinely missing/blank/malformed answer must reach the validator and
    be re-asked, not be rejected as a 422 before the state machine even
    sees it (brief: "a bad/blank answer RE-ASKS... it must never crash").
    """
    answer: Any = None


class InterviewStateOut(BaseModel):
    """The envelope every interview route returns (mirrors the brief's
    literal `{interview_id, step, question, options?, findings?}` shape),
    plus an optional `error` — attached only when the previous answer was
    invalid and this response is re-asking the same question.
    """
    interview_id: UUID
    step: str
    question: str
    options: list[dict] | None = None
    findings: dict | None = None
    error: str | None = None
