import Link from "next/link";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { BookOpen } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import type { AnchorHTMLAttributes } from "react";
import { AddToCurriculumDialog } from "@/components/chat/add-to-curriculum-dialog";
import type { ChatCitation } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface ChatMessageLink {
  label: string;
  href: string;
}

/** One transcript entry `chat-panel.tsx` has already decided to show. Not a
 * 1:1 mirror of the API's `MessageOut` — this also carries turns the API
 * never persists as a message row at all (the `job_pending` -> succeeded
 * hand-off is synthesized client-side once a poll completes; see
 * `chat-panel.tsx`'s `pollJob`), plus an optional `link` for that one case
 * (rendered as an inline `next/link` under the bubble's text). `citations`
 * (Plan 11 Task 3, C4) is the grounding `sendChatMessage`/`streamChatMessage`
 * came back with for an assistant turn that had something to cite — see
 * `renderCitations` below for how it becomes chips. */
export interface ChatDisplayMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  link?: ChatMessageLink;
  citations?: ChatCitation[] | null;
}

interface MessageListProps {
  messages: ChatDisplayMessage[];
  /** The session these messages belong to — carried onto the per-message
   * "add to curriculum" action for provenance (`meta.chat_session_id` on the
   * created lesson). Optional: history rows synthesized client-side render
   * fine without it. */
  sessionId?: string;
}

/** `react-markdown`'s default `<a>` renderer carries neither `target` nor
 * `rel` — harmless for same-origin navigation, but a model-authored link can
 * point anywhere, and a plain same-tab `<a href>` with no `rel` hands the
 * destination page a live `window.opener` back into this app (the classic
 * `rel="noopener noreferrer"` gap). Every markdown-rendered link goes through
 * this component instead of the library default so that never happens,
 * regardless of what URL the model wrote. */
function MarkdownLink({ href, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
      {children}
    </a>
  );
}

/** Markdown renderer for an assistant bubble (Plan 11 Task 3, C4 — Chris's
 * exact complaint: "no markdown viewer" was leaving a well-structured answer
 * as an unreadable wall of asterisks). `remark-gfm` adds tables/strikethrough/
 * task lists on top of `react-markdown`'s CommonMark baseline. SANITIZED by
 * construction, not by an explicit allow-list step: `react-markdown` never
 * renders raw HTML unless a `rehype-raw`-style plugin is added, and none is
 * — the model's own output is Markdown syntax at most, never live HTML/JS,
 * no matter what it types. User messages are NOT run through this (see the
 * render branch below) — only the model's own text is untrusted enough to
 * need it, and a user's plain-text turn rendering literal asterisks back to
 * themselves would be a regression, not a feature.
 *
 * Headings/lists/tables/etc. are left at their default browser sizing
 * (no `prose` typography plugin in this project) but nudged to sit well
 * inside a narrow chat bubble: tight vertical rhythm, no default heading
 * font-size jump, and code blocks that scroll horizontally instead of
 * blowing out the bubble's `max-w`. */
function MarkdownContent({ text }: { text: string }) {
  return (
    <div
      className={cn(
        "flex flex-col gap-2 text-sm",
        "[&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:font-semibold",
        "[&_h1]:mt-1 [&_h2]:mt-1 [&_h3]:mt-1 first:[&_h1]:mt-0 first:[&_h2]:mt-0 first:[&_h3]:mt-0",
        "[&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-5 [&_ol]:pl-5 [&_li]:leading-snug",
        "[&_p]:leading-relaxed [&_strong]:font-semibold",
        "[&_a]:font-medium [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-2 hover:[&_a]:opacity-80",
        "[&_code]:rounded [&_code]:bg-background/60 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-xs",
        "[&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-background/60 [&_pre]:p-2 [&_pre]:text-xs",
        "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
        "[&_table]:w-full [&_table]:border-collapse [&_th]:border [&_th]:border-border [&_th]:px-2 [&_th]:py-1",
        "[&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1",
        "[&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-2 [&_blockquote]:text-muted-foreground",
      )}
    >
      <Markdown remarkPlugins={[remarkGfm]} components={{ a: MarkdownLink }}>
        {text}
      </Markdown>
    </div>
  );
}

/** Citation chips under a grounded answer (Plan 11 Task 3, C4 — the payoff
 * of Plan 11 Task 1's forced-retrieval work reaching the chat UI at all):
 * one chip per citation, reading "{source_title} · p.{page_no}", linking to
 * `/{locale}/library/{source_id}?page={page_no}` — the exact Reader route
 * (Plan 9) that opens the real scanned page a grounded answer came from.
 * Only citations with a `page_no` render a chip: `page_no`/`page_id` are
 * `null` for a hit whose chunk predates page-addressable ingest
 * (`_to_citation`'s own documented case on the API side), and a chip with
 * nowhere real to deep-link would misrepresent the "click it to verify"
 * promise this whole feature exists for. */
function CitationChips({ citations, locale }: { citations: ChatCitation[]; locale: string }) {
  const t = useTranslations("chat.citations");
  const linkable = citations.filter((c): c is ChatCitation & { page_no: number } => c.page_no != null);
  if (linkable.length === 0) return null;

  return (
    <div className="mt-2 flex flex-wrap gap-1.5" data-testid="citation-chips">
      {linkable.map((citation, i) => (
        <Link
          key={`${citation.source_id}-${citation.page_no}-${i}`}
          href={`/${locale}/library/${citation.source_id}?page=${citation.page_no}`}
          data-testid="citation-chip"
          className="inline-flex items-center gap-1 rounded-full border border-border bg-background/60 px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
        >
          <BookOpen className="size-3 shrink-0" />
          {citation.source_title} · {t("pageLabel", { page: citation.page_no })}
        </Link>
      ))}
    </div>
  );
}

/** The chat transcript: user turns right-aligned, assistant turns (plain
 * narration, rejection narration, and the job-succeeded hand-off) left-
 * aligned — the standard "who said this" convention for a chat UI. This
 * component is deliberately dumb: `chat-panel.tsx` owns every state
 * transition that produces a `ChatDisplayMessage` (including the
 * `awaiting_approval` case, which renders as `ApprovalCard` instead of a
 * plain message here — see that component's own docstring for why it isn't
 * duplicated in both places); this only renders the resulting list. */
export function MessageList({ messages, sessionId }: MessageListProps) {
  const t = useTranslations("chat");
  const locale = useLocale();

  if (messages.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="chat-empty">
        {t("empty")}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="message-list">
      {messages.map((message, index) => (
        <div
          key={message.id}
          data-testid="chat-message"
          data-role={message.role}
          className={cn("flex", message.role === "user" ? "justify-end" : "justify-start")}
        >
          <div
            className={cn(
              "flex flex-col gap-1",
              message.role === "user" ? "max-w-[75%] items-end" : "max-w-[88%] items-start",
            )}
          >
            {/* A subtle role indicator (Task 2) — a small muted caption above
                each bubble. Alignment + bubble color already say "who said
                this" at a glance; this just names it explicitly for anyone
                scanning quickly, without any avatar/icon weight. */}
            <span
              data-testid="chat-message-role"
              className="px-1 text-[11px] font-medium tracking-wide text-muted-foreground/70 uppercase"
            >
              {message.role === "user" ? t("roleUser") : t("roleAssistant")}
            </span>
            <div
              className={cn(
                "rounded-2xl px-4 py-2.5 text-sm shadow-sm",
                message.role === "user"
                  ? "whitespace-pre-wrap bg-primary text-primary-foreground"
                  : "bg-muted text-foreground",
              )}
            >
              {message.role === "assistant" ? <MarkdownContent text={message.content} /> : message.content}
              {message.role === "assistant" && message.citations && message.citations.length > 0 && (
                <CitationChips citations={message.citations} locale={locale} />
              )}
              {message.link && (
                <Link
                  href={message.link.href}
                  data-testid="chat-message-link"
                  className="mt-1 block underline underline-offset-2"
                >
                  {message.link.label}
                </Link>
              )}
            </div>
            {/* The per-answer action row. Only real, non-empty assistant prose
                gets it — an approval hand-off line or an empty placeholder is
                not a lesson. */}
            {message.role === "assistant" && message.content.trim().length > 0 && (
              <div className="mt-0.5">
                <AddToCurriculumDialog
                  content={message.content}
                  citations={message.citations}
                  sessionId={sessionId}
                  question={
                    messages
                      .slice(0, index)
                      .reverse()
                      .find((m) => m.role === "user")?.content
                  }
                />
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
