"""`/backup` — the whole deployment in one file, and the way back from it.

ONE FORMAT, THREE USES. The archive this router emits is byte-for-byte the
shape `desktop/scripts/make-seed.sh` publishes as the desktop seed bundle, so
seed = backup = restore are a single format and a single restore path:

    guitar-backup-YYYY-MM-DD.tar.gz
    ├── db.dump         pg_dump -Fc of the whole database (custom format)
    ├── media/          settings.media_dir, whole (page scans)
    └── manifest.json   {created, app_commit, pg_major, schema}

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
sessions and writes blocks, and `pg_restore --clean` would drop the tables out
from under it.

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
from app.db import get_db
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

        manifest = {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Best effort, never a `git` subprocess: the deployed containers and
            # the desktop build both carry no .git — they bake APP_COMMIT in.
            "app_commit": os.environ.get("APP_COMMIT", "unknown"),
            "pg_major": _server_pg_major(db),
            "schema": _db_schema_rev(db),
        }

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
    a single byte of the tutor's data is touched), then (a) `pg_restore --clean
    --if-exists --no-owner --no-privileges --exit-on-error`, (b) `alembic
    upgrade head` (skipped when the restored dump has no `alembic_version` —
    see module docstring), (c) atomically swap `media/` into
    `settings.media_dir`.
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
            try:
                tar = tarfile.open(upload_path, "r:gz")
            except (tarfile.TarError, OSError) as e:
                raise _err(400, "not_a_backup", f"not a gzipped tar: {e}") from e
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

            # ---- (a) pg_restore ------------------------------------------
            # This request's own session ran queries above, which opened a
            # transaction; an idle-in-transaction connection holds ACCESS SHARE
            # locks that would deadlock `pg_restore --clean`'s DROPs. Release it.
            db.rollback()

            conn_args, env = _conn()
            argv = [
                _pg_bin("pg_restore"),
                "--clean", "--if-exists", "--no-owner", "--no-privileges",
                "--exit-on-error",
                *conn_args,
                str(extract_dir / "db.dump"),
            ]
            try:
                proc = _run(argv, env)
            except FileNotFoundError as e:
                raise _err(
                    409, "pg_tools_missing",
                    f"pg_restore not found ({argv[0]}) — set PG_BIN_DIR or "
                    "install postgresql-client",
                ) from e
            if proc.returncode != 0:
                raise _err(409, "restore_failed", f"pg_restore failed: {proc.stderr[-2000:]}")

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

        # The BM25 index self-heals: its staleness fingerprint no longer matches
        # the restored corpus, so the next search rebuilds it. Nothing to do.
        log.info("restore complete: manifest=%s", manifest)
        return {"ok": True, "restored_manifest": manifest}
    finally:
        _LOCK.release()


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
