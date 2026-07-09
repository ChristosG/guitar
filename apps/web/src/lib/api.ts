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
export type SourceStatus = "ingesting" | "ready" | "failed";

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
