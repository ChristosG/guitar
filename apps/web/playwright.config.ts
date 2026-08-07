import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  // ONE WORKER, AND THE CONFIG BELOW IS WHY — this is not a performance
  // preference, it is the only setting consistent with the rest of this file.
  // `webServer` pins a FIXED port and sets `reuseExistingServer: false`, so
  // there is exactly one `next dev` for the whole run and it is shared by every
  // worker. Playwright's default (half the CPU count — 10 on this machine) then
  // points ten parallel workers at one dev server that compiles routes on
  // demand, and tests start failing on `toBeVisible` timeouts scattered across
  // specs that have nothing to do with each other.
  //
  // That happened on 2026-08-07 and cost a real diagnosis: 76 of 204 failed,
  // including specs untouched for weeks, and every one of them passed again at
  // `--workers=1`. Left unpinned it reads as "the suite is flaky", which is the
  // most expensive kind of wrong.
  workers: 1,
  webServer: {
    // Host port 3000 is occupied by an unrelated pre-existing process on
    // this machine, so the local/test dev server binds to 3100 instead
    // (production runs in Docker on its own network namespace and is
    // unaffected — see apps/web/Dockerfile / docker-compose.yml).
    command: "npm run dev",
    port: 3100,
    env: { PORT: "3100" },
    reuseExistingServer: false,
  },
  use: {
    baseURL: "http://localhost:3100",
  },
});
