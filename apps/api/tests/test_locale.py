"""GREEK END-TO-END (Plan 13, Stage 5.1/5.3/5.4/5.5).

The UI has been bilingual since day one and the API never knew: `useLocale()`
was read by eight web components and every one of them used it to build an
href. Nothing carried it to the server. So the model chose the language, and
the app's DEFAULT locale is Greek — meaning the tutor's own default language
was the one nothing was written in.

Four things have to hold, and this file pins all four:

  1. `X-App-Locale` reaches the API, is normalized, never rejected, and
     defaults to `el` (`app.i18n`).
  2. The language rule reaches EVERY prompt builder — all five of them,
     including `artifacts/generate.py`, which had no locale at all. The rule
     is not just "answer in Greek": quote the ENGLISH source verbatim, and
     never translate a machine token (alphaTex, C/Am7/G7, Drop D, fret
     numbers, Tube Screamer).
  3. The locale is NOT the model's to choose: the tool schemas no longer offer
     it, and it is injected from the session at dispatch, at suspend, and
     again at resolve (where `edited_args` REPLACES `tool_args` wholesale).
  4. The lesson job actually carries it — the parameter that existed, was
     used by the prompt, and was never once supplied.
"""
import inspect
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.agent.loop as agent_loop
import app.artifacts.generate as artifacts_generate
import app.brain.retrieve as brain_retrieve
import app.curriculum.draft as curriculum_draft
import app.curriculum.outline as curriculum_outline
import app.curriculum.refine as curriculum_refine
import app.lessons.draft as lessons_draft
import app.routers.chat as chat_router
from app.agent.tools import LOCALE_ARG, TOOLS, with_locale
from app.artifacts.generate import _build_messages as build_artifact_messages
from app.brain.retrieve import Hit, build_grounded_messages
from app.curriculum.corpus import LibraryContext
# Stage 6 replaced curriculum/generate.py's THREE prompt builders (plan /
# grounded-module / general-module) with TWO: one over the whole library for the
# outline, one per lesson for the draft. `curriculum_draft.build_lesson_messages`
# is aliased because `lessons/draft.py` — a different feature, drafting a lesson
# from a Reader selection — already owns that name in this test module.
from app.curriculum.draft import LessonContext
from app.curriculum.draft import build_lesson_messages as build_curriculum_lesson_messages
from app.curriculum.outline import build_outline_messages
from app.curriculum.refine import build_refine_messages
from app.curriculum.shape import plan_shape
from app.db import Base, SessionLocal, engine
from app.i18n import (
    DEFAULT_LOCALE,
    LOCALE_HEADER,
    answer_in,
    language_directive,
    language_name,
    locale_dep,
    normalize_locale,
)
from app.lessons.draft import _build_messages as build_lesson_messages
from app.llm.tools_types import AssistantTurn, ToolCall
from app.main import app
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)


api = TestClient(app)


# ---------------------------------------------------------------------------
# 5.1 — the header, the normalizer, the default
# ---------------------------------------------------------------------------

def test_the_default_locale_is_greek_not_english():
    """The whole point. `apps/web/src/i18n/routing.ts` has `defaultLocale:
    "el"`; anything on this side still defaulting to "en" is a bug."""
    assert DEFAULT_LOCALE == "el"
    assert normalize_locale(None) == "el"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("el", "el"),
        ("en", "en"),
        ("el-GR", "el"),          # a browser-shaped tag
        ("en_US", "en"),          # underscore variant
        ("EL", "el"),             # case
        ("  el  ", "el"),         # whitespace
        ("", "el"),               # blank -> default
        (None, "el"),             # absent -> default
        ("zz", "el"),             # unknown -> default, NOT an error
        ("¯\\_(ツ)_/¯", "el"),     # garbage -> default, NOT an error
    ],
)
def test_normalize_locale_never_raises_and_always_lands_on_a_supported_locale(raw, expected):
    """A locale is a presentation preference, not user input to validate. A
    422 on a malformed header would turn a cosmetic mismatch into a broken
    page — see `app/i18n.py`'s docstring."""
    assert normalize_locale(raw) == expected


def test_locale_dep_is_just_the_normalizer_over_the_header():
    assert locale_dep("el-GR") == "el"
    assert locale_dep(None) == DEFAULT_LOCALE


def test_cors_preflight_allows_the_locale_header():
    """`X-App-Locale` is a CUSTOM header, so it is not a CORS "simple" header:
    if it isn't in `allow_headers`, EVERY browser call fails preflight and the
    API log shows nothing at all. `main.py` names it explicitly rather than
    relying on Starlette's `*` echo, so that this can be asserted."""
    r = api.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": LOCALE_HEADER,
        },
    )
    assert r.status_code == 200, r.text
    allowed = r.headers["access-control-allow-headers"].lower()
    assert LOCALE_HEADER.lower() in allowed


# ---------------------------------------------------------------------------
# 5.3 — the language rule, and its presence in ALL FIVE prompt builders
# ---------------------------------------------------------------------------

def test_the_directive_says_answer_in_greek_but_quote_the_english_source_verbatim():
    """The subtle requirement: the tutor writes Greek, his library is an
    ENGLISH book. Translating a quotation destroys the one property the whole
    grounding stack exists for — that a cited passage can be checked against
    the page scan it came from."""
    directive = language_directive("el")
    assert "Greek" in directive
    assert "ENGLISH" in directive
    assert "verbatim" in directive
    assert "never translate a quotation" in directive.lower()


def test_the_directive_exempts_machine_tokens_from_translation():
    """A model told "write in Greek" WILL otherwise transliterate a chord name
    or a pedal. AlphaTab throws on a translated alphaTex, and «Σωλήνας
    Ουρλιαχτού» is not a Tube Screamer."""
    directive = language_directive("el")
    for token in ("alphaTex", "Am7", "Drop D", "Tube Screamer", "fret"):
        assert token in directive


def test_language_name_is_a_name_not_a_code():
    # Models obey a language NAME far more reliably than a 2-letter code.
    assert language_name("el") == "Greek"
    assert language_name("en") == "English"
    assert answer_in("el") == "Answer in Greek (el)."


# Every module that builds a prompt. `artifacts/generate.py` is the one that had
# NO locale at all — which is why a Greek tutor's tab/gear/tone specs came back in
# English. `curriculum/generate.py` is NOT here any more and that is correct: it
# no longer builds prompts, it composes `outline` and `draft`, which do.
_BUILDER_MODULES = (
    brain_retrieve,
    lessons_draft,
    curriculum_outline,
    curriculum_draft,
    curriculum_refine,
    artifacts_generate,
)


_SHAPE = plan_shape(20, 1, 50)
_EMPTY_LIBRARY = LibraryContext(text="", token_count=0, fits=True)


def _lesson_ctx() -> LessonContext:
    return LessonContext(
        lesson_title="Power chords", lesson_objective="play a power chord cleanly",
        module_title="Tone", module_objective="learn tone", course_title="Blues",
        tier="library", position="lesson 1 of 4 in module 1 of 5",
        minutes=50, teaching_minutes=40, target_words=2200, floor_words=1760,
    )


@pytest.mark.parametrize("module", _BUILDER_MODULES, ids=lambda m: m.__name__)
def test_every_prompt_building_module_actually_calls_language_directive(module):
    """The grep-style assertion the plan asks for: a builder that merely
    *accepts* a locale and never puts it in the prompt is exactly the bug this
    stage exists to fix (`lessons/draft.py`'s `language` was a live example —
    used by the prompt, never supplied by anyone)."""
    source = inspect.getsource(module)
    assert "language_directive(" in source, f"{module.__name__} does not use the language rule"


@pytest.mark.parametrize("locale", ["el", "en"])
def test_every_builder_carries_the_directive_and_curriculum_keeps_it_out_of_the_prefix(locale):
    """Every prompt builder must carry `language_directive` SOMEWHERE — and for
    the two full-context curriculum builders, that somewhere must NOT be the
    system message: the system message is part of the shared cached prefix
    (`corpus.CURRICULUM_SYSTEM`), and a per-locale/per-lesson system string is
    exactly the silent cache-miss-per-variant bug the prefix refactor removed.
    The directive rides the volatile tail instead (which is also the strongest
    position — recency — with a 90K-token book in the middle)."""
    directive = language_directive(locale)
    hits = [
        Hit(chunk_id="c1", source_id="s1", source_title="Book", text="A tube amp breaks up.",
            section_path=None, page=12, score=0.9),
    ]

    # Non-curriculum builders: the directive still lives in the system prompt.
    systems = [
        build_grounded_messages("τι είναι το overdrive;", hits, locale=locale)[0]["content"],
        build_lesson_messages(
            text="Open position chords.", page_from=1, page_to=1,
            source_title="Book", language=locale,
        )[0]["content"],
        build_refine_messages(
            instruction="more detail about the Amp", title="Tone", body="A tube amp breaks up.",
            kind="segment", language=locale,
        )[0]["content"],
        build_artifact_messages(kind="tab", prompt="an E minor lick", hits=[], locale=locale)[0]["content"],
    ]
    for system in systems:
        assert directive in system

    # Full-context curriculum builders: directive in the tail, NEVER the system.
    for messages in (
        build_outline_messages(
            title="Blues", brief=None, language=locale, shape=_SHAPE,
            library=_EMPTY_LIBRARY, student_brief=None, gap_policy="general_knowledge",
        ),
        build_curriculum_lesson_messages(
            ctx=_lesson_ctx(), library=_EMPTY_LIBRARY, language=locale,
            student_brief=None, course_brief=None,
        ),
    ):
        assert directive not in messages[0]["content"], (
            "a locale-dependent system message re-breaks the shared cache prefix"
        )
        assert directive in messages[-1]["content"]


@pytest.mark.parametrize("locale", ["el", "en"])
def test_the_language_reminder_is_the_LAST_thing_before_generation(locale):
    """Recency beats the system prompt: when several thousand tokens of ENGLISH
    passages sit immediately before the generation point, a language
    instruction up in the system prompt is the least recent thing the model
    saw — and it answers in the language it just finished reading. Every
    builder that embeds source text therefore ends its USER message with
    `answer_in`."""
    tail = answer_in(locale)
    hits = [
        Hit(chunk_id="c1", source_id="s1", source_title="Book", text="A tube amp breaks up.",
            section_path=None, page=12, score=0.9),
    ]

    class _Passage:
        text = "A tube amp breaks up."

    users = [
        build_grounded_messages("τι είναι το overdrive;", hits, locale=locale)[1]["content"],
        build_lesson_messages(
            text="Open position chords.", page_from=1, page_to=1,
            source_title="Book", language=locale,
        )[1]["content"],
        build_outline_messages(
            title="Blues", brief=None, language=locale, shape=_SHAPE,
            library=_EMPTY_LIBRARY, student_brief=None, gap_policy="general_knowledge",
        )[-1]["content"],
        build_curriculum_lesson_messages(
            ctx=_lesson_ctx(), library=_EMPTY_LIBRARY, language=locale,
            student_brief=None, course_brief=None,
        )[-1]["content"],
        build_refine_messages(
            instruction="more detail about the Amp", title="Tone", body="A tube amp breaks up.",
            kind="segment", language=locale,
        )[-1]["content"],
        build_artifact_messages(
            kind="tone_recipe", prompt="a warm blues tone", hits=hits, locale=locale,
        )[1]["content"],
    ]
    for user in users:
        assert user.rstrip().endswith(tail), user[-120:]


def test_the_agent_system_prompt_carries_the_language_block():
    """`SYSTEM_PROMPT` itself stays a byte-identical constant (prefix caching,
    agentic-gotchas #8) — the per-session LANGUAGE block is APPENDED to it by
    the loop, which is why the prompt's own new sentence points AT that block
    rather than naming a language."""
    from app.agent.prompts import SYSTEM_PROMPT

    assert "LANGUAGE block" in SYSTEM_PROMPT
    built = agent_loop._ensure_system_prompt([{"role": "user", "content": "hi"}], "el")
    assert built[0]["content"] == f"{SYSTEM_PROMPT}\n\n{language_directive('el')}"


@pytest.mark.parametrize("locale", ["el", "en"])
def test_the_grounding_block_ends_with_the_language_reminder(locale):
    # Both branches: with hits, and the "your library has nothing" branch.
    hits = [
        Hit(chunk_id="c1", source_id="s1", source_title="Book", text="A tube amp breaks up.",
            section_path=None, page=12, score=0.9),
    ]
    assert agent_loop._grounding_block(hits, locale).endswith(answer_in(locale))
    assert agent_loop._grounding_block([], locale).endswith(answer_in(locale))


# ---------------------------------------------------------------------------
# 5.4 — the locale stops being model-chosen
# ---------------------------------------------------------------------------

def _tool_params(name: str) -> dict:
    return TOOLS[name].schema["function"]["parameters"]


@pytest.mark.parametrize("tool_name", sorted(LOCALE_ARG))
def test_no_tool_offers_the_model_a_language_parameter_anymore(tool_name):
    """`explain_concept.locale` (default "en"), `draft_lesson_from_selection.
    language` and — worst — `generate_curriculum.language`, a REQUIRED,
    model-chosen parameter. The model cannot supply what it isn't offered."""
    params = _tool_params(tool_name)
    assert "locale" not in params["properties"]
    assert "language" not in params["properties"]
    assert "language" not in params.get("required", [])
    assert "locale" not in params.get("required", [])


def test_with_locale_injects_the_session_language_and_overrides_a_stale_one():
    injected = with_locale("generate_curriculum", {"title": "Blues"}, "el")
    assert injected == {"title": "Blues", "language": "el"}

    # OVERRIDES, not "fills in if missing": a stale `ApprovalRequest.tool_args`
    # row, or a tutor hand-editing the approval card's JSON, must not outrank
    # the session.
    assert with_locale("explain_concept", {"query": "x", "locale": "en"}, "el")["locale"] == "el"

    # A tool with no language parameter is passed through untouched — but as a
    # COPY: `loop.py` and the chat router both hand this on, and the source
    # dict is a frozen ToolCall's / a plain sa.JSON column's value.
    args = {"session_id": "s1", "session_minutes": 30}
    out = with_locale("split_session", args, "el")
    assert out == args and out is not args


class _FakeProvider:
    """Mirrors `test_agent_hitl.py`'s: scripted turns, repeating the last."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls: list[list[dict]] = []

    def chat_tools(self, messages, tools, *, tool_choice="auto", temperature=0.3):
        self.calls.append([dict(m) for m in messages])
        return self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]


def test_a_suspended_mutation_carries_the_session_locale_in_its_args(monkeypatch):
    """At SUSPEND, not only at dispatch: the approval card the tutor reads, the
    wire transcript the model sees next turn, and the args that will eventually
    be dispatched are all THE SAME dict — so they cannot disagree about the
    language."""
    call = ToolCall(
        id="call_1", name="generate_curriculum",
        arguments={"title": "Blues", "profile": {"level": "beginner"}, "language": "en"},
    )
    monkeypatch.setattr(
        agent_loop, "get_provider",
        lambda: _FakeProvider([AssistantTurn(content="Sure.", tool_calls=[call])]),
    )

    result = agent_loop.run_agent_turn(
        None, [{"role": "user", "content": "φτιάξε μου ένα πρόγραμμα"}], locale="el",
    )

    assert result.status == "awaiting_approval"
    # The model said "en". The session says "el". The session wins.
    assert result.pending_tool["arguments"]["language"] == "el"

    # ...and the wire message the transcript persists says "el" too.
    wire_call = result.messages[-1]["tool_calls"][0]["function"]["arguments"]
    assert '"language": "el"' in wire_call


def test_an_inline_read_is_dispatched_with_the_session_locale(monkeypatch):
    captured = {}

    def _fake_explain(db, **kwargs):
        captured.update(kwargs)
        return {"text": "…", "citations": []}

    original = TOOLS["explain_concept"]
    monkeypatch.setitem(
        TOOLS, "explain_concept",
        type(original)(schema=original.schema, fn=_fake_explain, kind="read"),
    )
    monkeypatch.setattr(agent_loop, "search", lambda db, q, k=5: [])
    monkeypatch.setattr(
        agent_loop, "get_provider",
        lambda: _FakeProvider([
            AssistantTurn(
                content=None,
                tool_calls=[ToolCall(id="call_1", name="explain_concept",
                                     arguments={"query": "τι είναι το power chord;"})],
            ),
            AssistantTurn(content="Ένα power chord είναι…", tool_calls=[]),
        ]),
    )

    agent_loop.run_agent_turn(
        None, [{"role": "user", "content": "τι είναι το power chord;"}], locale="el",
    )

    assert captured["locale"] == "el"


def test_resolve_reinjects_the_locale_even_when_edited_args_drop_it(monkeypatch):
    """`edited_args` REPLACES `tool_args` wholesale. A tutor who opens the
    approval card, edits the JSON and simply doesn't retype `language` would
    otherwise hand `run_curriculum_job` a params dict with no language in it —
    a `TypeError` inside a background task, i.e. a job that just says
    "failed"."""
    monkeypatch.setattr(chat_router, "run_curriculum_job", lambda job_id: None)

    call = ToolCall(
        id="call_1", name="generate_curriculum",
        arguments={"title": "Blues", "profile": {"level": "beginner"}},
    )
    monkeypatch.setattr(
        agent_loop, "get_provider",
        lambda: _FakeProvider([AssistantTurn(content="Sure.", tool_calls=[call])]),
    )

    session_id = api.post("/chat", json={"student_id": None, "locale": "el"}).json()["session_id"]
    propose = api.post(f"/chat/{session_id}/messages", json={"content": "φτιάξε πρόγραμμα"})
    approval_id = propose.json()["approval_id"]

    r = api.post(
        f"/chat/{session_id}/approvals/{approval_id}/resolve",
        json={
            "decision": "approve",
            # The tutor retyped the args and dropped `language` entirely.
            "edited_args": {"title": "Μπλουζ", "profile": {"level": "beginner"}},
        },
    )
    assert r.status_code == 200, r.text
    job_id = uuid.UUID(r.json()["job_id"])

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        assert job.params["title"] == "Μπλουζ"      # his edit survived
        assert job.params["language"] == "el"       # and the locale came back
    finally:
        db.close()


def test_a_session_created_with_a_browser_shaped_locale_is_normalized(monkeypatch):
    session_id = api.post("/chat", json={"student_id": None, "locale": "el-GR"}).json()["session_id"]
    listed = api.get("/chat").json()
    # The session has no messages yet, so it is (correctly) not in the sidebar
    # list — read the column directly instead.
    assert listed == []
    db = SessionLocal()
    try:
        from app.models.chat import ChatSession
        assert db.get(ChatSession, uuid.UUID(session_id)).locale == "el"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5.5 — the dead parameter: the lesson job must actually carry the language
# ---------------------------------------------------------------------------

def _a_source(db) -> KnowledgeSource:
    src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds", status="ready")
    db.add(src)
    db.commit()
    return src


def test_the_enqueued_lesson_job_carries_the_ui_locale(client, db, monkeypatch):
    """THE dead-parameter bug (Plan 13, Stage 5.5): `draft_lesson_from_
    selection` took a `language`, its prompt used it — and `SelectionIn` had no
    field for it, so `run_lesson_job` read `params.get("language", "en")` and
    got "en" EVERY time. A Greek tutor selecting Greek text got an English
    lesson."""
    monkeypatch.setattr("app.routers.lessons.run_lesson_job", lambda job_id: None)
    src = _a_source(db)

    r = client.post(
        "/lessons/from-selection",
        json={"source_id": str(src.id), "page_no": 47, "text": "The Tube Screamer..."},
        headers={LOCALE_HEADER: "el"},
    )

    assert r.status_code == 202
    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert job.params["language"] == "el"


def test_the_lesson_job_honours_an_english_ui_too(client, db, monkeypatch):
    monkeypatch.setattr("app.routers.lessons.run_lesson_job", lambda job_id: None)
    src = _a_source(db)

    r = client.post(
        "/lessons/from-selection",
        json={"source_id": str(src.id), "page_no": 47, "text": "The Tube Screamer..."},
        headers={LOCALE_HEADER: "en-US"},
    )

    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert job.params["language"] == "en"


def test_a_request_with_no_locale_header_still_gets_a_greek_lesson(client, db, monkeypatch):
    """No header at all (an old client, curl, a bug in the web build) must not
    silently revert to English — the app's default is Greek."""
    monkeypatch.setattr("app.routers.lessons.run_lesson_job", lambda job_id: None)
    src = _a_source(db)

    r = client.post(
        "/lessons/from-selection",
        json={"source_id": str(src.id), "page_no": 47, "text": "The Tube Screamer..."},
    )

    job = db.get(GenerationJob, uuid.UUID(r.json()["job_id"]))
    assert job.params["language"] == "el"


def test_the_lesson_prompt_actually_receives_that_language(monkeypatch):
    """End of the chain: the runner's `language` reaches the model's prompt."""
    system = build_lesson_messages(
        text="Open position chords.", page_from=1, page_to=1,
        source_title="Book", language="el",
    )[0]["content"]
    assert language_directive("el") in system


def test_the_artifact_generator_takes_a_locale_at_the_http_boundary(client, db, monkeypatch):
    """`artifacts/generate.py` had NO locale at all — Greek artifacts came out
    English. The endpoint now reads `X-App-Locale` and threads it into the
    prompt."""
    captured = {}

    def _fake_provider():
        class _P:
            def guided_json(self, messages, schema):
                captured["system"] = messages[0]["content"]
                return {
                    "name": "G", "frets": [3, 2, 0, 0, 0, 3],
                    "fingers": [3, 2, 0, 0, 0, 4],
                }
        return _P()

    monkeypatch.setattr(artifacts_generate, "get_provider", _fake_provider)

    r = client.post(
        "/artifacts/generate",
        json={"kind": "chord_diagram", "prompt": "G major open chord", "ground": False},
        headers={LOCALE_HEADER: "el"},
    )
    assert r.status_code == 200, r.text
    assert language_directive("el") in captured["system"]
