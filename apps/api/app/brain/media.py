"""The ONLY module in this codebase allowed to delete anything under
`settings.media_dir`. Nothing else calls `os.remove`/`shutil.rmtree` on that
tree — grep for it.

WHY IT EXISTS. `paginate_source` writes one JPEG per PDF page
(`{media_dir}/{source_id}/{page_no:04d}.jpg`); the tutor's 77-page book is
~25MB of them. `DELETE /knowledge/sources/{id}` cascades the Page and Chunk
rows and leaves every one of those files on disk FOREVER — nothing in this app
has ever removed one. Re-uploading the same book after a bad OCR run (which is
exactly what a tutor does) leaks another 25MB, permanently, under a directory
named after a source id that no longer exists. Re-paginating leaked too: the
delete-and-re-insert of Page rows never touched the old files, so a shorter
replacement PDF left the tail pages of the previous one orphaned in place.

WHY IT IS ONE MODULE WITH TWO ASSERTIONS. Recursive deletion driven by a value
that came out of a database is the single most dangerous thing this app does. A
`source_id` that is somehow empty, `.`, `..`, or `/etc` turns `rmtree` into an
incident. So every path that gets deleted here must pass BOTH:

  1. it is named by a real `uuid.UUID` — the directory name is re-derived from
     the parsed UUID's own string form, never from caller-supplied text, so
     path separators and traversal segments cannot survive the round trip; and
  2. its `os.path.realpath` is strictly INSIDE `os.path.realpath(media_dir)` —
     which also refuses a symlink pointing out of the tree.

A path that fails either check is logged and skipped, never deleted, and never
raised over — see `purge_source_media`'s contract below.

Deletion is BEST-EFFORT AND NEVER FATAL. The database is the source of truth
about what the tutor has; a leaked JPEG is a wasted megabyte, but an exception
thrown while cleaning up after a successful `DELETE` would turn a completed
deletion into a 500 and make the row look undeletable. So the DB commit happens
FIRST and this runs after it, for effect only.
"""
import logging
import os
import shutil
import uuid

from sqlalchemy import select

from app.config import settings
from app.models.knowledge import KnowledgeSource

log = logging.getLogger(__name__)


def _source_media_dir(source_id) -> str | None:
    """The absolute directory holding one source's page scans, or None if the
    id or the resulting path fails either safety check (see module docstring).

    Note the deliberate round trip through `uuid.UUID(...)` and back to `str`:
    the directory name is generated from the PARSED uuid, so even a caller that
    hands us `"../../etc"` gets a `ValueError` here rather than a path.
    """
    try:
        parsed = uuid.UUID(str(source_id))
    except (ValueError, AttributeError, TypeError):
        log.warning("media: refusing to delete — not a uuid: %r", source_id)
        return None

    root = os.path.realpath(settings.media_dir)
    target = os.path.realpath(os.path.join(root, str(parsed)))
    # `commonpath` (not `startswith`) — "/media-old/x" starts with "/media" as a
    # string but is a different directory. `+ os.sep` on the parent, plus the
    # explicit `target != root` guard, also means "delete the media root itself"
    # can never be expressed here.
    if target == root or os.path.commonpath([root, target]) != root:
        log.warning("media: refusing to delete — outside media_dir: %s", target)
        return None
    return target


def purge_source_media(source_id) -> int:
    """Delete every page scan belonging to `source_id`. Returns the number of
    files removed (0 if the source never had any — url/text/note sources have
    no scans at all, per spec D2).

    Never raises: called from `delete_source` AFTER the DB commit, and from
    `paginate_source` before a re-render. A missing directory is the normal
    case, not an error.
    """
    target = _source_media_dir(source_id)
    if target is None or not os.path.isdir(target):
        return 0
    try:
        n = sum(len(files) for _, _, files in os.walk(target))
        shutil.rmtree(target)
        log.info("media: purged %d file(s) for source_id=%s", n, source_id)
        return n
    except OSError:
        log.warning("media: could not purge %s", target, exc_info=True)
        return 0


def sweep_orphaned_media(db) -> int:
    """Delete every directory under `media_dir` whose name is a UUID with no
    matching `KnowledgeSource` row. Returns the number of directories removed.

    Runs once at boot (`app.main`'s lifespan), alongside the job sweeps that
    already live there, and it is the ONLY thing that can ever clean up the
    scans leaked by every `DELETE` this app served before `purge_source_media`
    existed — the tutor's real deployment has some.

    Skips anything that is not a plain UUID-named directory: `media_dir` is a
    mounted volume, and this must never take an interest in a file some other
    part of the system put there (the two safety checks in `_source_media_dir`
    are what enforce that; a name that isn't a UUID simply isn't ours).
    """
    root = settings.media_dir
    if not os.path.isdir(root):
        return 0

    live = {
        str(sid) for sid in db.scalars(select(KnowledgeSource.id)).all()
    }
    removed = 0
    for name in os.listdir(root):
        if not os.path.isdir(os.path.join(root, name)):
            continue
        try:
            uuid.UUID(name)
        except ValueError:
            continue                       # not one of ours — leave it alone
        if name in live:
            continue
        purge_source_media(name)
        # Counted by what actually happened on disk, not by the file count
        # purge returns — an ALREADY-EMPTY orphan directory (0 files) is still
        # an orphan this sweep removed.
        if not os.path.isdir(os.path.join(root, name)):
            removed += 1
    if removed:
        log.info("media: swept %d orphaned source director(ies)", removed)
    return removed
