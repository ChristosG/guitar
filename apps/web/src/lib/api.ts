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
