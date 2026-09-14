"""`/backup` — the whole deployment in one file, and the way back from it.

ONE FORMAT, THREE USES. The archive this router emits is byte-for-byte the
shape `desktop/scripts/make-seed.sh` publishes as the desktop seed bundle, so
seed = backup = restore are a single format and a single restore path:

    guitar-backup-YYYY-MM-DD.tar.gz
    ├── db.dump         pg_dump -Fc of the whole database (custom format)
    ├── media/          settings.media_dir, whole (page scans)
    ├── superseded/     <data>/media_superseded_* — ONLY when they exist; see below
    └── manifest.json   {created, app_commit, pg_major, schema, superseded_media}

THE DISPLACED-LIBRARY CASE, and why `superseded/` is in the archive at all.
The desktop shell never deletes the tutor's data: when it has to get a database
out of the way it renames it, and it renames `<data>/media` alongside it as
`<data>/media_superseded_<stamp>` (`desktop/src-tauri/src/firstrun.rs`). Those
folders are SIBLINGS of `settings.media_dir`, not children, so a backup that
tarred only `media_dir` captured the STARTER library and not his — silently,
confidently, and at the exact moment he most needs a backup that works.

Two ways to close that, and this file takes the second:

  * REFUSE while any of them exist. Rejected. `GET /backup/export` is reached
    from a plain `<a href>` download in the settings page (see
    `apps/web/src/components/settings/backup-card.tsx`), so a 409 renders as a
    raw JSON body in the webview — an incomprehensible failure, traded for a
    silently wrong one, at the worst possible moment.
  * EXPORT THEM, under `superseded/<name>/`, and say so in the manifest. That is
    what happens.

Why NOT merged into `media/`: `media/` is what restore installs over
`settings.media_dir`, and the `db.dump` in the same archive does not index these
files. Merging them would make every restore re-orphan the lot on the next boot
sweep. `superseded/` keeps them recoverable by a human without letting an
automated restore invent a pairing — and restore puts them back beside
`media_dir` exactly where it found them, never overwriting one that is already
there.

Size, which is the real objection. The tutor's Mac has 8GB of RAM and not much
spare disk, and a `media_superseded_*` folder is a whole library. Three things
make it acceptable: the archive is built on disk and streamed from disk
(`FileResponse`), so RAM is not the constraint at any size; a displaced library
is the SAME library `media/` would have held on a healthy install, so the
archive is its normal size plus a starter library rather than double; and the
shell's displacement path closes permanently once one first run completes, so
"several of them at once" is a crash-loop artefact and not a steady state. A
backup that is large is a solvable problem. A backup that quietly contains the
wrong library is not a problem anyone will notice until it is too late.

A RESTORE EMPTIES THE SCHEMA FIRST, and does not ask `pg_restore --clean` to
do it. `--clean` emits a DROP for every object THE DUMP knows about and nothing
else, so its success depends on the *history* of the target database. The
webapp's database calls the chunk->page foreign key `chunk_page_id_fkey` (every
dump cut from it carries that name); a database the migrations built calls it
`fk_chunk_page_id`. Restoring the one into the other (CI on the v0.5.2 bundle,
2026-09-14) died on "cannot drop constraint page_pkey ... fk_chunk_page_id
depends on it" — with the tables already half-dropped. `_reset_schema` drops
the whole `public` schema instead: nothing survives to be named, so nothing can
be named wrongly. `pg_restore` then runs into an empty database with no
`--clean`/`--if-exists` at all.

AND BECAUSE THAT RESET DELETES, IT IS PRECEDED BY A SAFETY DUMP. This app
displaces, it never deletes: a restore that emptied the schema and then failed
inside `pg_restore` would leave the tutor with nothing, which is the one
outcome a restore may not produce. So before a single object is dropped,
`_safety_dump` writes a `pg_dump -Fc` of the database exactly as it is to
`<media_dir>/../pre-restore-<stamp>.dump` — a SIBLING of the media dir like the
`media_superseded_*` folders, never a temp dir, and never deleted afterwards
(it is a few MB, and it is his data). If that dump cannot be made, the restore
refuses and nothing has changed. If the upload's `pg_restore` fails after the
reset, `_put_back` empties the schema again, restores the safety dump, re-runs
`alembic upgrade head` if it carried one, and the 409 tells the tutor in his own
error message that his previous data was put back and from which file. The
media directory is never in question either way: the swap only happens after a
`pg_restore` that succeeded.

`manifest.schema` is the alembic revision the database was at when the backup
was taken (read from its `alembic_version` table; null when that table does not
exist — e.g. a dev/test DB built by `create_all`). Restore runs
`alembic upgrade head` after `pg_restore` precisely so an OLDER backup's schema
is brought current — and skips that step when the restored dump carries no
`alembic_version` at all, because "upgrade from base onto existing tables" is a
guaranteed pile of CREATE TABLE conflicts, not a migration.

PG_BIN_DIR (environment, deliberately NOT `app/config.py` — that file is owned
by a concurrent workstream): the directory holding `pg_dump`/`pg_restore`. The
desktop build ships its own PostgreSQL 16 binaries and points this at them;
empty/unset means "rely on PATH" (the docker image, where the postgres client
tools are installed normally).

CONCURRENCY: one module-level lock covers both endpoints — a restore that
starts while an export is still streaming (or a second restore while the first
is mid-`pg_restore`) would interleave two writers over the same database and
media directory. Busy -> 409 `backup_busy`, never a queue: the tutor pressing
the button twice wants one backup, not two. Restore additionally refuses while
any `GenerationJob` is `running` (409 `jobs_running`): a draft fan-out holds DB
sessions and writes blocks, and the schema reset would drop the tables out from
under it.

FAILURE TAXONOMY, mirrored from `routers/settings.py`'s style: HTTP 4xx with
`{"detail": {"code": ..., "message": ...}}`. The UI maps `code` to one Greek
sentence (`backup.errors.*` in `messages/el.json`); `message` is server prose
the tutor never reads. Codes: `not_a_backup`, `pg_major_mismatch`,
`pg_tools_missing`, `restore_failed`, `jobs_running`, `backup_busy`.

The BM25 index is NOT rebuilt here on purpose: it self-heals via its staleness
fingerprint (`app/brain/lexical.py`) — the first search after a restore sees
the corpus changed and rebuilds. Dense vectors ride inside `db.dump` itself.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.db import engine, get_db
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"])

# One lock for export AND restore — see the module docstring. `threading`, not
# `asyncio`: both endpoints are sync `def`s, so FastAPI runs them in the
# threadpool where an asyncio lock would be the wrong primitive.
_LOCK = threading.Lock()


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _pg_bin(name: str) -> str:
    """Path to a postgres client tool. PG_BIN_DIR is read at CALL time (not
    import) so tests — and the desktop launcher, which computes its resources
    path after startup — can set it late."""
    bin_dir = os.environ.get("PG_BIN_DIR", "")
    return str(Path(bin_dir) / name) if bin_dir else name


def _conn() -> tuple[list[str], dict[str, str]]:
    """`settings.database_url` (postgresql+psycopg://user:pass@host:port/db)
    translated to pg_dump/pg_restore CLI args + env. The password travels via
    PGPASSWORD, never argv — argv is world-readable in `ps`."""
    url = make_url(settings.database_url)
    args = [
        "-h", url.host or "localhost",
        "-p", str(url.port or 5432),
        "-U", url.username or "postgres",
        "-d", url.database or "postgres",
    ]
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    return args, env


def _server_pg_major(db: Session) -> int:
    version_num = int(db.execute(text("SELECT current_setting('server_version_num')")).scalar_one())
    return version_num // 10000


def _db_schema_rev(db: Session) -> str | None:
    """The alembic revision the database is actually at, or None when the
    `alembic_version` table does not exist (a `create_all` dev/test DB)."""
    if not _has_alembic_table(db):
        return None
    return db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()


def _has_alembic_table(db: Session) -> bool:
    return bool(
        db.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL")).scalar_one()
    )


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """One seam for every pg_dump/pg_restore invocation — what the unit tests
    monkeypatch to assert exact argv/env without a real postgres."""
    return subprocess.run(argv, env=env, capture_output=True, text=True)


# The desktop shell's prefix for a media tree it renamed out of the way. Kept in
# step with `SUPERSEDED_MEDIA_PREFIX` in `desktop/src-tauri/src/firstrun.rs` —
# the two are one contract and the shell's recovery note names this shape.
SUPERSEDED_MEDIA_PREFIX = "media_superseded_"

# What the archive carries them as. Deliberately NOT `media/`: see the module
# docstring.
SUPERSEDED_ARCNAME = "superseded"

_SUPERSEDED_NOTE = "READ-ME-superseded.txt"

_SUPERSEDED_NOTE_TEXT = """\
ΜΗΝ ΑΓΝΟΗΣΕΤΕ ΑΥΤΟΝ ΤΟΝ ΦΑΚΕΛΟ — DO NOT IGNORE THIS FOLDER

Αυτό το αντίγραφο ασφαλείας περιέχει ΚΑΙ αρχεία που το Angel OS είχε βάλει
στην άκρη (φάκελος "superseded"). Δεν χάθηκε τίποτα — αλλά σημαίνει ότι κάποια
στιγμή η βάση δεδομένων και τα αρχεία σας είχαν χωριστεί. Δείξτε αυτό το αρχείο
σε όποιον σας υποστηρίζει.

---

This backup contains a "superseded/" folder as well as "media/".

That means the machine it was taken from had one or more
"media_superseded_<timestamp>" folders beside its live media folder: the desktop
app had set a library aside (it never deletes) and had not been given it back.
THE db.dump IN THIS ARCHIVE DOES NOT INDEX THE FILES IN "superseded/" — that is
precisely why they are not in "media/", and why restoring this archive will not
put them into service on its own.

Restoring this archive puts each "superseded/<name>" back beside the live media
folder under its own name, exactly where it was found, and never overwrites one
that is already there. Pairing it back up with the database it belongs to is a
human decision: read "READ-ME-superseded-data.txt" in the app's data folder,
which records what was set aside, when, and what it goes with.
"""


def _displaced_media_dirs() -> list[Path]:
    """`<media_dir>/../media_superseded_*` — the libraries the desktop shell has
    renamed out of the way and not yet been given back.

    Siblings, not children, which is the whole reason they were being missed.
    Symlinks are skipped: this list is fed to `tar.add`, and following a link
    out of the data folder is not something an export gets to do.
    """
    media_dir = Path(settings.media_dir)
    parent = media_dir.parent
    if not parent.is_dir():
        return []
    found = [
        p
        for p in parent.iterdir()
        if p.name.startswith(SUPERSEDED_MEDIA_PREFIX)
        and p.is_dir()
        and not p.is_symlink()
    ]
    return sorted(found, key=lambda p: p.name)


def _tree_bytes(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


# --------------------------------------------------------------------------
# EXPORT
# --------------------------------------------------------------------------

@router.get("/export")
def export_backup(db: Session = Depends(get_db)) -> FileResponse:
    """Build the archive in a temp dir, stream it as a download, clean up after.

    Built-then-streamed rather than streamed-on-the-fly: `pg_dump -Fc` wants a
    seekable output file anyway, the whole thing is ~300MB at the top end, and
    `FileResponse` streams from disk in chunks — the response never holds the
    archive in memory. The temp dir (and the lock) are released by the
    response's BackgroundTask, i.e. after the last byte left.
    """
    if not _LOCK.acquire(blocking=False):
        raise _err(409, "backup_busy", "an export or restore is already running")

    tmpdir = tempfile.mkdtemp(prefix="gt-backup-")

    def _cleanup() -> None:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _LOCK.release()

    try:
        dump_path = str(Path(tmpdir) / "db.dump")
        conn_args, env = _conn()
        argv = [_pg_bin("pg_dump"), "-Fc", *conn_args, "-f", dump_path]
        try:
            proc = _run(argv, env)
        except FileNotFoundError as e:
            raise _err(
                409, "pg_tools_missing",
                f"pg_dump not found ({argv[0]}) — set PG_BIN_DIR or install postgresql-client",
            ) from e
        if proc.returncode != 0:
            raise _err(409, "export_failed", f"pg_dump failed: {proc.stderr[-2000:]}")

        # The libraries the desktop shell has set aside and not been given back.
        # Enumerated BEFORE the manifest so the archive can describe itself: a
        # backup that carries these is not an ordinary backup, and the one thing
        # it must never be is quiet about it.
        displaced = _displaced_media_dirs()
        manifest = {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Best effort, never a `git` subprocess: the deployed containers and
            # the desktop build both carry no .git — they bake APP_COMMIT in.
            "app_commit": os.environ.get("APP_COMMIT", "unknown"),
            "pg_major": _server_pg_major(db),
            "schema": _db_schema_rev(db),
            # Empty list on every healthy install, which is also every docker
            # deployment. Present either way so the field's absence means "an
            # older build wrote this", not "there were none".
            "superseded_media": [
                {"name": d.name, "bytes": _tree_bytes(d)} for d in displaced
            ],
        }
        if displaced:
            log.warning(
                "backup: this install has %d set-aside media folder(s) (%s) — "
                "including them in the archive under %s/. The db.dump does NOT "
                "index them; see the archive's %s.",
                len(displaced),
                ", ".join(d.name for d in displaced),
                SUPERSEDED_ARCNAME,
                _SUPERSEDED_NOTE,
            )

        archive_path = Path(tmpdir) / "backup.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(dump_path, arcname="db.dump")
            media_dir = Path(settings.media_dir)
            if media_dir.is_dir():
                # The whole media dir as `media/` — exactly make-seed.sh's shape.
                tar.add(media_dir, arcname="media")
            else:
                info = tarfile.TarInfo("media")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            # …and everything that was displaced away from it, under its own
            # name, NOT merged into `media/`. Merging would hand a restore a
            # library its `db.dump` cannot name, which the next boot sweep would
            # quarantine straight back out again.
            for d in displaced:
                tar.add(d, arcname=f"{SUPERSEDED_ARCNAME}/{d.name}")
            if displaced:
                note_path = Path(tmpdir) / _SUPERSEDED_NOTE
                note_path.write_text(_SUPERSEDED_NOTE_TEXT, encoding="utf-8")
                tar.add(note_path, arcname=_SUPERSEDED_NOTE)
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            tar.add(manifest_path, arcname="manifest.json")

        filename = f"guitar-backup-{datetime.now(timezone.utc):%Y-%m-%d}.tar.gz"
        return FileResponse(
            str(archive_path),
            media_type="application/gzip",
            filename=filename,
            background=BackgroundTask(_cleanup),
        )
    except BaseException:
        _cleanup()
        raise


# --------------------------------------------------------------------------
# RESTORE
# --------------------------------------------------------------------------

def _validate_member_name(name: str) -> None:
    """Reject anything that could write outside the extraction dir."""
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts:
        raise _err(400, "not_a_backup", f"archive member escapes the archive: {name!r}")


def _reset_schema(db: Session) -> None:
    """Empty the database before `pg_restore` — the whole `public` schema, in
    one statement, extension included (CASCADE takes `vector` with it; the
    dump's own CREATE EXTENSION puts it back).

    Why not `pg_restore --clean`: `--clean` drops only what the DUMP knows.
    The webapp's database calls the chunk->page key `chunk_page_id_fkey`; a
    database the migrations built calls it `fk_chunk_page_id`. Restoring the
    one into the other (CI, 2026-09-14, the v0.5.2 bundle) died on
    "cannot drop constraint page_pkey ... fk_chunk_page_id depends on it" with
    the tables half-dropped. A restore whose outcome depends on what the
    target used to call its constraints is not a restore."""
    db.execute(text(
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public; "
        "GRANT ALL ON SCHEMA public TO public;"
    ))
    db.commit()


def _safety_dump(conn_args: list[str], env: dict[str, str]) -> Path:
    """`pg_dump -Fc` of the database as it is RIGHT NOW, before the restore
    drops anything, written beside `settings.media_dir`.

    A sibling of the media dir — the same place the desktop shell puts a
    `media_superseded_*` library — and deliberately NOT a temp dir: the whole
    point is that it outlives this request. It is never deleted, not even on a
    restore that succeeded: it is a few MB, it is the tutor's data, and "the
    app tidied away the only copy of what you had before" is exactly the class
    of helpfulness this file exists to refuse.

    Failing here is safe: nothing has been reset yet, so the 409 leaves the
    database untouched.
    """
    dest = (
        Path(settings.media_dir).parent
        / f"pre-restore-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.dump"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = [_pg_bin("pg_dump"), "-Fc", *conn_args, "-f", str(dest)]
    try:
        proc = _run(argv, env)
    except FileNotFoundError as e:
        raise _err(
            409, "pg_tools_missing",
            f"pg_dump not found ({argv[0]}) — set PG_BIN_DIR or install postgresql-client",
        ) from e
    if proc.returncode != 0:
        raise _err(
            409, "restore_failed",
            "a safety copy of your current data could not be made, so nothing "
            f"was changed (pg_dump failed: {proc.stderr[-2000:]})",
        )
    log.info("restore: safety copy of the current database written to %s", dest)
    return dest


def _put_back(db: Session, safety: Path, conn_args: list[str], env: dict[str, str]) -> str:
    """Undo a half-done restore: empty the schema again and restore the safety
    dump `_safety_dump` took before any of this started.

    Returns the sentence appended to the `restore_failed` message — the tutor
    reads a Greek sentence keyed off the `code`, but whoever is helping him
    reads this, and what they most need to know is whether his data is back and
    where the file is if it is not.

    Everything is caught: this runs on a path that is ALREADY failing, and an
    exception escaping here would replace a precise "your restore failed and
    your data is back" with an opaque 500.
    """
    try:
        _reset_schema(db)
        argv = [
            _pg_bin("pg_restore"),
            "--no-owner", "--no-privileges", "--exit-on-error",
            *conn_args,
            str(safety),
        ]
        proc = _run(argv, env)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-2000:] or f"pg_restore exited {proc.returncode}")
        # The safety dump came out of the live database, so on the desktop it
        # always carries alembic_version; bring it back to head exactly as a
        # successful restore would.
        has_alembic = _has_alembic_table(db)
        db.rollback()
        if has_alembic:
            _run_alembic_upgrade()
    except Exception as e:  # noqa: BLE001 — see the docstring
        log.exception("restore: putting the previous data back FAILED (%s)", safety)
        return (
            f" — AND putting your previous data back failed: {e}; "
            f"the file {safety} holds it"
        )
    log.info("restore: upload rejected by pg_restore; previous data put back from %s", safety)
    try:
        # Same reason as the success path: the schema (and with it the `vector`
        # extension) has been dropped and recreated twice now, so every pooled
        # connection is holding a type OID that no longer exists.
        engine.dispose()
    except Exception:  # noqa: BLE001 — the data is already back
        log.exception("restore: engine.dispose() after a put-back failed")
    return f" — your previous data was put back from {safety}"


def _run_alembic_upgrade() -> None:
    """`alembic upgrade head`, programmatically and cwd-independent: both the
    ini path and `script_location` are absolute, so this works no matter what
    directory uvicorn (or the desktop launcher) happens to run from.
    `alembic/env.py` reads `settings.database_url`, i.e. the same DB the dump
    was just restored into."""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    api_dir = Path(__file__).resolve().parents[2]  # .../apps/api
    cfg = AlembicConfig(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    alembic_command.upgrade(cfg, "head")


@router.post("/restore")
def restore_backup(file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    """Replace EVERYTHING with the archive's contents. See the module docstring
    for format, locking and the failure codes.

    Order: validate the whole archive first (a corrupt upload must fail before
    a single byte of the tutor's data is touched), then (a) `_safety_dump` of
    the database as it stands, `_reset_schema` — `DROP SCHEMA public CASCADE` —
    and `pg_restore --no-owner --no-privileges --exit-on-error` into the empty
    database (NOT `--clean`: see `_reset_schema` and the module docstring for
    the CI failure that bought this); a `pg_restore` that fails here is undone
    by `_put_back` from the safety dump, (b) `alembic upgrade head` (skipped
    when the restored dump has no `alembic_version` — see module docstring),
    (c) atomically swap `media/` into `settings.media_dir`.
    """
    if not _LOCK.acquire(blocking=False):
        raise _err(409, "backup_busy", "an export or restore is already running")
    try:
        running = (
            db.query(GenerationJob).filter(GenerationJob.status == "running").count()
        )
        if running:
            raise _err(
                409, "jobs_running",
                f"{running} generation job(s) are running — a restore would drop "
                "the tables out from under them",
            )
        server_major = _server_pg_major(db)

        with tempfile.TemporaryDirectory(prefix="gt-restore-") as tmpdir:
            upload_path = Path(tmpdir) / "upload.tar.gz"
            with upload_path.open("wb") as out:
                shutil.copyfileobj(file.file, out)

            # ---- validate ------------------------------------------------
            # "r:*", not "r:gz": Safari's "open safe files" gunzips a download
            # on arrival and hands the tutor a plain `guitar-backup-....tar`.
            # Refusing his own backup because his browser helped is not a
            # format check, it is a papercut.
            try:
                tar = tarfile.open(upload_path, "r:*")
            except (tarfile.TarError, OSError) as e:
                raise _err(400, "not_a_backup", f"not a tar archive: {e}") from e
            with tar:
                names = tar.getnames()
                if "db.dump" not in names or "manifest.json" not in names:
                    raise _err(
                        400, "not_a_backup",
                        "archive is missing db.dump and/or manifest.json",
                    )
                for member in tar.getmembers():
                    _validate_member_name(member.name)
                    if member.islnk() or member.issym():
                        # A link member could point extraction outside tmpdir.
                        # Real backups never contain links (JPEGs + two files).
                        raise _err(
                            400, "not_a_backup",
                            f"archive contains a link member: {member.name!r}",
                        )
                try:
                    manifest = json.load(tar.extractfile("manifest.json"))  # type: ignore[arg-type]
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    raise _err(400, "not_a_backup", f"unreadable manifest.json: {e}") from e
                if not isinstance(manifest, dict) or not isinstance(
                    manifest.get("pg_major"), int
                ):
                    raise _err(400, "not_a_backup", "manifest.json has no pg_major")
                if manifest["pg_major"] != server_major:
                    raise _err(
                        409, "pg_major_mismatch",
                        f"backup is from PostgreSQL {manifest['pg_major']}, "
                        f"server is {server_major}",
                    )

                # Extract while validated members are in hand. Python 3.12's
                # 'data' filter is belt-and-braces under the explicit checks
                # above (absolute paths, .., links are already rejected).
                extract_dir = Path(tmpdir) / "x"
                extract_dir.mkdir()
                tar.extractall(extract_dir, filter="data")

            # ---- (a) safety dump, empty the schema, then pg_restore -------
            # This request's own session ran queries above, which opened a
            # transaction; an idle-in-transaction connection holds ACCESS SHARE
            # locks that would block the DROP SCHEMA below. Release it.
            db.rollback()

            conn_args, env = _conn()
            # BEFORE anything is dropped. A failure here is a 409 over a
            # database nobody has touched yet.
            safety = _safety_dump(conn_args, env)

            _reset_schema(db)

            argv = [
                _pg_bin("pg_restore"),
                "--no-owner", "--no-privileges", "--exit-on-error",
                *conn_args,
                str(extract_dir / "db.dump"),
            ]
            try:
                proc = _run(argv, env)
            except FileNotFoundError as e:
                raise _err(
                    409, "pg_tools_missing",
                    f"pg_restore not found ({argv[0]}) — set PG_BIN_DIR or "
                    f"install postgresql-client{_put_back(db, safety, conn_args, env)}",
                ) from e
            if proc.returncode != 0:
                raise _err(
                    409, "restore_failed",
                    f"pg_restore failed: {proc.stderr[-2000:]}"
                    f"{_put_back(db, safety, conn_args, env)}",
                )

            # ---- (b) alembic upgrade head --------------------------------
            has_alembic = _has_alembic_table(db)
            db.rollback()  # same idle-in-transaction reasoning as above
            if has_alembic:
                try:
                    _run_alembic_upgrade()
                except Exception as e:
                    raise _err(
                        409, "restore_failed", f"alembic upgrade after restore failed: {e}"
                    ) from e
            else:
                log.info("restore: no alembic_version in the dump — skipping upgrade")

            # ---- (c) media swap ------------------------------------------
            _swap_media(extract_dir / "media")
            # …and (d) put back anything the archive carried under
            # `superseded/`. Without this the restore would silently DELETE a
            # displaced library — it would be extracted into the temp dir and
            # thrown away with it — which is the same failure as the export bug
            # this section exists to fix, one step further along.
            _restore_superseded(extract_dir / SUPERSEDED_ARCNAME)

            try:
                engine.dispose()  # pooled connections cached the OLD vector type OID
            except Exception:  # noqa: BLE001 — the restore already happened
                log.exception("restore: engine.dispose() failed")

        # The BM25 index self-heals: its staleness fingerprint no longer matches
        # the restored corpus, so the next search rebuilds it. Nothing to do.
        log.info("restore complete: manifest=%s", manifest)
        return {"ok": True, "restored_manifest": manifest}
    finally:
        _LOCK.release()


def _restore_superseded(staged: Path) -> None:
    """Put each `superseded/<name>` back beside `settings.media_dir` under its
    own name.

    NEVER OVERWRITES. A `media_superseded_*` folder already on this machine is
    the tutor's data that this app did not put there in this request, and the
    one thing a restore may not do is replace it with a same-named folder out of
    an archive. Colliding entries are left in place and logged; the archive can
    be unpacked by hand.

    Names are checked here as well as by `_validate_member_name`: this one turns
    an archive entry into a path OUTSIDE the extraction directory, so it gets
    its own belt. Only `media_superseded_*`, only a single path component.
    """
    if not staged.is_dir():
        return
    dest_parent = Path(settings.media_dir).parent
    dest_parent.mkdir(parents=True, exist_ok=True)
    for entry in sorted(staged.iterdir()):
        name = entry.name
        if (
            not entry.is_dir()
            or entry.is_symlink()
            or not name.startswith(SUPERSEDED_MEDIA_PREFIX)
            or name != Path(name).name
            or name in (".", "..")
        ):
            log.warning("restore: ignoring unexpected %s/%s", SUPERSEDED_ARCNAME, name)
            continue
        target = dest_parent / name
        if target.exists():
            log.warning(
                "restore: %s already exists — left exactly as it is, and the copy "
                "in the archive was NOT unpacked over it",
                target,
            )
            continue
        shutil.move(str(entry), str(target))
        log.info("restore: put the set-aside media folder %s back", target)


def _swap_media(new_media: Path) -> None:
    """Atomically-as-possible replace `settings.media_dir` with `new_media`.

    Move the extracted tree to a temp SIBLING of media_dir first (that hop may
    cross filesystems — tmpdir is often tmpfs — so `shutil.move`, which copies
    when rename can't). The final swap is two same-filesystem `os.rename`s:
    old aside, new in, then delete old. A crash between the renames leaves the
    data on disk under a recognizable name rather than half-copied.
    """
    media_dir = Path(settings.media_dir)
    media_dir.parent.mkdir(parents=True, exist_ok=True)

    token = uuid.uuid4().hex[:8]
    staging = media_dir.parent / f".media-new-{token}"
    if new_media.is_dir():
        shutil.move(str(new_media), str(staging))
    else:
        staging.mkdir()  # a backup with an empty media/ entry only

    old = media_dir.parent / f".media-old-{token}"
    try:
        if media_dir.exists():
            os.rename(media_dir, old)
        os.rename(staging, media_dir)
    except OSError:
        # Roll the old dir back if the swap half-happened, then re-raise.
        if old.exists() and not media_dir.exists():
            os.rename(old, media_dir)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
