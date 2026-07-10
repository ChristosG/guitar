"use client";

import { useTranslations } from "next-intl";
import { ChatPanel } from "@/components/chat/chat-panel";

// Client component for the same reason as knowledge/students/curricula/
// artifacts pages: `ChatPanel` talks to the API straight from the browser
// (see lib/api.ts's top docstring on why — the app owns CORS specifically so
// the browser, not the Next.js server, is the caller), which is also what
// makes it visible to Playwright's `page.route`.
export default function ChatPage() {
  const t = useTranslations("chat");

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="chat-heading">
          {t("heading")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      <ChatPanel />
    </div>
  );
}
