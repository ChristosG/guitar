"use client";

import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";

export function ThemeToggle() {
  // Use resolvedTheme (the actually-rendered "light"/"dark"), not `theme`, which stays
  // "system" until an explicit choice — otherwise the first click is a no-op for
  // visitors whose OS prefers dark.
  const { resolvedTheme, setTheme } = useTheme();
  return (
    <Button
      data-testid="theme-toggle"
      variant="outline"
      onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
    >
      🌓
    </Button>
  );
}
