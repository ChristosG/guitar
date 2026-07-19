"use client";

import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

interface SuggestionChipsProps {
  /** 0-3 short "next move" strings, already trimmed/capped server-side
   * (`schemas/chat.py`'s `SuggestionsOut`) — rendered verbatim, one chip
   * each. Empty renders nothing at all, not an empty heading. */
  suggestions: string[];
  /** Mirrors `ChatPanel`'s own `composerDisabled` — a chip is a shortcut for
   * the composer, so it must go quiet exactly when the composer would
   * (sending, an approval open, a job in flight): the composer is ALWAYS
   * primary, a chip is never a second way past its own gate. */
  disabled?: boolean;
  /** Sends the clicked suggestion as the next user turn, through the SAME
   * send path a typed message takes (`ChatPanel`'s `sendContent`) — a chip is
   * a shortcut into the composer's own flow, never a parallel one. */
  onSelect: (suggestion: string) => void;
}

/**
 * "Next move" suggestion chips (chat overhaul, Piece B) — rendered below the
 * latest assistant message once `ChatPanel`'s non-blocking `POST /chat/{id}/
 * suggestions` fetch resolves. Deliberately subtle, secondary styling (a
 * muted outline pill, not a button-weight CTA): the composer is the one
 * primary way to talk to the copilot, and a chip is an optional shortcut
 * that should read as "here's a tap-to-skip-typing option", never as a menu
 * competing with the text box beneath it.
 *
 * A small, standalone component (not folded into `MessageList`) because it
 * is keyed on the CONVERSATION's live state (`ChatPanel`'s own `suggestions`/
 * `composerDisabled`), not on any one persisted message row — `MessageList`
 * stays the dumb renderer of what already happened; this renders what could
 * happen next.
 */
export function SuggestionChips({ suggestions, disabled, onSelect }: SuggestionChipsProps) {
  const t = useTranslations("chat.suggestions");

  if (suggestions.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5" data-testid="suggestion-chips">
      <span className="px-1 text-[11px] font-medium tracking-wide text-muted-foreground/70 uppercase">
        {t("heading")}
      </span>
      <div className="flex flex-wrap gap-1.5">
        {suggestions.map((suggestion, i) => (
          <button
            key={`${i}-${suggestion}`}
            type="button"
            data-testid="suggestion-chip"
            disabled={disabled}
            onClick={() => onSelect(suggestion)}
            className={cn(
              "rounded-full border border-border bg-background/60 px-3 py-1 text-left text-xs text-muted-foreground",
              "transition-colors hover:bg-background hover:text-foreground",
              "disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-background/60 disabled:hover:text-muted-foreground",
            )}
          >
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}
