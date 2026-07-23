import * as React from "react"

import { cn } from "@/lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      // `supports-[field-sizing:content]:placeholder-shown:h-16` kills the
      // shrink-on-first-keystroke jump (Chris, 2026-07-23): under
      // `field-sizing: content` an EMPTY textarea is sized to its WRAPPED
      // placeholder, so on narrow screens a two-line Greek placeholder made
      // the box taller than the one-line content that replaced it — the box
      // visibly collapsed the moment he typed. Pinning the placeholder-shown
      // state to the same 64px the typed state resolves to (min-h-16) means
      // there is nothing to shrink FROM; content growth upward is untouched.
      // Scoped to @supports so browsers without field-sizing (which never had
      // the bug — they size by `rows`) keep their taller rows-driven empties.
      className={cn(
        "flex field-sizing-content min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-base transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        "supports-[field-sizing:content]:placeholder-shown:h-16",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }
