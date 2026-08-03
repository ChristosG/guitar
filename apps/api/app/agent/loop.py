"""The ReAct tool-calling loop: call `chat_tools` -> dispatch any tool_calls
-> feed results back -> repeat. Mirrors `/mnt/nvme2TB/vllm_interract/
examples/tool_calling_minimal.py` exactly (the brief's own named reference
shape: call -> append the assistant turn WITH tool_calls -> if none, done,
else dispatch each + append a `{"role":"tool", "tool_call_id",...}` per call
-> repeat under a step cap), plus the guards from `/mnt/nvme2TB/
vllm_interract/reference/agentic-gotchas.md`:
  - #4 hallucinated/unknown tool names never crash the loop — guarded into
    an `"ERROR: unknown tool"` tool-result instead.
  - #5 malformed tool-call JSON (`ToolArgsError`) gets a BOUNDED repair
    re-prompt, not an unbounded retry, and the loop itself is always capped
    (`max_steps`).
  - #8 keep the system+tools prefix stable — `SYSTEM_PROMPT` (a plain
    top-level constant in `prompts.py`) and `_tool_schemas()` (deterministic,
    same list every call within a process) never interpolate per-request data.

Plan 11 Task 1 (C1/C2 — "forced retrieval, not requested") adds a pre-hop
BEFORE the main loop below ever calls the model: on a fresh, CONTENT-BEARING
user turn (see `_is_content_bearing`'s own docstring for the exact rule and
its defence), `run_agent_turn` itself calls `app.brain.retrieve.search` —
not a tool the model may or may not decide to invoke — and appends the hits
as a `GROUNDING` block (`_grounding_block`) onto the END of that SAME user
turn's own content, before the first `chat_tools` call (NOT a second system
message — the live vLLM chat template 400s on a system message that isn't
the very first one, and a mid-transcript system message would defeat
agentic-gotchas.md #8's prefix-caching invariant regardless; see
`_grounding_block`'s own docstring). The model never gets a turn where it
could have skipped retrieval. Every hit is also surfaced as a citation
(`_to_citation`) on `AgentResult.citations`, persisted onto the `Message` row
by `app/routers/chat.py` via `app.agent.transcript.persist_new_messages` —
this is what lets the UI show a citation chip back to a real page. A turn
with NO hits (none that cleared `retrieve.search`'s floor) still gets a GROUNDING
block — one that instructs the model to say his library doesn't cover this
and label the rest of the answer as general knowledge, rather than silently
falling back to unlabelled pretrained knowledge. This pre-hop runs BEFORE
the mutation-suspend logic below and does not touch it in any way: it only
ever rewrites the trailing user message's own content before the loop
starts, and only ever adds `citations` to the `AgentResult` this function
already returns from every branch — C6 (the approval gate) is unchanged.

Plan 5 Task 2 built this loop dispatching ONLY "read" tools inline (the
`TOOLS` registry was reads-only). THIS task (3) adds "mutation" entries into
that same registry (`app/agent/tools.py`) and teaches this loop to SUSPEND
instead of executing them:

  - `_tool_schemas()` now exposes BOTH kinds to the model (it needs to see
    mutation tools to ever propose one).
  - READ `ToolCall`s still dispatch inline, unchanged, same turn.
  - The FIRST `ToolCall` in a turn whose registry `kind == "mutation"` is
    NEVER dispatched — `run_agent_turn` returns `AgentResult(status=
    "awaiting_approval", pending_tool={tool_call_id, name, arguments})`
    instead, stopping the loop right there.
  - This loop stays SESSION-AGNOSTIC: it does not persist an
    `ApprovalRequest` or a `Message` itself (Task 4's chat router owns that —
    it persists the suspend info this returns, and later resumes this same
    loop after a human decision). See `_dispatch_read_call`/
    `_first_mutation_index`/the dispatch loop below for exactly how a
    per-turn mix of reads and (at most one visible) mutation is handled
    without ever leaving more than one tool_call unanswered in `messages`
    (protocol integrity — see the dispatch loop's own comment).

Plan 12 Task 5 (G5) adds a PRE-model short-circuit, same shape as C1's
forced-retrieval pre-hop: on a fresh user turn, if
`app.agent.guards.looks_like_named_song_request` trips (Chris's OTHER live
bug — asked for the "Smells Like Teen Spirit" riff, got a real
`generate_artifact` tab back with one note repeated seven times, because the
model cannot actually recall a specific song's recording and invents
instead), the turn is answered with the session-locale decline constant
(`guards.named_song_decline_message`) and the model is NEVER called. This has to happen before the model sees the request
at all — there is no reliable way to make the model itself decline, since it
is the very thing that fabricates when asked. See `guards.py`'s own
docstring for the full "why not a hardcoded song list" detection rationale
and its false-positive analysis.

Task 8 (same plan) reordered this against C1: G5 now runs AFTER the C1
grounding pre-hop below, not before, and only declines when that search came
back with NO hits. The tutor complained — correctly — that a transcription
from a book he OWNS, on a real page, was being refused unread because the
decline used to fire before his library was ever searched. The guard itself
is right to exist (it is anti-hallucination, not copyright enforcement — its
own message leads with "I don't actually have it memorized"), so it was
reordered, not removed: search first, decline only on a genuine miss.

Plan 11 Task 2 (C3) adds a POST-TURN guard on top of all of the above: when a
turn ends with a plain answer (no tool_calls), and that answer's `content`
trips `app.agent.guards.looks_like_tablature` (a free-typed ASCII tab —
Chris's exact bug, see that module's own docstring), the bluff is NEVER
appended to `messages` and NEVER returned as `content`. The loop re-prompts
the model ONCE with a corrective user turn naming the tool it should have
called (`_TAB_BLUFF_REPROMPT_MESSAGE`), bounded by `_MAX_TAB_BLUFF_ATTEMPTS`
the same "bounded, not unbounded" way `_MAX_REPAIR_ATTEMPTS` bounds the
ToolArgsError repair loop above; if the model bluffs again anyway, an honest
fallback (`_TAB_BLUFF_FALLBACK_MESSAGE`) is substituted instead — see the
comment above those constants for why re-prompting (not auto-proposing a
`generate_artifact` call the guard itself would have to guess the args for)
is the right recovery. This is entirely independent of the mutation-suspend
machinery above: it only ever fires on the "no tool_calls" branch, so it
cannot interact with — and does not touch — the HITL suspend path at all.
"""
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Iterator

from app.agent.guards import (
    looks_like_named_song_request,
    looks_like_tablature,
    named_song_decline_message,
    strip_curriculum_context,
)
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS, with_locale
from app.brain.retrieve import search
from app.i18n import DEFAULT_LOCALE, answer_in, language_directive
from app.llm.errors import ToolArgsError
from app.llm.factory import get_provider
from app.llm.tools_types import AssistantTurn, ToolCall
from app.prompts.overrides import resolve
from app.text.normalize import fold, has_greek

log = logging.getLogger(__name__)

# "Consecutive" (agentic-gotchas.md #5's "cap it" warning) — a successful
# `chat_tools` call resets this streak (see the reset right after the
# try/except below), so this bounds a run of BACK-TO-BACK malformed-JSON
# failures, not a lifetime total across an otherwise-healthy turn.
_MAX_REPAIR_ATTEMPTS = 2

_REPAIR_MESSAGE = "Your previous tool call had invalid JSON arguments. Retry with valid JSON."
_GIVEUP_MESSAGE = "Sorry, I couldn't complete that request. Could you rephrase it?"
_MAX_STEPS_MESSAGE = "I couldn't finish that within the allotted steps. Could you try rephrasing or narrowing the request?"

# --- C3: no free-typed tablature ------------------------------------------
#
# Chris's exact bug: asked for a G major scale tab, the model typed ASCII
# into a code fence (also WRONG — see `app/agent/guards.py`'s docstring)
# instead of calling `generate_artifact`. `looks_like_tablature` (imported
# above) detects that shape; this bounded counter/message pair is this
# loop's RECOVERY once it does — same "bounded, not unbounded" shape as
# `_MAX_REPAIR_ATTEMPTS`'s ToolArgsError repair above, deliberately reusing
# that precedent rather than inventing a new retry idiom.
#
# RECOVERY CHOICE (the brief asks for this to be defended): option (a) —
# suppress the bluff and re-prompt the model ONCE, telling it plainly what
# it did wrong and which tool to call instead — with option (b) — an honest
# "I'll generate that properly" fallback — as the bound's fallback if the
# model bluffs again anyway. Rejected (c) "auto-propose a generate_artifact
# call": the loop has no reliable way to synthesize a correct `kind`/
# `prompt` from a bluff's prose (a mis-guessed kind/prompt would show the
# tutor an HITL approval card for a request he didn't actually make, which
# is worse than an honest "let me redo that" — the approval card is meant to
# gate a call the MODEL chose, not one this guard invented on its behalf).
# Re-prompting instead gives the model a real chance to make the right call
# itself (this is exactly what the live-model check in the task report
# verifies actually happens), and the bounded fallback (b) still guarantees
# the invariant even in the worst case: the bluff NEVER reaches
# `AgentResult.content` or `AgentResult.messages`, no matter how many times
# the model keeps bluffing.
_MAX_TAB_BLUFF_ATTEMPTS = 1
_TAB_BLUFF_REPROMPT_MESSAGE = (
    "You just wrote tablature/a chord diagram as plain text instead of "
    "calling generate_artifact. Never do that — call generate_artifact now "
    "so it renders as a real, playable artifact instead of typed ASCII."
)
_TAB_BLUFF_FALLBACK_MESSAGE = (
    "Let me generate that properly as a real, playable artifact instead of "
    "typing it out — please ask again and I'll call the right tool."
)


# ---------------------------------------------------------------------------
# C1: forced retrieval pre-hop — the content-bearing rule
# ---------------------------------------------------------------------------
#
# THE RULE: a turn forces a library search only when it plausibly asks a
# guitar technique/theory/gear/tone QUESTION. Two categories are excluded
# even though neither is small talk:
#   1. small talk / pleasantries ("hi", "hello", "thanks") — nothing to
#      look up.
#   2. an INSTRUCTION about an entity already in the tutor's OWN data (a
#      curriculum, lesson, session, or a request for a generated artifact/
#      tab/diagram/scale — plus the student/note/progress nouns, whose
#      tools and screens the desktop build removed but which the regex
#      still matches on purpose; see `_ENTITY_OR_ARTIFACT_RE`'s own comment
#      below) — these are resolved by a read/mutation tool
#      (`app/agent/tools.py`) or by nothing at all, never by a
#      library search: "split session 2 of that lesson" is about session #2
#      of one specific row, not a question his book could ever answer, and
#      forcing a search on it would inject irrelevant "grounding" noise into
#      a command turn for no benefit.
#
# DEFENCE: kept a plain regex/keyword rule — deliberately NOT an LLM call.
# The entire point of C1 is removing an unreliable judgment call FROM the
# model ("the model routinely declines to retrieve"); asking a model
# (possibly the very same one) to classify the turn first would just move
# that same unreliability one hop earlier and re-introduce exactly the
# failure mode this task exists to remove. A keyword rule is auditable,
# deterministic, and it is the SAME split `SYSTEM_PROMPT` itself draws
# ("curricula, lessons, sessions, artifacts" — the tool-routed topics — vs
# "any guitar technique/theory/gear/tone question"), just enforced in code
# instead of requested in prose. Not a byte-for-byte mirror any more: the
# desktop build dropped students/notes/progress from the prompt along with
# their tools, while this regex deliberately KEEPS those nouns — a turn
# about them is still a bookkeeping turn a library search cannot help, so
# matching them keeps suppressing pointless searches (see
# `_ENTITY_OR_ARTIFACT_RE`'s own comment). See `tests/test_agent_grounding.py`'s "TEST BOTH
# SIDES" section for the concrete cases this is pinned against (small talk,
# entity instructions, and genuine content questions).
#
# PLAN 13, TASK 5.2 — THE RULE WAS ENGLISH-ONLY, AND THE APP'S DEFAULT LOCALE
# IS GREEK (`apps/web/src/i18n/routing.ts`: `defaultLocale: "el"`).
#
# `_QUESTION_RE` matched `?` and the English wh-words. A GREEK QUESTION ENDS IN
# `;`, not `?` — and begins «Τι», not "what". So `_is_content_bearing` returned
# False for every question the tutor actually asks, the forced-retrieval pre-hop
# never fired, and every Greek content question was answered from the model's
# general knowledge with NO library search at all. This is not a hypothetical:
# it is the mechanism behind Chris's report that the app "just uses the llm
# general knowledge", and it has been live since `el` became the default.
#
# Two Greek-specific traps, both handled by folding the text first
# (`app/text/normalize.py::fold`) and writing the patterns unaccented:
#
#   - THE ACCENT MOVES UNDER INFLECTION. μάθημα -> μαθήματα (the tonos jumps
#     from the alpha to the eta). A pattern written `μάθημ` matches the singular
#     and misses the plural. Folding strips accents, so `μαθημ` matches both.
#   - THERE ARE TWO QUESTION MARKS. The Greek question mark is U+037E (·;·), but
#     nearly every keyboard emits the ASCII semicolon U+003B instead. Both must
#     match, and a bare `;` in Latin text is a statement separator — which is
#     why the `;` alternative is gated on the text actually containing Greek.
#
# THE GREEK LISTS ARE EXACT MIRRORS OF THE ENGLISH ONES — no more, no less. It
# is tempting to "improve" them (the English `scales?` exclusion is arguably
# over-broad: "what is the pentatonic scale?" is a real library question that it
# suppresses). Resist it here. A divergent rule means the app behaves
# differently depending on which language the tutor typed in, which is a far
# worse bug than a shared over-broadness — and `tests/test_greek_grounding.py`
# pins the mirror by asserting EN and EL give the SAME answer for translated
# pairs. Fix the over-broadness once, for both, or not at all.
_SMALL_TALK_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|thanks|thank you|ok|okay|cool|bye|goodbye|"
    r"good\s+(morning|afternoon|evening|night)|how'?s?\s+it\s+going|"
    r"how\s+are\s+you|what'?s\s+up"
    # Greek mirror (folded: unaccented, lowercase, final-sigma unified)
    r"|γεια|καλημερα|καλησπερα|καληνυχτα|ευχαριστω|ενταξει|τι κανεις|"
    r"αντιο|χαιρετω)\b",
    re.IGNORECASE,
)

# Nouns naming the tutor's OWN data or a structured artifact request
# (tab/chord diagram/scale). Curricula/lessons/sessions/artifacts are
# exactly the domain `SYSTEM_PROMPT` routes to a read/mutation tool rather
# than a knowledge question about the library. The student/note/progress
# stems STAY in this list even though their tools and screens are gone
# (the desktop build removed them, and `SYSTEM_PROMPT` no longer names
# them): a turn mentioning them is still about the tutor's own bookkeeping
# — nothing his books could answer — so matching them here keeps
# suppressing a pointless, noise-injecting library search on such turns.
# Suppression is cheap and correct; only DROPPING real content questions
# would be a bug.
#
# The Greek stems are truncated before the inflectional ending on purpose
# (`μαθητ` covers μαθητής/μαθητή/μαθητές/μαθητών/μαθήτρια; `κλιμακ` covers
# κλίμακα/κλίμακες/κλιμάκων), which is what makes a stem-list work at all
# against an inflected language. `\b` is unreliable at a Greek word boundary
# after folding, so these are matched as stems with a trailing `\w*`.
_ENTITY_OR_ARTIFACT_RE = re.compile(
    r"\b(students?|curricul(?:um|a)\w*|lessons?|sessions?|notes?|progress|"
    r"artifacts?|chord\s+diagrams?|diagrams?|tabs?|scales?)\b"
    # Greek mirror, stem-matched (folded)
    r"|(μαθητ\w*|μαθητρι\w*|προγραμμ\w*\s+σπουδων|curriculum|μαθημ\w*|"
    r"συνεδρι\w*|σημειωσ\w*|σημειωσε\w*|προοδ\w*|"
    r"ταμπλατουρ\w*|διαγραμμ\w*|κλιμακ\w*)",
    re.IGNORECASE,
)

# A positive signal that the turn is actually ASKING something, not issuing
# a bare command ("do three things", "split session 2") — a literal "?", or
# a leading wh-question word. Deliberately excludes bare modal starters
# ("do", "can", "is", ...): those are just as common as imperative-sentence
# openers ("do three things") as they are real questions, so including them
# produced false positives on exactly the entity-instruction turns category
# 2 above exists to exclude.
#
# Greek: the wh-words (folded, so «Πώς» -> `πως` and «Γιατί» -> `γιατι`), plus
# the two question marks. `;` (U+003B) and `;` (U+037E) are both "the Greek
# question mark" in practice — see the block comment above.
_QUESTION_RE = re.compile(
    r"\?|^\s*(what|why|how|when|where|which)\b"
    r"|^\s*(τι|γιατι|πως|ποτε|που|ποιος|ποια|ποιο|ποιες|ποιοι|ποσο|ποσα)\b",
    re.IGNORECASE,
)

# The Greek question mark, in both the forms a keyboard actually produces.
# Gated on the text containing Greek: a bare `;` in Latin text is a statement
# separator, not a question ("do this; then that").
_GREEK_QUESTION_MARK_RE = re.compile(r"[;;]")

# THIS MODULE NO LONGER OWNS A RELEVANCE FLOOR (Plan 13, Stage 4.4). It used to:
# `_RELEVANCE_FLOOR = 0.15`, applied to `search()`'s hits right here. It was the
# THIRD floor in the codebase (`curriculum/ground.py` had two more), it disagreed
# with both, and — being a raw cosine threshold — it would have silently become
# meaningless the moment `Hit.score` turned into an RRF fusion score that tops out
# near 0.033. All three moved into `app.brain.retrieve.search`, which is the only
# place that knows which embedding model produced the numbers. `search()`'s
# results are now used as given; nothing below re-filters them.


def _is_content_bearing(text: str) -> bool:
    """Should this turn search the library first? **Default: YES.**

    THE DEFAULT WAS INVERTED (Plan 13). This function used to require a POSITIVE
    match — a `?` or a leading wh-word — before it would ground anything. That
    is a "prove you are a question" rule, and it fails open: anything the
    keyword list doesn't recognise is silently answered from the model's memory
    with no library search at all.

    Which is exactly what happened. A Greek question ends in `;` and begins «Τι»,
    so the rule recognised none of them, and EVERY question the tutor asked in
    his own default language was answered ungrounded. But translating the
    keyword list into Greek only moves the problem: no keyword list reliably
    classifies natural language, and the next phrasing it doesn't know fails the
    same silent way. Chris's read was correct — patching the pattern was fixing
    the symptom.

    The rule that existed at all is a QWEN-ERA ARTEFACT. The forced-retrieval
    pre-hop was built because the 9B model kept declining to call the search tool
    (see this module's C1 notes), so the code took the decision away from it. The
    pre-hop is still worth keeping — "always search his library before answering
    a content question" is a product promise, not a model workaround — but the
    GATE in front of it no longer needs to be clever, because the model behind it
    no longer needs to be tricked.

    So the gate is now a cheap NEGATIVE filter: ground unless the turn is
    obviously small talk, or obviously an instruction about the tutor's own data
    (a student, a lesson, "split session 2") that a tool answers and the library
    never could. Everything else grounds.

    The asymmetry is the whole point:
      - a false NEGATIVE (we skip the search) = an ungrounded answer. Wrong, and
        invisible. This is the bug we just lived through.
      - a false POSITIVE (we search when we didn't need to) = ~7ms and a few
        hundred tokens of grounding the model ignores. Nothing.
    A rule whose failure mode is 7ms should fail toward searching.

    Matched against FOLDED text (`app/text/normalize.py`) so the negative filter
    itself works in Greek — accents move under inflection (μάθημα -> μαθήματα)
    and `.lower()` keeps them.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    folded = fold(raw)
    if _SMALL_TALK_RE.match(folded):
        return False
    if _ENTITY_OR_ARTIFACT_RE.search(folded):
        return False
    return True


def _snippet(text: str, limit: int = 300) -> str:
    """A bounded preview of a hit's chunk text for a citation payload — the
    full text already reached the model via the GROUNDING block; this is
    only what a citation chip would show, not what the model reasons over.
    """
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[:limit].rstrip() + "…"


def _to_citation(hit) -> dict:
    """One `AgentResult.citations` entry (C2) from an `app.brain.retrieve.
    Hit` — `{source_id, source_title, page_no, page_id, snippet}`, the exact
    shape the brief specifies so the UI can render a chip that deep-links
    into the Reader at the cited page. `page_no`/`page_id` are None when the
    chunk predates page-addressable ingest (`Hit`'s own documented case) —
    passed through as-is rather than papered over.
    """
    return {
        "source_id": str(hit.source_id),
        "source_title": hit.source_title,
        "page_no": hit.page,
        "page_id": str(hit.page_id) if hit.page_id is not None else None,
        "snippet": _snippet(hit.text),
    }


_NO_HITS_GROUNDING = (
    "GROUNDING: searched the tutor's library for this question and found nothing "
    "relevant. Say plainly, in your answer, that his material doesn't cover this, "
    "and label the rest of your answer as general knowledge (not from his library)."
)
NO_HITS_SLICE_ID = "chat.no_hits"

# The grounding block, lifted out of `_grounding_block` byte-identically so the tutor
# can rewrite it. `{passages}` is HIS library's text and `{answer_in}` is the tail
# reminder — both filled at call time, and both required (`overrides.validate` rejects
# an edit that drops either). Dropping `{passages}` would leave the model an
# instruction to cite context it was never given.
GROUNDING_BLOCK = (
    "GROUNDING — from the tutor's library, the passages most relevant to this "
    "question:\n\n{passages}\n\n"
    "Answer from this context and cite the passages you use inline as [n]. If "
    "none of it actually answers the question, say his material doesn't cover "
    "this and label the rest of your answer as general knowledge.\n\n"
    "{answer_in}"
)
GROUNDING_SLICE_ID = "chat.grounding"


def _grounding_block(hits: list, locale: str, source=None) -> str:
    """The injected context TEXT (C1/C2) — appended onto the end of the
    user's OWN turn (see `run_agent_turn`'s pre-hop), deliberately NOT a
    second `{"role": "system", ...}` message: the real vLLM chat template
    this app targets 400s a system message that isn't the very first one
    ("System message must be at the beginning") — confirmed against the
    live server while building this pre-hop, not a theoretical concern. A
    mid-transcript system message would also defeat agentic-gotchas.md #8's
    prefix-caching invariant (`loop.py`'s own module docstring) even if the
    server tolerated it: that invariant is about `SYSTEM_PROMPT` staying
    BYTE-IDENTICAL turn over turn, and retrieved passages are per-query by
    definition. Appending to the user turn instead keeps index 0 untouched
    (still exactly `SYSTEM_PROMPT`) and the dynamic part where it belongs —
    in the part of the prefix that was already going to change this turn.

    Empty `hits` (no results, or every result screened out by
    `retrieve.search`'s own floor) gets the explicit "say so, label as general
    knowledge" instruction (`_NO_HITS_GROUNDING`) rather than silently
    omitting a grounding block — an omitted block is indistinguishable from
    "retrieval wasn't attempted" and invites exactly the unlabelled-
    pretrained-knowledge failure C2 exists to prevent.

    THE LANGUAGE REMINDER GOES LAST (Plan 13, Stage 5.3). The passages are the
    tutor's ENGLISH book, they can run to thousands of tokens, and they sit
    immediately before the point where the model starts writing. A "write in
    Greek" instruction up in the system prompt is, by then, the least recent
    thing it saw — and it shows: it finishes reading English and answers in
    English. `app.i18n.answer_in`, at the very tail of this block, is what
    actually holds. Both branches get it, the no-hits one included (nothing
    about "we found nothing" makes the answer's language matter less).
    """
    if not hits:
        return f"{resolve(source, NO_HITS_SLICE_ID, _NO_HITS_GROUNDING)}\n\n{answer_in(locale, source)}"
    passages = "\n\n".join(
        f"[{i}] (source_id={hit.source_id}, page={hit.page}) {hit.text}"
        for i, hit in enumerate(hits, start=1)
    )
    return resolve(source, GROUNDING_SLICE_ID, GROUNDING_BLOCK).format(
        passages=passages, answer_in=answer_in(locale, source),
    )


@dataclass
class AgentResult:
    """One agent turn's outcome. `status` is "answer" (a final reply — the
    only status Task 2 ever produced) or "awaiting_approval" (Task 3: the
    loop suspended on a proposed MUTATION tool call — see `pending_tool`).
    `messages` is the FULL updated transcript — the caller's input plus
    every new turn this call appended — ready to persist and pass back in
    as-is on the next turn (mirrors `AssistantTurn`'s own "container, not a
    compared value" rationale for staying a plain, non-frozen dataclass).

    `pending_tool` (Task 3) is None for "answer", and otherwise
    `{tool_call_id, name, arguments}` for the ONE suspended mutation call —
    Task 4's chat router persists an `ApprovalRequest(tool_name=pending_tool
    ["name"], tool_args=pending_tool["arguments"])` keyed off this, and, on
    resolve, answers `pending_tool["tool_call_id"]` with a `{"role":"tool",
    ...}` result before resuming `run_agent_turn` — this loop itself never
    persists anything (session-agnostic; Task 4 owns persistence).

    `citations` (Plan 11 Task 1, C1/C2) is the list of `_to_citation(hit)`
    dicts for whatever this turn's forced-retrieval pre-hop found —
    `[]` when the turn wasn't content-bearing (no search ran at all) OR the
    search ran and everything it found fell below its floor. Populated on
    EVERY return branch below (not just the plain "answer" path), since a
    turn can still be genuinely grounded even when it also suspends on a
    mutation in the same reply (see `test_agent_grounding.py`'s C1/C6
    composition test) — `app/routers/chat.py` persists this onto the
    `Message` row it writes for the turn's own trailing assistant message.
    """

    status: str
    content: str | None
    messages: list[dict]
    pending_tool: dict | None = None
    citations: list[dict] = field(default_factory=list)


def _tool_schemas() -> list[dict]:
    # Both kinds are exposed to the model (Task 3) — it must be able to SEE
    # a mutation tool to ever propose one for this loop to suspend on. Kept
    # as an explicit kind-check (not just "every entry unconditionally") so
    # a future non-model-facing registry `kind` wouldn't silently leak in.
    return [entry.schema for entry in TOOLS.values() if entry.kind in ("read", "mutation")]


SYSTEM_SLICE_ID = "chat.system"


def _ensure_system_prompt(messages: list[dict], locale: str, source=None) -> list[dict]:
    """Prepend `SYSTEM_PROMPT` + the session's LANGUAGE block unless the
    transcript already starts with a system message. Callers (Task 4's chat
    router) own the transcript across turns and pass the full history back in
    each time — prepending unconditionally would accumulate a duplicate system
    message every turn.

    The LANGUAGE block (`app.i18n.language_directive`, Plan 13 Stage 5.3) is
    appended to `SYSTEM_PROMPT` rather than baked into it: the prompt is a
    module-level constant precisely so it stays byte-identical (agentic-
    gotchas #8 — prefix caching), and a locale is per-session. Appending keeps
    BOTH properties: the string is still fully determined by the session, so it
    is still byte-identical turn over turn WITHIN a conversation (a session's
    `locale` is set at creation and never changes), which is the only scope in
    which prefix caching can hit anyway.
    """
    if messages and messages[0].get("role") == "system":
        return messages
    prompt = resolve(source, SYSTEM_SLICE_ID, SYSTEM_PROMPT)
    system = f"{prompt}\n\n{language_directive(locale, source)}"
    return [{"role": "system", "content": system}, *messages]


def _wire_assistant_message(content: str | None, tool_calls: list[ToolCall]) -> dict:
    """Reconstruct the assistant turn in OpenAI WIRE shape for history — NOT
    the parsed `AssistantTurn` shape `chat_tools` returns (`arguments` there
    is already a dict; the wire shape needs the JSON string back). Required
    for the protocol to continue correctly on the next call (per this task's
    brief). `tool_calls` is omitted entirely when there are none — an empty
    list is a different wire shape than "no tool_calls key at all".
    """
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in tool_calls
        ]
    return message


def _stringify(result) -> str:
    """Every tool `fn` returns a plain JSON-serializable Python object (see
    `tools.py`'s docstring) — turning that into the tool message's `content`
    string is this loop's job, not each tool's. `default=str` covers the
    handful of non-JSON-native types the registry's results carry (`UUID`,
    `datetime`) without every tool needing to stringify its own fields.
    """
    return json.dumps(result, default=str)


def _dispatch_read_call(db, call: ToolCall, messages: list[dict]) -> None:
    """Execute one READ tool call inline and append its `{"role":"tool",
    ...}` result to `messages` in place — the exact guarded dispatch
    (unknown-tool name -> "ERROR: unknown tool"; a tool raising mid-call ->
    "ERROR: tool ... failed") Task 2 established. Factored out of
    `run_agent_turn`'s main loop so BOTH call sites that dispatch calls
    inline — a plain all-reads turn, and the reads that precede a suspending
    mutation in the same turn — share one implementation instead of two
    copies that could drift apart.

    Never called for a mutation `ToolCall`: `run_agent_turn` always
    intercepts those (via `_first_mutation_index`) before reaching here, so
    this function has no mutation-vs-read branch of its own to get wrong.
    """
    entry = TOOLS.get(call.name)
    if entry is None:
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": f"ERROR: unknown tool '{call.name}'",
        })
        return
    try:
        result = entry.fn(db, **call.arguments)
    except Exception as exc:
        # A KNOWN tool can still raise mid-dispatch (e.g. valid JSON but a
        # shape the fn doesn't accept) — chat_tools already returned
        # successfully so this never raises ToolArgsError; same "guard,
        # don't crash the whole turn" spirit as the unknown-tool-name guard
        # above, just one layer deeper.
        log.warning("tool %r raised during dispatch", call.name, exc_info=True)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": f"ERROR: tool '{call.name}' failed: {exc}",
        })
        return
    messages.append({
        "role": "tool",
        "tool_call_id": call.id,
        "content": _stringify(result),
    })


def _first_mutation_index(tool_calls: list[ToolCall]) -> int | None:
    """Index of the FIRST call in this turn whose registry entry is a
    `kind == "mutation"`, or None if every call this turn is a read (an
    unknown/unregistered name is never a mutation, so it's treated the same
    as a read here — dispatched inline by `_dispatch_read_call`, which is
    where the actual "unknown tool" guard lives, not here).
    """
    for i, call in enumerate(tool_calls):
        entry = TOOLS.get(call.name)
        if entry is not None and entry.kind == "mutation":
            return i
    return None


def run_agent_turn(
    db, messages: list[dict], *, locale: str = DEFAULT_LOCALE, max_steps: int = 6,
    raw_user_text: str | None = None,
    precomputed_first: AssistantTurn | None = None,
) -> AgentResult:
    """`locale` is the SESSION's language (`ChatSession.locale`, set from the
    browser's `X-App-Locale` at session creation) — `app/routers/chat.py`
    passes it on every call. It is threaded into the system prompt, into the
    tail of the GROUNDING block, and INTO EVERY TOOL CALL THE MODEL PROPOSES
    (`app.agent.tools.with_locale`) — including the one this turn suspends on,
    so the wire transcript, the approval card the tutor sees, and the params of
    the job that eventually runs all carry the same locale. Defaults to `el`
    (the app's default), never `en`.

    `precomputed_first` (CORE_DECISIONS.md §3 — the tool-turn double-billing
    fix) is an `AssistantTurn` the STREAMING endpoint already paid a full
    model call for before falling back to REST (see `app.agent.handoff`).
    When given, the loop's FIRST iteration consumes it in place of calling
    `provider.chat_tools` — everything downstream of that call (locale
    injection, read dispatch, the HITL mutation suspend, the C3 tablature
    re-prompt, and every SUBSEQUENT provider call) runs exactly as if the
    model had just returned it, so the turn's semantics are identical to a
    fresh run minus the duplicate first bill. Single-use by construction
    (cleared the moment it is consumed); None — the default and every
    pre-existing call site — changes nothing.
    """
    messages = _ensure_system_prompt(list(messages), locale, db)
    tools = _tool_schemas()
    provider = get_provider()
    repair_attempts = 0
    tab_bluff_attempts = 0
    last_content: str | None = None
    citations: list[dict] = []

    # --- C1: forced retrieval pre-hop ---------------------------------------
    # Runs FIRST now (Task 8 — see G5 below for why the reorder). Fires ONLY
    # when the newest message in the transcript is a fresh user turn (i.e.
    # `messages[-1]["role"] == "user"`) — a resumed turn after an HITL
    # resolve always ends with a `{"role": "tool", ...}` answer to the
    # approved/rejected mutation, never a user message, so this never
    # re-triggers mid-approval. `search()` is called directly — not offered
    # to the model as a tool it might decline — and its hits are appended
    # onto the END of the user's own turn (see `_grounding_block`'s own
    # docstring for why NOT a second system message) BEFORE the loop's first
    # `chat_tools` call below, so every branch that follows (plain answer,
    # mutation suspend, max_steps fallback, ...) already has grounding in
    # `messages` by construction.
    #
    # `messages[-1] = {...}` REPLACES the list slot with a NEW dict rather
    # than mutating `last["content"]` in place: `messages` here shares its
    # element objects (not just the list) with the caller's own transcript
    # (`_ensure_system_prompt` unpacks the caller's original dicts into a new
    # list, but doesn't copy the dicts themselves) — `app/routers/chat.py`
    # holds onto that same list as `prior_wire` and diffs against it
    # (`_new_tail`) to compute what to persist. An in-place mutation would
    # retroactively rewrite `prior_wire`'s own last entry too, corrupting
    # that diff; reassigning the slot only ever changes what THIS function's
    # local list points to. `last` stays bound to the ORIGINAL (pre-grounding)
    # dict throughout — including for the G5 check right below, which must
    # judge the tutor's own words, not his words plus a retrieved-text tail.
    #
    # `named_song` is computed HERE, not inline in the G5 `if` below, because
    # it also has to widen THIS gate: `_is_content_bearing` treats any turn
    # mentioning "tab"/"tabs" as an artifact-generation instruction and
    # deliberately skips grounding for it (see that function's own
    # docstring) — right for "give me a G major scale tab", wrong for "give
    # me the tab for Sweet Child O' Mine", which is `looks_like_named_song_
    # request`'s single most common phrasing (its own trigger-word set
    # includes "tab"/"tabs"). Without the `or named_song` below, C1 would
    # never search for exactly the requests G5 exists to check, `hits` would
    # stay empty by never being asked, and Task 8's whole fix would be a
    # no-op for the majority of real named-song phrasings.
    last = messages[-1]
    hits: list = []
    is_user_turn = last.get("role") == "user"
    last_text = last.get("content") or ""
    # G5's invariant ("judge the tutor's own words" — see the comment above)
    # now holds against the ROUTER's injection too, not just this loop's own
    # grounding tail: `app/routers/chat.py` appends the whole curriculum tree
    # to `last_text` before we ever run, and a guitar course tree contains
    # trigger words in its lesson titles. The router passes the raw text
    # explicitly; the strip is the fallback for callers that don't.
    guard_text = raw_user_text if raw_user_text is not None else strip_curriculum_context(last_text)
    named_song = is_user_turn and looks_like_named_song_request(guard_text)
    if is_user_turn and (named_song or _is_content_bearing(guard_text)):
        hits = search(db, guard_text, k=5)
        citations = [_to_citation(hit) for hit in hits]
        grounded_content = f"{last_text}\n\n{_grounding_block(hits, locale, db)}"
        messages[-1] = {**last, "content": grounded_content}

    # --- G5: named-song decline pre-model short-circuit ---------------------
    # Still runs BEFORE the model is ever called (see this module's own
    # docstring above) — there is no reliable way to make the model itself
    # decline, since it is the very thing that fabricates when asked. But
    # (Task 8) it no longer runs before C1's search above, and it now fires
    # ONLY when that search came back empty (`not hits`): the tutor's library
    # is checked FIRST, so a transcription he OWNS, on a real page, is
    # answered with a citation instead of refused unread — the ordering bug
    # behind "those books are copywrited, but i bought them and they're
    # mine". A genuine miss (his library does NOT have it) still declines,
    # because that is exactly the case where the model would fabricate.
    if named_song and not hits:
        # In the SESSION's language (`named_song_decline_message`) — this is
        # the one reply in the product the model never writes, so it is also
        # the one reply the LANGUAGE directive can never translate; a
        # constant-per-locale lookup is what keeps it both deterministic AND
        # in the tutor's own language.
        decline = named_song_decline_message(locale)
        messages.append({"role": "assistant", "content": decline})
        return AgentResult(
            status="answer", content=decline,
            messages=messages, citations=citations,
        )

    for _ in range(max_steps):
        if precomputed_first is not None:
            # CORE_DECISIONS.md §3: the streaming endpoint ALREADY BILLED this
            # turn's first model call before falling back to REST — consume
            # that response instead of paying for the identical call again.
            # Already parsed (it came out of a successful `chat_tools_stream`),
            # so the ToolArgsError repair below can't apply to it. Cleared
            # immediately: only ever the FIRST call's stand-in, never a later
            # hop's.
            turn, precomputed_first = precomputed_first, None
        else:
            try:
                turn = provider.chat_tools(messages, tools, tool_choice="auto")
            except ToolArgsError:
                repair_attempts += 1
                if repair_attempts >= _MAX_REPAIR_ATTEMPTS:
                    log.warning("chat_tools: giving up after %d consecutive ToolArgsErrors", repair_attempts)
                    # Append the giveup reply to history too, not just return it as
                    # `content`: `AgentResult.messages` is documented as the full
                    # transcript, and Task 4's router persists `messages` while
                    # showing `content` to the user — without this append the
                    # "couldn't complete" reply silently drops out of history and
                    # the model has no memory of it next turn.
                    messages.append({"role": "assistant", "content": _GIVEUP_MESSAGE})
                    return AgentResult(status="answer", content=_GIVEUP_MESSAGE, messages=messages, citations=citations)
                # The broken turn is NOT added to history (no assistant message,
                # no dangling tool_call) — just a corrective user turn, so the
                # model gets a clean shot at a valid call next time.
                messages.append({"role": "user", "content": _REPAIR_MESSAGE})
                continue

        repair_attempts = 0  # a successful call resets the CONSECUTIVE-failure streak
        last_content = turn.content

        if not turn.tool_calls:
            if looks_like_tablature(turn.content or ""):
                # C3: the assistant free-typed a tab instead of calling
                # generate_artifact. The bluff is NOT appended to `messages`
                # here — it must never reach the tutor, not even inside the
                # transcript history — see the module-level comment above
                # `_MAX_TAB_BLUFF_ATTEMPTS` for the recovery choice/defence.
                if tab_bluff_attempts >= _MAX_TAB_BLUFF_ATTEMPTS:
                    log.warning(
                        "run_agent_turn: model free-typed tablature again after a "
                        "re-prompt; suppressing and returning an honest fallback"
                    )
                    messages.append({"role": "assistant", "content": _TAB_BLUFF_FALLBACK_MESSAGE})
                    return AgentResult(
                        status="answer", content=_TAB_BLUFF_FALLBACK_MESSAGE,
                        messages=messages, citations=citations,
                    )
                tab_bluff_attempts += 1
                log.warning(
                    "run_agent_turn: model free-typed tablature instead of calling "
                    "generate_artifact; re-prompting once"
                )
                messages.append({"role": "user", "content": _TAB_BLUFF_REPROMPT_MESSAGE})
                continue
            messages.append(_wire_assistant_message(turn.content, turn.tool_calls))
            return AgentResult(status="answer", content=turn.content, messages=messages, citations=citations)

        # THE LOCALE IS INJECTED ONCE, HERE, into every call the model just
        # proposed (`app.agent.tools.with_locale`) — before the wire message is
        # reconstructed, before any read is dispatched, and before a mutation is
        # suspended. Doing it in one place is exactly what makes those three
        # AGREE: the transcript the model sees next turn, the args on the
        # approval card, and the `GenerationJob.params` the runner will read all
        # come from the same injected dict. `ToolCall` is frozen (a parsed call
        # is a value), so this rebuilds each one rather than mutating it.
        tool_calls = [
            replace(call, arguments=with_locale(call.name, call.arguments, locale))
            for call in turn.tool_calls
        ]

        pending_index = _first_mutation_index(tool_calls)

        if pending_index is None:
            # No mutation anywhere in this turn — unchanged from Task 2:
            # every call is dispatched inline, in order.
            messages.append(_wire_assistant_message(turn.content, tool_calls))
            for call in tool_calls:
                _dispatch_read_call(db, call, messages)
            continue

        # --- SUSPEND: a mutation was proposed --------------------------------
        # PROTOCOL INTEGRITY (this task's brief flags this as the part a
        # reviewer will scrutinize): at most ONE tool_call may be left
        # unanswered in `messages` when this function returns, because Task
        # 4 answers exactly that one call at resolve time and then calls
        # chat_tools again to resume — a second, still-dangling unanswered
        # call would desync that resume (the wire protocol requires every
        # tool_call in an assistant turn to get a paired tool result before
        # the next assistant turn).
        #
        # So the reconstructed assistant message includes ONLY the calls up
        # to and including the pending mutation:
        #   - calls BEFORE it (`handled_calls`) are genuine reads (by
        #     construction of `_first_mutation_index`'s "first" scan) and
        #     are dispatched + answered right here, same as the no-mutation
        #     branch above.
        #   - the pending mutation itself is included in the wire message
        #     but deliberately left UNANSWERED — that's what "suspend" means.
        #   - any calls AFTER it are DROPPED from the reconstructed message
        #     entirely, not merely left unanswered: they were never
        #     dispatched (the model proposed them before a human ever got a
        #     chance to gate the mutation ahead of them), so they must not
        #     appear to have been asked either — including them unanswered
        #     would be a SECOND dangling call, and dispatching them out of
        #     the model's intended order would run a call the human never
        #     gated. If still relevant, the model can re-propose them once
        #     the loop resumes after this mutation is resolved.
        handled_calls = tool_calls[:pending_index]
        pending_call = tool_calls[pending_index]

        messages.append(_wire_assistant_message(turn.content, handled_calls + [pending_call]))
        for call in handled_calls:
            _dispatch_read_call(db, call, messages)

        return AgentResult(
            status="awaiting_approval",
            content=turn.content,
            messages=messages,
            pending_tool={
                "tool_call_id": pending_call.id,
                "name": pending_call.name,
                "arguments": pending_call.arguments,
            },
            citations=citations,
        )

    log.warning("run_agent_turn: max_steps=%d exhausted without a final answer", max_steps)
    # `last_content is None` (an explicit identity check, not a falsy-check:
    # `AssistantTurn.content` is documented None-not-"" when absent) means every
    # step was a tool call with no final text, so the fallback note is genuinely
    # being substituted and must be appended to history to keep `messages`
    # consistent with `content` (Task 4's router persists `messages`, shows
    # `content`). A truthy `last_content` is ALREADY in `messages` from the last
    # iteration's assistant-turn append above — returning it as `content`
    # without re-appending avoids a duplicate assistant message.
    if last_content is None:
        messages.append({"role": "assistant", "content": _MAX_STEPS_MESSAGE})
        return AgentResult(status="answer", content=_MAX_STEPS_MESSAGE, messages=messages, citations=citations)
    return AgentResult(status="answer", content=last_content, messages=messages, citations=citations)


# ---------------------------------------------------------------------------
# Plan 11 Task 3 (C4): SSE token streaming — the plain-answer path only
# ---------------------------------------------------------------------------
#
# `stream_plain_turn` is deliberately NOT a streaming version of the full
# `run_agent_turn` ReAct loop above. It handles exactly one case — a fresh,
# content-bearing user turn that resolves in a SINGLE model call with no
# tool_calls and no C3 tablature bluff — and yields text deltas as the model
# writes them for that case. Every other outcome (a tool/mutation call was
# proposed, the model bluffed a hand-typed tab, the call raised, the
# provider doesn't support streaming at all) yields a `"fallback"` event and
# returns WITHOUT ever yielding `"done"`.
#
# THE HONEST SIMPLIFICATION (the task brief explicitly blesses this): the
# ReAct dispatch loop, the HITL mutation-suspend gate (C6 — reviewed as
# airtight three times), and the C3 post-turn guard's bounded re-prompt are
# NOT reimplemented here. Reimplementing them against a streaming transport
# would mean two independent copies of the single most safety-critical piece
# of this app (the approval gate) that could drift apart — for a feature
# whose entire point is a nicer wait, not a new capability. Instead: this
# function only ever emits `"done"` for a turn simple enough to never need
# any of that machinery, and defers to the EXISTING, unchanged
# `run_agent_turn` (via the REST turn endpoint) for literally everything
# else. `app/routers/chat.py`'s streaming endpoint persists NOTHING on a
# `"fallback"` — the caller (the browser) resends the same content through
# `POST .../messages`, which runs the full loop from a clean slate, so a
# fallback can never leave a half-persisted, protocol-inconsistent
# transcript behind.
#
# Runs the EXACT SAME forced-retrieval pre-hop as `run_agent_turn` (C1/C2 —
# same `_is_content_bearing` gate, same floor — which now lives inside
# `search()` — same `_grounding_block`) so a streamed answer is grounded identically to a REST
# one; only the model-call transport differs.
def stream_plain_turn(
    db, messages: list[dict], *, locale: str = DEFAULT_LOCALE,
    raw_user_text: str | None = None,
) -> Iterator[dict]:
    """Yields, in order:
      - zero or more `{"event": "delta", "text": ...}` as content streams in.
      - exactly one terminal event, one of:
        - `{"event": "done", "content": str, "citations": list[dict],
           "messages": list[dict]}` — `messages` is the input transcript
          (system-prompted + grounded, same as `run_agent_turn` builds) with
          the final assistant turn appended, ready to hand to
          `persist_new_messages` exactly like `AgentResult.messages` is.
        - `{"event": "fallback", "reason": "tool_call" | "tablature" |
           "error"}` — see this module's own comment block above for what
          each reason means and what the caller must do (never persist;
          resend via the REST turn endpoint instead). The "tool_call" and
          "tablature" fallbacks ALSO carry `"turn"`: the complete, already-
          billed `AssistantTurn` this stream call produced (CORE_DECISIONS.md
          §3) — the router stashes it (`app.agent.handoff`) so the REST
          resend can consume it via `run_agent_turn(precomputed_first=...)`
          instead of re-billing the identical first call. The "error"
          fallback carries no turn (the stream died before a complete
          response existed). `"turn"` is server-internal: the SSE event the
          router emits to the browser still carries only `{"reason"}`.

    `locale`: same session language `run_agent_turn` takes, same system-prompt
    and GROUNDING-tail treatment — a streamed answer must not be in a different
    language from the REST answer to the same question. No tool-call locale
    injection here, because this function NEVER dispatches a tool call: any
    proposed call is a `"fallback"` and `run_agent_turn` re-runs the turn.
    """
    messages = _ensure_system_prompt(list(messages), locale, db)
    tools = _tool_schemas()
    provider = get_provider()
    citations: list[dict] = []

    # Same pre-hop as `run_agent_turn`, and run FIRST for the same Task 8
    # reason (see that function's own extensive comment for the full
    # rationale) — his library is searched before the named-song guard below
    # ever gets a say. `named_song` widens this gate the same way it does in
    # `run_agent_turn` (see that function's comment for why: `tab`/`tabs` is
    # both an `_is_content_bearing` exclusion AND G5's own most common
    # trigger word, so without this a named-song "tab" request would never
    # get searched at all). Duplicated here (not extracted into a shared
    # helper) because it's genuinely small and this module already follows
    # the "small deliberate duplication over a cross-call shared helper with
    # more parameters than callers" precedent (e.g. `_stringify` in
    # `app/routers/chat.py`).
    last = messages[-1]
    hits: list = []
    is_user_turn = last.get("role") == "user"
    last_text = last.get("content") or ""
    # Same `guard_text` invariant as `run_agent_turn` (see that function's
    # comment above its own identical block) — judge the tutor's own words,
    # never the router's injected curriculum tree.
    guard_text = raw_user_text if raw_user_text is not None else strip_curriculum_context(last_text)
    named_song = is_user_turn and looks_like_named_song_request(guard_text)
    if is_user_turn and (named_song or _is_content_bearing(guard_text)):
        hits = search(db, guard_text, k=5)
        citations = [_to_citation(hit) for hit in hits]
        grounded_content = f"{last_text}\n\n{_grounding_block(hits, locale, db)}"
        messages[-1] = {**last, "content": grounded_content}

    # Same G5 pre-model short-circuit as `run_agent_turn` — see that
    # function's own comment and this module's top-level docstring for the
    # full rationale, and Task 8 for why it now runs AFTER the pre-hop above,
    # gated on `not hits`. Emitted as a plain "done" (not a "fallback"):
    # there is nothing for the REST path to redo here, the decline itself IS
    # the final answer, same as `run_agent_turn`'s equivalent branch returns
    # it directly rather than falling back.
    if named_song and not hits:
        # Session-locale variant, same as `run_agent_turn`'s branch above —
        # see the comment there for why this constant-per-locale lookup is
        # the only way this reply can be both deterministic and Greek.
        decline = named_song_decline_message(locale)
        messages.append({"role": "assistant", "content": decline})
        yield {
            "event": "done", "content": decline,
            "citations": citations, "messages": messages,
        }
        return

    final_content: str | None = None
    tool_calls: list = []
    try:
        for event in provider.chat_tools_stream(messages, tools, tool_choice="auto", temperature=0.3):
            if event["type"] == "content":
                yield {"event": "delta", "text": event["text"]}
            elif event["type"] == "done":
                final_content = event["content"]
                tool_calls = event["tool_calls"]
    except Exception:
        # Broad on purpose: a malformed tool-call JSON (`ToolArgsError`), a
        # provider without a real streaming implementation
        # (`NotImplementedError`, the base class's default), or any transport
        # error mid-stream must never leak as a raw 500 out of an
        # already-started SSE response — it must become an honest fallback
        # instead, same posture as every other guard in this loop.
        log.warning("stream_plain_turn: stream failed; falling back to REST", exc_info=True)
        yield {"event": "fallback", "reason": "error"}
        return

    if tool_calls:
        # A tool/mutation call was proposed — including possibly a genuine
        # mutation that would need the HITL gate. NEVER dispatched here (see
        # this module's comment block above for why); the REST endpoint's
        # full `run_agent_turn` is what may act on it. The response itself
        # was already BILLED though, so it rides the fallback event for the
        # router to stash (CORE_DECISIONS.md §3) — discarding it here is what
        # used to make every tool-calling turn cost double.
        yield {
            "event": "fallback", "reason": "tool_call",
            "turn": AssistantTurn(content=final_content, tool_calls=tool_calls),
        }
        return

    if final_content and looks_like_tablature(final_content):
        # C3: same guard `run_agent_turn` applies — a hand-typed tab must
        # never reach the tutor. This function does not attempt the bounded
        # re-prompt/fallback recovery `run_agent_turn` does (that recovery
        # itself makes another model call, which would need to stream too,
        # compounding exactly the complexity this function exists to avoid);
        # it simply refuses to emit the bluff and defers to the REST path,
        # which already re-prompts correctly. Carrying the billed turn along
        # (same §3 handoff as the tool_call branch) does not weaken that
        # guard: `run_agent_turn`'s own C3 branch treats the precomputed
        # bluff exactly as a fresh model response — never appended, never
        # shown — and its bounded re-prompt becomes the turn's SECOND call
        # instead of its third.
        yield {
            "event": "fallback", "reason": "tablature",
            "turn": AssistantTurn(content=final_content, tool_calls=[]),
        }
        return

    messages.append(_wire_assistant_message(final_content, []))
    yield {"event": "done", "content": final_content, "citations": citations, "messages": messages}
