"""
Slack bot using Bolt with Socket Mode.

Behaviors:
  - Announces new newsletters to SLACK_ANNOUNCE_CHANNEL
  - Answers Q&A when mentioned (@BandBot what is call time Friday?)
  - Answers Q&A in DMs (just send a message directly)
  - Responds to "help" with a usage guide
"""

import logging
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from datetime import datetime, timedelta

from src.config import SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_ANNOUNCE_CHANNEL, ADMIN_SLACK_USER_IDS
from src.vector_store import VectorStore
from src import database as db

logger = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN)
_vector_store: VectorStore | None = None

MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+>")

ADD_PATTERN = re.compile(r"^add:\s*(.+)", re.IGNORECASE)
URL_LINE_PATTERN = re.compile(r"^url:\s*(\S+)", re.IGNORECASE)


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
        f"<{url}|Read the full newsletter>"
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


# Phrases the bot uses when it has nothing — these mark an info gap in the log.
NO_ANSWER_MARKERS = (
    "couldn't find", "could not find", "not in the excerpts", "isn't in the",
    "is not in the", "don't have any newsletters", "no newsletters in my archive",
    "ran into an error",
)


def _is_no_answer(answer: str) -> bool:
    a = (answer or "").lower()
    return any(m in a for m in NO_ANSWER_MARKERS)


def _handle_question(question: str, say, user_id: str = None, source: str = "dm", thread_ts=None):
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

    # Record for the weekly admin digest (never let logging break the reply)
    try:
        db.log_qa(user_id, source, question, answer, not _is_no_answer(answer))
    except Exception as e:
        logger.error(f"Failed to log Q&A: {e}")

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
    # Post as a regular top-level message rather than a thread reply — more
    # obvious for less Slack-savvy users than a reply tucked into a thread.
    _handle_question(question, say, user_id=event.get("user"), source="mention")


@app.event("message")
def handle_message(event, say):
    """
    Handle direct messages only. Channel questions are answered solely via an
    explicit @-mention (see handle_mention) — the bot never auto-answers
    un-mentioned messages in a channel.
    """
    # Ignore bot messages (including our own "thinking..." edits) to avoid loops
    if event.get("bot_id") or event.get("subtype"):
        return

    # Only respond in direct messages; ignore everything else in channels.
    if event.get("channel_type") != "im":
        return

    raw_text = event.get("text", "")
    if MENTION_PATTERN.search(raw_text):
        return

    question = raw_text.strip()
    if ADD_PATTERN.match(question):
        _handle_add_command(question, event.get("user", ""), say)
    elif question.lower() == "digest":
        _handle_digest_command(event.get("user", ""), say)
    else:
        _handle_question(question, say, user_id=event.get("user"), source="dm")


# ---------------------------------------------------------------------------
# Weekly Q&A digest (admin-only)
# ---------------------------------------------------------------------------

def _display_name(user_id: str, cache: dict) -> str:
    if not user_id:
        return "Unknown"
    if user_id not in cache:
        try:
            p = app.client.users_info(user=user_id)["user"]
            cache[user_id] = (p.get("real_name")
                              or p.get("profile", {}).get("display_name")
                              or p.get("name") or user_id)
        except Exception:
            cache[user_id] = user_id
    return cache[user_id]


def send_weekly_digest():
    """DM each admin a summary of the questions asked in the last 7 days, with
    the ones the bot couldn't answer called out as likely info gaps."""
    if not ADMIN_SLACK_USER_IDS:
        logger.info("Weekly digest skipped — no ADMIN_SLACK_USER_IDS configured")
        return

    end = datetime.utcnow()
    start = end - timedelta(days=7)
    rows = db.get_qa_between(start, end)
    names: dict = {}

    gaps = [r for r in rows if not r["answered"]]
    label = f"{start.strftime('%b %d').lstrip('0')}–{end.strftime('%b %d').lstrip('0')}"
    lines = [
        f":bar_chart: *beatBot — questions this week* ({label})",
        f"*{len(rows)}* question{'s' if len(rows) != 1 else ''} asked"
        + (f" · :warning: *{len(gaps)}* the bot couldn't answer" if gaps else ""),
    ]

    if gaps:
        lines.append("\n:warning: *Couldn't answer — likely info gaps:*")
        for r in gaps:
            lines.append(f"• \"{r['question']}\" — _{_display_name(r['user_id'], names)}_")

    if rows:
        lines.append("\n*All questions:*")
        for r in rows[:60]:
            when = datetime.fromisoformat(r["asked_at"]).strftime("%a")
            flag = "" if r["answered"] else " :warning:"
            lines.append(f"• \"{r['question']}\" — _{_display_name(r['user_id'], names)}_ ({when}){flag}")
        if len(rows) > 60:
            lines.append(f"_…and {len(rows) - 60} more._")
    else:
        lines.append("\n_No questions were asked this week._")

    text = "\n".join(lines)
    for admin_id in ADMIN_SLACK_USER_IDS:
        try:
            app.client.chat_postMessage(channel=admin_id, text=text)
        except Exception as e:
            logger.error(f"Failed to DM weekly digest to {admin_id}: {e}")
    logger.info(f"Weekly digest sent to {len(ADMIN_SLACK_USER_IDS)} admin(s) "
                f"({len(rows)} Q&A, {len(gaps)} gaps)")


def _handle_digest_command(user_id: str, say):
    """Admin-only: DM `digest` to get this week's Q&A summary on demand
    (the same report the weekly job sends). Runs inside the live app, so no
    Railway shell needed."""
    if user_id not in ADMIN_SLACK_USER_IDS:
        say("Sorry, only the bot admin can pull the Q&A digest.")
        return
    say(":hourglass_flowing_sand: Building the last 7 days of Q&A…")
    try:
        send_weekly_digest()
    except Exception as e:
        logger.error(f"Manual digest failed: {e}")
        say("Sorry, I couldn't build the digest — check the logs.")


# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------

def start():
    """Start the Slack bot in Socket Mode (blocking)."""
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Starting Slack bot in Socket Mode...")
    handler.start()
