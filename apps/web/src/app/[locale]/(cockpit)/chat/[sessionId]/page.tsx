"use client";

import { useParams } from "next/navigation";
import { ChatPanel } from "@/components/chat/chat-panel";

/** One conversation, addressed by id — the whole point of Stage 5.6. The
 * `key` is what makes switching sessions in the sidebar correct: without it
 * React would REUSE the `ChatPanel` instance across the navigation, keeping
 * the previous conversation's transcript, pending approval and in-flight
 * state while the new session's history loaded underneath it. Keyed, the old
 * panel unmounts and the new one hydrates from scratch.
 */
export default function ChatSessionPage() {
  const params = useParams<{ sessionId: string }>();
  const sessionId = params.sessionId;

  return <ChatPanel key={sessionId} sessionId={sessionId} />;
}
