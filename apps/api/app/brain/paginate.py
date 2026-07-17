"""Turn a source into Page rows — the unit of OCR, of the reader, and of a
verifiable citation.

A PDF becomes one Page per physical page, each with its scan rendered to disk
(`status="pending"` — Task 4's OCR job fills in the text). Everything else
(url/text/note) becomes exactly ONE Page, already `ready`, with the extracted
text and no image (spec D2). That degenerate row is deliberate: it means the
reader, the chunker, the citation renderer and the retry path never branch on
`source.type`.

110dpi is measured, not guessed — and it is a CEILING, not a preference. At
150dpi this vLLM server rejects the image outright (HTTP 400: "image item with
length 2080 exceeds pre-allocated encoder cache size 2048"), so every page
would fail. At 110dpi the local VL model transcribes a real book page
faithfully at ~1,127 prompt tokens. Do not raise this without also raising the
server's mm-encoder budget.
"""
import logging
import os

import fitz

from app.brain.extract import extract_text
from app.brain.media import purge_source_media
from app.brain.textlayer import has_content_images, text_layer_kind
from app.config import settings
from app.models.knowledge import Page

log = logging.getLogger(__name__)

RENDER_DPI = 110      # HARD CEILING on this server — see module docstring


def paginate_source(db, source_id, *, kind, data=None, url=None, text=None) -> list[Page]:
    """Create this source's Page rows — idempotent: re-running for an
    existing source (Task 6's retry-a-failed-ingest path) REPLACES its Page
    rows rather than duplicating them. Deleting a source's Pages cascades
    (ON DELETE CASCADE) to their Chunks too, which is correct here: a
    re-paginate invalidates whatever was chunked/embedded against the old
    pages anyway.
    """
    # Bulk delete (not ORM cascade): the FK's ON DELETE CASCADE removes this
    # source's Chunks too, in the SAME statement/transaction — no separate
    # commit needed for that to take effect. What IS needed: this session
    # (expire_on_commit=False, and synchronize_session=False on the delete
    # itself) has no idea any of that happened, so any Chunk/Page objects
    # already sitting in its identity map are now silently stale. Expire them
    # so the next access re-fetches from the DB instead of handing back rows
    # that no longer exist. Deliberately NOT committing here: the delete and
    # the fresh inserts below stay one atomic transaction, so a failure
    # partway through page creation doesn't leave the source pageless.
    db.query(Page).filter(Page.source_id == source_id).delete(synchronize_session=False)
    db.expire_all()

    # The Page rows are gone; their scans must go with them (Stage 7.3). Deleting
    # the rows alone was a leak with a sharp edge: a re-render from a SHORTER PDF
    # leaves the previous document's tail pages sitting on disk under this
    # source's id — files no row points at, that nothing will ever clean up, and
    # that a future `{page_no:04d}.jpg` write will happily read as if they were
    # this document's. Same-length overwrites hid it. `purge_source_media` is a
    # no-op for a source that has no scans at all (url/text/note — spec D2) and
    # for a first ingest, which is every PDF ingest this app performs today.
    purge_source_media(source_id)

    if kind == "pdf" and data:
        return _paginate_pdf(db, source_id, data)
    return _single_page(db, source_id, kind=kind, url=url, text=text)


def _paginate_pdf(db, source_id, data: bytes) -> list[Page]:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        out_dir = os.path.join(settings.media_dir, str(source_id))
        os.makedirs(out_dir, exist_ok=True)

        pages: list[Page] = []
        for i in range(doc.page_count):
            page_no = i + 1
            pix = doc[i].get_pixmap(dpi=RENDER_DPI)
            rel = os.path.join(str(source_id), f"{page_no:04d}.jpg")
            with open(os.path.join(settings.media_dir, rel), "wb") as fh:
                fh.write(pix.tobytes("jpeg"))

            # WHAT the text layer is decides whether we may keep it. A real font
            # is publisher text — take it, free, and no model ever needs to look.
            # A GlyphLessFont layer is an invisible OCR layer over a scan: it is
            # someone else's Tesseract, it lost every fraction glyph in the book
            # (measured: 0 of `¼½¾` survive in 2,012,859 chars across the three
            # scanned books), and adopting it would make that permanent, because
            # `ready` means nothing revisits the page. So we drop it and re-read
            # the scan ourselves.
            kind = text_layer_kind(doc[i])
            if kind == "digital":
                layer = (doc[i].get_text() or "").strip()
                if has_content_images(doc[i]):
                    # Keep the publisher text — it is correct and free — but the
                    # picture on this page is content too, and no text layer
                    # describes a picture. Vision ADDS to the text here; it does
                    # not replace it (contrast the non-digital branch below,
                    # which discards the inherited layer outright).
                    text, status, reason, provenance = layer or None, "pending", "image_region", None
                else:
                    text, status, reason, provenance = layer, "ready", None, "text_layer"
            else:
                text, status, provenance = None, "pending", None
                reason = "no_text_layer" if kind == "none" else "inherited_ocr"

            page = Page(
                source_id=source_id, page_no=page_no, image_path=rel,
                text=text or None, status=status,
                text_source=provenance, ocr_reason=reason,
            )
            db.add(page)
            pages.append(page)

        db.commit()
        log.info("paginate: source=%s pages=%d", source_id, len(pages))
        return pages
    finally:
        doc.close()


def _single_page(db, source_id, *, kind, url, text) -> list[Page]:
    sections = extract_text(kind, url=url, text=text)
    body = "\n\n".join(s.text for s in sections).strip()
    page = Page(
        source_id=source_id, page_no=1, image_path=None,
        text=body or None,
        status="ready" if body else "empty",
    )
    db.add(page)
    db.commit()
    return [page]
