# GuitarTutor desktop shell

A Tauri 2.x macOS/Linux app that bundles the entire GuitarTutor stack — no
Docker, no installs, no network beyond `api.anthropic.com` (the tutor's own
Claude API key, pasted in Settings). The shell supervises four bundled
runtimes and shows the web app in a native window.

## Architecture

```
GuitarTutor.app / .deb / .AppImage
└── resources/
    ├── pg/            Postgres 16.9 (theseus-rs full distribution) + pgvector 0.8.0
    ├── python/        CPython 3.12 (python-build-standalone) with apps/api
    │                  pip-installed into site-packages
    ├── api/           app/ + alembic/ + alembic.ini (uvicorn's cwd)
    ├── node/bin/node  Node 22 LTS (single binary)
    ├── web/           apps/web `.next/standalone` + `.next/static` + `public`
    ├── models/e5-small/  model.onnx + tokenizer.json (multilingual-e5-small fp32)
    └── seed/          OPTIONAL: db.dump + media/ + manifest.json (first-run seed)

boot:  splash → firstrun (initdb + Greek-collation guard + secrets + seed)
       → pg_ctl -w start → alembic upgrade head → uvicorn :8791 → node :8790
       → poll /health/ready (db && embed; a 409 llm_not_configured also passes —
         it is the honest "no key pasted yet" state and proves the API is up)
       → main window at EXACTLY http://localhost:8790
```

Ports: web **8790** and API **8791** are FROZEN — the web bundle derives its
API base from the literal `localhost` origin and the session cookie is
host-only. Postgres picks the first free port in 5434–5444 at every start
(passed via `pg_ctl -o "-p …"`, so the conf file port is irrelevant).

## On-disk layout (survives updates)

| what | macOS | Linux |
|---|---|---|
| data (pgdata/, media/, secrets/, meta.json) | `~/Library/Application Support/GuitarTutor` | `${XDG_DATA_HOME:-~/.local/share}/guitar-tutor` |
| logs (postgres.log, api.log, web.log) | `~/Library/Logs/GuitarTutor` | `${XDG_STATE_HOME:-~/.local/state}/guitar-tutor/log` |

`secrets/secrets.json` (chmod 600) holds APP_SECRET + ENCRYPTION_SECRET,
generated once and **never regenerated** — ENCRYPTION_SECRET encrypts the
tutor's stored Anthropic key; losing it bricks the key. An unreadable file is
a fatal error, not a rewrite. Logs are truncated at startup when >10MB.

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
desktop/scripts/smoke.sh         # boots the staged tree end-to-end
cd desktop/src-tauri && cargo tauri build --bundles appimage,deb   # or: app (macOS)
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
`desktop-linux.yml` (ubuntu-22.04 → .deb + .AppImage). Both trigger on pushes
to `desktop`, tags `desktop-v*`, and manual dispatch; tags publish a GitHub
release. Linux also runs the API pytest gate against a pgvector service
container mapped to host port 5434 (the port `tests/conftest.py` hardcodes) —
macOS runners have no Docker, so the gate deliberately lives only there.
Heavy stages are cached: staged python keyed on the pyproject.toml hash, the
postgres tree keyed on zonky+pgvector versions (saved only after its
acceptance test passed, because a failed script fails the job and an
actions/cache entry is only written by a green job), model + node dists by
version. The macOS bundle is ad-hoc signed inside-out (`codesign -s -` on
every executable/dylib, then the .app) — enough for "Open Anyway", see
INSTALL.txt.

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
