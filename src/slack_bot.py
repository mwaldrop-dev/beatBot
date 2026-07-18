"""
Slack bot using Bolt with Socket Mode.

Behaviors:
  - Announces new newsletters to SLACK_ANNOUNCE_CHANNEL
  - Answers Q&A when mentioned (@BandBot what is call time Friday?)
  - Answers Q&A in DMs (just send a message directly)
  - In SLACK_ANNOUNCE_CHANNEL specifically, answers question-like messages
    even without an @-mention
  - Responds to "help" with a usage guide
"""

import logging
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.config import SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_ANNOUNCE_CHANNEL, ADMIN_SLACK_USER_IDS
from src.vector_store import VectorStore

logger = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN)
_vector_store: VectorStore | None = None

MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+>")

QUESTION_WORDS = (
    "who", "what", "when", "where", "why", "how", "is", "are", "can",
    "could", "do", "does", "did", "will", "would", "should", "any",
)

ADD_PATTERN = re.compile(r"^add:\s*(.+)", re.IGNORECASE)
URL_LINE_PATTERN = re.compile(r"^url:\s*(\S+)", re.IGNORECASE)


def _looks_like_question(text: str) -> bool:
    """Heuristic: does this message look like it's asking something?"""
    stripped = text.strip()
    if not stripped:
        return False
    if "?" in stripped:
        return True
    first_word = re.split(r"\W+", stripped.lower(), maxsplit=1)[0]
    return first_word in QUESTION_WORDS


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


def _handle_add_command(text: str, user_id: str, say):
    """
    Admin-only: DM the bot `add: <title>` on the first line, optionally
    `url: <link>` on the next, then the content on the rest — indexes it
    into the same searchable archive as newsletters and calendar events.
    """
    if user_id not in ADMIN_SLACK_USER_IDS:
        say("Sorry, only the bot admin can add entries to the knowledge base.")
        return

    lines = text.split("\n")
    title = ADD_PATTERN.match(lines[0]).group(1).strip()

    remaining = lines[1:]
    url = ""
    if remaining:
        url_match = URL_LINE_PATTERN.match(remaining[0].strip())
        if url_match:
            url = url_match.group(1)
            remaining = remaining[1:]

    body = "\n".join(remaining).strip()
    if not body:
        say(
            "Got a title but no content to add — send `add: Title` on the "
            "first line, optionally `url: <link>` on the next, then the "
            "details on the following line(s)."
        )
        return

    chunk_count = get_vector_store().add_manual_entry(title=title, url=url, body=body)
    confirmation = f":white_check_mark: Added *{title}* to the knowledge base ({chunk_count} chunk(s))."
    if url:
        confirmation += f"\n<{url}|Reference link>"
    say(confirmation)


@app.event("app_mention")
def handle_mention(event, say):
    """User @-mentioned the bot in a channel."""
    text = event.get("text", "")
    question = MENTION_PATTERN.sub("", text).strip()
    thread_ts = event.get("thread_ts") or event.get("ts")
    _handle_question(question, say, thread_ts=thread_ts)


@app.event("message")
def handle_message(event, say):
    """
    Handle direct messages, and question-like messages in
    SLACK_ANNOUNCE_CHANNEL that don't @-mention the bot (explicit mentions
    are handled by handle_mention via the app_mention event instead, so
    they're skipped here to avoid answering twice).
    """
    # Ignore bot messages (including our own "thinking..." edits) to avoid loops
    if event.get("bot_id") or event.get("subtype"):
        return

    raw_text = event.get("text", "")
    if MENTION_PATTERN.search(raw_text):
        return

    question = raw_text.strip()

    if event.get("channel_type") == "im":
        if ADD_PATTERN.match(question):
            _handle_add_command(question, event.get("user", ""), say)
        else:
            _handle_question(question, say)
        return

    if event.get("channel") == SLACK_ANNOUNCE_CHANNEL and _looks_like_question(question):
        thread_ts = event.get("thread_ts") or event.get("ts")
        _handle_question(question, say, thread_ts=thread_ts)


# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------

def start():
    """Start the Slack bot in Socket Mode (blocking)."""
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Starting Slack bot in Socket Mode...")
    handler.start()
