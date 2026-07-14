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
import uuid

from app.brain.media import purge_source_media, sweep_orphaned_media
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
