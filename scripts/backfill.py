#!/usr/bin/env python3
"""
Index the full newsletter archive into the vector store without posting
Slack announcements for each one. Useful for the first run against an
inbox that already has newsletters in it.

Run this BEFORE starting the bot for the first time:
    python scripts/backfill.py

After this runs, newsletters found here are marked processed, so the
normal scheduler will skip them and only announce genuinely new ones.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

from src import database as db
from src.gmail_client import GmailClient
from src.newsletter_parser import fetch_and_parse
from src.vector_store import VectorStore


def backfill():
    db.init_db()
    vs = VectorStore()
    gmail = GmailClient()

    newsletters = gmail.fetch_new_newsletters(since_timestamp=None)
    logger.info(f"Found {len(newsletters)} newsletter(s) matching the Gmail query")

    indexed = skipped = failed = 0

    for nl in newsletters:
        gmail_id = nl["gmail_id"]
        subject = nl["subject"]
        date_str = nl["date"]
        url = nl["newsletter_url"]

        if db.is_processed(gmail_id):
            skipped += 1
            continue

        parsed = fetch_and_parse(url)
        if not parsed:
            logger.warning(f"Could not parse {subject!r} ({url}) — skipping")
            failed += 1
            continue

        if not vs.newsletter_already_indexed(gmail_id):
            vs.add_newsletter(
                gmail_id=gmail_id,
                subject=subject,
                url=url,
                date_str=date_str,
                chunks=parsed.chunks,
            )

        db.mark_processed(gmail_id, subject, date_str, url)
        indexed += 1
        logger.info(f"Indexed: {subject!r}")

    logger.info(
        f"Backfill complete — indexed {indexed}, skipped {skipped} "
        f"(already processed), failed {failed}."
    )
    logger.info(
        "No Slack announcements were sent and no poll checkpoint was set — "
        "run the bot normally next; it will only announce newsletters not "
        "seen here."
    )


if __name__ == "__main__":
    backfill()
