"use client";

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export interface ConfirmOptions {
  /** What is about to happen, named. Never "Are you sure?". */
  title: string;
  /** The consequence, in the tutor's language — what is LOST, and whether it
   * comes back. This is the whole reason the dialog exists; a call site that
   * can count what it's about to destroy (a curriculum root: N modules, M
   * lessons) is expected to say so here. */
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Paints the accept button red and shows a warning glyph. Every current
   * caller is destructive; the flag exists so this hook can also front a
   * non-destructive "this will cost money / this cannot be undone" prompt
   * later without a second component. */
  destructive?: boolean;
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/** `const ok = await confirm({...}); if (!ok) return;` — deliberately the
 * whole API. Any shape that made a call site restructure its handler into
 * callbacks would have lost this argument with the twelve handlers that
 * needed guarding, and the app would still be one stray click away from
 * cascading a curriculum root. */
export function useConfirm(): ConfirmFn {
  const confirm = useContext(ConfirmContext);
  if (!confirm) {
    throw new Error("useConfirm() must be used inside <ConfirmProvider> (mounted in app/[locale]/layout.tsx)");
  }
  return confirm;
}

interface PendingConfirm {
  options: ConfirmOptions;
  settle: (ok: boolean) => void;
}

/** One dialog for the whole app, driven by a promise. Mounted once in the
 * locale layout so every client component below it can call `useConfirm()`
 * without threading a dialog through props.
 *
 * The promise is settled EXACTLY once, from `close()` — not from the accept
 * button alone. Escape, a backdrop click and the X all route through Base
 * UI's `onOpenChange(false)`, and if those paths didn't settle it, an
 * `await confirm(...)` would hang forever and the caller's `finally` (the
 * one that clears its `deleting` spinner) would never run. Cancel is the
 * default outcome of every ambiguous exit, which is also the right default
 * for a destructive prompt. */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const t = useTranslations("confirm");
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  // The accept button is deliberately NOT autofocused: a tutor who hits Enter
  // by reflex on a dialog he didn't expect must not thereby delete his book.
  const cancelRef = useRef<HTMLButtonElement>(null);

  const confirm = useCallback<ConfirmFn>((options) => {
    return new Promise<boolean>((resolve) => {
      setPending({ options, settle: resolve });
    });
  }, []);

  function close(ok: boolean) {
    setPending((current) => {
      current?.settle(ok);
      return null;
    });
  }

  const options = pending?.options;
  const destructive = options?.destructive ?? true;

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog
        open={pending !== null}
        onOpenChange={(next) => {
          if (!next) close(false);
        }}
      >
        {options && (
          <DialogContent data-testid="confirm-dialog" initialFocus={cancelRef} showCloseButton={false}>
            <DialogHeader>
              <DialogTitle data-testid="confirm-title" className="flex items-center gap-2">
                {destructive && <AlertTriangle className="size-4 shrink-0 text-destructive" aria-hidden />}
                {options.title}
              </DialogTitle>
            </DialogHeader>

            {options.body && (
              <DialogBody>
                <DialogDescription
                  data-testid="confirm-body"
                  className="whitespace-pre-line"
                  render={<div />}
                >
                  {options.body}
                </DialogDescription>
              </DialogBody>
            )}

            <DialogFooter>
              <Button
                ref={cancelRef}
                type="button"
                variant="outline"
                data-testid="confirm-cancel"
                onClick={() => close(false)}
              >
                {options.cancelLabel ?? t("cancel")}
              </Button>
              <Button
                type="button"
                variant={destructive ? "destructive" : "default"}
                data-testid="confirm-accept"
                onClick={() => close(true)}
              >
                {options.confirmLabel ?? t("confirm")}
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    </ConfirmContext.Provider>
  );
}
