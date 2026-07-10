import Link from "next/link";
import { useTranslations } from "next-intl";
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
 * (rendered as an inline `next/link` under the bubble's text). */
export interface ChatDisplayMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  link?: ChatMessageLink;
}

interface MessageListProps {
  messages: ChatDisplayMessage[];
}

/** The chat transcript: user turns right-aligned, assistant turns (plain
 * narration, rejection narration, and the job-succeeded hand-off) left-
 * aligned — the standard "who said this" convention for a chat UI. This
 * component is deliberately dumb: `chat-panel.tsx` owns every state
 * transition that produces a `ChatDisplayMessage` (including the
 * `awaiting_approval` case, which renders as `ApprovalCard` instead of a
 * plain message here — see that component's own docstring for why it isn't
 * duplicated in both places); this only renders the resulting list. */
export function MessageList({ messages }: MessageListProps) {
  const t = useTranslations("chat");

  if (messages.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="chat-empty">
        {t("empty")}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="message-list">
      {messages.map((message) => (
        <div
          key={message.id}
          data-testid="chat-message"
          data-role={message.role}
          className={cn("flex", message.role === "user" ? "justify-end" : "justify-start")}
        >
          <div
            className={cn(
              "max-w-[80%] rounded-2xl px-3.5 py-2 text-sm whitespace-pre-wrap",
              message.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted text-foreground",
            )}
          >
            {message.content}
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
        </div>
      ))}
    </div>
  );
}
