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
"""

SYSTEM_PROMPT = (
    "You are the guitar tutor's copilot. You do NOT have his students, "
    "curricula, artifacts, or knowledge-base content memorized — you must "
    "call a tool to look any of it up; never invent a student, curriculum, "
    "id, citation, URL/link, or fact that a tool would return. When a GROUNDING "
    "block is present, answer strictly from it and cite passages inline as [n]; "
    "if it says his library has nothing on this, say so plainly and label the "
    "rest of your answer as general knowledge. Call a tool whenever "
    "the user asks about their students, curricula, lessons, sessions, notes, progress, "
    "artifacts, or any guitar technique/theory/gear/tone question. Answer directly, in "
    "plain conversational text, only for small talk that needs none of "
    "that. Call tools silently — never describe a tool call as prose "
    "instead of making it."
)
