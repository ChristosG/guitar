"""The ONLY module in this codebase allowed to delete anything under
`settings.media_dir`. Nothing else calls `os.remove`/`shutil.rmtree` on that
tree — grep for it.

TWO CALLERS, TWO DIFFERENT ANSWERS — read this before changing either.

`purge_source_media` DELETES. It runs on an INSTRUCTION: the tutor pressed
delete on a source, or `paginate_source` is about to re-render the pages it is
removing. There is a live decision behind every call.

`sweep_orphaned_media` QUARANTINES — it renames each orphan into
`<media_dir>/_superseded/<stamp>/` and reaps that batch thirty days later. It
runs on an INFERENCE: "no `KnowledgeSource` row exists for this directory,
therefore nobody wants it." Nobody asked for any of it. That difference is the
whole reason the two behave differently, and it is not a style preference:

  * The inference is only as good as the database it is made against, and it is
    made against WHATEVER DATABASE THE PROCESS HAPPENS TO BE CONNECTED TO. Point
    this app at a fresh `guitar` — which the desktop shell's first-run resume
    does deliberately, and which a hand-run `ALTER DATABASE … RENAME` at 1am
    does by accident — and EVERY directory in the tree is an orphan by this
    definition. The sweep then deletes the tutor's entire library, minutes after
    a shell that went to great lengths not to delete anything. That happened;
    this quarantine is the second half of the fix (the first is that the shell
    now moves `<data>/media` aside WITH the database it belongs to, see
    `desktop/src-tauri/src/firstrun.rs::displace_media`).
  * What it removes is NOT only regenerable renders. `<uuid>/source.pdf` is the
    original file the tutor uploaded — often his only copy of a scan he made
    himself. The "a leaked JPEG is a wasted megabyte" argument that justifies
    deletion elsewhere in this module is simply false for that file.
  * The cost is disk, and it is close to nothing: `purge_source_media` runs
    synchronously on every DELETE and every re-paginate, so a healthy install
    gives this sweep NOTHING to collect. Its real workload is the one-time
    historical leak from before this module existed. What it costs is real for
    thirty days and then gone.

Two consequences worth stating rather than discovering. The quarantine sits
inside `media_dir`, so `GET /backup/export` (which tars the whole tree) includes
it — a bigger export that carries MORE of the tutor's data, not less. (The
shell's `<data>/media_superseded_*` are SIBLINGS of `media_dir` and were missed
by exactly that reasoning; `routers/backup.py` now carries them too, under
`superseded/`, and argues the trade there.) And the
thirty-day reap below is a genuinely irreversible delete; it is the only one in
this module that no human asked for, which is why it is bounded, logged, and
explained in a file left in the folder.

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
import calendar
import logging
import os
import shutil
import time
import uuid

from sqlalchemy import select

from app.config import settings
from app.models.knowledge import KnowledgeSource

log = logging.getLogger(__name__)

# Where `sweep_orphaned_media` puts what it takes. Deliberately NOT a UUID, so
# the sweep skips it exactly the way it skips anything else it did not create.
# One fixed folder with one dated batch per sweep, so the whole quarantine is
# one thing to look at, delete, or copy off.
QUARANTINE_DIR = "_superseded"

# How long a quarantined batch is kept. The same thirty days the desktop shell
# gives its own set-aside databases (`SUPERSEDED_MIN_AGE` in `firstrun.rs`) —
# longer than it takes anyone to notice that something is missing and ask.
QUARANTINE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

# `YYYYMMDDTHHMMSSZ`. Byte-for-byte the shell's `utc_compact_stamp`, and for the
# same two reasons: it is a legal filename everywhere, and it is fixed-width and
# big-endian, so it can be compared and sorted without parsing a date back out
# of a directory listing. Matching the shell also means one glance at
# `<data>/` and `<data>/media/_superseded/` reads as one story.
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

_QUARANTINE_README = "READ-ME-superseded-media.txt"

_QUARANTINE_README_TEXT = """\
ΜΗΝ ΔΙΑΓΡΑΨΕΤΕ ΑΥΤΟΝ ΤΟΝ ΦΑΚΕΛΟ — DO NOT DELETE THIS FOLDER

Το GuitarTutor βρήκε εδώ αρχεία (PDF βιβλίων και σαρωμένες σελίδες) που δεν
αντιστοιχούν σε καμία καταχώριση στη βάση δεδομένων του. ΔΕΝ τα διέγραψε: τα
μετέφερε εδώ. Κάθε υποφάκελος έχει την ημερομηνία που έγινε αυτό.

Αν διαπιστώσετε ότι λείπει κάποιο βιβλίο σας, δείξτε αυτόν τον φάκελο σε όποιον
σας υποστηρίζει — τα αρχεία είναι εδώ και μπορούν να επανέλθουν.

ΠΡΟΣΟΧΗ: κάθε υποφάκελος διαγράφεται οριστικά 30 ημέρες μετά την ημερομηνία του.
Αν χρειάζεστε κάτι από εδώ, αντιγράψτε το ΤΩΡΑ κάπου αλλού.

---

GuitarTutor found files here (book PDFs and page scans) that no row in its
database refers to. It did NOT delete them — it moved them here. Each
subdirectory is named for the moment that happened, in UTC.

The usual cause is harmless: a book that was deleted on purpose, or a re-scan
that replaced its pages. The cause that matters is a database that was replaced,
restored or renamed — after which NOTHING in the database refers to ANY of these
files, and every book in the library ends up in here at once. If that is what
you are looking at, the files are all present and nothing has been lost.

TO PUT THEM BACK, with GuitarTutor closed. The layout in here is

    media/_superseded/<date>/<book-id>/source.pdf

and the tree they came out of — the one GuitarTutor reads — is

    media/<book-id>/source.pdf

So put the right database back first, then move each <book-id> folder out of
media/_superseded/<date>/ and into media/ itself. That is TWO levels up, not
one: one level up lands you in "_superseded/", which is still not where
GuitarTutor looks.

Each subdirectory here is DELETED FOR GOOD 30 days after the date in its name.
If you need anything out of here, copy it somewhere else now.
"""


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


def _quarantine_root() -> str:
    return os.path.join(settings.media_dir, QUARANTINE_DIR)


def _write_quarantine_readme(root: str) -> None:
    """Explain the folder, in the folder. Best-effort: a note that could not be
    written is not a reason to fail a sweep that has just put files safely to
    one side.
    """
    try:
        with open(os.path.join(root, _QUARANTINE_README), "w", encoding="utf-8") as fh:
            fh.write(_QUARANTINE_README_TEXT)
    except OSError:
        log.warning("media: could not write the quarantine note in %s", root, exc_info=True)


def reap_quarantined_media(now: float | None = None) -> int:
    """Delete quarantined batches older than `QUARANTINE_MAX_AGE_SECONDS`.
    Returns how many batches were removed.

    THIS IS THE ONE IRREVERSIBLE DELETE IN THIS MODULE THAT NOBODY ASKED FOR, so
    it is drawn as narrowly as it can be:

      * the age comes from the batch's OWN NAME, which only this function's
        counterpart writes. A directory whose name is not one of our stamps is
        never touched — the same rule the desktop shell's reaper follows, and
        for the same reason: removing things we do not recognise is exactly the
        behaviour this module exists to eliminate.
      * a failure is logged and skipped, never raised: this runs inside the
        startup lifespan, and a full or read-only disk must not stop the app.

    Named and called separately from the sweep so it is one grep away, and so a
    test can drive it without inventing orphans.
    """
    root = _quarantine_root()
    if not os.path.isdir(root):
        return 0
    cutoff = (time.time() if now is None else now) - QUARANTINE_MAX_AGE_SECONDS
    reaped = 0
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            made_at = calendar.timegm(time.strptime(name, _STAMP_FORMAT))
        except ValueError:
            continue                       # not a batch we wrote — leave it alone
        if made_at >= cutoff:
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            log.warning("media: could not reap quarantined batch %s", path, exc_info=True)
            continue
        reaped += 1
        log.info(
            "media: reaped quarantined batch %s — set aside more than %d days ago",
            name, QUARANTINE_MAX_AGE_SECONDS // 86400,
        )
    return reaped


def sweep_orphaned_media(db) -> int:
    """SET ASIDE every directory under `media_dir` whose name is a UUID with no
    matching `KnowledgeSource` row, and reap anything set aside more than thirty
    days ago. Returns the number of directories set aside on this pass.

    Runs once at boot (`app.main`'s lifespan), alongside the job sweeps that
    already live there, and it is the ONLY thing that can ever clean up the
    scans leaked by every `DELETE` this app served before `purge_source_media`
    existed — the tutor's real deployment has some.

    IT NO LONGER DELETES, and the module docstring argues why at length. In
    short: this is the one destructive path in the app driven by an INFERENCE
    ("no row refers to it") rather than by an instruction, that inference is
    made against whatever database this process happens to be connected to, and
    a database that has been replaced, restored or renamed turns every book the
    tutor owns into an orphan at once. Renaming into `_superseded/<stamp>/` costs
    disk for thirty days and makes that whole class of failure — including the
    ones nobody has thought of — recoverable.

    Skips anything that is not a plain UUID-named directory: `media_dir` is a
    mounted volume, and this must never take an interest in a file some other
    part of the system put there (the two safety checks in `_source_media_dir`
    are what enforce that; a name that isn't a UUID simply isn't ours — and that
    is also what keeps the sweep from taking an interest in its own quarantine).
    """
    root = settings.media_dir
    if not os.path.isdir(root):
        return 0

    # Reap FIRST: the space is back before any more is used, and the batch
    # created below — zero seconds old — is trivially not a candidate on the
    # pass that created it.
    reap_quarantined_media()

    live = {
        str(sid) for sid in db.scalars(select(KnowledgeSource.id)).all()
    }
    orphans = []
    for name in os.listdir(root):
        if not os.path.isdir(os.path.join(root, name)):
            continue
        try:
            uuid.UUID(name)
        except ValueError:
            continue                       # not one of ours — leave it alone
        if name in live:
            continue
        orphans.append(name)
    if not orphans:
        return 0

    batch = os.path.join(_quarantine_root(), time.strftime(_STAMP_FORMAT, time.gmtime()))
    try:
        os.makedirs(batch, exist_ok=True)
    except OSError:
        # Nothing is deleted as a fallback. A sweep that cannot set files aside
        # simply does not run; the orphans stay exactly where they are and cost
        # the same disk they were already costing.
        log.warning("media: cannot open a quarantine folder at %s — sweeping nothing",
                    batch, exc_info=True)
        return 0
    _write_quarantine_readme(_quarantine_root())

    moved = 0
    for name in sorted(orphans):
        # The SAME two safety checks a delete goes through: the path is rebuilt
        # from the parsed UUID, and its realpath must be strictly inside
        # media_dir. A rename is less dangerous than an rmtree, but "less
        # dangerous" is not a reason to hand it an unchecked path.
        target = _source_media_dir(name)
        if target is None or not os.path.isdir(target):
            continue
        try:
            os.rename(target, os.path.join(batch, name))
        except OSError:
            log.warning("media: could not set aside %s", target, exc_info=True)
            continue
        moved += 1
    if moved:
        log.info(
            "media: set aside %d orphaned source director(ies) in %s — NOTHING WAS "
            "DELETED; see %s",
            moved, batch, os.path.join(_quarantine_root(), _QUARANTINE_README),
        )
    else:
        # An empty batch directory would accumulate one per boot and look like
        # something happened on each of them.
        try:
            os.rmdir(batch)
        except OSError:
            pass
    return moved
