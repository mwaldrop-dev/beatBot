"""
Band Newsletter Bot — main entry point.

Starts:
  1. SQLite database initialization
  2. Background scheduler (Gmail polling every 15 min)
  3. An immediate first poll on startup
  4. Slack bot in Socket Mode (blocking — keeps the process alive)
"""

import logging
import os
import sys

# Configure logging before importing anything else
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Ensure DATA_DIR exists early
from src.config import DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)

from src import database as db
from src.scheduler import start_scheduler, poll_and_ingest
from src.slack_bot import start


def main():
    logger.info("Band Newsletter Bot starting up...")

    # Initialize database
    db.init_db()

    # Start background scheduler
    scheduler = start_scheduler(poll_interval_minutes=15)

    # Run one immediate poll so we catch up on anything that arrived since last deploy
    logger.info("Running initial poll...")
    try:
        poll_and_ingest()
    except Exception as e:
        logger.error(f"Initial poll failed (non-fatal): {e}")

    # Start Slack bot — this blocks until the process is killed
    try:
        start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
