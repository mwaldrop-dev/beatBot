"""
Background scheduler that polls Gmail every 15 minutes for new newsletters,
fetches the Membership Toolkit page, indexes the content, and announces to Slack.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from src import database as db
from src.calendar_client import fetch_all_events
from src.gmail_client import GmailClient
from src.newsletter_parser import fetch_and_parse
from src.vector_store import VectorStore

logger = logging.getLogger(__name__)

_vector_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store


def poll_and_ingest():
    """
    Core polling job:
    1. Ask Gmail for new newsletter emails since last poll
    2. For each new email, fetch the MT page and index it
    3. Announce to Slack
    """
    logger.info("=== Newsletter poll starting ===")

    since = db.get_last_poll_time()
    now = datetime.now(timezone.utc)

    try:
        gmail = GmailClient()
        newsletters = gmail.fetch_new_newsletters(since_timestamp=since)
    except Exception as e:
        logger.error(f"Gmail fetch failed: {e} — will retry from the same checkpoint next poll")
        return

    logger.info(f"Gmail returned {len(newsletters)} newsletter(s) to process")
    vs = get_vector_store()

    # Import here to avoid circular import at module load time
    from src.slack_bot import announce_newsletter

    any_failures = False

    for nl in newsletters:
        gmail_id = nl["gmail_id"]
        subject = nl["subject"]
        date_str = nl["date"]
        url = nl["newsletter_url"]

        if db.is_processed(gmail_id):
            logger.info(f"Already processed {gmail_id!r} — skipping")
            continue

        # Fetch and parse the MT page
        parsed = fetch_and_parse(url)
        if not parsed:
            logger.warning(f"Could not parse newsletter at {url} — will retry next poll")
            any_failures = True
            continue

        # Index into ChromaDB (skip if already there, e.g. from a previous partial run)
        if not vs.newsletter_already_indexed(gmail_id):
            vs.add_newsletter(
                gmail_id=gmail_id,
                subject=subject,
                url=url,
                date_str=date_str,
                chunks=parsed.chunks,
            )
        else:
            logger.info(f"Chunks for {gmail_id!r} already in vector store — skipping re-index")

        # Mark as processed in SQLite
        db.mark_processed(gmail_id, subject, date_str, url)

        # Announce to Slack
        try:
            announce_newsletter(subject=subject, url=url, date_str=date_str)
            db.mark_announced(gmail_id)
        except Exception as e:
            logger.error(f"Slack announcement failed for {gmail_id!r}: {e}")

    if any_failures:
        logger.info(
            "One or more newsletters failed to parse this poll — not advancing the "
            "checkpoint so they're retried next time"
        )
    else:
        db.set_last_poll_time(now)

    # Calendar sync is independent of the newsletter checkpoint above — a
    # failure here shouldn't block newsletter processing or vice versa.
    try:
        events = fetch_all_events()
        vs.sync_calendar_events(events)
    except Exception as e:
        logger.error(f"Calendar sync failed: {e}")

    logger.info("=== Newsletter poll complete ===")


def start_scheduler(poll_interval_minutes: int = 15) -> BackgroundScheduler:
    """Start the background polling scheduler and return it."""
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        poll_and_ingest,
        trigger="interval",
        minutes=poll_interval_minutes,
        id="newsletter_poll",
        name="Poll Gmail for new newsletters",
        max_instances=1,  # Prevent overlapping runs
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info(f"Scheduler started — polling every {poll_interval_minutes} minutes")
    return scheduler
