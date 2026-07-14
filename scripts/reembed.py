"""CLI wrapper around `app.brain.reembed.reembed_all` — the deploy-time half of
the embedding migration (Plan 13, Stage 4.2). All the reasoning lives in that
module's docstring; this file exists so the operation is one command:

    docker compose exec -T api python - < scripts/reembed.py
    docker compose exec -T api python - --only-stale < scripts/reembed.py

(Piped over stdin, following `scripts/retrieval_baseline.py`'s own convention:
this directory is at the REPO root and the api image's build context is
`apps/api`, so these files are not inside the container.)

The logic is in `app/` and not here because `POST /knowledge/reindex` runs the
exact same code — the tutor's iMac has no shell anybody is going to open.
"""
import argparse
import logging
import sys

from app.brain.reembed import reembed_all
from app.db import SessionLocal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-embed every chunk in place.")
    parser.add_argument(
        "--only-stale",
        action="store_true",
        help="skip sources already stamped with the current embed model",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db = SessionLocal()
    try:
        summary = reembed_all(db, only_stale=args.only_stale)
    finally:
        db.close()
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
