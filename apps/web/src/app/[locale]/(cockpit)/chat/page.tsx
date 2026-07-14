"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { useChatSessions } from "@/components/chat/chat-sessions";
import { ApiError, createChatSession } from "@/lib/api";

/** `/{locale}/chat` — the nav link's target, and now a REDIRECTOR: a
 * conversation lives at `/{locale}/chat/{sessionId}`, because a session id
 * that exists only in React state is exactly the bug this slice fixes.
 *
 * Resume-the-most-recent, don't-always-create: the tutor bouncing Today ->
 * Chat -> Today -> Chat should land back in the conversation he was having,
 * not in a new blank one (and not leave a trail of empty sessions behind
 * him). A brand-new session is created only when there is genuinely nothing
 * to resume — the sidebar's "New chat" button is the explicit way to start
 * another.
 *
 * `router.replace`, never `push`: this URL must not sit in the back stack, or
 * "back" from a conversation would land here and redirect straight forward
 * into it again.
 */
export default function ChatIndexPage() {
  const t = useTranslations("chat");
  const locale = useLocale();
  const router = useRouter();
  const { sessions, loading, error } = useChatSessions();
  const [createError, setCreateError] = useState<string | null>(null);

  // The list load and the redirect both fire from effects; without this ref
  // Strict Mode's double-invoke would POST /chat twice and leave one orphan
  // session behind on every single dev visit — the precise failure mode the
  // old `chat-panel.tsx` had in production.
  const routedRef = useRef(false);

  useEffect(() => {
    if (routedRef.current || loading || error) return;
    routedRef.current = true;

    if (sessions.length > 0) {
      router.replace(`/${locale}/chat/${sessions[0].id}`);
      return;
    }
    createChatSession(null, locale)
      .then(({ session_id }) => router.replace(`/${locale}/chat/${session_id}`))
      .catch((err) => setCreateError(err instanceof ApiError ? err.detail : t("sessionError")));
  }, [sessions, loading, error, router, locale, t]);

  if (createError || error) {
    return (
      <p role="alert" data-testid="chat-session-error" className="text-sm text-destructive">
        {createError ?? error}
      </p>
    );
  }

  return (
    <div
      role="status"
      data-testid="chat-resuming"
      className="flex items-center gap-2 text-sm text-muted-foreground"
    >
      <Loader2 className="size-4 animate-spin" />
      {t("resuming")}
    </div>
  );
}
