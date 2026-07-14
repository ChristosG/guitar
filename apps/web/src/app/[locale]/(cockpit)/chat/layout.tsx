"use client";

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { ChatSessionsProvider } from "@/components/chat/chat-sessions";
import { ChatSidebar } from "@/components/chat/chat-sidebar";

/** The chat cockpit's frame: heading + conversation sidebar, wrapped around
 * BOTH `/{locale}/chat` (the resume/create redirector) and
 * `/{locale}/chat/{sessionId}` (a conversation).
 *
 * It has to be a layout, not a shared component in each page: an App Router
 * layout persists across navigation between the routes it wraps, so switching
 * conversations in the sidebar re-renders only the panel, and the session list
 * (fetched once, in `ChatSessionsProvider`) does not flicker or refetch on
 * every click.
 */
export default function ChatLayout({ children }: { children: ReactNode }) {
  const t = useTranslations("chat");

  return (
    <ChatSessionsProvider>
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-2xl font-semibold" data-testid="chat-heading">
            {t("heading")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subheading")}</p>
        </div>

        <div className="grid gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
          <ChatSidebar />
          <div className="min-w-0">{children}</div>
        </div>
      </div>
    </ChatSessionsProvider>
  );
}
