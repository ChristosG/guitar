"""EXTEND WITH CHAT: the tutor tells one block what to change, in a sentence.

Chris asked for exactly this: "a button to Extend with chat where the user writes
e.g. change this and give more detail about the Amp — and it actually follows his
instruction."

SHIPPED AS A PATCH WITH AN UNDO, NOT AS AN ACCEPT/REJECT DIFF.

The obvious design is a side-by-side diff with accept/reject per hunk, streamed
live. Two reasons it is the wrong one here, and both are practical rather than
philosophical:

  * A diff table over prose the tutor has never read is not a review surface, it
    is homework. `meta.prev_body` plus an Undo button is a fifth of the code and
    answers the only question he will actually ask, which is "put it back".
  * "Stream it into a live preview" sounds free and is not: this is a
    schema-constrained call, so what streams is RAW JSON, and a live preview of
    raw JSON is worse than a spinner.

The block's EXISTING provenance goes back into the prompt. A refine that forgets
where the block came from is how a cited, grounded paragraph quietly becomes an
uncited general-knowledge one that still has the citation chip attached.
"""
from __future__ import annotations

from app.i18n import answer_in, language_directive
from app.llm.factory import get_provider

REFINE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Keep the existing title unless the instruction asks you to change it.",
        },
        "body": {
            "type": "string",
            "description": "The FULL rewritten text of this block — not a diff, not a note about what you changed.",
        },
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}


def build_refine_messages(
    *,
    instruction: str,
    title: str,
    body: str,
    kind: str,
    language: str,
    citations: list[dict] | None = None,
    context: str | None = None,
) -> list[dict]:
    """Pure. The tutor's instruction is the LAST thing the model reads, after the
    block and its provenance — recency wins, and what he typed is the point of the
    call."""
    system = (
        "You are editing ONE block of a guitar teacher's lesson, on his "
        "instruction. Output ONLY the JSON object.\n\n"
        "REWRITE THE WHOLE BLOCK, in full. Do the thing he asked and change "
        "nothing else — this is HIS work, and he asked for one edit, not a "
        "rewrite. If his instruction only touches one paragraph, the rest of the "
        "block should come back recognisably the same.\n\n"
        "NEVER invent a page citation. The provenance below is what this block was "
        "written from; if your edit goes beyond it, say so in the prose rather "
        "than attaching a page number to it.\n\n"
        f"{language_directive(language)}"
    )

    parts = [f"BLOCK ({kind}): {title}", "", body]
    if citations:
        cited = ", ".join(
            f"{c.get('source_title') or c.get('source_ref')} p.{c.get('page')}"
            for c in citations
        )
        parts.append(f"\nTHIS BLOCK WAS WRITTEN FROM: {cited}")
    if context:
        parts.append(f"\nFROM HIS LIBRARY:\n{context}")
    parts.append(f"\nWHAT THE TUTOR WANTS CHANGED:\n{instruction}")
    parts.append(f"\n{answer_in(language)}")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


def refine_block(db, block, instruction: str) -> dict:
    """Rewrite `block` per `instruction`, stashing the old body for Undo. Mutates
    `block`; the caller commits.

    `prev_body` is stashed on EVERY refine, overwriting the last one — the Undo is
    one level deep and is not an edit history. A tutor who refines twice and wants
    the original back is asking for version control, and version control over
    lesson prose is a different feature with a different UI.
    """
    from app.brain.retrieve import search

    meta = block.meta or {}
    citations = meta.get("citations") or []

    # Ground the edit in his library the same way the block was grounded in the
    # first place. Cheap (one search), and it is what makes "give more detail about
    # the Amp" produce HIS amp chapter rather than a generic one.
    context = None
    try:
        hits = search(db, f"{block.title} {instruction}", k=4)
        if hits:
            context = "\n\n".join(f"[{h.source_title}, p.{h.page}] {h.text}" for h in hits)
    except Exception:
        # Retrieval is an improvement here, not a precondition — the block's own
        # text and provenance are already in the prompt. A dead embedder must not
        # take the Extend-with-chat button down with it.
        context = None

    result = get_provider().guided_json(
        build_refine_messages(
            instruction=instruction,
            title=block.title,
            body=block.body or "",
            kind=block.kind,
            language=block.language,
            citations=citations,
            context=context,
        ),
        REFINE_SCHEMA,
        role="draft",
    )

    prev_body, prev_title = block.body, block.title
    block.title = (result.get("title") or block.title).strip() or block.title
    block.body = result.get("body") or block.body
    # WHOLE-DICT REASSIGNMENT — Block.meta is plain sa.JSON with no MutableDict.
    block.meta = {
        **meta,
        "prev_body": prev_body,
        "prev_title": prev_title,
        "refined": True,
        "refine_instruction": instruction,
    }
    return {"title": block.title, "body": block.body}


def undo_refine(block) -> bool:
    """Put it back. Returns False when there is nothing stashed."""
    meta = block.meta or {}
    if "prev_body" not in meta:
        return False
    block.body = meta.get("prev_body")
    block.title = meta.get("prev_title") or block.title
    block.meta = {
        k: v for k, v in meta.items()
        if k not in ("prev_body", "prev_title", "refined", "refine_instruction")
    }
    return True
