"""`/backup/export` + `/backup/restore` (`app/routers/backup.py`).

UNIT LEVEL: the pg_dump/pg_restore subprocess seam (`backup._run`) is
monkeypatched — argv and env are asserted exactly, no real postgres tools run.
Everything else is real: the tar is really built and really parsed, the
validation really rejects, the media swap really swaps directories on disk,
and the lock really refuses a second caller. The database queries
(`server_version_num`, `alembic_version`, the `GenerationJob` gate) run
against the real guitar_test DB conftest manages.

INTEGRATION (`@pytest.mark.integration`): one real round-trip —
pg_dump the test DB, wipe a table, pg_restore, assert the rows came back —
run ONLY when PostgreSQL 16 client tools are found (PATH, a staged desktop
build, or /usr/lib/postgresql/16/bin). The conftest's
`_integration_needs_a_real_key` autouse skip is OVERRIDDEN in this module: it
gates live-LLM spend, and this test spends none — it needs pg tools, not a key.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import app.routers.backup as backup
from app.models.generation_job import GenerationJob

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

# conftest pins DATABASE_URL to this before any app import.
DB = {"host": "localhost", "port": "5434", "user": "guitar",
      "password": "guitar", "database": "guitar_test"}
CONN_ARGS = ["-h", DB["host"], "-p", DB["port"], "-U", DB["user"], "-d", DB["database"]]


class FakeRun:
    """Stands in for `backup._run` — records every (argv, env), optionally
    writes the `-f` output file (pg_dump's job), optionally fails."""

    def __init__(self, returncode: int = 0, stderr: str = "", raises: Exception | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        if self.raises is not None:
            raise self.raises
        if "-f" in argv:  # pg_dump writes its output file
            Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMP fake dump bytes")
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)


def make_archive(
    *,
    db_dump: bytes | None = b"PGDMP fake",
    manifest: dict | None = None,
    media: dict[str, bytes] | None = None,
    extra_member: str | None = None,
) -> bytes:
    """A small in-memory archive in exactly make-seed.sh's shape."""
    if manifest is None:
        manifest = {"created": "2026-07-30T00:00:00Z", "app_commit": "abc",
                    "pg_major": 16, "schema": None}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        if db_dump is not None:
            add("db.dump", db_dump)
        if manifest is not False:
            add("manifest.json", json.dumps(manifest).encode())
        dir_info = tarfile.TarInfo("media")
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o755
        tar.addfile(dir_info)
        for name, data in (media or {}).items():
            add(f"media/{name}", data)
        if extra_member is not None:
            add(extra_member, b"evil")
    return buf.getvalue()


def post_restore(client, archive: bytes):
    return client.post(
        "/backup/restore",
        files={"file": ("backup.tar.gz", archive, "application/gzip")},
    )


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    d = tmp_path / "media"
    d.mkdir()
    (d / "0001.jpg").write_bytes(b"old-scan")
    monkeypatch.setattr("app.config.settings.media_dir", str(d))
    return d


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def test_export_builds_the_seed_format_archive(client, media_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    monkeypatch.setenv("APP_COMMIT", "deadbeef")

    res = client.get("/backup/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/gzip"
    disposition = res.headers["content-disposition"]
    assert re.search(r'filename="guitar-backup-\d{4}-\d{2}-\d{2}\.tar\.gz"', disposition)

    # exact pg_dump argv + env
    assert len(fake.calls) == 1
    argv, env = fake.calls[0]
    assert argv[0] == "pg_dump"          # no PG_BIN_DIR -> bare name, PATH
    assert argv[1] == "-Fc"
    assert argv[2:10] == CONN_ARGS
    assert argv[-2] == "-f"
    assert env["PGPASSWORD"] == DB["password"]

    # the archive really is make-seed.sh's shape: db.dump + media/ + manifest.json
    with tarfile.open(fileobj=io.BytesIO(res.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert "db.dump" in names
        assert "manifest.json" in names
        assert any(n in ("media", "media/") for n in names)
        assert "media/0001.jpg" in names
        assert tar.extractfile("media/0001.jpg").read() == b"old-scan"
        assert tar.extractfile("db.dump").read() == b"PGDMP fake dump bytes"
        manifest = json.load(tar.extractfile("manifest.json"))
    assert set(manifest) == {"created", "app_commit", "pg_major", "schema"}
    assert manifest["app_commit"] == "deadbeef"
    assert manifest["pg_major"] == 16      # the real test server
    assert manifest["schema"] is None      # create_all test DB has no alembic_version
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["created"])


def test_export_respects_pg_bin_dir(client, media_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    monkeypatch.setenv("PG_BIN_DIR", "/opt/pg16/bin")

    res = client.get("/backup/export")
    assert res.status_code == 200
    argv, _ = fake.calls[0]
    assert argv[0] == "/opt/pg16/bin/pg_dump"


def test_export_pg_dump_missing_is_a_code_not_a_500(client, media_dir, monkeypatch):
    monkeypatch.setattr(backup, "_run", FakeRun(raises=FileNotFoundError("pg_dump")))
    res = client.get("/backup/export")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "pg_tools_missing"


def test_export_pg_dump_failure_reports_stderr(client, media_dir, monkeypatch):
    monkeypatch.setattr(backup, "_run", FakeRun(returncode=1, stderr="boom: no such database"))
    res = client.get("/backup/export")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "export_failed"
    assert "boom" in detail["message"]


# --------------------------------------------------------------------------
# restore — validation
# --------------------------------------------------------------------------

def test_restore_rejects_a_non_tar_upload(client, media_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    res = post_restore(client, b"this is not a tarball at all")
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "not_a_backup"
    assert fake.calls == []  # pg_restore never ran


def test_restore_rejects_a_tar_missing_db_dump(client, media_dir, monkeypatch):
    monkeypatch.setattr(backup, "_run", FakeRun())
    res = post_restore(client, make_archive(db_dump=None))
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "not_a_backup"


def test_restore_rejects_pg_major_mismatch(client, media_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    archive = make_archive(manifest={"created": "x", "app_commit": "x",
                                     "pg_major": 15, "schema": None})
    res = post_restore(client, archive)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "pg_major_mismatch"
    assert fake.calls == []


@pytest.mark.parametrize("evil", ["../evil.txt", "media/../../evil.txt", "/etc/evil"])
def test_restore_rejects_path_traversal(client, media_dir, monkeypatch, tmp_path, evil):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    res = post_restore(client, make_archive(extra_member=evil))
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "not_a_backup"
    assert fake.calls == []
    assert not (tmp_path / "evil.txt").exists()
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"  # untouched


# --------------------------------------------------------------------------
# restore — the happy path and its failure modes
# --------------------------------------------------------------------------

def test_restore_runs_pg_restore_and_swaps_media(client, media_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    alembic_calls: list[str] = []
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: alembic_calls.append("ran"))

    archive = make_archive(media={"0002.jpg": b"restored-scan"})
    res = post_restore(client, archive)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["restored_manifest"]["pg_major"] == 16

    # exact pg_restore argv + env
    assert len(fake.calls) == 1
    argv, env = fake.calls[0]
    assert argv[0] == "pg_restore"
    assert argv[1:6] == ["--clean", "--if-exists", "--no-owner",
                         "--no-privileges", "--exit-on-error"]
    assert argv[6:14] == CONN_ARGS
    assert argv[-1].endswith("/db.dump")
    assert env["PGPASSWORD"] == DB["password"]

    # media atomically replaced: new content in, old content gone
    assert (media_dir / "0002.jpg").read_bytes() == b"restored-scan"
    assert not (media_dir / "0001.jpg").exists()
    leftovers = [p.name for p in media_dir.parent.iterdir() if p.name.startswith(".media-")]
    assert leftovers == []

    # test DB was built by create_all -> no alembic_version -> upgrade skipped
    assert alembic_calls == []


def test_restore_refuses_while_a_generation_job_is_running(client, media_dir, monkeypatch, db):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    db.add(GenerationJob(kind="curriculum", status="running", params={}))
    db.commit()

    res = post_restore(client, make_archive())
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "jobs_running"
    assert fake.calls == []
    assert (media_dir / "0001.jpg").exists()  # nothing touched


def test_restore_pg_restore_failure_reports_stderr(client, media_dir, monkeypatch):
    monkeypatch.setattr(backup, "_run", FakeRun(returncode=1, stderr="pg_restore: error: kaput"))
    res = post_restore(client, make_archive())
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "restore_failed"
    assert "kaput" in detail["message"]
    # media untouched on a failed restore
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"


def test_restore_pg_tools_missing(client, media_dir, monkeypatch):
    monkeypatch.setattr(backup, "_run", FakeRun(raises=FileNotFoundError("pg_restore")))
    res = post_restore(client, make_archive())
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "pg_tools_missing"


# --------------------------------------------------------------------------
# the lock
# --------------------------------------------------------------------------

def test_both_endpoints_409_while_the_lock_is_held(client, media_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    assert backup._LOCK.acquire(blocking=False)
    try:
        res = client.get("/backup/export")
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "backup_busy"

        res = post_restore(client, make_archive())
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "backup_busy"
        assert fake.calls == []
    finally:
        backup._LOCK.release()


def test_the_lock_is_released_after_export_and_after_restore(client, media_dir, monkeypatch):
    monkeypatch.setattr(backup, "_run", FakeRun())
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: None)

    assert client.get("/backup/export").status_code == 200
    assert not backup._LOCK.locked()  # released by the streamed response's cleanup

    assert post_restore(client, make_archive()).status_code == 200
    assert not backup._LOCK.locked()

    # ... and after a FAILED restore too (the finally path)
    assert post_restore(client, b"garbage").status_code == 400
    assert not backup._LOCK.locked()


# --------------------------------------------------------------------------
# integration — a real pg_dump/pg_restore round-trip
# --------------------------------------------------------------------------

@pytest.fixture
def _integration_needs_a_real_key():
    """OVERRIDES the conftest autouse skip of the same name (see module
    docstring): that gate is about live-LLM spend, and this integration test
    makes no LLM call — it needs PostgreSQL client tools, not an API key."""
    return


def _find_pg16_bin() -> str | None:
    """A dir holding PostgreSQL 16 pg_dump+pg_restore, or None.

    Probes, in order: PATH; anything a desktop build staged
    (desktop/.cache, desktop/**/resources/pg/bin); the stock Debian/Ubuntu
    versioned location."""
    def is_pg16(pg_dump: Path) -> bool:
        try:
            out = subprocess.run([str(pg_dump), "--version"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        m = re.search(r"\(PostgreSQL\)\s+(\d+)", out)
        return bool(m and int(m.group(1)) == 16)

    on_path = shutil.which("pg_dump")
    if on_path and is_pg16(Path(on_path)) and shutil.which("pg_restore"):
        return str(Path(on_path).parent)

    repo = Path(__file__).resolve().parents[3]
    desktop = repo / "desktop"
    candidates: list[Path] = []
    for base in (desktop / ".cache", desktop):
        if base.is_dir():
            candidates += sorted(base.glob("**/pg_dump"))
    candidates.append(Path("/usr/lib/postgresql/16/bin/pg_dump"))
    for pg_dump in candidates:
        if pg_dump.is_file() and (pg_dump.parent / "pg_restore").is_file() and is_pg16(pg_dump):
            return str(pg_dump.parent)
    return None


PG16_BIN = _find_pg16_bin()


@pytest.mark.integration
@pytest.mark.skipif(PG16_BIN is None, reason="no PostgreSQL 16 pg_dump/pg_restore found")
def test_real_round_trip_export_wipe_restore(client, media_dir, monkeypatch, db):
    """Export the real test DB, wipe a table and the media dir, restore the
    archive, and assert both came back. Uses the pg tools at PG16_BIN via
    PG_BIN_DIR — exactly how the desktop build points at its own binaries."""
    monkeypatch.setenv("PG_BIN_DIR", PG16_BIN)

    job = GenerationJob(kind="curriculum", status="succeeded",
                        params={"marker": "round-trip"})
    db.add(job)
    db.commit()
    job_id = job.id

    res = client.get("/backup/export")
    assert res.status_code == 200, res.text
    archive = res.content

    # wipe: the table rows and the media file both go
    db.query(GenerationJob).delete()
    db.commit()
    (media_dir / "0001.jpg").unlink()
    (media_dir / "0001.jpg").write_bytes(b"tampered")
    assert db.query(GenerationJob).count() == 0
    # release this session's transaction — an idle-in-transaction connection
    # would hold locks that deadlock pg_restore --clean's DROPs
    db.rollback()

    res = post_restore(client, archive)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["restored_manifest"]["pg_major"] == 16

    restored = db.query(GenerationJob).all()
    assert [j.id for j in restored] == [job_id]
    assert restored[0].params == {"marker": "round-trip"}
    assert restored[0].status == "succeeded"
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"
