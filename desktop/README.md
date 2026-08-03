# Angel OS desktop shell

A Tauri 2.x macOS/Linux app that bundles the entire Angel OS stack — no
Docker, no installs, no network beyond `api.anthropic.com` (the tutor's own
Claude API key, pasted in Settings). The shell supervises four bundled
runtimes and shows the web app in a native window.

## Architecture

```
Angel OS.app / .deb
└── resources/
    ├── pg/            Postgres 16.9 (theseus-rs full distribution) + pgvector 0.8.0
    ├── python/        CPython 3.12 (python-build-standalone) with apps/api
    │                  pip-installed into site-packages
    ├── api/           app/ + alembic/ + alembic.ini (uvicorn's cwd)
    ├── node/bin/node  Node 22 LTS (single binary)
    ├── web/           apps/web `.next/standalone` + `.next/static` + `public`
    ├── models/e5-small/  model.onnx + tokenizer.json (multilingual-e5-small fp32)
    └── seed/          OPTIONAL: db.dump + media/ + manifest.json (first-run seed)

boot:  splash → dirs → instance guard → secrets → PICK THE PG PORT
       → firstrun (initdb + Greek-collation guard + seed — minutes, on a fresh
         box: a 250MB pg_restore and a media copy)
       → pg_ctl -w start → alembic upgrade head
       → PICK THE APP PORTS (web, api — last pair preferred) → write meta.json
       → uvicorn :<api> → node :<web>  (api child gets CORS_ORIGINS=:<web>)
       → poll /health/ready (db && embed; a 409 llm_not_configured also passes —
         it is the honest "no key pasted yet" state and proves the API is up)
       → main window at EXACTLY http://localhost:<web>, with
         window.__GT_API_BASE__ = "http://localhost:<api>" injected before any
         page script runs
```

**Where those two picks sit in that list is the design, not the code's
tidiness.** A bind-probe is only a claim about the instant it ran (see Ports
below), so every port is chosen as late as it possibly can be — one rule, two
picks, three children: the app PAIR immediately before the uvicorn/node spawns
that bind it, the postgres port immediately before `pg_ctl start`. Picking all
three up front, which is the natural-looking place and what this file used to
describe, would put initdb, a `pg_restore`, a media copy and alembic between
probe and bind on a first run — turning a microsecond window into a multi-minute
one, during which anything on the machine can take the port. `main.rs` says so
at both call sites; moving either pick "somewhere cleaner" reopens the race.

## Ports

**No app port is frozen any more; the ORIGIN still is.** Both app ports are
derived at boot by `firstrun::pick_app_ports`, which bind-probes the loopback
interface (`TcpListener::bind`, listener dropped immediately) and takes, in
order:

1. **the pair this install used last time**, if both are still free —
   `meta.json` in the data dir records `web_port`/`api_port`, rewritten on
   every boot right after the pick;
2. **8790 / 8791**, the historical pair, when nothing is remembered or the
   remembered pair is taken;
3. **the first free pair scanning upward** — web from 8790, api from the first
   free port *above* the chosen web port, so the api is always strictly above
   the web port and the two can never be the same number. The web candidate is
   the outer loop: if every api candidate above a free web port is taken, the
   scan backtracks to the next web port rather than giving up (a blocked-off
   band is exactly what a corporate agent or another Electron app leaves
   behind). The postgres port is excluded from both, so the three never
   collide.

So a box already running the compose webapp on 8790/8791 boots anyway: the app
rolls past it instead of refusing to start. Only an entirely exhausted range
raises the bilingual "no free application port" dialog. Postgres is picked the
same way in 5434–5444 and passed via `pg_ctl -o "-p …"`, so the conf file's
port is irrelevant.

The bind-probe is a TOCTOU check by construction — the socket must be closed
before the child can be handed the number — which is exactly why the pick sits
where it does in the boot list above: microseconds before the bind, never
minutes. A lost race surfaces as a child that exits immediately, which the
supervisor already reports as "the backend stopped"; it is not silently retried.
This is also why INSTALL.txt promises the tutor only that the app "normally"
starts alongside other programs, and tells him to quit any other copy of
Angel OS if it ever says it cannot start — that (plus a truly exhausted
range) is the surviving failure path, and the one action that fixes it.

### Which ports did this launch actually pick?

Never assume 8790/8791 — and do not send anyone to stderr for the answer. A
`.app` double-clicked in Finder has no terminal attached at all (its output goes
to the macOS system log), and a `.deb` started from the desktop menu does no
better (the journal, at best). That is the whole reason the pick is written to
two FILES the moment it is made.

**The app log** — one line per boot, appended right after the pick, with the
reason in words:

```
2026-07-30 09:14:02Z ports: web=8792 api=8793 postgres=5434 (the usual pair was
taken, so these were chosen instead; the page is told the API base explicitly)
```

(one line in the file, wrapped here. The parenthesis is one of three: kept from
the last launch / the usual pair / the usual pair was taken.)

| | path |
|---|---|
| macOS | `~/Library/Logs/AngelOS/app.log` |
| Linux | `${XDG_STATE_HOME:-~/.local/state}/guitar-tutor/log/app.log` |

It is append-only and never truncated (unlike the three child logs), so the LAST
`ports:` line is the one this launch chose, and the ones above it are history.
Over the phone the tutor does not have to find that path: **Backend ▸ Show
Logs** in the menu bar opens the containing folder in Finder / the file manager.

**meta.json** — the same pair as data instead of prose, rewritten on every boot
(`web_port` / `api_port`, alongside `pg_port`):

| | path |
|---|---|
| macOS | `~/Library/Application Support/AngelOS/meta.json` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/guitar-tutor/meta.json` |

`paths::app_log` does still mirror every line to stderr, so launching the binary
from a terminal shows all of this live — but that is the developer's path, not
something to ask the tutor for.

### Why step 1 exists: browser storage is partitioned by port

The webview is a browser, and an *origin* includes the port:
`http://localhost:8790` and `http://localhost:8792` are two different origins,
hence two different `localStorage` (and IndexedDB) buckets. A rolled port
therefore silently resets every localStorage-backed UI preference — today the
next-themes dark/light choice and `curricula.dismissedInterviews` (the
dismissed resume-interview chips) — which reads to the tutor as "the app forgot
my settings". Nothing in the database is affected (that is postgres, not the
browser) and the session cookie survives too, because cookies are keyed by
*host* and ignore the port. Preferring the previously-used pair makes the
storage reset the rare case instead of an every-launch one.

### The origin, the injected global, CORS

The window loads a **literal `localhost`** origin — never 127.0.0.1, never
`tauri://`. That is load-bearing: the session cookie is host-only, and the web
bundle's *fallback* derives its API base from the origin's hostname. Only the
port floats.

Which is exactly why the port can no longer be derived from the origin, so the
shell **tells** the page where the API landed, via Tauri's
`WebviewWindowBuilder::initialization_script` — i.e. before any page script
runs (`main.rs::show_main_window`):

```js
window.__GT_API_BASE__ = "http://localhost:<apiport>"   // string, no trailing slash
```

`apps/web/src/lib/api.ts::resolveApiBase` prefers that global over everything
else — **but only when the page's own origin is loopback**
(`window.location.hostname` is `localhost` or `127.0.0.1`). Off loopback the
global is ignored outright, as is a missing, empty or non-string one anywhere;
all of those fall through to `NEXT_PUBLIC_API_BASE` and then to the origin
derivation (`localhost` → `:8791`), so the browser deployment is untouched — the
desktop path is purely additive. The name `__GT_API_BASE__` is a string contract
across two languages that nothing type-checks, so CI greps for it on both sides
(see CI below).

**That gate is a security control, not a tidiness rule.** ONE bundle ships to
both this shell and `guitar.cgrigoriadis.online`, and the resolved base is where
every credentialed request goes — the login POST included. Ungated, anything
that got a moment of script execution on the public site (an injected tag, a
compromised dependency, a stored-XSS sink) could set one global before
`lib/api.ts` evaluates and quietly aim the tutor's password and session at a
host of its choosing: no navigation, nothing on screen, nothing to notice. On a
loopback origin the shell is the only thing that could have served the page, so
the global has a legitimate source there and nowhere else — and since the window
always loads a literal `localhost` origin (above), the gate costs the desktop
exactly nothing. `apps/web/tests/api-base.spec.ts` pins both halves ("on a
NON-loopback origin the injected global is ignored", and the rendered export
href obeys the same gate).

Because web and API sit on two different ports of the same host, the browser
enforces CORS between them even with auth off, and `config.py`'s default
`CORS_ORIGINS` hardcodes `:8790` — a rolled web port would fail preflight as an
opaque `TypeError: Failed to fetch`. So the supervisor hands the api child
`CORS_ORIGINS=http://localhost:<web>,http://127.0.0.1:<web>` (origins are
compared as strings, so both spellings are listed). The API sets
`allow_credentials=True`, so a wildcard is not an option.

A backend restart from the menu/crash dialog deliberately **reuses the same
pair** rather than rescanning: the live window was told the API base when it
was created, and only a full relaunch can tell it a different one.

## On-disk layout (survives updates)

| what | macOS | Linux |
|---|---|---|
| data (pgdata/, media/, secrets/, meta.json) | `~/Library/Application Support/AngelOS` | `${XDG_DATA_HOME:-~/.local/share}/guitar-tutor` |
| logs (app.log, postgres.log, api.log, web.log) | `~/Library/Logs/AngelOS` | `${XDG_STATE_HOME:-~/.local/state}/guitar-tutor/log` |

`secrets/secrets.json` (chmod 600) holds APP_SECRET + ENCRYPTION_SECRET,
generated once and **never regenerated** — ENCRYPTION_SECRET encrypts the
tutor's stored Anthropic key; losing it bricks the key. An unreadable file is
a fatal error, not a rewrite. The three CHILD logs are truncated at startup when
>10MB; `app.log` — the shell's own, carrying the port line and every FATAL — is
appended to forever, because it is the one file support has to read backwards.

## Building locally

Stage the four runtimes (host must match TARGET for python/postgres — they
execute target binaries), then build:

```sh
export TARGET=linux-x64          # or darwin-arm64 on an Apple Silicon Mac
desktop/scripts/stage-web.sh     # npm ci + next build → resources/web
desktop/scripts/stage-node.sh    # nodejs.org dist, bin/node only
desktop/scripts/stage-python.sh  # python-build-standalone + pip install apps/api
desktop/scripts/stage-model.sh   # e5-small model.onnx + tokenizer.json
desktop/scripts/stage-postgres.sh  # PG 16.9 + pgvector built against the
                                   # bundled tree's own pg_config (no host
                                   # postgres needed — just cc/make/git)
desktop/scripts/smoke.sh         # boots the staged tree's API tier end-to-end
cd desktop/src-tauri && cargo tauri build --bundles deb   # or: app (macOS)
```

Downloads are cached under `desktop/.cache/`. `RES_DIR=/some/scratch` reroutes
staging output (used for dry runs). `stage-postgres.sh` ends with a hard
acceptance test — initdb from the assembled tree, `CREATE EXTENSION vector`,
a `vector(384)` insert + `<=>` ORDER BY, and `SELECT lower('ΚΙΘΑΡΑ')='κιθαρα'`
— so a broken tree can never be staged (or cached by CI).

Linux build deps: `libwebkit2gtk-4.1-dev build-essential curl wget file
libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`.

## CI

`.github/workflows/desktop-macos.yml` (macos-14 → .dmg with INSTALL.txt) and
`desktop-linux.yml` (ubuntu-22.04 → .deb; AppImage is deliberately off —
linuxdeploy chokes opaquely on the 1.3GB payload — and to keep that decision
in one place it is also absent from tauri.conf.json's bundle targets, so a
bare `cargo tauri build` on a dev box cannot wander into the same opaque
failure CI sidesteps with explicit `--bundles`). Both trigger on pushes
to `desktop`, tags `desktop-v*`, and manual dispatch; tags publish a GitHub
release.

**All the gates live in desktop-linux.yml** (macOS runners have no Docker, and
duplicating the platform-independent ones would only double the failure
surface). The `build` job there needs both test jobs; the macOS workflow is
independent and does not wait for them, so a red gate blocks the .deb and is
meant to be read as "this commit is broken, .dmg included".

- `test-api` — the API pytest suite against a pgvector service container mapped
  to host port 5434 (the port `tests/conftest.py` hardcodes).
- `test-web` — the **API-base contract gate** (below) plus the hermetic
  Playwright suite from `apps/web`: every API call is `page.route`-mocked and
  the config boots its own `next dev`, so it needs no database, no API and no
  network. It runs on its own runner where nothing else binds a port, and
  `reuseExistingServer: false` means a squatted 3100 fails loudly instead of
  quietly testing someone else's server.
- `cargo test` in the build job, placed *before* the ~40 minutes of staging so
  a broken port scan or origin spelling fails in seconds (dev profile — the
  release profile is fat-LTO'd for the shipped binary).

The **contract gate** greps the exact symbol `__GT_API_BASE__` in both
`desktop/src-tauri/src/main.rs` (which injects it) and `apps/web/src/lib/api.ts`
(which reads it), and fails with an explanation naming both files. Renaming one
side alone compiles, type-checks and ships — and silently reintroduces the bug
this whole design removes: the page falls back to deriving `:8791` from its
origin and calls a port the shell does not own. `apps/web/tests/api-base.spec.ts`
is the behavioural half of the same contract (injected wins / absent falls back
/ malformed falls back), and it runs in `test-web`.

Heavy stages are cached: staged python keyed on the pyproject.toml hash, the
postgres tree keyed on zonky+pgvector versions (saved only after its
acceptance test passed, because a failed script fails the job and an
actions/cache entry is only written by a green job), model + node dists by
version. The macOS bundle is ad-hoc signed inside-out (`codesign -s -` on
every executable/dylib, then the .app) — enough for "Open Anyway", see
INSTALL.txt.

Both jobs then run `desktop/scripts/smoke.sh` against the resources the
*bundler* actually copied (Linux extracts the .deb first; a bad resources glob
once shipped an empty app), so the gate is "the bundled tree really boots", not
"the staging scripts ran". Smoke boots its own postgres and scans its own high
ports (pg 54410–54430, api 18791–18811), so it collides with nothing else on
the runner, nor with a locally running app — and it hardcodes no app port, so
it stays correct however the shell's derived pair lands.

## The seed

`desktop/scripts/make-seed.sh` runs on the HOST BOX ONLY (never CI): it
`pg_dump -Fc`'s the live compose deployment and tars the media volume — the
exact read-only invocations `scripts/backup.sh` has proven — into

```
seed.tar.gz = db.dump (pg_dump -Fc) + media/ + manifest.json
```

and uploads it to the `seed-data` GitHub release. CI downloads it
(`continue-on-error`: a missing release just renames the artifact `-noseed`)
and unpacks it into `resources/seed/`. Because CI unpacks the archive at
build time, the app needs **no archive code at runtime**: first run
`pg_restore --no-owner --no-privileges` the dump and plain-copies `media/`
into the data dir. Keep this shape stable — the future `/backup` endpoints
are meant to emit the same archive.

## Replacing the placeholder icon

`desktop/src-tauri/icons/` is generated by `desktop/scripts/gen-icons.py`
(Pillow): a placeholder rounded square + sound-hole + strings. Replace it
either by dropping a 1024×1024 `icon-source.png` next to the script and
re-running it, or with the official generator from any square PNG:

```sh
cargo tauri icon path/to/your-icon.png   # regenerates desktop/src-tauri/icons/
```

## Known trade-offs / deviations

- **Postgres binaries come from theseus-rs/postgresql-binaries, not zonky.**
  The plan named zonky's Maven jars, but their txz ships ONLY
  initdb/pg_ctl/postgres (verified against
  embedded-postgres-binaries-linux-amd64-16.9.0.jar) — no psql (the Greek
  collation assertion), no createdb, no pg_restore (seed restore), no
  pg_isready (the watchdog). theseus-rs publishes the same-purpose embedded
  builds as the FULL distribution, sha256-signed, for exactly our two targets;
  its relocatable pg_config also lets pgvector build against the bundled tree
  itself (perfect ABI match, `OPTFLAGS=""` for portability — pgvector's
  default -march=native would SIGILL on older CPUs).

- **Greek collation**: first run asserts `lower('ΚΙΘΑΡΑ')='κιθαρα'` via psql;
  on failure it re-initdbs with `--locale-provider=icu --icu-locale=el` when
  the bundled build supports ICU, else shows a fatal bilingual dialog. Greek
  search is the product — a cluster that can't fold Greek case is not allowed
  to exist.
- **Crash dialogs are two-step** (Restart → More… → Show logs/Quit): the
  dialog plugin offers at most two buttons per dialog.
- **AUTH_ENABLED=0**: single-user app on 127.0.0.1; both API listeners bind
  loopback only. The web bundle is built with `NEXT_PUBLIC_AUTH_ENABLED=0` to
  match.
