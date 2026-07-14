"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";
import { ApiError, listChatSessions, type ChatSessionSummary } from "@/lib/api";

interface ChatSessionsValue {
  sessions: ChatSessionSummary[];
  loading: boolean;
  error: string | null;
  /** Re-reads `GET /chat`. Called by the sidebar after a rename/delete, and
   * by `ChatPanel` after a turn completes — a session's title and preview are
   * SERVER-derived (the title from its first user message), so the sidebar
   * cannot synthesize them and must ask. */
  refresh: () => Promise<void>;
  /** Local, optimistic title swap so a rename doesn't wait a round-trip to
   * show. The `refresh` that follows is what makes it authoritative. */
  applyTitle: (sessionId: string, title: string) => void;
  removeSession: (sessionId: string) => void;
}

const ChatSessionsContext = createContext<ChatSessionsValue | null>(null);

/** Owns the chat sidebar's session list, mounted in `chat/layout.tsx` — a
 * LAYOUT, not a page, so it survives navigation between `/{locale}/chat/{a}`
 * and `/{locale}/chat/{b}`: switching conversations must not refetch (and
 * re-flash) the list you clicked from.
 *
 * The list also has to be reachable from `ChatPanel`, which lives under this
 * layout in the page slot and is the only thing that knows a turn just
 * landed. React context rather than prop-drilling because layout `children`
 * cannot take props at all — there is no other seam between a layout and the
 * page it wraps.
 */
export function ChatSessionsProvider({ children }: { children: ReactNode }) {
  const t = useTranslations("chat.history");
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    return listChatSessions()
      .then((rows) => {
        setSessions(rows);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("loadError")))
      .finally(() => setLoading(false));
  }, [t]);

  // Strict Mode double-invokes mount effects in dev; a GET is idempotent, but
  // the ref keeps the list from flashing twice on every dev load.
  const loadedRef = useRef(false);
  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    void refresh();
  }, [refresh]);

  const applyTitle = useCallback((sessionId: string, title: string) => {
    setSessions((prev) => prev.map((s) => (s.id === sessionId ? { ...s, title } : s)));
  }, []);

  const removeSession = useCallback((sessionId: string) => {
    setSessions((prev) => prev.filter((s) => s.id !== sessionId));
  }, []);

  return (
    <ChatSessionsContext.Provider
      value={{ sessions, loading, error, refresh, applyTitle, removeSession }}
    >
      {children}
    </ChatSessionsContext.Provider>
  );
}

export function useChatSessions(): ChatSessionsValue {
  const value = useContext(ChatSessionsContext);
  if (!value) {
    throw new Error("useChatSessions() must be used inside <ChatSessionsProvider> (chat/layout.tsx)");
  }
  return value;
}
