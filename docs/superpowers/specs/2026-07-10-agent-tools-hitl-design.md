# Agent + Tools + HITL (Chat Copilot) — Design (Plan 5)

**Date:** 2026-07-10 · **Branch:** `build/poc` · **Status:** designed autonomously under Chris's overnight mandate (architecture reversals flagged in the SDD ledger's MORNING BRIEFING for his thumbs-up). Grounded in `.superpowers/sdd/plan5-recon.md` (live tool-calling probe + codebase audit).

## Goal
A chat copilot for the tutor: he talks to it, it **uses tools** to act on the real system (search the Brain, generate/segment curricula, generate artifacts, manage students), and **every mutating action is gated by human approval** (approve / edit / reject) before it touches the DB. The agent is the unifying surface over Plans 2–4 + 8.

## Key architecture decisions (deviations from the 2026-07-06 design spec — deliberate, flagged to Chris)
1. **Hand-rolled ReAct tool-loop over the existing `LLMProvider` seam — NOT LangGraph.** LangGraph/LangChain are absent from the repo; the bespoke `QwenVLLM` isn't a LangChain `BaseChatModel` and adopting LangGraph needs an adapter shim + a second provider path + a LangGraph-owned Postgres checkpoint schema outside Alembic. The local Qwen does **native OpenAI tool-calling** (probed live: `--enable-auto-tool-choice --tool-call-parser qwen3_coder`), so a plain loop over the raw client is clean and keeps the "one seam, one provider flag" property every prior plan preserved.
2. **REST-turn + poll transport — NOT SSE.** Nothing SSE-shaped exists on either end. The codebase's own answer to "slow LLM call over a proxied Cloudflare edge" is Plan 8's enqueue+poll — a long SSE connection spanning a multi-tool turn (which can itself call the 49–179 s `generate_curriculum`) risks the same edge timeout SSE was supposed to help with. A turn that hits an HITL pause or a slow job simply **returns a pending state**; the client polls (reusing Plan 8's proven pattern). SSE can be layered on later purely for token-narration polish.
3. **Uniform HITL rule: EVERY mutating tool is approval-gated** (approve-before-execute). Simpler + safer than the design's carve-out that listed artifact-render tools outside HITL. Reads execute transparently, same turn; mutations always pause for one human decision.
4. **HITL contract = approve-before-execute, not preview-result-before-commit** for the two generation tools. `generate_curriculum`/`generate_artifact` persist+commit internally (no produce/persist split), and refactoring both for true result-preview is out of PoC scope. So for generation the human approves the *request* (kind + prompt/params); for the cheap synchronous mutations (`segment_block`, `update_block`, `assign_curriculum`, `create/update_student`) the approval card can show a real preview/diff since those are computable before commit.

## Architecture

### The agent loop (`app/agent/`)
- Extend `LLMProvider` (`app/llm/base.py`) with ONE tools-aware method, e.g. `chat_tools(messages, tools, *, tool_choice="auto") -> AssistantTurn` (a small dataclass carrying `content: str | None` + `tool_calls: list[ToolCall]`), implemented on `QwenVLLM` exactly as the recon probe called it (raw `openai` client with `tools=`). Keeps the Qwen↔Claude one-flag swap intact.
- `app/agent/loop.py`: a bounded ReAct loop (`MAX_STEPS` cap) — call `chat_tools`; if `tool_calls`, dispatch each to a `TOOLS` registry of real Python callables; append `{"role":"tool", tool_call_id, content}`; repeat. Reads dispatch inline. A **mutating** tool call does NOT execute — it suspends the turn (see HITL). Apply the `reference/agentic-gotchas.md` playbook: tool-first imperative system prompt, guard hallucinated/unknown tool names, bounded repair on malformed tool-call JSON, keep the system+tools prefix stable, default `tool_choice="auto"`.
- `app/agent/tools.py`: the tool registry — JSON-schema tool definitions + the Python dispatch fn for each, tagged `read` vs `mutation`.

### Tools (SCOPED — primitives only; composites + Notes/Progress deferred to Plan 6)
**Reads (no HITL, inline):** `search_knowledge` (`brain.retrieve.search`), `explain_concept` (`brain.retrieve.answer` — grounded, cited), `list_students`/`list_curricula`/`list_artifacts`/`get_curriculum` (existing GET routes/services).
**Mutations (HITL-gated):** `create_student`, `update_student` (students CRUD); `generate_curriculum` (async — approval → enqueue `GenerationJob` → poll, per Plan 8); `segment_block`, `update_block`, `assign_curriculum` (synchronous, previewable); `generate_artifact` (approval on kind+prompt → generate+persist).
**DEFERRED to Plan 6** (no backing yet — recon §C gaps): `log_progress` (Progress model exists, no service), `add_note`/`promote_note_to_knowledge` (no Note model), and composites `plan_todays_lesson`/`recreate_tone` (belong with Today/Prep). The agent registry is extensible — Plan 6 adds these tools once their services exist.

### HITL state machine (`app/models/chat.py` + `app/routers/chat.py`)
New models + Alembic migration:
- `ChatSession(id, student_id?, timestamps)`
- `Message(id, session_id, role[user|assistant|tool], content, tool_calls JSON?, timestamps)`
- `ApprovalRequest(id, session_id, tool_name, tool_args JSON, status[pending|approved|rejected], edited_args JSON?, result_ref?, resolved_at?)`

Flow (stateless HTTP; all state in Postgres, mirroring `GenerationJob`'s restart-safe design):
1. `POST /chat/{session_id}/messages {content}` → persist the user message; run the loop.
2. Reads run inline. On the first **mutating** tool selection, STOP without executing: persist an `ApprovalRequest(pending)` + an assistant `Message` describing the proposal; return `{status:"awaiting_approval", approval_id, tool_name, args, preview}`.
3. `POST /chat/{session_id}/approvals/{approval_id}/resolve {decision, edited_args?}`:
   - **approve** → execute the (possibly edited) mutation. Async (`generate_curriculum`): enqueue the `GenerationJob`, return `{status:"job_pending", job_id}` (client falls into Plan 8's poll). Sync mutations: execute inline, append a tool-result message, resume the loop once to narrate, return the final turn.
   - **reject** → mark rejected, append `{"role":"tool","content":"User rejected this action."}`, resume the loop so the model can adapt.
4. `GET /chat/{session_id}` (history) + `GET /chat/{session_id}/pending` (any open approval).
5. Extend the startup sweep (`app/jobs/sweep.py`) to also fail/close chat turns orphaned mid-flight by a restart (optional hardening).

### Chat UI (`apps/web/src/components/chat/` + a cockpit page)
A chat panel (message list, composer) + an **approval card** component (tool name, args, preview/diff, Approve / Edit / Reject) rendered when a turn returns `awaiting_approval`; on `job_pending`, reuse the Plan 8 poll to show generation progress then the result. Add to nav. `lib/api.ts`: chat send / resolve / history helpers + types. Bilingual GR/EN.

## Testing
- Unit: `chat_tools` on `QwenVLLM` (mock the openai client → assert tool_calls parsed into `AssistantTurn`); the loop (stubbed provider) — read-tool inline dispatch, mutation → suspends with an `ApprovalRequest`, MAX_STEPS cap, unknown-tool guard, malformed-args bounded repair.
- Integration (`guitar_test`): `POST /chat/.../messages` with a read intent → answered inline; with a mutation intent → 200 `awaiting_approval` + a pending `ApprovalRequest` row (monkeypatch the provider so no live LLM); `resolve approve` on a sync mutation → executes + narrates; on the async `generate_curriculum` → `job_pending` + a `GenerationJob` row; `reject` → resumes.
- Live-LLM (opt-in marker): a real end-to-end turn (real model picks a tool) — the design's §10 "drive the real model" rule at the real tool roster (re-verify the gotchas hold beyond the 1-tool probe).
- Frontend Playwright (mocked, 3100): send → approval card renders → approve → result; reject path; the async job_pending → poll → render path.
- Real e2e (report): via the running stack, a real chat that generates a curriculum (approve → job → tree) and a chord diagram (approve → artifact), in a browser, screenshot.

## Scope / out of scope
**In:** the agent loop + `chat_tools` seam method; HITL state machine + chat router; the primitive tool set; chat UI + approval card; real e2e. **Out (→ Plan 6):** Notes/Progress models + their tools, composite tools (`plan_todays_lesson`, `recreate_tone`), Today/Prep. **Out (YAGNI/later):** SSE token streaming, LangGraph, multi-agent, auth.

## Risks (from recon §F)
- Gotchas at real scale (a longer system prompt can silently kill tool-calling; the 9B model can emit malformed tool-call JSON) — must re-verify end-to-end with the real roster, not assume from the 1-tool probe. → the live-LLM test + real e2e are the guard.
- The async-generate tool composes TWO async pauses (HITL approval + job poll) — the state machine (§ above) handles both via `session_id`-keyed Postgres rows, not two bolted-on mechanisms.
- Provider-seam addition (`chat_tools`) must keep the Qwen↔Claude one-flag swap property.

## Process
Written up as a task-by-task plan (`writing-plans`), executed subagent-driven (per-task reviews + whole-plan review), TDD, committed to `build/poc` (never merged). Same discipline as Plans 1–4/8.
