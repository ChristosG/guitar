"""`/backup/export` + `/backup/restore` (`app/routers/backup.py`).

UNIT LEVEL: the pg_dump/pg_restore subprocess seam (`backup._run`) is
monkeypatched — argv and env are asserted exactly, no real postgres tools run.
Everything else is real: the tar is really built and really parsed, the
validation really rejects, the media swap really swaps directories on disk,
and the lock really refuses a second caller. The database queries
(`server_version_num`, `alembic_version`, the `GenerationJob` gate) run
against the real guitar_test DB conftest manages.

REAL POSTGRES: three tests at the bottom run the actual pg_dump/pg_restore
against the actual `guitar_test` server — the round trip (dump, wipe, restore,
assert the rows came back), the renamed-constraint case that `--clean` died on,
and the safety net (a failed restore really puts his rows back). They run ONLY
when PostgreSQL 16 client tools are found (PATH, a staged desktop build, or
/usr/lib/postgresql/16/bin). The conftest's `_integration_needs_a_real_key`
autouse skip is OVERRIDDEN in this module: it gates live-LLM spend, and these
tests spend none — they need pg tools, not a key.

All three really run `DROP SCHEMA public CASCADE` on the shared test database,
and conftest's `_test_database` builds that schema once per SESSION. So each of
them takes `restore_the_schema_afterwards`, which puts the tables back however
the test ends — without it one failure at the wrong moment leaves every later
test in the run staring at a database with no tables.
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
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import app.routers.backup as backup
from app.db import Base
from app.db import engine as real_engine
from app.models.generation_job import GenerationJob
from app.routers.backup import SUPERSEDED_ARCNAME, SUPERSEDED_MEDIA_PREFIX

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


class DisposeSpy:
    """Stands in for `backup.engine` — the restore only ever calls `.dispose()`
    on it, and every exit from the reset/restore block must call it exactly
    once: the schema (and the `vector` type OID every pooled connection cached)
    has been replaced underneath the pool."""

    def __init__(self) -> None:
        self.disposed = 0

    def dispose(self) -> None:
        self.disposed += 1


def make_archive(
    *,
    db_dump: bytes | None = b"PGDMP fake",
    manifest: dict | None = None,
    media: dict[str, bytes] | None = None,
    superseded: dict[str, dict[str, bytes]] | None = None,
    extra_member: str | None = None,
    compress: bool = True,
) -> bytes:
    """A small in-memory archive in exactly make-seed.sh's shape.

    `compress=False` is the uncompressed `.tar` Safari's "open safe files"
    leaves behind after it gunzips a download."""
    if manifest is None:
        manifest = {"created": "2026-07-30T00:00:00Z", "app_commit": "abc",
                    "pg_major": 16, "schema": None}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if compress else "w") as tar:
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
        for folder, files in (superseded or {}).items():
            for name, data in files.items():
                add(f"superseded/{folder}/{name}", data)
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
    assert set(manifest) == {
        "created", "app_commit", "pg_major", "schema", "superseded_media",
    }
    # Present and empty on a healthy install — the field's ABSENCE means "an
    # older build wrote this archive", never "there were none".
    assert manifest["superseded_media"] == []
    assert SUPERSEDED_ARCNAME not in " ".join(names), \
        "nothing to carry, so no superseded/ folder is invented"
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
# the displaced-library case
# --------------------------------------------------------------------------

def _displace(media_dir, stamp: str, books: dict[str, bytes]) -> Path:
    """What the desktop shell leaves behind when it sets a library aside: a
    SIBLING of media_dir named media_superseded_<stamp>. It never deletes."""
    d = media_dir.parent / f"{SUPERSEDED_MEDIA_PREFIX}{stamp}"
    d.mkdir()
    for name, data in books.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
    return d


def test_export_carries_a_displaced_library_and_says_so(client, media_dir, monkeypatch):
    """THE BUG: `media_dir` held the STARTER library and his five books were in
    a `media_superseded_*` SIBLING, which the export did not look at. A backup
    taken after a bad reseed therefore contained the wrong library, silently,
    at the exact moment he most needed one that worked.
    """
    monkeypatch.setattr(backup, "_run", FakeRun())
    _displace(media_dir, "20260730T142530Z",
              {"aaaa/source.pdf": b"HIS ONLY COPY", "aaaa/0001.jpg": b"his page"})

    res = client.get("/backup/export")
    assert res.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(res.content), mode="r:gz") as tar:
        names = tar.getnames()
        # His books are in the archive…
        member = f"{SUPERSEDED_ARCNAME}/{SUPERSEDED_MEDIA_PREFIX}20260730T142530Z/aaaa/source.pdf"
        assert member in names, names
        assert tar.extractfile(member).read() == b"HIS ONLY COPY"
        # …NOT merged into media/, which is what a restore installs over the
        # live tree and which this db.dump does not index.
        assert not any(n.startswith("media/aaaa") for n in names), names
        assert "media/0001.jpg" in names, "the live tree is still exported as media/"
        # …and the archive describes itself, so nobody has to infer any of this.
        manifest = json.load(tar.extractfile("manifest.json"))
        assert manifest["superseded_media"] == [
            {"name": f"{SUPERSEDED_MEDIA_PREFIX}20260730T142530Z",
             "bytes": len(b"HIS ONLY COPY") + len(b"his page")}
        ]
        note = tar.extractfile("READ-ME-superseded.txt").read().decode("utf-8")
    assert "does not ignore" not in note.lower()
    assert any("\u0370" <= c <= "\u03ff" for c in note), "Greek half"
    assert "DOES NOT INDEX" in note


def test_a_restore_puts_a_displaced_library_back_and_never_overwrites_one(
    client, media_dir, monkeypatch
):
    """The other half. Extracting `superseded/` into a temp dir and throwing it
    away with the temp dir would turn "restore my backup" into a silent delete
    of the very library the export went out of its way to carry.
    """
    monkeypatch.setattr(backup, "_run", FakeRun())
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: None)
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")
    name = f"{SUPERSEDED_MEDIA_PREFIX}20260730T142530Z"
    mine = _displace(media_dir, "20260101T000000Z", {"keep.pdf": b"ALREADY HERE"})

    archive = make_archive(
        media={"0002.jpg": b"restored-scan"},
        superseded={
            name: {"aaaa/source.pdf": b"HIS ONLY COPY"},
            # A folder of the SAME name as one already on this machine. The one
            # on disk is his data and this app did not put it there in this
            # request; it must survive untouched.
            mine.name: {"keep.pdf": b"FROM THE ARCHIVE"},
        },
    )
    assert post_restore(client, archive).status_code == 200

    back = media_dir.parent / name
    assert (back / "aaaa/source.pdf").read_bytes() == b"HIS ONLY COPY"
    assert (mine / "keep.pdf").read_bytes() == b"ALREADY HERE", "never overwritten"
    assert (media_dir / "0002.jpg").read_bytes() == b"restored-scan"


def test_restore_ignores_a_superseded_entry_it_did_not_write(
    client, media_dir, monkeypatch
):
    """`_restore_superseded` turns an archive entry into a path OUTSIDE the
    extraction directory, so it gets its own name check on top of the
    archive-wide one."""
    monkeypatch.setattr(backup, "_run", FakeRun())
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: None)
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")

    archive = make_archive(superseded={"not-ours": {"x.txt": b"nope"}})
    assert post_restore(client, archive).status_code == 200
    assert not (media_dir.parent / "not-ours").exists()


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


def test_restore_accepts_a_plain_uncompressed_tar(client, media_dir, monkeypatch):
    """Safari's "open safe files" gunzips a download and leaves `x.tar`."""
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: None)
    archive = make_archive(media={"0002.jpg": b"restored-scan"}, compress=False)
    res = post_restore(client, archive)
    assert res.status_code == 200, res.text


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
    resets: list[str] = []
    monkeypatch.setattr(backup, "_reset_schema", lambda db: resets.append("reset"))
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")
    alembic_calls: list[str] = []
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: alembic_calls.append("ran"))
    spy = DisposeSpy()
    monkeypatch.setattr(backup, "engine", spy)

    archive = make_archive(media={"0002.jpg": b"restored-scan"})
    res = post_restore(client, archive)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["restored_manifest"]["pg_major"] == 16

    # exact pg_restore argv + env — no `--clean`/`--if-exists`: the schema was
    # emptied wholesale first, so there is nothing for pg_restore to drop.
    assert len(fake.calls) == 1
    argv, env = fake.calls[0]
    assert argv[0] == "pg_restore"
    assert argv[1:4] == ["--no-owner", "--no-privileges", "--exit-on-error"]
    assert argv[4:12] == CONN_ARGS
    assert argv[-1].endswith("/db.dump")
    assert env["PGPASSWORD"] == DB["password"]

    assert resets == ["reset"]
    assert spy.disposed == 1, "the pool's cached vector type OID is stale after a restore"

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
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")
    res = post_restore(client, make_archive())
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "restore_failed"
    assert "kaput" in detail["message"]
    # media untouched on a failed restore
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"


def test_a_failed_restore_puts_the_previous_data_back(client, media_dir, monkeypatch):
    """"The app displaces, it never deletes" — and the schema reset DELETES.
    A `pg_restore` that dies after it would leave the tutor with an empty
    database, which is the one outcome a restore may not produce. So the
    database is dumped beside media_dir first and put straight back when the
    upload is rejected."""

    class ScriptedRun(FakeRun):
        """Safety pg_dump ok; the upload's pg_restore fails; the put-back
        pg_restore of the safety dump succeeds."""

        def __call__(self, argv, env):
            n = len(self.calls)
            self.calls.append((list(argv), dict(env)))
            if "-f" in argv:
                Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMP safety copy")
            rc, err = (1, "boom") if n == 1 else (0, "")
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr=err)

    fake = ScriptedRun()
    monkeypatch.setattr(backup, "_run", fake)
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: None)
    # The DDL itself is proven by the real round-trip tests below; guitar_test
    # is shared, and what this test is about is the argv sequence around it.
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)

    res = post_restore(client, make_archive(media={"0002.jpg": b"restored-scan"}))
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "restore_failed"
    assert "boom" in detail["message"]
    assert "put back" in detail["message"]

    # exactly three subprocesses, in this order: the safety dump, the upload
    # that failed, the safety dump going back in.
    assert [argv[0] for argv, _ in fake.calls] == ["pg_dump", "pg_restore", "pg_restore"]
    safety_argv = fake.calls[0][0]
    assert safety_argv[1] == "-Fc"
    safety_path = Path(safety_argv[safety_argv.index("-f") + 1])
    assert safety_path.parent == media_dir.parent, "a SIBLING of media_dir, not a tempdir"
    assert safety_path.name.startswith("pre-restore-") and safety_path.suffix == ".dump"
    assert fake.calls[1][0][-1].endswith("/db.dump")   # the upload's dump
    assert fake.calls[2][0][-1] == str(safety_path)    # …and his data going back
    assert str(safety_path) in detail["message"], "he is told where the file is"

    # the safety copy is KEPT, and the media dir was never touched at all
    assert safety_path.read_bytes() == b"PGDMP safety copy"
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"
    assert not (media_dir / "0002.jpg").exists()


def test_a_failed_restore_whose_put_back_also_fails_says_so(client, media_dir, monkeypatch):
    """The worst case, and the one where prose matters most: the upload was
    rejected AND his data could not be put back. The message must not claim it
    was — it must say so plainly and name the file that still holds it, because
    that file is now the only route back."""

    class BothFail(FakeRun):
        """pg_dump (safety) ok; the upload's pg_restore fails; the put-back
        pg_restore fails too."""

        def __call__(self, argv, env):
            n = len(self.calls)
            self.calls.append((list(argv), dict(env)))
            if "-f" in argv:
                Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMP safety copy")
            rc, err = (0, "") if n == 0 else (1, "boom" if n == 1 else "bang")
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr=err)

    fake = BothFail()
    monkeypatch.setattr(backup, "_run", fake)
    monkeypatch.setattr(backup, "_run_alembic_upgrade", lambda: None)
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)

    res = post_restore(client, make_archive())
    assert res.status_code == 409
    message = res.json()["detail"]["message"]
    assert res.json()["detail"]["code"] == "restore_failed"
    assert "boom" in message                                   # why the upload failed
    assert "AND putting your previous data back failed" in message
    assert "bang" in message                                   # why the put-back failed
    assert "put back from" not in message, "it must NOT claim his data came back"

    safety_argv = fake.calls[0][0]
    safety_path = Path(safety_argv[safety_argv.index("-f") + 1])
    assert str(safety_path) in message, "the only route back is named"
    assert safety_path.read_bytes() == b"PGDMP safety copy", "and it is still there"
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"


def test_an_alembic_failure_after_the_restore_names_the_safety_copy(
    client, media_dir, monkeypatch
):
    """`pg_restore` succeeded, so his NEW data is in and is NOT thrown away for
    a migration that stumbled. But the message has to name the safety copy —
    that file is the only thing standing between him and a database he cannot
    open — and the pool still has to be disposed: the schema was replaced
    whether or not alembic finished."""
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)
    safety = media_dir.parent / "pre-restore-unit.dump"
    monkeypatch.setattr(backup, "_safety_dump", lambda conn_args, env: safety)
    # The create_all test DB has no alembic_version, so the branch has to be
    # opened deliberately.
    monkeypatch.setattr(backup, "_has_alembic_table", lambda db: True)

    def boom() -> None:
        raise RuntimeError("Can't locate revision identified by 'c7d8e9f0a1b2'")

    monkeypatch.setattr(backup, "_run_alembic_upgrade", boom)
    spy = DisposeSpy()
    monkeypatch.setattr(backup, "engine", spy)

    res = post_restore(client, make_archive(media={"0002.jpg": b"restored-scan"}))
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "restore_failed"
    assert "c7d8e9f0a1b2" in detail["message"]
    assert f"also saved at {safety}" in detail["message"]
    assert spy.disposed == 1, "the schema was replaced even though alembic failed"
    # The media swap never ran, so the old tree is untouched.
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"


def test_restore_is_busy_when_the_schema_cannot_be_locked(client, media_dir, monkeypatch):
    """`DROP SCHEMA public CASCADE` waits behind any open transaction for as
    long as that transaction lives. Without `lock_timeout` this request hangs
    holding `_LOCK`, so every retry from Settings answers `backup_busy` and
    nothing says why. The timeout turns that into one honest sentence — and
    PostgreSQL DDL being transactional, nothing was dropped."""
    fake = FakeRun()
    monkeypatch.setattr(backup, "_run", fake)
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")

    def locked(db) -> None:
        raise OperationalError(
            "DROP SCHEMA public CASCADE", {},
            Exception("canceling statement due to lock timeout"),
        )

    monkeypatch.setattr(backup, "_reset_schema", locked)

    res = post_restore(client, make_archive())
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "restore_failed"
    assert detail["message"] == (
        "the database is busy with another request — try again in a moment"
    )
    assert fake.calls == [], "no pg_restore ran, so nothing was dropped"
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"


def test_restore_pg_tools_missing(client, media_dir, monkeypatch):
    monkeypatch.setattr(backup, "_run", FakeRun(raises=FileNotFoundError("pg_restore")))
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")
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
    monkeypatch.setattr(backup, "_reset_schema", lambda db: None)
    monkeypatch.setattr(backup, "_safety_dump",
                        lambda conn_args, env: media_dir.parent / "pre-restore-unit.dump")

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


@pytest.fixture
def restore_the_schema_afterwards():
    """Put `guitar_test`'s tables back however the test ends.

    The three tests below really run `DROP SCHEMA public CASCADE` against the
    shared database, and conftest's `_test_database` builds that schema once
    per SESSION — so an assertion that fails between the drop and the restore
    (or a `pg_restore` that dies) would hand every later test in the run a
    database with no tables and an error naming none of this. `create_all`
    skips tables that already exist, so on the happy path this is a no-op.

    The `vector` extension goes first: `Chunk.embedding` is a Vector column,
    and CASCADE took the extension with the schema.
    """
    yield
    with real_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(real_engine)


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
def test_real_round_trip_export_wipe_restore(
    client, media_dir, monkeypatch, db, restore_the_schema_afterwards
):
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

    safety = sorted(media_dir.parent.glob("pre-restore-*.dump"))
    assert len(safety) == 1 and safety[0].stat().st_size > 0, \
        "the pre-restore safety copy of his data is kept beside media_dir"


@pytest.mark.skipif(PG16_BIN is None, reason="no PostgreSQL 16 pg_dump/pg_restore found")
def test_restore_does_not_depend_on_the_live_databases_constraint_names(
    client, media_dir, monkeypatch, db, restore_the_schema_afterwards
):
    """2026-09-14, CI on the v0.5.2 bundle: restoring a dump whose chunk->page
    foreign key is named `chunk_page_id_fkey` into a database whose migrations
    named it `fk_chunk_page_id` died in `pg_restore --clean` ("cannot drop
    constraint page_pkey ... fk_chunk_page_id depends on it"). A restore must
    not care what the target database used to call its constraints."""
    monkeypatch.setenv("PG_BIN_DIR", PG16_BIN)
    res = client.get("/backup/export")
    assert res.status_code == 200, res.text
    archive = res.content
    # Now give the live DB a dependent object under a name the dump cannot know.
    db.execute(text("ALTER TABLE chunk DROP CONSTRAINT IF EXISTS chunk_page_id_fkey"))
    db.execute(text("ALTER TABLE chunk DROP CONSTRAINT IF EXISTS fk_chunk_page_id"))
    db.execute(text(
        "ALTER TABLE chunk ADD CONSTRAINT renamed_by_a_later_migration "
        "FOREIGN KEY (page_id) REFERENCES page(id) ON DELETE CASCADE"
    ))
    db.commit()
    res = post_restore(client, archive)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    names = {r[0] for r in db.execute(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'chunk'::regclass"
    ))}
    assert "renamed_by_a_later_migration" not in names

    safety = sorted(media_dir.parent.glob("pre-restore-*.dump"))
    assert len(safety) == 1 and safety[0].stat().st_size > 0, \
        "the pre-restore safety copy of his data is kept beside media_dir"


@pytest.mark.skipif(PG16_BIN is None, reason="no PostgreSQL 16 pg_dump/pg_restore found")
def test_a_failed_restore_really_puts_the_database_back(
    client, media_dir, monkeypatch, db, restore_the_schema_afterwards
):
    """The safety net, against a real PostgreSQL rather than a scripted fake:
    a real `DROP SCHEMA public CASCADE` really happens, the upload really is
    rejected by a real `pg_restore`, and his row is really still there
    afterwards. "The app displaces, it never deletes" has to survive the
    failure path or it is not an invariant."""
    monkeypatch.setenv("PG_BIN_DIR", PG16_BIN)

    job = GenerationJob(kind="curriculum", status="succeeded",
                        params={"marker": "must survive a failed restore"})
    db.add(job)
    db.commit()
    job_id = job.id
    db.rollback()  # no idle-in-transaction locks against the DROP SCHEMA

    # A structurally valid archive whose db.dump is garbage: validation passes,
    # the safety dump is taken, the schema is really emptied, and then the real
    # pg_restore rejects it.
    res = post_restore(client, make_archive(db_dump=b"this is not a pg_dump archive"))
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "restore_failed"
    assert "put back" in detail["message"], detail["message"]

    restored = db.query(GenerationJob).all()
    assert [j.id for j in restored] == [job_id], "his data survived a failed restore"
    assert restored[0].params == {"marker": "must survive a failed restore"}
    # …and the media dir never entered it at all.
    assert (media_dir / "0001.jpg").read_bytes() == b"old-scan"
