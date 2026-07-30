"""Stage 7.3 — page scans must die with the source that rendered them, and this
module must be incapable of deleting anything else.

The leak: `DELETE /knowledge/sources/{id}` cascaded the Page/Chunk rows and left
every JPEG on disk. The tutor's book is ~25MB of them, and re-uploading it after
a bad OCR run (exactly what you do) leaked another 25MB, forever.

The other half of these tests is the part that matters more: `purge_source_media`
is recursive deletion driven by a value out of a database, so it must refuse
anything that is not a UUID-named directory strictly inside `media_dir`.
"""
import os
import time
import uuid

from app.brain.media import (
    QUARANTINE_DIR,
    QUARANTINE_MAX_AGE_SECONDS,
    purge_source_media,
    reap_quarantined_media,
    sweep_orphaned_media,
)
from app.models.knowledge import KnowledgeSource, Page


def _scanned_book(db, tmp_path, monkeypatch, *, pages=3) -> KnowledgeSource:
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    src = KnowledgeSource(type="pdf", title="Book", status="ready", char_count=10)
    db.add(src)
    db.commit()
    d = tmp_path / str(src.id)
    d.mkdir()
    for i in range(1, pages + 1):
        (d / f"{i:04d}.jpg").write_bytes(b"jpeg-bytes")
        db.add(Page(source_id=src.id, page_no=i, status="ready", text="t",
                    image_path=f"{src.id}/{i:04d}.jpg"))
    db.commit()
    return src


def test_deleting_a_source_deletes_its_scans(db, client, tmp_path, monkeypatch):
    src = _scanned_book(db, tmp_path, monkeypatch, pages=3)
    assert (tmp_path / str(src.id)).is_dir()

    assert client.delete(f"/knowledge/sources/{src.id}").status_code == 204

    assert not (tmp_path / str(src.id)).exists()


def test_the_row_is_still_deleted_even_if_the_files_cannot_be(db, client, tmp_path, monkeypatch):
    """Commit first, unlink after. A filesystem that refuses the unlink must not
    make the source look undeletable — the DB is the truth about what he has."""
    src = _scanned_book(db, tmp_path, monkeypatch)
    source_id = src.id
    monkeypatch.setattr("app.brain.media.shutil.rmtree",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))

    assert client.delete(f"/knowledge/sources/{source_id}").status_code == 204
    db.expire_all()          # the request committed on its OWN session
    assert db.get(KnowledgeSource, source_id) is None


def test_purge_refuses_anything_that_is_not_a_uuid(tmp_path, monkeypatch):
    """A traversal attempt cannot even name a path: the directory name is
    re-derived from a PARSED uuid, so `..` never survives the round trip."""
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    victim = tmp_path.parent / "not-media"
    victim.mkdir(exist_ok=True)
    (victim / "important.txt").write_text("keep me")

    assert purge_source_media("../not-media") == 0
    assert purge_source_media("") == 0
    assert purge_source_media(None) == 0
    assert (victim / "important.txt").exists()


def test_purge_refuses_a_symlink_pointing_out_of_media_dir(tmp_path, monkeypatch):
    """`realpath`, not the literal path: a symlink named like a legitimate source
    id must not be a way to hand `rmtree` somewhere else."""
    media = tmp_path / "media"
    media.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me")
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(media))

    sid = uuid.uuid4()
    os.symlink(outside, media / str(sid))

    assert purge_source_media(sid) == 0
    assert (outside / "keep.txt").exists()


def test_purge_of_a_source_with_no_scans_is_a_no_op(tmp_path, monkeypatch):
    """url/text/note sources have no scans at all (spec D2) — and neither does a
    source whose files were already cleaned up. Neither is an error."""
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    assert purge_source_media(uuid.uuid4()) == 0


def test_boot_sweep_collects_scans_whose_source_is_gone_and_keeps_the_rest(
    db, tmp_path, monkeypatch
):
    """The backstop for every DELETE this app served before Stage 7.3 — the
    tutor's real deployment has some. It must take exactly the orphans."""
    live = _scanned_book(db, tmp_path, monkeypatch, pages=2)
    orphan = tmp_path / str(uuid.uuid4())
    orphan.mkdir()
    (orphan / "0001.jpg").write_bytes(b"leaked")
    not_ours = tmp_path / "exports"          # not a UUID — none of our business
    not_ours.mkdir()
    (not_ours / "note.txt").write_text("hands off")

    assert sweep_orphaned_media(db) == 1

    assert not orphan.exists()
    assert (tmp_path / str(live.id)).is_dir()
    assert (not_ours / "note.txt").exists()


# ---- the sweep quarantines; it does not delete ------------------------------


def _batches(root):
    q = root / QUARANTINE_DIR
    return sorted(p.name for p in q.iterdir() if p.is_dir()) if q.is_dir() else []


def test_the_boot_sweep_sets_orphans_aside_instead_of_deleting_them(
    db, tmp_path, monkeypatch
):
    """The sweep runs on an INFERENCE, not an instruction: nobody asked for any
    of this to go. So it renames into `_superseded/<stamp>/`, and every byte —
    including the original `source.pdf` the tutor uploaded — is still readable
    afterwards.
    """
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    orphan_id = str(uuid.uuid4())
    orphan = tmp_path / orphan_id
    orphan.mkdir()
    (orphan / "source.pdf").write_bytes(b"HIS ONLY COPY")
    (orphan / "0001.jpg").write_bytes(b"page render")

    assert sweep_orphaned_media(db) == 1
    assert not orphan.exists(), "it must be out of the way of the live tree"

    batches = _batches(tmp_path)
    assert len(batches) == 1, batches
    kept = tmp_path / QUARANTINE_DIR / batches[0] / orphan_id
    assert kept.is_dir()
    assert (kept / "source.pdf").read_bytes() == b"HIS ONLY COPY"
    assert (kept / "0001.jpg").read_bytes() == b"page render"

    # …and the folder explains itself, in both languages, to whoever opens it.
    note = (tmp_path / QUARANTINE_DIR / "READ-ME-superseded-media.txt").read_text(
        encoding="utf-8"
    )
    assert "DO NOT DELETE THIS FOLDER" in note
    assert any("Ͱ" <= c <= "Ͽ" for c in note), "Greek half"
    assert "30 days" in note, "the reap must be disclosed, not sprung"


def test_the_quarantine_note_gives_directions_that_actually_work(
    db, tmp_path, monkeypatch
):
    """The note is INSTRUCTIONS, followed literally, by somebody who has just
    discovered a library is missing. It said "move these directories back up one
    level into media/" — and one level up from `_superseded/<date>/` is
    `_superseded/`, which is not where GuitarTutor looks. Following it exactly
    left every book still invisible, under a heading that promised recovery.
    """
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    orphan_id = str(uuid.uuid4())
    (tmp_path / orphan_id).mkdir()
    (tmp_path / orphan_id / "source.pdf").write_bytes(b"HIS ONLY COPY")

    assert sweep_orphaned_media(db) == 1
    kept = tmp_path / QUARANTINE_DIR / _batches(tmp_path)[0] / orphan_id

    # THE ARITHMETIC, off the real tree rather than off the prose: the folder
    # holding the book is `<date>/`, and getting from there to media_dir is two
    # levels, not one.
    assert kept.parent.parent == tmp_path / QUARANTINE_DIR, "one level up is not media/"
    assert kept.parent.parent.parent == tmp_path, "two levels up is"

    note = (tmp_path / QUARANTINE_DIR / "READ-ME-superseded-media.txt").read_text(
        encoding="utf-8"
    )
    assert "back up one level" not in note, "the off-by-one instruction is gone"
    assert "TWO levels up, not\none" in note
    # …and it prints both real layouts, so the reader can compare rather than
    # count.
    assert "media/_superseded/<date>/<book-id>/source.pdf" in note
    assert "media/<book-id>/source.pdf" in note


def test_a_whole_library_orphaned_by_a_database_swap_survives_the_sweep(
    db, tmp_path, monkeypatch
):
    """THE BLOCKER, from the API side.

    The desktop shell renames the tutor's `guitar` database aside and builds a
    starter database beside it. Minutes later, on the SAME boot, this sweep runs
    against that starter database — in which not one of his books has a row. The
    old code `rmtree`d every one of them, `source.pdf` included, which made the
    recovery note's promise ("rename the database back and everything returns")
    false. Nothing here may be able to do that again.
    """
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    library = [str(uuid.uuid4()) for _ in range(5)]
    for sid in library:
        d = tmp_path / sid
        d.mkdir()
        (d / "source.pdf").write_bytes(f"book {sid}".encode())

    # A database that knows nothing about any of it — exactly what a fresh
    # `guitar` looks like the instant after a reseed.
    assert db.query(KnowledgeSource).count() == 0
    assert sweep_orphaned_media(db) == 5

    batch = tmp_path / QUARANTINE_DIR / _batches(tmp_path)[0]
    for sid in library:
        assert (batch / sid / "source.pdf").read_bytes() == f"book {sid}".encode()


def test_quarantined_media_is_reaped_after_thirty_days_and_never_before(tmp_path, monkeypatch):
    """The one irreversible delete in this module nobody asked for — so it is
    bounded by the batch's own name, and by nothing else.
    """
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    q = tmp_path / QUARANTINE_DIR
    q.mkdir()
    now = time.time()

    def batch(name, contents=b"x"):
        d = q / name / str(uuid.uuid4())
        d.mkdir(parents=True)
        (d / "source.pdf").write_bytes(contents)
        return q / name

    old = batch(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now - 40 * 86400)))
    fresh = batch(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now - 3 * 86400)))
    edge = batch(
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now - QUARANTINE_MAX_AGE_SECONDS + 3600))
    )
    # A folder somebody else put in here. Unparseable name → never touched,
    # whatever its age. Same rule as the desktop shell's reaper.
    alien = q / "my-copy-before-the-upgrade"
    alien.mkdir()
    (alien / "keep.txt").write_text("mine")

    assert reap_quarantined_media(now=now) == 1
    assert not old.exists()
    assert fresh.is_dir(), "well inside the window"
    assert edge.is_dir(), "one hour short of the cutoff is still kept"
    assert (alien / "keep.txt").exists(), "a name we did not write is never reaped"


def test_a_sweep_that_finds_nothing_leaves_no_debris(db, tmp_path, monkeypatch):
    """Idempotent, and quiet: a boot that finds no orphans must not leave an
    empty dated folder behind — one per boot would make every launch look like
    something had happened.
    """
    live = _scanned_book(db, tmp_path, monkeypatch, pages=1)

    assert sweep_orphaned_media(db) == 0
    assert sweep_orphaned_media(db) == 0
    assert _batches(tmp_path) == []
    assert (tmp_path / str(live.id)).is_dir()


def test_the_sweep_never_takes_an_interest_in_its_own_quarantine(db, tmp_path, monkeypatch):
    """`_superseded` is not a UUID, so the sweep skips it exactly the way it
    skips anything else it did not create. Without that, a second boot would
    re-quarantine the first boot's quarantine, forever.
    """
    monkeypatch.setattr("app.brain.media.settings.media_dir", str(tmp_path))
    orphan = tmp_path / str(uuid.uuid4())
    orphan.mkdir()
    (orphan / "source.pdf").write_bytes(b"keep me")

    assert sweep_orphaned_media(db) == 1
    first = _batches(tmp_path)
    assert sweep_orphaned_media(db) == 0
    assert _batches(tmp_path) == first
    assert (tmp_path / QUARANTINE_DIR / first[0] / orphan.name / "source.pdf").exists()
