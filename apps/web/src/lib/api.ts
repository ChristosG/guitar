/**
 * Typed fetch helpers for the Knowledge Brain API (`/knowledge/*`).
 *
 * The browser calls the API directly — it does not go through a Next.js
 * proxy/route handler (see `app/main.py`'s CORS middleware, which exists
 * specifically because the browser is the caller). `NEXT_PUBLIC_API_BASE` is
 * inlined at build time (a Docker build ARG in production — see
 * `apps/web/Dockerfile`/`docker-compose.yml`); the fallback below matches
 * `.env.example`'s default for local `next dev`.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8791";

export type SourceKind = "text" | "url";
/** Mirrors `app.models.knowledge.KnowledgeSource.status`'s full lifecycle
 * (`ingest.py`'s module docstring + `ocr.py`'s `_rollup_source_status`):
 * "ingesting" (transient, committed before the pipeline runs) -> "ready"
 * (char_count > 0) or "empty" (pipeline succeeded but extracted nothing —
 * SPEC D6: this is NOT "ready", see this file's `library` section docstring
 * below) or "failed" (pipeline raised). Note there is no "ocr_running" value
 * here — that only ever exists on `Page.status` (`PAGE_STATUSES` on the API
 * side); a source's own row doesn't change while its pages are mid-OCR, so
 * the Library page synthesizes an "OCR'ing" display state itself instead of
 * reading it off `SourceOut.status` (see `library/page.tsx`'s `ocrProgress`
 * state). */
export type SourceStatus = "ingesting" | "ready" | "empty" | "failed";

export interface SourceOut {
  id: string;
  title: string;
  type: string;
  status: SourceStatus | (string & {});
  domain: string | null;
  language: string | null;
  char_count: number | null;
  error: string | null;
  created_at: string;
  /** Which `Collection` this source is filed under, or `null` for the
   * synthetic "Unfiled" bucket the Library page renders client-side (there
   * is no `Collection` row for "Unfiled" — it's just every source with a
   * null FK). Added alongside `routers/library.py` (Plan 9 Task 6). */
  collection_id: string | null;
}

export interface ChunkPreviewOut {
  id: string;
  text: string;
  section_path: string | null;
  page: number | null;
}

export interface SourceDetailOut extends SourceOut {
  chunks: ChunkPreviewOut[];
}

export interface HitOut {
  chunk_id: string;
  source_id: string;
  source_title: string;
  text: string;
  section_path: string | null;
  page: number | null;
  score: number;
}

export interface SearchResponse {
  hits: HitOut[];
}

export interface AskResponse {
  text: string;
  citations: HitOut[];
}

export interface CreateSourceInput {
  kind: SourceKind;
  title: string;
  domain?: string | null;
  language?: string | null;
  text?: string | null;
  url?: string | null;
}

export interface UploadSourceInput {
  title: string;
  domain?: string | null;
  language?: string | null;
  file: File;
}

export interface SearchInput {
  query: string;
  k?: number;
  domain?: string | null;
  language?: string | null;
}

export interface AskInput {
  query: string;
  locale: string;
  k?: number;
}

/** Thrown for any non-2xx response. `detail` is the server's message when parseable
 * (FastAPI's `{"detail": ...}` shape), else the raw response text/status. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

async function parseErrorDetail(res: Response): Promise<string> {
  const raw = await res.text().catch(() => "");
  try {
    const body = JSON.parse(raw);
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail
        .map((d: { msg?: string }) => d.msg ?? JSON.stringify(d))
        .join("; ");
    }
  } catch {
    // not JSON — fall through to the raw text below
  }
  return raw || res.statusText;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  const headers: HeadersInit = {
    ...(init.body != null && !isFormData ? { "Content-Type": "application/json" } : {}),
    ...(init.headers ?? {}),
  };
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    throw new ApiError(res.status, await parseErrorDetail(res));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function listSources(): Promise<SourceOut[]> {
  return request<SourceOut[]>("/knowledge/sources");
}

export function createSource(input: CreateSourceInput): Promise<SourceOut> {
  return request<SourceOut>("/knowledge/sources", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function uploadSource(input: UploadSourceInput): Promise<SourceOut> {
  const form = new FormData();
  form.append("title", input.title);
  if (input.domain) form.append("domain", input.domain);
  if (input.language) form.append("language", input.language);
  form.append("file", input.file);
  return request<SourceOut>("/knowledge/sources/upload", {
    method: "POST",
    body: form,
  });
}

export function getSource(id: string): Promise<SourceDetailOut> {
  return request<SourceDetailOut>(`/knowledge/sources/${id}`);
}

export function deleteSource(id: string): Promise<void> {
  return request<void>(`/knowledge/sources/${id}`, { method: "DELETE" });
}

export function searchKnowledge(input: SearchInput): Promise<SearchResponse> {
  return request<SearchResponse>("/knowledge/search", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function askKnowledge(input: AskInput): Promise<AskResponse> {
  return request<AskResponse>("/knowledge/ask", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Typed fetch helpers for the Students + Curriculum APIs (`/students`,
 * `/curricula`, `/blocks`). Same direct-from-browser convention as the
 * Knowledge helpers above (see this file's top docstring) — the cockpit
 * pages call these straight from client components so Playwright's
 * `page.route` interception sees every request.
 */

export interface StudentOut {
  id: string;
  name: string;
  birthdate: string | null;
  level: string | null;
  instrument: string | null;
  preferred_language: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface CreateStudentInput {
  name: string;
  birthdate?: string | null;
  level?: string | null;
  instrument?: string | null;
  preferred_language?: string;
}

export interface UpdateStudentInput {
  name?: string;
  birthdate?: string | null;
  level?: string | null;
  instrument?: string | null;
  preferred_language?: string;
}

export function listStudents(): Promise<StudentOut[]> {
  return request<StudentOut[]>("/students");
}

export function createStudent(input: CreateStudentInput): Promise<StudentOut> {
  return request<StudentOut>("/students", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateStudent(id: string, input: UpdateStudentInput): Promise<StudentOut> {
  return request<StudentOut>(`/students/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

export function deleteStudent(id: string): Promise<void> {
  return request<void>(`/students/${id}`, { method: "DELETE" });
}

/** Recursive Block tree shape returned by every `/curricula/*` and
 * `/blocks/*` route (`routers.curriculum.block_to_tree` on the API side):
 * course -> module -> lesson -> segment for curriculum content, or
 * delivery_root -> session for a segmented delivery plan. `kind` is a soft,
 * relabelable string on the API — deliberately typed as `string` here, not
 * a union, so an unrecognized future kind still renders instead of failing
 * a type check. */
export interface BlockNode {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  est_minutes: number | null;
  order: number;
  language: string;
  plane: string;
  student_id: string | null;
  children: BlockNode[];
}

/** `GET /curricula` row shape — template roots only (no `children`). */
export interface CurriculumListItem {
  id: string;
  title: string;
  language: string;
  target_profile: Record<string, unknown> | null;
  created_at: string;
}

export interface GenerateCurriculumInput {
  title: string;
  language: string;
  profile: Record<string, unknown>;
  domain?: string | null;
  target_minutes_total?: number | null;
}

/** `GET /jobs/{id}` response shape — mirrors the API's `schemas/jobs.py`'s
 * `JobOut` exactly. The poll target for async generation jobs (curriculum
 * generation today — see `startCurriculumGeneration` below). `status` is
 * `pending | running | succeeded | failed` on the API, but kept as a plain
 * `string` here for the same reason `BlockNode.kind`/`ArtifactOut.kind` are:
 * an unrecognized future status should still round-trip instead of failing
 * a type check. */
export interface JobOut {
  id: string;
  kind: string;
  status: string;
  result_root_id: string | null;
  error: string | null;
  error_kind: string | null;
  created_at: string;
  updated_at: string;
}

/** `POST /curricula/generate`'s 202 response — mirrors `schemas/jobs.py`'s
 * `JobAccepted`, just enough for the caller to start polling `getJob`. */
export interface JobAccepted {
  job_id: string;
  status: string;
}

export interface BlockUpdateInput {
  title?: string;
  body?: string | null;
  est_minutes?: number | null;
  order?: number;
  kind?: string;
}

export interface SegmentBlockInput {
  session_minutes: number;
  cadence_per_week?: number;
  student_id?: string | null;
}

export function listCurricula(): Promise<CurriculumListItem[]> {
  return request<CurriculumListItem[]>("/curricula");
}

/**
 * Typed fetch helpers for the guided curriculum-authoring interview
 * (`routers/curriculum.py`'s `/curricula/interview...` routes, Plan 12 Task
 * 3 / G2) — mirrors `apps/api/app/schemas/interview.py` field-for-field.
 * Chris: "maybe llm can act as an assistant there bro, guiding him, and
 * asking him questions or corrections throughout the process." The state
 * machine itself lives entirely server-side (`app.curriculum.interview`'s
 * own docstring: the model never tracks `step`); this client is just a thin
 * typed wrapper around start/answer/get, same direct-from-browser
 * convention as every other section of this file.
 */

/** One `options[]` entry on the "who"/"sources" steps. `type`/`char_count`/
 * `default_selected` are only ever populated for "sources" (every source's
 * title/type/char_count so the tutor can tell real material from a
 * synthetic filler source apart — see `InterviewDialog`'s own docstring);
 * "who"'s student options never set them. All optional here (not a
 * discriminated union) since both step's options share this one shape on
 * the wire (`describe_step`'s two `options=[...]` list comprehensions in
 * `app.curriculum.interview`). */
export interface InterviewOption {
  value: string;
  label: string;
  type?: string;
  char_count?: number | null;
  default_selected?: boolean;
}

/** One grounding passage the "preview" step's `ground_topic` retrieval
 * actually found for a module — deliberately carries `source_title`, NOT
 * `source_id` (`_compute_preview` in `app.curriculum.interview` only ever
 * serializes `source_title`/`page_no`/`score` onto the wire). A citation
 * link into the Reader (`/library/{source_id}?page={page_no}`) therefore
 * has to resolve `source_id` client-side, by matching `source_title`
 * against the source catalog the "sources" step's own `options` already
 * handed this dialog — see `InterviewPreviewStep`'s `sourceIdByTitle` map. */
export interface InterviewPassage {
  source_title: string;
  page_no: number | null;
  score: number;
}

/** One planned module of the "preview" step's findings. `gap: true` means
 * `ground_topic` found nothing above the relevance floor for this module in
 * the sources the tutor chose — an honest gap, never silently hidden (see
 * `app.curriculum.ground`'s module docstring on the floor itself). */
export interface InterviewModule {
  title: string;
  objective: string;
  gap: boolean;
  passages: InterviewPassage[];
}

/** `interview.preview` on the wire — mirrors `_compute_preview`'s return
 * shape. Rendered by both the "preview" step (to review) and the "confirm"
 * step (the same cached findings, per `describe_step`'s "confirm" branch —
 * `_compute_preview` never runs twice for one interview). */
export interface InterviewPreview {
  course_title: string;
  modules: InterviewModule[];
  gap_count: number;
}

/** The `{interview_id, step, question, options?, findings?, error?}` envelope
 * every interview route returns (mirrors `schemas/interview.py`'s
 * `InterviewStateOut`). `step` is one of `STEP_ORDER` ("who" | "duration" |
 * "sources" | "preview" | "confirm") or the terminal "done" — kept as a
 * plain `string` here (not a union), same "an unrecognized future step
 * still round-trips" reasoning as `BlockNode.kind`/`JobOut.status`
 * elsewhere in this file. `error` is set only when the previous answer was
 * invalid and this response is re-asking the same step's question. */
export interface InterviewStateOut {
  interview_id: string;
  step: string;
  question: string;
  options: InterviewOption[] | null;
  findings: InterviewPreview | null;
  error: string | null;
}

export interface InterviewStartInput {
  title: string;
  domain?: string | null;
}

/** Starts a brand-new interview at its first ("who") step. */
export function startInterview(input: InterviewStartInput): Promise<InterviewStateOut> {
  return request<InterviewStateOut>("/curricula/interview", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Current state of an in-progress interview — refresh-safe (`GET
 * .../{id}` never recomputes the cached "preview" findings, per
 * `app.curriculum.interview.render_state`'s own docstring). Exposed for
 * completeness; `InterviewDialog` keeps its own in-memory state across
 * steps and doesn't need to re-fetch mid-flow, same "not yet called, here
 * for future resume support" posture as `getChatHistory` elsewhere in this
 * file. */
export function getInterview(interviewId: string): Promise<InterviewStateOut> {
  return request<InterviewStateOut>(`/curricula/interview/${interviewId}`);
}

/** Advances one step of the interview. The response is EITHER the next
 * `InterviewStateOut` (a non-final step, or a re-ask with `.error` set on an
 * invalid answer) OR, on the final "confirm" step once approved, a 202
 * `JobAccepted` — mirrors `answer_curriculum_interview`'s own two-shape
 * response on the API side exactly (see that route's docstring: "or — on
 * the final 'confirm' step, once approved — enqueue the REAL grounded
 * generation job and return 202 {job_id, status}"). Callers MUST
 * discriminate the result themselves (`isJobAccepted` below) rather than
 * assuming one shape — this is not a discriminated union on a shared `kind`
 * field, since neither shape carries one. */
export function answerInterview(
  interviewId: string,
  answer: unknown,
): Promise<InterviewStateOut | JobAccepted> {
  return request<InterviewStateOut | JobAccepted>(`/curricula/interview/${interviewId}/answer`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}

/** Discriminates `answerInterview`'s two possible response shapes — a
 * `JobAccepted` always has `job_id`, an `InterviewStateOut` never does. */
export function isJobAccepted(
  result: InterviewStateOut | JobAccepted,
): result is JobAccepted {
  return typeof (result as JobAccepted).job_id === "string";
}

/** Enqueues curriculum generation (Plan 8 Task 3/4): `generate_curriculum`
 * itself is still the same SLOW guided-JSON LLM call measured at
 * 49-179s/call, but that work now runs off the request path — this call
 * returns a 202 `JobAccepted` almost immediately. Callers MUST poll
 * `getJob(job_id)` until `status` is `"succeeded"` (then fetch the tree via
 * `getCurriculum(result_root_id)`) or `"failed"`, and MUST show a clear,
 * non-frozen loading state for the whole wait (see
 * `components/curriculum/generate-dialog.tsx`'s poll loop). */
export function startCurriculumGeneration(input: GenerateCurriculumInput): Promise<JobAccepted> {
  return request<JobAccepted>("/curricula/generate", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Poll target for `startCurriculumGeneration` (and any future async job
 * kind — `JobOut.kind` distinguishes them). */
export function getJob(jobId: string): Promise<JobOut> {
  return request<JobOut>(`/jobs/${jobId}`);
}

export function getCurriculum(rootId: string): Promise<BlockNode> {
  return request<BlockNode>(`/curricula/${rootId}`);
}

export function getBlock(id: string): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${id}`);
}

export function updateBlock(id: string, input: BlockUpdateInput): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

export function deleteBlock(id: string): Promise<void> {
  return request<void>(`/blocks/${id}`, { method: "DELETE" });
}

export function segmentBlock(id: string, input: SegmentBlockInput): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${id}/segment`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function assignCurriculum(rootId: string, studentId: string): Promise<BlockNode> {
  return request<BlockNode>(`/curricula/${rootId}/assign`, {
    method: "POST",
    body: JSON.stringify({ student_id: studentId }),
  });
}

/**
 * Progress / LessonLog / student-detail types (Plan 6 Task 4) — mirrors
 * `apps/api/app/schemas/students.py`'s `ProgressOut`/`LessonLogOut`/
 * `AssignmentSummary`/`StudentDetailOut` field-for-field. `status` on
 * `ProgressOut`/`ProgressInput` is the same soft, relabelable plain `string`
 * (not a union) as `BlockNode.kind`/`JobOut.status` above, for the identical
 * reason: this app's 4 known values (not_started/introduced/practicing/
 * mastered — see `components/students/progress-row.tsx`) are a UI picker's
 * concern, not this layer's.
 */

export interface ProgressOut {
  id: string;
  student_id: string;
  block_id: string;
  status: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

/** `blockId` is camelCase here for the same reason `CreateArtifactInput.
 * blockId` is below (see this file's Artifacts-section docstring): callers
 * read/destructure it more naturally than `block_id`. `notes` is passed
 * straight through to `POST /students/{id}/progress`'s wholesale-overwrite
 * `notes` field (see `upsertProgress` below) — callers changing ONLY
 * `status` MUST still resend the row's current `notes`, or the API clears
 * it (mirrors `app.curriculum.progress.upsert_progress`'s own docstring:
 * "OVERWRITTEN wholesale ... not merged in PATCH-fashion"). */
export interface ProgressInput {
  blockId: string;
  status: string;
  notes?: string | null;
}

export interface LessonLogOut {
  id: string;
  student_id: string;
  session_block_id: string;
  date: string | null;
  taught: boolean;
  notes: string | null;
  homework: string | null;
  created_at: string;
  updated_at: string;
}

/** Same camelCase-input convention as `ProgressInput` above (`sessionBlockId`
 * for the wire's `session_block_id`). Unlike `ProgressInput`, this always
 * CREATES a new row (`POST /students/{id}/lessons` never upserts — see
 * `LessonLogIn`'s own docstring), so there's no overwrite footgun to
 * document here. */
export interface LessonLogInput {
  sessionBlockId: string;
  date?: string | null;
  taught?: boolean;
  notes?: string | null;
  homework?: string | null;
}

/** One row of `StudentDetailOut.assignments` — `curriculum_block_id` is the
 * TEMPLATE block's id, NOT a student-specific clone (see `AssignmentSummary`'s
 * own docstring on the API side: the clone's id is never recorded anywhere).
 * `assignCurriculum` above is what actually creates one of these server-side;
 * its `BlockNode` response is the clone, not this summary row. */
export interface AssignmentSummary {
  assignment_id: string;
  curriculum_block_id: string;
  title: string;
}

/** `GET /students/{id}/detail`'s aggregate response — everything the
 * student-detail cockpit page (`app/[locale]/(cockpit)/students/[id]/
 * page.tsx`) needs in one round trip. */
export interface StudentDetailOut {
  student: StudentOut;
  assignments: AssignmentSummary[];
  progress: ProgressOut[];
  recent_lessons: LessonLogOut[];
}

export function getStudentDetail(id: string): Promise<StudentDetailOut> {
  return request<StudentDetailOut>(`/students/${id}/detail`);
}

/** Upserts the student's Progress row for `input.blockId` — see
 * `ProgressInput`'s own docstring above for the "must resend `notes`"
 * caveat this call is NOT responsible for enforcing (the caller is). */
export function upsertProgress(studentId: string, input: ProgressInput): Promise<ProgressOut> {
  return request<ProgressOut>(`/students/${studentId}/progress`, {
    method: "POST",
    body: JSON.stringify({
      block_id: input.blockId,
      status: input.status,
      notes: input.notes ?? null,
    }),
  });
}

/** Always creates a new LessonLog row — see `LessonLogInput`'s own docstring. */
export function logLesson(studentId: string, input: LessonLogInput): Promise<LessonLogOut> {
  return request<LessonLogOut>(`/students/${studentId}/lessons`, {
    method: "POST",
    body: JSON.stringify({
      session_block_id: input.sessionBlockId,
      date: input.date ?? null,
      taught: input.taught ?? false,
      notes: input.notes ?? null,
      homework: input.homework ?? null,
    }),
  });
}

/**
 * Typed fetch helpers for the Artifacts API (`/artifacts/*`). Same direct-
 * from-browser convention as the Knowledge/Curriculum helpers above (see this
 * file's top docstring) — the Artifacts gallery page and the curriculum
 * board's per-segment "attach artifact" dialog both call these straight from
 * client components, so `page.route` sees every request.
 *
 * `kind` is deliberately typed as a plain `string` here, not the 7-member
 * `ArtifactKind` union `components/artifacts/types.ts` defines — same
 * reasoning as `BlockNode.kind` above: a kind this layer doesn't recognize
 * should still round-trip instead of failing a type check. Callers that need
 * the fixed 7-kind picker list import `ArtifactKind`/`ARTIFACT_KINDS` from
 * `components/artifacts/kinds.ts` instead.
 *
 * `blockId` (on `ListArtifactsInput`/`CreateArtifactInput`/
 * `GenerateArtifactInput`) is camelCase at the call site — unlike e.g.
 * `SegmentBlockInput.session_minutes`/`GenerateCurriculumInput.
 * target_minutes_total` above, which mirror their POST body's snake_case
 * field names 1:1 and are passed straight through via `JSON.stringify
 * (input)`. Here each function below builds the snake_case `block_id` wire
 * field itself instead, since callers (the gallery form, the block-card
 * attach dialog) read far more naturally passing/destructuring `blockId`.
 * `ArtifactOut` is unaffected by this — it mirrors the response body
 * verbatim (snake_case `block_id`/`created_at`/...), consistent with every
 * other `*Out` interface in this file; only these *input* shapes get the
 * camelCase treatment.
 */

export interface ArtifactOut {
  id: string;
  kind: string;
  spec: Record<string, unknown>;
  title: string;
  tags: string[];
  source: string;
  block_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateArtifactInput {
  kind: string;
  spec: Record<string, unknown>;
  title?: string | null;
  tags?: string[];
  blockId?: string | null;
}

export interface GenerateArtifactInput {
  kind: string;
  prompt: string;
  blockId?: string | null;
  ground?: boolean;
}

export interface ListArtifactsInput {
  blockId?: string | null;
  kind?: string | null;
}

export function listArtifacts({ blockId, kind }: ListArtifactsInput = {}): Promise<ArtifactOut[]> {
  const params = new URLSearchParams();
  if (blockId) params.set("block_id", blockId);
  if (kind) params.set("kind", kind);
  const qs = params.toString();
  return request<ArtifactOut[]>(`/artifacts${qs ? `?${qs}` : ""}`);
}

export function getArtifact(id: string): Promise<ArtifactOut> {
  return request<ArtifactOut>(`/artifacts/${id}`);
}

export function createArtifact(input: CreateArtifactInput): Promise<ArtifactOut> {
  return request<ArtifactOut>("/artifacts", {
    method: "POST",
    body: JSON.stringify({
      kind: input.kind,
      spec: input.spec,
      title: input.title ?? null,
      tags: input.tags ?? [],
      block_id: input.blockId ?? null,
    }),
  });
}

/** Fast relative to `startCurriculumGeneration` above — one spec, not a
 * nested tree (a chord diagram or tone recipe is a single small
 * `guided_json` call, seconds not minutes — see deploy-recon notes) — but
 * still genuinely async, so callers still need *a* loading state, just not
 * the curriculum dialog's poll-loop/blocking-dialog treatment (see
 * `components/artifacts/generate-form.tsx`). */
export function generateArtifact(input: GenerateArtifactInput): Promise<ArtifactOut> {
  return request<ArtifactOut>("/artifacts/generate", {
    method: "POST",
    body: JSON.stringify({
      kind: input.kind,
      prompt: input.prompt,
      block_id: input.blockId ?? null,
      ground: input.ground ?? false,
    }),
  });
}

export function deleteArtifact(id: string): Promise<void> {
  return request<void>(`/artifacts/${id}`, { method: "DELETE" });
}

/**
 * Typed fetch helpers for the Chat API (`/chat/*`) — Plan 5's HITL agent
 * copilot: session creation, turn-taking, and the approve/edit/reject flow
 * that gates every mutation tool call. Same direct-from-browser convention as
 * every other section of this file (see this file's top docstring); mirrors
 * `apps/api/app/schemas/chat.py` field-for-field, including that module's own
 * "one flat response shape, fields vary by `status`" choice for `ChatTurnOut`
 * (see its docstring) rather than a discriminated union per status.
 *
 * `POST /chat/{id}/messages` answers 409 if an approval is already open on
 * that session (`routers/chat.py`'s `post_message` guard, checked BEFORE the
 * new user message is even persisted). This app's UI
 * (`components/chat/chat-panel.tsx`) is what's responsible for never
 * triggering that: it disables the composer for the entire time a
 * `ChatTurnOut` with `status: "awaiting_approval"` is unresolved, so a 409
 * here would mean this client's own guard has a bug, not an expected
 * response to design around.
 */

export type ChatTurnStatus = "answer" | "awaiting_approval" | "job_pending";

/** One entry of a grounded turn's `citations` (Plan 11 Task 1, C1/C2, mirrors
 * `app.agent.loop._to_citation`'s exact shape) — what lets the chat UI render
 * a chip that deep-links into the Reader (Plan 9) at the real scanned page a
 * grounded answer actually came from. `page_no`/`page_id` are `null` when the
 * underlying chunk predates page-addressable ingest; `message-list.tsx` only
 * renders a chip when `page_no` is set (a chip with nowhere real to link
 * would be worse than no chip). */
export interface ChatCitation {
  source_id: string;
  source_title: string;
  page_no: number | null;
  page_id: string | null;
  snippet: string;
}

/** Mirrors `schemas/chat.py`'s `ChatTurnOut` — the response shape for both
 * `sendChatMessage` and `resolveApproval`. Which fields are populated
 * depends on `status`: "answer" -> `content`; "awaiting_approval" ->
 * `approval_id`/`tool_name`/`tool_args`/`description`; "job_pending"
 * (resolve only) -> `job_id`. `status` keeps the same "soft union" shape as
 * `SourceOut.status`/`JobOut.status` above (a plain API-side `str`, not a
 * `Literal`) — an unrecognized future status should still round-trip
 * instead of failing a type check. `citations` (Plan 11 Task 1/3) is set on
 * an "answer"/"awaiting_approval" turn that had something to cite, `null`/
 * omitted otherwise. */
export interface ChatTurnOut {
  status: ChatTurnStatus | (string & {});
  content?: string | null;
  approval_id?: string | null;
  tool_name?: string | null;
  tool_args?: Record<string, unknown> | null;
  description?: string | null;
  job_id?: string | null;
  citations?: ChatCitation[] | null;
}

/** One row of `getChatHistory`'s response — mirrors `schemas/chat.py`'s
 * `MessageOut` (user/assistant rows only; the API omits tool/system rows —
 * internal plumbing the chat UI never needs to render). */
export interface ChatMessageOut {
  id: string;
  role: string;
  content: string | null;
  created_at: string;
  citations?: ChatCitation[] | null;
}

/** Mirrors `schemas/chat.py`'s `PendingApprovalOut` — `GET
 * /chat/{id}/pending`'s response when a mutation is awaiting a decision, or
 * `null` when the session has none open. */
export interface PendingApprovalOut {
  id: string;
  tool_name: string;
  tool_args: Record<string, unknown>;
  status: string;
  created_at: string;
}

export interface ResolveApprovalInput {
  decision: "approve" | "reject";
  /** Only meaningful alongside `decision: "approve"` — the tutor's edited
   * `tool_args` to run the tool with instead of the originally-proposed ones
   * (the "Edit" path in `ApprovalCard`). Omitted/`null` means "run it with
   * the args as originally proposed" (mirrors `ApprovalResolveRequest.
   * edited_args`, which the API itself only ever reads on the approve
   * branch — see `resolve_approval`'s reject branch in `routers/chat.py`). */
  editedArgs?: Record<string, unknown> | null;
}

/** `POST /chat`'s response — mirrors `schemas/chat.py`'s
 * `ChatSessionCreated`: just enough to start posting messages/resolving
 * approvals against this session. `studentId` is optional (a chat session
 * need not be scoped to one student). */
export function createChatSession(studentId?: string | null): Promise<{ session_id: string }> {
  return request<{ session_id: string }>("/chat", {
    method: "POST",
    body: JSON.stringify({ student_id: studentId ?? null }),
  });
}

/** Posts one user turn and runs the agent loop against it. Callers MUST NOT
 * call this again while a previous `ChatTurnOut` came back `awaiting_
 * approval` and hasn't been resolved yet — the API 409s (see this section's
 * top docstring) — `chat-panel.tsx` enforces that by disabling its composer
 * for exactly that window. */
export function sendChatMessage(sessionId: string, content: string): Promise<ChatTurnOut> {
  return request<ChatTurnOut>(`/chat/${sessionId}/messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

/** Terminal outcome of `streamChatMessage` (Plan 11 Task 3, C4): "done" is a
 * genuine, now-persisted plain answer (mirrors `ChatTurnOut`'s "answer"
 * case, `citations` included); "fallback" means the SSE endpoint declined to
 * stream this turn (a tool/mutation call, a post-turn guard trip, or a mid-
 * stream error — see `app/routers/chat.py`'s `post_message_stream` docstring
 * for the exact reasons) and persisted NOTHING — the caller MUST resend the
 * same `content` through the existing `sendChatMessage` REST call to get an
 * authoritative response (that call is what actually runs the full ReAct
 * loop, the HITL suspend, and the C3 tablature guard; this stream endpoint
 * only ever handles the safe common case). */
export type ChatStreamOutcome =
  | { status: "done"; citations: ChatCitation[] | null }
  | { status: "fallback"; reason: string };

/** One parsed `event: <name>\ndata: <json>\n\n` block off the stream — see
 * `app/routers/chat.py`'s `post_message_stream` for the exact 3 event names
 * this ever emits ("delta"/"done"/"fallback"). Returns `null` for a block
 * this client doesn't recognize (forward-compatible: an unrecognized event
 * is just skipped, not a parse error) or one with unparseable JSON `data`. */
function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let eventName: string | null = null;
  let dataLine: string | null = null;
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice("event:".length).trim();
    else if (line.startsWith("data:")) dataLine = line.slice("data:".length).trim();
  }
  if (!eventName || dataLine == null) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLine) };
  } catch {
    return null;
  }
}

/**
 * SSE token streaming for the plain-answer path (Plan 11 Task 3, C4: `POST
 * /chat/{session_id}/messages/stream`, `text/event-stream`). Calls `onDelta`
 * with each text chunk AS IT ARRIVES (so a caller can append it to a live
 * transcript bubble), then resolves to the stream's terminal outcome — see
 * `ChatStreamOutcome`'s own docstring for what "done" vs "fallback" means
 * and what a caller MUST do on "fallback".
 *
 * A network/parse failure (fetch rejects, a non-2xx status, the body ends
 * without ever reaching a terminal event) resolves to `{status:
 * "fallback", reason: "error"}` rather than throwing — same posture as the
 * server's own "the safe path never persists a half-formed turn" contract;
 * callers don't need a separate try/catch just to reach the same REST
 * fallback a `"fallback"` outcome already tells them to take.
 *
 * Deliberately `fetch` + a manual `ReadableStream` reader, NOT the browser's
 * `EventSource` — `EventSource` only supports a bare `GET` with no request
 * body, and this call needs to POST the user's `content`.
 *
 * Each parsed event is followed by a `setTimeout(0)` yield before the next
 * one is processed — even when several arrive in the same underlying network
 * chunk (plausible for a small/local response) — so the caller's `onDelta`-
 * driven UI updates land as SEPARATE renders instead of one micro-batched
 * swap. This is deliberate smoothing (the same idea real streaming chat UIs
 * apply regardless of network chunking), not a workaround for a bug: without
 * it, "streamed" text could legitimately still LOOK like a single swap on a
 * fast/local connection, defeating the entire point of streaming (Chris's
 * complaint was the dead pause, not the byte-level transport).
 */
export async function streamChatMessage(
  sessionId: string,
  content: string,
  onDelta: (text: string) => void,
): Promise<ChatStreamOutcome> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/chat/${sessionId}/messages/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
  } catch {
    return { status: "fallback", reason: "error" };
  }
  if (!res.ok || !res.body) {
    return { status: "fallback", reason: "error" };
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseBlock(block);
        if (!parsed) continue;

        if (parsed.event === "delta") {
          const text = (parsed.data as { text?: string }).text;
          if (text) onDelta(text);
        } else if (parsed.event === "done") {
          const citations = (parsed.data as { citations?: ChatCitation[] | null }).citations ?? null;
          return { status: "done", citations };
        } else if (parsed.event === "fallback") {
          const reason = (parsed.data as { reason?: string }).reason ?? "unknown";
          return { status: "fallback", reason };
        }
        // Deliberate yield — see this function's own docstring.
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
  } catch {
    return { status: "fallback", reason: "error" };
  }
  // The body ended without a "done"/"fallback" event — treat as a fallback
  // rather than silently returning nothing.
  return { status: "fallback", reason: "error" };
}

/** Resolves one pending approval: reject narrates the refusal and hands
 * control back to the model; approve either runs the mutation synchronously
 * (`status: "answer"`) or, for the one `async_job` tool (`generate_
 * curriculum`), enqueues a background job (`status: "job_pending"`, `job_id`
 * set) — see `resolve_approval` in `routers/chat.py`. Callers handling
 * `job_pending` should poll the EXISTING Plan 8 `getJob` below, same ~2s/cap
 * convention as `components/curriculum/generate-dialog.tsx`. */
export function resolveApproval(
  sessionId: string,
  approvalId: string,
  input: ResolveApprovalInput,
): Promise<ChatTurnOut> {
  return request<ChatTurnOut>(`/chat/${sessionId}/approvals/${approvalId}/resolve`, {
    method: "POST",
    body: JSON.stringify({
      decision: input.decision,
      edited_args: input.editedArgs ?? null,
    }),
  });
}

/** The session's transcript (user/assistant rows only, oldest first).
 * Exposed for API completeness (mirrors `GET /chat/{id}`) — the chat page
 * always starts a brand-new session on mount (`chat-panel.tsx`), which can
 * never already have history, so nothing in this app calls this yet; it's
 * here for whenever this app grows a way to resume a previous session (e.g.
 * a session id kept in the URL/storage). */
export function getChatHistory(sessionId: string): Promise<ChatMessageOut[]> {
  return request<ChatMessageOut[]>(`/chat/${sessionId}`);
}

/** The session's currently-open approval, or `null`. Same "exposed for
 * completeness, not yet called" status as `getChatHistory` above, for the
 * same reason — mirrors `GET /chat/{id}/pending`, useful once this app can
 * resume a session that might already have one outstanding (a fresh session
 * from `createChatSession` never does). */
export function getPendingApproval(sessionId: string): Promise<PendingApprovalOut | null> {
  return request<PendingApprovalOut | null>(`/chat/${sessionId}/pending`);
}

/**
 * Typed fetch helpers for the Notes API (`/notes/*`) — Plan 6's free-form
 * teaching notes with an optional promote-to-Brain action. Same direct-
 * from-browser convention as every other section of this file (see this
 * file's top docstring); mirrors `apps/api/app/schemas/notes.py` field-for-
 * field (`NoteCreate`/`NoteUpdate`/`NoteOut`/`NotePromoteOut`).
 */

export interface NoteOut {
  id: string;
  title: string;
  body: string;
  tags: string[];
  student_id: string | null;
  promoted_to_knowledge: boolean;
  created_at: string;
  updated_at: string;
}

/** `POST /notes/{id}/promote`'s response — the updated Note plus the id of
 * the `KnowledgeSource` the call just created (mirrors `NotePromoteOut` on
 * the API side). Structurally a superset of `NoteOut`, so a value of this
 * type is assignable anywhere a `NoteOut` is expected — e.g. swapping a
 * promoted note straight into a `NoteOut[]` list without re-shaping it. */
export interface NotePromoteOut extends NoteOut {
  source_id: string;
}

export interface NoteCreateInput {
  title: string;
  body: string;
  tags?: string[];
  student_id?: string | null;
}

/** All fields optional — PATCH semantics, mirrors `NoteUpdate`. Same caveat
 * that schema's docstring documents: the router drops every `null`-valued
 * field uniformly (`exclude_unset=True, exclude_none=True`), so sending
 * `student_id: null` to clear an existing link is a no-op, not a clear —
 * this app's edit form doesn't attempt to work around that (a fresh
 * unlinked note is the workaround if ever needed). */
export interface NoteUpdateInput {
  title?: string;
  body?: string;
  tags?: string[];
  student_id?: string | null;
}

export function listNotes(studentId?: string | null): Promise<NoteOut[]> {
  const params = new URLSearchParams();
  if (studentId) params.set("student_id", studentId);
  const qs = params.toString();
  return request<NoteOut[]>(`/notes${qs ? `?${qs}` : ""}`);
}

export function createNote(input: NoteCreateInput): Promise<NoteOut> {
  return request<NoteOut>("/notes", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateNote(id: string, input: NoteUpdateInput): Promise<NoteOut> {
  return request<NoteOut>(`/notes/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

export function deleteNote(id: string): Promise<void> {
  return request<void>(`/notes/${id}`, { method: "DELETE" });
}

/** Idempotent-as-409 on the API (a note already promoted refuses a second
 * promote outright — see `routers/notes.py`'s docstring). This app's UI
 * only ever calls this once per note (`NoteCard` hides/disables the action
 * once `promoted_to_knowledge` is true), so a 409 here would mean this
 * client's own guard has a bug, not an expected response to design around —
 * same posture this file's `sendChatMessage` docstring takes for the
 * analogous chat 409. */
export function promoteNote(id: string): Promise<NotePromoteOut> {
  return request<NotePromoteOut>(`/notes/${id}/promote`, { method: "POST" });
}

/**
 * Typed fetch helpers for the Library API (`routers/library.py`, Plan 9
 * Task 6) — collections, the source patch/retry/OCR controls, and the
 * page-level reader. Same direct-from-browser convention as every other
 * section of this file (see this file's top docstring). Ingestion itself
 * (`createSource`/`uploadSource`/`listSources`/`deleteSource` above) stays
 * where it is — this section is everything downstream of a `SourceOut`
 * already existing, mirroring the API-side router split.
 */

export interface CollectionOut {
  id: string;
  name: string;
  source_count: number;
}

export function listCollections(): Promise<CollectionOut[]> {
  return request<CollectionOut[]>("/library/collections");
}

export function createCollection(name: string): Promise<CollectionOut> {
  return request<CollectionOut>("/library/collections", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

/** Mirrors `routers/library.py::patch_source`'s response — deliberately NOT
 * the full `SourceOut` (the API only echoes back the 3 fields it touched).
 * Callers that need the full row (every caller in this app) just `refresh()`
 * their own `listSources()` afterward rather than trying to splice this
 * partial shape back into one. */
export interface SourcePatchResult {
  id: string;
  title: string;
  collection_id: string | null;
}

/** Files (or un-files, via `collectionId: null`) a source into a
 * `Collection`. `collection_id` is ALWAYS sent explicitly (never omitted) —
 * `routers/library.py::patch_source` distinguishes "field omitted" (leave
 * untouched) from "field explicitly null" (move to Unfiled) via Pydantic's
 * `model_fields_set`, and this call only ever means the latter. */
export function moveSource(id: string, collectionId: string | null): Promise<SourcePatchResult> {
  return request<SourcePatchResult>(`/knowledge/sources/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ collection_id: collectionId }),
  });
}

/** Retries a failed/empty ingest. 202 with a `job_id` for "pdf" (re-enqueues
 * OCR) and "url" (re-fetches the source's own stored `url`) sources; the API
 * 409s for any other type ("text"/"note"/"image" have no original to
 * re-fetch — see `routers/library.py::retry_source`'s docstring), which
 * surfaces here as a thrown `ApiError`. Callers (`SourceRow`) only render
 * the Retry button for "pdf"/"url" sources in the first place, so a 409 here
 * would mean that gate has a bug, not an expected response to design
 * around — same posture this file takes for `sendChatMessage`'s 409. */
export function retrySource(id: string): Promise<{ job_id: string | null }> {
  return request<{ job_id: string | null }>(`/knowledge/sources/${id}/retry`, { method: "POST" });
}

/** Enqueues OCR for a "pdf" source's pending/failed pages. Safe to call
 * unconditionally right after a PDF upload (`AddSourceDialog` does exactly
 * that) — a page that's already `ready` (e.g. one with a native text layer,
 * skipped by pagination) is left untouched; `ocr_source` only re-picks-up
 * pending/failed/ocr_running pages (see `app.brain.ocr.ocr_source`'s own
 * docstring). */
export function startOcr(id: string): Promise<{ job_id: string }> {
  return request<{ job_id: string }>(`/knowledge/sources/${id}/ocr`, { method: "POST" });
}

/** One row of `GET /knowledge/sources/{id}/pages` — mirrors
 * `schemas/library.py`'s `PageSummary`. `status` is the full
 * `PAGE_STATUSES` set (`pending`/`ocr_running`/`ready`/`failed`/`empty`) as
 * a plain `string`, not a union — same "unrecognized future value still
 * round-trips" reasoning as `SourceOut.status`/`JobOut.status` above. Used
 * by the Library page to derive OCR progress ("page N of M ready") for a
 * source it's actively watching — see `library/page.tsx`'s `watchOcr`. */
export interface PageSummary {
  page_no: number;
  status: string;
}

export function listSourcePages(id: string): Promise<PageSummary[]> {
  return request<PageSummary[]>(`/knowledge/sources/${id}/pages`);
}

/** `GET /knowledge/sources/{id}/pages/{n}` — mirrors `schemas/library.py`'s
 * `PageOut`: the reader's single-page view. `image_url` is a path relative
 * to `API_BASE` (`/media/pages/{page_id}.jpg`, only set when the page has a
 * scan — see `routers/library.py::get_page_image`'s docstring on why a D2
 * degenerate url/text/note page has none); resolve it with `apiMediaUrl`
 * below before using it as an `<img src>`. */
export interface PageDetailOut {
  id: string;
  page_no: number;
  status: string;
  text: string | null;
  image_url: string | null;
  total_pages: number;
}

export function getSourcePage(id: string, pageNo: number): Promise<PageDetailOut> {
  return request<PageDetailOut>(`/knowledge/sources/${id}/pages/${pageNo}`);
}

/** Resolves a server-relative media path (`PageDetailOut.image_url`) against
 * `API_BASE` for use as an `<img src>` — same "browser is the caller, not a
 * Next.js server" reasoning as this file's top docstring, so this can't be a
 * root-relative Next.js asset path. */
export function apiMediaUrl(path: string): string {
  return `${API_BASE}${path}`;
}

/**
 * Typed fetch helper for the Library-to-lesson-authoring seam
 * (`routers/lessons.py::from_selection`, Plan 10 Task 1) — the Reader's
 * "author a lesson" action. Enqueues a `GenerationJob(kind="lesson")` and
 * returns 202 almost immediately (same `JobAccepted` shape/convention as
 * `startCurriculumGeneration` below): `draft_lesson_from_selection` is a
 * blocking guided-JSON LLM call, too slow for a synchronous request/response
 * cycle. Callers MUST poll `getJob(job_id)` until it reaches a terminal
 * status — `result_root_id` is the drafted lesson's root Block id, fetchable
 * via `getLesson` once `status === "succeeded"` — same "enqueue, then poll"
 * contract `components/library/selection-action.tsx` drives, mirroring
 * `components/curriculum/generate-dialog.tsx`'s poll loop verbatim.
 */
export function authorFromSelection(
  sourceId: string,
  pageNo: number,
  text: string,
): Promise<JobAccepted> {
  return request<JobAccepted>("/lessons/from-selection", {
    method: "POST",
    body: JSON.stringify({ source_id: sourceId, page_no: pageNo, text }),
  });
}

/**
 * Typed fetch helpers for Lesson Authoring (`routers/lessons.py`, Plan 10
 * Task 4) — the lesson list + outline editor. Same direct-from-browser
 * convention as every other section of this file. `getLesson`/
 * `splitSession`/`mergeSessions`/`addSession` all return the SAME `BlockNode`
 * tree shape `getCurriculum`/`getBlock` do above (`block_to_tree`, reused
 * verbatim by `routers.lessons` from `routers.curriculum` — see that
 * router's own docstring) — a lesson is just `Block(kind="lesson")` with
 * `session` children and `item` grandchildren, no separate shape needed.
 * Renaming/deleting any single session or item block reuses the EXISTING
 * `updateBlock`/`deleteBlock` above — there is no lesson-specific PATCH/
 * DELETE route.
 */

export interface LessonProvenance {
  source_id: string;
  page_no: number;
}

/** `GET /lessons` row shape — mirrors `schemas/lessons.py`'s
 * `LessonListItem`. `provenance` is `null` for a lesson created any way
 * other than `POST /lessons/from-selection` (there is currently no other
 * way to create one, but the API leaves room for it). */
export interface LessonListItem {
  id: string;
  title: string;
  created_at: string;
  provenance: LessonProvenance | null;
}

export function listLessons(): Promise<LessonListItem[]> {
  return request<LessonListItem[]>("/lessons");
}

/** `GET /lessons/{id}` — the lesson -> session -> item tree. Deliberately
 * does NOT carry `provenance` (that only lives on `LessonListItem`, above —
 * see `schemas/lessons.py`'s `get_lesson` route, which returns the shared
 * `BlockTreeOut` with no provenance field at all): a caller that needs both
 * the tree AND the provenance chip (the editor page) also calls
 * `listLessons()` and looks its own id up there. */
export function getLesson(id: string): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${id}`);
}

export interface SplitSessionInput {
  session_minutes: number;
}

/** Cuts one over-long session into several, packed to ~`session_minutes`
 * each (`app.lessons.edit.split_session`'s deterministic bin-packer — no LLM
 * call). Returns the WHOLE lesson tree, not just the new sessions — callers
 * replace their entire tree state with the response wholesale (see
 * `components/lessons/session-card.tsx`'s `applyTree`) rather than trying to
 * splice the split session's replacement in by hand. 422s (thrown here as an
 * `ApiError`) if the session has no items to split. */
export function splitSession(lessonId: string, sessionId: string, input: SplitSessionInput): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${lessonId}/sessions/${sessionId}/split`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Folds >=2 sessions of the same lesson into the first (`sessionIds[0]`,
 * the survivor). The API 422s (surfaced as an `ApiError`) unless the given
 * sessions are ADJACENT in order (`app.lessons.edit.merge_sessions`'s own
 * docstring) — this app's UI never offers a picker that could ask for a
 * non-adjacent pair (`SessionCard`'s "merge with the one below" only ever
 * passes `[this session, the very next one]`), so a 422 here is a genuine
 * edge case to surface honestly, not a client bug to paper over. Returns
 * the whole lesson tree, same as `splitSession`. */
export function mergeSessions(lessonId: string, sessionIds: string[]): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${lessonId}/sessions/merge`, {
    method: "POST",
    body: JSON.stringify({ session_ids: sessionIds }),
  });
}

export interface AddSessionInput {
  title: string;
  est_minutes?: number | null;
  after?: string | null;
}

/** Appends a new, empty session (or inserts it right after `input.after`).
 * Returns the whole lesson tree, same as `splitSession`/`mergeSessions`. */
export function addSession(lessonId: string, input: AddSessionInput): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${lessonId}/sessions`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}
