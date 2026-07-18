"""
Slack bot using Bolt with Socket Mode.

Behaviors:
  - Announces new newsletters to SLACK_ANNOUNCE_CHANNEL
  - Answers Q&A when mentioned (@BandBot what is call time Friday?)
  - Answers Q&A in DMs (just send a message directly)
  - Responds to "help" with a usage guide
"""

import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.config import SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_ANNOUNCE_CHANNEL
from src.vector_store import VectorStore

logger = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN)
_vector_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store


# ---------------------------------------------------------------------------
# Announce a new newsletter (called from the scheduler)
# ---------------------------------------------------------------------------

def announce_newsletter(subject: str, url: str, date_str: str):
    """Post a new newsletter announcement to the configured channel."""
    text = (
        f":mega: *New Band Newsletter!*\n"
        f"*{subject}*\n"
        f"<{url}|Read the full newsletter>\n"
        f"_You can ask me questions about it — just mention me in this channel or send me a DM!_"
    )
    try:
        app.client.chat_postMessage(
            channel=SLACK_ANNOUNCE_CHANNEL,
            text=text,
            unfurl_links=False,
        )
        logger.info(f"Announced newsletter: {subject!r}")
    except Exception as e:
        logger.error(f"Failed to announce newsletter: {e}")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    ":musical_note: *Band Newsletter Bot — Help*\n\n"
    "I have access to the archive of band newsletters and can answer your questions.\n\n"
    "*How to ask questions:*\n"
    "• Mention me in any channel: `@BandBot what is call time for Friday's game?`\n"
    "• Or just send me a direct message.\n\n"
    "*Example questions:*\n"
    "• _What is call time for the game this week?_\n"
    "• _When is the next home game?_\n"
    "• _What do I need to bring to the next performance?_\n"
    "• _What are the fundraiser details?_\n"
    "• _Is there a rehearsal this week?_\n\n"
    "I search across all newsletters in my archive to find the answer."
)


def _handle_question(question: str, say, thread_ts=None):
    """Look up an answer and post it."""
    question = question.strip()
    if not question:
        say(HELP_TEXT, thread_ts=thread_ts)
        return

    lower = question.lower()
    if lower in ("help", "?", "help me"):
        say(HELP_TEXT, thread_ts=thread_ts)
        return

    # Show a "thinking" message for better UX
    thinking = say(":hourglass_flowing_sand: Searching the newsletters...", thread_ts=thread_ts)

    try:
        answer = get_vector_store().answer_question(question)
    except Exception as e:
        logger.error(f"Q&A error: {e}")
        answer = "Sorry, I ran into an error searching the newsletters. Please try again."

    # Update the thinking message with the actual answer
    try:
        app.client.chat_update(
            channel=thinking["channel"],
            ts=thinking["ts"],
            text=answer,
        )
    except Exception:
        # If update fails, just post a new message
        say(answer, thread_ts=thread_ts)


@app.event("app_mention")
def handle_mention(event, say):
    """User @-mentioned the bot in a channel."""
    # Strip the mention prefix (<@UXXXXXXX> ...) from the text
    text = event.get("text", "")
    # Remove the first <@...> token
    import re
    question = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    thread_ts = event.get("thread_ts") or event.get("ts")
    _handle_question(question, say, thread_ts=thread_ts)


@app.event("message")
def handle_dm(event, say):
    """Handle direct messages to the bot."""
    # Only respond to DMs (channel_type = 'im'), not channel messages
    if event.get("channel_type") != "im":
        return
    # Ignore bot messages to avoid loops
    if event.get("bot_id") or event.get("subtype"):
        return

    question = event.get("text", "").strip()
    _handle_question(question, say)


# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------

def start():
    """Start the Slack bot in Socket Mode (blocking)."""
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Starting Slack bot in Socket Mode...")
    handler.start()
