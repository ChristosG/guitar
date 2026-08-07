"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

interface ReviseScope {
  /** The module the drawer is currently pointed at, or null for the whole
   * course. */
  scope: { id: string; title: string } | null;
  /** Text the drawer should drop into its composer, or undefined. */
  seed: string | undefined;
  /** Bumped every time something asks the drawer to open. A COUNTER rather
   * than a boolean, because the drawer owns its own open/closed state (it can
   * be closed by Escape, the X, or the backdrop) and a boolean here would
   * fight it — a change in this number is a REQUEST to open, not a claim about
   * whether it is open. */
  openRequest: number;
  openForModule: (moduleId: string, moduleTitle: string, seed: string) => void;
  openWholeCourse: () => void;
  clearScope: () => void;
}

const Ctx = createContext<ReviseScope | null>(null);

/** Shared state between the module ⋯ menu and the revise drawer.
 *
 * IT LIVES ABOVE BOTH OF THEM, and it has to. `ReviseDrawer` and `TreeBoard`
 * are SIBLINGS on the curriculum page — the drawer is a floating panel in the
 * top row, the board is the page body — so a provider inside the drawer never
 * reaches the cards, and a prop would have to be threaded through `TreeBoard`
 * and then every level of `BlockCard`'s own recursion to reach one menu item.
 * `block-card.tsx` already notes that "no clean callback channel exists from
 * this card up to the page"; this is that channel, added once rather than
 * drilled.
 *
 * Consumers get `null` outside a provider, so a `BlockCard` rendered anywhere
 * else (a test, a future screen) simply omits the menu item instead of
 * crashing. */
export function ReviseScopeProvider({ children }: { children: ReactNode }) {
  const [scope, setScope] = useState<ReviseScope["scope"]>(null);
  const [seed, setSeed] = useState<string | undefined>(undefined);
  const [openRequest, setOpenRequest] = useState(0);

  const openForModule = useCallback((moduleId: string, moduleTitle: string, seedText: string) => {
    setScope({ id: moduleId, title: moduleTitle });
    setSeed(seedText);
    setOpenRequest((n) => n + 1);
  }, []);

  const openWholeCourse = useCallback(() => {
    // The drawer's own button clears any scope a previous module opening left
    // behind — otherwise "Revise with AI" would silently stay pointed at
    // whatever module was restructured last.
    setScope(null);
    setSeed(undefined);
    setOpenRequest((n) => n + 1);
  }, []);

  const clearScope = useCallback(() => {
    setScope(null);
    setSeed(undefined);
  }, []);

  const value = useMemo(
    () => ({ scope, seed, openRequest, openForModule, openWholeCourse, clearScope }),
    [scope, seed, openRequest, openForModule, openWholeCourse, clearScope],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useReviseScope(): ReviseScope | null {
  return useContext(Ctx);
}
