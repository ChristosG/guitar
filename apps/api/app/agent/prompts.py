"""System prompt for the ReAct agent loop (`app/agent/loop.py`).

Deliberately SHORT and IMPERATIVE — per `/mnt/nvme2TB/vllm_interract/
reference/agentic-gotchas.md` #1: a longer, "helpful assistant"-style prompt
(identity + style + process prose) empirically SUPPRESSES tool-calling on
this exact model — it writes the answer as prose instead of calling a tool,
even with `tool_choice="auto"` and the tools clearly defined. This prompt is
tool-first and near-top-loaded with the one instruction that matters.

Also deliberately carries NO facts about students/curricula/knowledge-base
content (per agentic-gotchas.md #2: facts in the prompt make the model
answer from them and skip the retrieval tool entirely) — it only tells the
model it does NOT have that data memorized and must call a tool to get it.

Kept in its own module (not inlined in `loop.py`) so the system+tools prefix
`loop.py` sends is a stable, reviewable constant — agentic-gotchas.md #8
notes vLLM's automatic prefix caching keys off a byte-identical prefix, so an
accidental per-request edit to this string would quietly break KV reuse
across every turn, not just be a style nit.

Plan 11 Task 1 (C1/C2) added ONE more sentence — not a new paragraph, not a
longer "helpful assistant" identity block, just one more imperative
instruction in the same tool-first register as the rest — telling the model
how to use the GROUNDING block `loop.py`'s forced-retrieval pre-hop now
injects before the model ever sees a content-bearing turn (see `loop.py`'s
own `_is_content_bearing`/`_grounding_message`). The model is never asked
whether to retrieve — that decision was moved into code — so this sentence
only covers what to DO with grounding that is already there: cite it, or say
plainly that it doesn't cover the question and label the rest as general
knowledge.

Plan 11 Task 2 (C3) added ONE more sentence again — same register, still not
a new paragraph — the direct fix for the exact bug Chris hit: asked for a G
major scale tab, the model typed six lines of (also WRONG) ASCII into a code
fence instead of calling `generate_artifact`, the schema-validated, AlphaTab-
rendered, ACTUALLY PLAYABLE artifact Plan 4 already built. The new sentence
bans free-typed tablature/chord diagrams outright and names the tool to call
instead. Belt-and-suspenders, not the only defence: `app/agent/guards.py`'s
`looks_like_tablature` is the enforcement layer for when the model ignores
this sentence anyway (see `loop.py`'s post-turn guard) — the prompt sentence
lowers how often the model needs catching, the code guard is what makes it
not matter when it does.

Plan 12 Task 5 (G5) added ONE more sentence again, same register: Chris's
OTHER live bug, asking for the "Smells Like Teen Spirit" riff and getting one
note repeated seven times, because the model cannot actually recall a
specific copyrighted recording and invents when asked anyway. The new
sentence tells it to say so honestly and offer a real alternative instead,
rather than fabricate. Belt-and-suspenders again, same split as C3's: the
REAL enforcement is `app/agent/guards.py`'s `looks_like_named_song_request`,
a PRE-model short-circuit in `loop.py` that answers a detected named-song ask
without ever calling the model at all (there is no reliable way to make the
model itself decline — it's the very thing that fabricates) — this sentence
only helps for the phrasings the code guard's generic-vocabulary detector
doesn't catch, it is not what the task's acceptance rests on.

Plan 13 Stage 5.3 added ONE more sentence, same register — the prompt had ZERO
language instruction, so the model answered a Greek tutor in whatever language
the last thing it read happened to be (usually his English library). The
sentence is deliberately locale-INDEPENDENT: it points at the LANGUAGE block
`loop.py` appends to this constant per session (`app.i18n.language_directive`)
rather than naming a language itself, which keeps this string a constant — and
keeps the cached prefix byte-identical for a given session, since a session's
locale does not change mid-conversation (`ChatSession.locale`).
"""

SYSTEM_PROMPT = (
    "You are the guitar tutor's copilot. You do NOT have his "
    "curricula, artifacts, or knowledge-base content memorized — you must "
    "call a tool to look any of it up; never invent a curriculum, "
    "id, citation, URL/link, or fact that a tool would return. When a GROUNDING "
    "block is present, answer strictly from it and cite passages inline as [n]; "
    "if it says his library has nothing on this, say so plainly and label the "
    "rest of your answer as general knowledge. Call a tool whenever "
    "the user asks about their curricula, lessons, sessions, "
    # NO "students"/"notes"/"progress" in this topic list (nor "student" in
    # the never-invent list above): the desktop build REMOVED the student/
    # note agent tools (see tools.py's module docstring) — naming a topic
    # with no backing tool sends the model hunting for a tool that no longer
    # exists.
    "artifacts, or any guitar technique/theory/gear/tone question. Answer directly, in "
    "plain conversational text, only for small talk that needs none of "
    "that. Call tools silently — never describe a tool call as prose "
    "instead of making it. Never write tablature or chord diagrams as "
    "text — always call generate_artifact so it renders as a real, "
    "playable artifact. You cannot recall a specific copyrighted "
    "recording's exact tab/riff/solo from memory, so never invent one — say "
    "so honestly and offer a real alternative instead (the chord "
    "progression in that style, a scale, or a technique exercise). "
    "Always write your answer in the language the LANGUAGE block below "
    "names, whatever language his library or your grounding is written in."
)
