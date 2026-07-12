# Copilot Rebuild — Design (Plan 11)

**Sub-project C of the 3-part redesign.** A (Library) → B (Lesson Authoring) → **C (Copilot)**.
**Status:** authored during the autonomous overnight run, against the real interfaces A and B produced.

---

## 1. Why this exists

Chris, on the chat, verbatim:

> "the chat interface sucks btw.. no markdown viewer, no stream, i hope its truly rag from his notes
> though. and also on the chat might call tools like tab generation (i think this work!) but partly ..
> this is the response i got: [an ASCII tab in a code fence]"

Three separate failures in one message, and I confirmed all three in the code:

1. **It is not truly RAG.** `prompts.py` *asks* the model to call a retrieval tool; `tool_choice="auto"`
   lets it decline. It routinely answers guitar questions from pretrained knowledge and never searches
   his library. Plan 9's root-cause was that the library was empty — **that is now fixed** (77 pages,
   194,671 chars, page-addressable). The library is no longer the excuse. The *agent* is.
2. **No markdown.** `message-list.tsx` renders `{message.content}` as a raw string in a bubble.
3. **It free-typed a tab.** Asked for a G major scale, the model typed ASCII into a code fence instead
   of calling `generate_artifact` — so the AlphaTab renderer (which does real notation *and playback*)
   never ran. And the tab it typed was **wrong**: every string showed `0-2-4-5-7-8-10`, which is not a
   G major scale. It bluffed, and the bluff was unverifiable prose.

Plan 5's reviewer already flagged (1) and (3) as behavioural gaps and deferred them "for prompt-tuning
with Chris". **Prompt-tuning is not the fix.** The model's goodwill is not a mechanism.

## 2. The principle

> **Move the anti-hallucination pressure out of the prompt and into the code.**

A prompt is a request. A code path is a guarantee. Every decision below converts a request into a guarantee.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| C1 | **Retrieval is FORCED, not requested.** Before the model answers any content question, the loop runs `search()` over his library itself and injects the results into the turn. The model never gets the chance to skip retrieval. | `tool_choice="auto"` means "the model may decline", and it does. Forcing the first hop is a two-line change to the loop and removes an entire class of failure. His material always wins when it exists. |
| C2 | **Every answer carries provenance, and provenance is *rendered*.** A claim grounded in his library shows the source and the page ("Getting Great Guitar Sounds, p.21") as a chip that opens the Reader at that scan. A claim from general knowledge is labelled as such. | Chris asked for general knowledge to still be available ("how will he do research?"). The danger was never general knowledge — it was *unlabelled* knowledge. Label it and both are safe. Plan 9 made citations verifiable; this is what cashes that in. |
| C3 | **Structured artifacts can never be free-typed.** Tabs, chord diagrams, scales, tone recipes are produced ONLY by `generate_artifact` (guided-JSON, schema-validated, rendered by AlphaTab with real playback). The system prompt forbids ASCII tablature outright, and a post-turn guard detects a free-typed tab in the narration and replaces it with a real artifact call. | This is the exact bug Chris hit. A schema-validated artifact cannot be "wrong" the way a typed tab can — and it *plays*. A code fence full of dashes is a worse product than the renderer we already built and shipped in Plan 4. |
| C4 | **Markdown rendering + streaming.** The transcript renders markdown (headings, lists, bold, code). Token streaming via SSE so answers appear as they're written instead of after a 15s pause. | He asked for both. The 15s dead pause is what makes it feel broken even when it works. |
| C5 | **A `find_lesson` read tool (resolve by title).** | Plan 10's live run: the agent needed 3 attempts to split the right session because no tool resolves "that lesson" — it must be handed a uuid, and it guesses. HITL caught every wrong guess, but the fix is to stop making it guess. |
| C6 | **The HITL gate is untouched.** Every mutation still suspends for approval. | Reviewed as airtight twice. C makes the agent *more* capable; the gate is what makes that safe. Do not weaken it while touching the loop. |

## 4. What gets built

**Backend**
- `app/agent/loop.py` — forced-retrieval pre-hop (C1). Runs `search()` before the first model call on any
  content-bearing turn, injects hits as a grounded context block with source+page ids attached.
- `app/agent/prompts.py` — a hard prohibition on ASCII tablature and on inventing citations/URLs; an
  instruction to cite the injected context. Kept to ONE paragraph (a longer prompt empirically suppresses
  tool-calling on this model — established, do not relearn it).
- `app/agent/guards.py` (new) — the C3 post-turn guard: detect free-typed tablature in assistant prose.
- `app/agent/tools.py` — `find_lesson` (read, resolve by title) per C5.
- `app/routers/chat.py` — SSE streaming endpoint (C4). The existing REST turn stays for tests.
- Citations flow through `AgentResult` → the persisted `Message` → the API, so the UI can render chips.

**Frontend**
- `message-list.tsx` — markdown rendering + citation chips that deep-link into the Reader at the cited page.
- `chat-panel.tsx` — consume the SSE stream; render tokens as they arrive; keep the approval card flow intact.

## 5. Out of scope

The contextual side-panel (agent docked beside the lesson/source he's viewing) that Chris and I discussed.
It is a good idea and the tools now exist for it, but the standalone chat — his "fast shelter" — is what
is broken *today*, and shipping a second surface before the first one is trustworthy is the wrong order.

## 6. Testing

TDD throughout; the provider is faked in unit tests as it already is everywhere.

**Acceptance test:**

> Ask the chat, *"what does pick thickness do to my tone?"* → it **searches his library without being
> asked**, answers from **his book**, and shows a **citation chip to p.21** that opens the real scan.
> Then ask, *"give me a G major scale tab"* → it calls `generate_artifact` and renders a **real, playable
> tab** — **not** ASCII in a code fence. Then ask something his library does not cover → it says so
> plainly and labels the answer as general knowledge.

The third case is as important as the first two: an agent that cannot say "your material doesn't cover
this" is an agent that will invent something.
