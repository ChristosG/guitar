/** Localized text for a failed `GenerationJob` — keyed off `error_kind`, the
 * taxonomy the API's runners maintain precisely so the frontend does not have
 * to parse English prose. The three surfaces that render `job.error` raw
 * (interview dialog, Reader selection bar, chat panel) used to show a Greek
 * tutor "No Anthropic API key is configured. Open Settings and paste your
 * key." — in English, for the one error he must act on himself.
 *
 * `auth` / `rate_limit` / `timeout` / `too_long` get their own fully localized
 * copy (their meaning is fixed and the action each one asks for differs).
 * `internal` and `upstream` — the other two kinds the API emits — get
 * `generic`, and so does an unset kind, and so does any kind added to the API
 * after this file was written.
 *
 * THE SERVER'S OWN STRING NEVER REACHES THE TUTOR. `upstream` used to keep it
 * ("it can carry call-specific detail"), and what it actually carries is
 * English written for us: "Lesson drafting failed (model returned
 * invalid/truncated output). Try again.", a provider's raw message, sometimes
 * a status code. Greek is the product; detail he cannot read is not detail. The
 * string stays in the payload for whoever is debugging — it is simply never
 * rendered.
 */
export interface JobErrorLike {
  error: string | null;
  error_kind: string | null;
}

type MessageKey = "auth" | "rate_limit" | "timeout" | "too_long" | "generic";

/** Every `error_kind` the API can set, and the message each one gets. Written
 * out in full — including the two that map to `generic` — so this file says
 * what happens to `internal` and `upstream` instead of leaving them to a
 * fallback nobody can see. */
const KIND_MESSAGES: Record<string, MessageKey> = {
  auth: "auth",
  rate_limit: "rate_limit",
  timeout: "timeout",
  too_long: "too_long",
  internal: "generic",
  upstream: "generic",
};

export function jobErrorText(
  job: JobErrorLike,
  t: (key: MessageKey) => string,
): string {
  return t(KIND_MESSAGES[job.error_kind ?? ""] ?? "generic");
}
