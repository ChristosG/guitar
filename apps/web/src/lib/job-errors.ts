/** Localized text for a failed `GenerationJob` — keyed off `error_kind`, the
 * taxonomy the API's runners maintain precisely so the frontend does not have
 * to parse English prose. The three surfaces that render `job.error` raw
 * (interview dialog, Reader selection bar, chat panel) used to show a Greek
 * tutor "No Anthropic API key is configured. Open Settings and paste your
 * key." — in English, for the one error he must act on himself.
 *
 * `auth` / `rate_limit` / `timeout` get fully localized copy (their meaning is
 * fixed). `upstream` keeps the server's own message when there is one — it can
 * carry call-specific detail — and everything else falls back to the caller's
 * generic error string.
 */
export interface JobErrorLike {
  error: string | null;
  error_kind: string | null;
}

const LOCALIZED_KINDS = new Set(["auth", "rate_limit", "timeout", "too_long"]);

export function jobErrorText(
  job: JobErrorLike,
  t: (key: "auth" | "rate_limit" | "timeout" | "too_long" | "generic") => string,
): string {
  const kind = job.error_kind ?? "";
  if (LOCALIZED_KINDS.has(kind)) {
    return t(kind as "auth" | "rate_limit" | "timeout" | "too_long");
  }
  return job.error || t("generic");
}
