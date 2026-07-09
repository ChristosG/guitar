# Agent + Tools + HITL (Chat Copilot) Implementation Plan (Plan 5)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** A chat copilot that uses tools to act on the real system (Brain search, curriculum/artifact generation, student management), with **every mutation gated by human approval** (approve/edit/reject) before it touches the DB.

**Architecture:** A hand-rolled ReAct tool-loop over the existing `LLMProvider` seam (native Qwen tool-calling — probed live), a Postgres-backed HITL state machine (`ChatSession`/`Message`/`ApprovalRequest`, mirroring Plan 8's `GenerationJob` restart-safe design), and REST-turn + poll transport (reusing Plan 8's job pattern for the slow async `generate_curriculum` tool). Design: `docs/superpowers/specs/2026-07-10-agent-tools-hitl-design.md`. Recon (tool inventory + live probe): `.superpowers/sdd/plan5-recon.md`.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic; the raw `openai` client via `QwenVLLM`; React/Next.js.

## Global Constraints (verbatim)
- **Reuse the seam, no new framework:** extend `LLMProvider` with ONE tools-aware method; NO LangGraph/LangChain (absent from the repo; keeps the Qwen↔Claude one-flag swap intact). NO SSE (REST-turn + poll, reuse Plan 8's `GenerationJob` poll).
- **Uniform HITL rule:** EVERY mutating tool is approval-gated (approve-before-execute). Reads execute inline, same turn. Mutations NEVER execute without an approved `ApprovalRequest`.
- **HITL contract = approve the REQUEST** (kind/params) for the two generation tools (`generate_curriculum`, `generate_artifact` — they persist+commit internally, no produce/persist split); sync mutations (`segment_block`, `update_block`, `assign_curriculum`, `create/update_student`) may show a real preview.
- **The async `generate_curriculum` tool composes TWO pauses** — the HITL approval AND Plan 8's job poll — via `session_id`-keyed Postgres rows, not two bolted-on mechanisms. On approve → enqueue the existing `GenerationJob` → return `job_pending` + `job_id`.
- **Agentic gotchas** (`/mnt/nvme2TB/vllm_interract/reference/agentic-gotchas.md`): tool-first imperative system prompt; guard hallucinated/unknown tool names; bounded repair on malformed tool-call JSON; keep system+tools prefix stable; default `tool_choice="auto"`.
- Tests isolate to `guitar_test` (conftest). **Bilingual GR/EN.** Web dev/test **port 3100**. Live-LLM tests use the opt-in `integration` marker. Monkeypatch the provider in non-live tests (no live LLM in the fast suite).
- **SCOPE:** primitives only. DEFER to Plan 6: `log_progress`/`add_note`/`promote_note_to_knowledge` (no backing yet) + composites `plan_todays_lesson`/`recreate_tone`.

## File Structure
```
apps/api/app/
  llm/base.py (modify)      # + chat_tools(messages, tools, *, tool_choice) -> AssistantTurn ABC method
  llm/qwen.py (modify)      # implement chat_tools on QwenVLLM (raw openai client, tools=)
  llm/tools_types.py        # AssistantTurn, ToolCall dataclasses
  agent/{__init__,loop,tools,prompts}.py   # ReAct loop, tool registry (read+mutation), system prompt
  models/chat.py            # ChatSession, Message, ApprovalRequest + migration
  schemas/chat.py           # request/response models
  routers/chat.py           # POST messages, POST approvals/{id}/resolve, GET history/pending
  main.py (modify)          # include chat.router
apps/web/src/
  lib/api.ts (modify)       # chat send/resolve/history + types
  components/chat/{chat-panel,approval-card,message-list}.tsx
  app/[locale]/(cockpit)/chat/page.tsx
  messages/{en,el}.json (modify)
```

---

## Task 1: `chat_tools` provider seam + tool dataclasses
**Files:** Create `app/llm/tools_types.py`; modify `app/llm/base.py` (ABC), `app/llm/qwen.py` (impl); tests.
**Interfaces — Consumes:** the existing `QwenVLLM.__init__` openai client (`app/llm/qwen.py:15-19`), `guided_json` as the style precedent.
**Produces:** `@dataclass ToolCall{id:str, name:str, arguments:dict}`; `@dataclass AssistantTurn{content:str|None, tool_calls:list[ToolCall]}`. `LLMProvider.chat_tools(messages:list[dict], tools:list[dict], *, tool_choice:str="auto", temperature:float=0.3) -> AssistantTurn` (abstractmethod). `QwenVLLM.chat_tools`: calls `self.client.chat.completions.create(model, messages, tools=tools, tool_choice=tool_choice, extra_body={chat_template_kwargs:{enable_thinking:False}})`, parses `choices[0].message` into `AssistantTurn` (tool_calls → `ToolCall` with `json.loads(arguments)`; a malformed `arguments` → raise a typed `ToolArgsError` for the loop's bounded repair).
- [ ] TDD: with a mocked openai client returning (a) a tool_calls message → `chat_tools` yields `AssistantTurn` with parsed `ToolCall`s; (b) a plain content message → `content` set, `tool_calls==[]`; (c) malformed tool `arguments` JSON → `ToolArgsError`. RED→GREEN. Commit.

## Task 2: ReAct loop + read-only tool registry
**Files:** Create `app/agent/{__init__,loop,tools,prompts}.py`; tests.
**Interfaces — Consumes:** `chat_tools`/`AssistantTurn`/`ToolCall` (T1); `get_provider()` (`app/llm/factory.py`); read services — `brain.retrieve.search(db, query, *, k=8, domain=None, language=None) -> list[Hit]`, `brain.retrieve.answer(db, query, *, locale, k=8) -> Answer(text, citations)`; list services for students/curricula/artifacts (existing GET routes/services).
**Produces:** `app/agent/tools.py` — a `TOOLS` registry: each entry = `{schema: <json-schema tool def>, fn: <callable(db, **args)>, kind: "read"|"mutation"}`. This task registers ONLY reads: `search_knowledge`, `explain_concept` (→ answer), `list_students`, `list_curricula`, `list_artifacts`, `get_curriculum`. `app/agent/prompts.py` — the tool-first imperative system prompt. `app/agent/loop.py` — `run_agent_turn(db, messages, *, max_steps=6) -> AgentResult`: calls `chat_tools(messages, [t.schema for read tools], tool_choice="auto")`; dispatches each read `ToolCall` to its `fn` (guarding UNKNOWN tool names → append an error tool-message, don't crash; `ToolArgsError` → one bounded repair re-prompt); appends `{"role":"tool", tool_call_id, content}`; loops until the model returns content with no tool_calls or `max_steps` hit. Returns the final assistant content + the transcript.
- [ ] TDD (stubbed provider — no live LLM): a turn where the stub emits a `search_knowledge` call then a final answer → the loop dispatches the real search (or a stubbed one), appends the tool result, returns the answer; an unknown tool name → guarded (error tool-message, loop continues, no crash); `max_steps` cap respected; a `ToolArgsError` → one repair attempt then give up gracefully. RED→GREEN. Commit.

## Task 3: HITL state machine — chat models + suspend-on-mutation
**Files:** Create `app/models/chat.py` + migration; modify `app/agent/loop.py` (suspend), `app/agent/tools.py` (register mutation tools); tests.
**Interfaces — Consumes:** T2 loop + registry; `GenerationJob` pattern (`app/models/generation_job.py`) as the model style precedent; mutation services — `create_student`/`update_student` (`routers/students.py` + `schemas/students.py` `StudentCreate/Update`), `segment_block(db, block_id, *, session_minutes, cadence_per_week=1, student_id=None)`, `update_block` (`BlockUpdate`), `assign_curriculum(root_id, student_id)`, `generate_artifact(db, *, kind, prompt, block_id=None, ground=False)`, and `generate_curriculum(db, *, title, language, profile, domain=None, target_minutes_total=None)` (but see Task 4 for its async enqueue).
**Produces:** `app/models/chat.py` — `ChatSession(id, student_id:UUID|None, +mixins)`, `Message(id, session_id FK, role:str(16), content:Text|None, tool_calls:JSON|None, +mixins)`, `ApprovalRequest(id, session_id FK, tool_name:str(50), tool_args:JSON, status:str(16) default "pending", edited_args:JSON|None, result_ref:str|None, resolved_at:DateTime|None, +mixins)` + Alembic migration (remember the pgvector `ix_chunk_embedding_hnsw` autogenerate-false-positive removal — Plan 4/8 lesson). Register the MUTATION tools in `TOOLS` (kind="mutation") with their JSON schemas. Modify `run_agent_turn`: when a dispatched `ToolCall` is a `mutation`, DO NOT execute — persist an `ApprovalRequest(pending, tool_name, tool_args)` + an assistant `Message` describing the proposal, and return an `AgentResult(status="awaiting_approval", approval_id, tool_name, args)`, stopping the loop.
- [ ] TDD (stubbed provider): a turn where the stub emits a `generate_artifact` (mutation) call → the loop SUSPENDS: an `ApprovalRequest(status="pending", tool_name="generate_artifact", tool_args=...)` row exists, the mutation fn was NOT called (assert via a spy), and the result is `awaiting_approval`. Model roundtrip for the 3 chat models. Migration up/down. RED→GREEN. Commit.

## Task 4: chat router — messages + resolve (approve/reject/resume) + async compose
**Files:** Create `app/schemas/chat.py`, `app/routers/chat.py`; modify `app/main.py`; tests.
**Interfaces — Consumes:** T2/T3 (loop, models, registry); `run_curriculum_job`/`GenerationJob` enqueue pattern (Plan 8 `routers/curriculum.py:143-186` — the agent's `generate_curriculum` approval enqueues the SAME way); `BackgroundTasks`.
**Produces:**
- `POST /chat/{session_id}/messages {content}` → persist the user `Message`; run `run_agent_turn` on the full history; return either the final assistant turn OR `{status:"awaiting_approval", approval_id, tool_name, args, description}`.
- `POST /chat/{session_id}/approvals/{approval_id}/resolve {decision:"approve"|"reject", edited_args?}` →
  - **approve** + a SYNC mutation (`create_student`/`update_student`/`segment_block`/`update_block`/`assign_curriculum`/`generate_artifact`): execute the (possibly `edited_args`) mutation inline, mark the `ApprovalRequest` approved (+`result_ref`), append a tool-result `Message`, resume `run_agent_turn` ONCE to narrate, return the final turn.
  - **approve** + the ASYNC `generate_curriculum`: create a `GenerationJob(kind="curriculum", params=args)` + `BackgroundTasks.add_task(run_curriculum_job, job.id)` (commit before returning), mark approved (+`result_ref=job_id`), return `{status:"job_pending", job_id}` (client falls into Plan 8's poll).
  - **reject**: mark rejected, append `{"role":"tool", tool_call_id, content:"User rejected this action."}`, resume the loop so the model adapts; return the turn.
- `GET /chat/{session_id}` (history), `GET /chat/{session_id}/pending` (open `ApprovalRequest` or null). `POST /chat` to create a session. Wire `main.py`.
- [ ] TDD (monkeypatch the provider; NO live LLM): `POST .../messages` (stub → read intent) → inline answer; (stub → mutation intent) → `awaiting_approval` + pending row. `resolve approve` a `segment_block` → the real segment runs, `ApprovalRequest` approved, a narration turn returns. `resolve approve` a `generate_curriculum` → `job_pending` + a `GenerationJob` row (monkeypatch `run_curriculum_job` no-op so no live LLM). `resolve reject` → rejected + a resume turn. Unknown session/approval → 404. RED→GREEN. Commit.

## Task 5: chat UI + approval card
**Files:** Create `apps/web/src/components/chat/{chat-panel,message-list,approval-card}.tsx`, `app/[locale]/(cockpit)/chat/page.tsx`; modify `apps/web/src/lib/api.ts`, nav (`app-shell.tsx`), `messages/{en,el}.json`; tests.
**Interfaces — Consumes:** T4 routes; the existing Plan 8 `getJob` poll (`lib/api.ts`) for the `job_pending` path; `Artifact`/`BlockNode` renderers to show results inline.
**Produces:** `lib/api.ts` — `createChatSession()`, `sendChatMessage(sessionId, content)`, `resolveApproval(sessionId, approvalId, {decision, editedArgs?})`, `getChatHistory(sessionId)` + types (mirror `schemas/chat.py`). A chat page: message list + composer; when a turn returns `awaiting_approval`, render an **ApprovalCard** (tool name, args, Approve / Edit / Reject) → `resolveApproval`; when a resolve returns `job_pending`, reuse the poll to show progress then render the tree; render tool results (artifact via `<Artifact>`, curriculum tree link). Add "Chat" to nav. Bilingual EN+EL for all strings.
- [ ] Playwright (mocked, 3100): send a message → mock `awaiting_approval` → the ApprovalCard renders with the tool + args → Approve → mock the resolved turn → result shown. A Reject path. The async `job_pending` → poll (pending→succeeded) → tree rendered. Keep the whole `apps/web` suite green. RED→GREEN. Commit.

## Task 6: real e2e (report) + live-LLM verification
**Files:** a live-LLM `integration` test (`apps/api/tests/`); `docs/superpowers/plans/README.md`.
- [ ] Live-LLM integration test (opt-in marker): a real `POST /chat/.../messages` where the REAL model, given the REAL tool roster + system prompt, correctly picks a tool (assert a tool was proposed / an `awaiting_approval` for a mutation intent) — re-verifying the gotchas hold beyond the 1-tool probe (the design's "drive the real model" rule).
- [ ] **Real e2e (report):** via the running stack + a real browser, hold a chat that (a) asks a grounded question → cited answer; (b) proposes generating a curriculum → Approve → job → tree; (c) proposes a chord diagram → Approve → artifact rendered. Screenshot. Commit + update README. Leave the stack healthy.

## Self-Review
Coverage vs spec: chat_tools seam (T1) ✓ · ReAct loop + read tools (T2) ✓ · HITL models + suspend-on-mutation (T3) ✓ · chat router approve/reject/resume + async compose (T4) ✓ · chat UI + approval card (T5) ✓ · live-LLM + real e2e (T6) ✓. Uniform-HITL + approve-before-execute + async-compose all honored. Types: `chat_tools→AssistantTurn/ToolCall→run_agent_turn→AgentResult`; `ChatSession/Message/ApprovalRequest→chat routes→lib/api chat helpers`. Deferred (spec/scope): Notes/Progress tools + composites → Plan 6; SSE/LangGraph → out. Gotchas guarded (unknown-tool, bounded repair, tool-first prompt) in T2; re-verified live in T6.
