import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
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
