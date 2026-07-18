"""
Gmail API client — polls for newsletter emails and extracts Membership Toolkit URLs.
"""

import re
import base64
import logging
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import (
    GMAIL_CLIENT_ID,
    GMAIL_CLIENT_SECRET,
    GMAIL_REFRESH_TOKEN,
    NEWSLETTER_SENDER_EMAIL,
    NEWSLETTER_SUBJECT_KEYWORD,
)

logger = logging.getLogger(__name__)

# Matches Membership Toolkit newsletter URLs.
# Typical pattern: https://[subdomain].membershiptoolkit.com/np/...
# or https://membershiptoolkit.com/np/...
MT_URL_PATTERN = re.compile(
    r'https?://[a-zA-Z0-9\-\.]*membershiptoolkit\.com/[^\s"\'<>]+',
    re.IGNORECASE,
)

# Also catch generic "View in browser" / "view this email" links that often
# point to the hosted version of the newsletter (common in MT emails).
HOSTED_VIEW_PATTERN = re.compile(
    r'https?://[a-zA-Z0-9\-\.]*membershiptoolkit\.com/[^\s"\'<>]*(?:email|newsletter|np)[^\s"\'<>]*',
    re.IGNORECASE,
)


def _build_service():
    """Build an authenticated Gmail service using stored OAuth2 credentials."""
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_body(payload: dict) -> str:
    """Recursively extract and decode email body text from a Gmail message payload."""
    body_text = ""

    if payload.get("body", {}).get("data"):
        raw = payload["body"]["data"]
        body_text += base64.urlsafe_b64decode(raw + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        mime = part.get("mimeType", "")
        if mime in ("text/html", "text/plain"):
            data = part.get("body", {}).get("data", "")
            if data:
                body_text += base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        elif mime.startswith("multipart/"):
            body_text += _decode_body(part)

    return body_text


def _extract_mt_url(body: str) -> Optional[str]:
    """
    Find the best Membership Toolkit URL in the email body.
    Prefers the hosted newsletter/view link over generic MT links.
    """
    # Try the more specific hosted newsletter pattern first
    hosted = HOSTED_VIEW_PATTERN.findall(body)
    if hosted:
        return hosted[0].rstrip(".,;)")

    # Fall back to any MT URL
    general = MT_URL_PATTERN.findall(body)
    if general:
        return general[0].rstrip(".,;)")

    return None


def _get_header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


class GmailClient:
    def __init__(self):
        self.service = _build_service()

    def fetch_new_newsletters(self, since_timestamp: Optional[datetime] = None) -> list[dict]:
        """
        Fetch emails from NEWSLETTER_SENDER_EMAIL received after since_timestamp.
        Returns a list of dicts with keys: gmail_id, subject, date, newsletter_url.
        """
        query_parts = [f"from:{NEWSLETTER_SENDER_EMAIL}"]
        if NEWSLETTER_SUBJECT_KEYWORD:
            query_parts.append(f'subject:"{NEWSLETTER_SUBJECT_KEYWORD}"')
        if since_timestamp:
            # Gmail uses epoch seconds for the 'after:' operator
            epoch = int(since_timestamp.replace(tzinfo=timezone.utc).timestamp())
            query_parts.append(f"after:{epoch}")

        query = " ".join(query_parts)
        logger.info(f"Gmail query: {query}")

        results = []
        page_token = None

        try:
            while True:
                kwargs = {"userId": "me", "q": query, "maxResults": 50}
                if page_token:
                    kwargs["pageToken"] = page_token

                response = self.service.users().messages().list(**kwargs).execute()
                messages = response.get("messages", [])

                for msg_ref in messages:
                    msg = self.service.users().messages().get(
                        userId="me", id=msg_ref["id"], format="full"
                    ).execute()

                    payload = msg["payload"]
                    headers = payload.get("headers", [])
                    subject = _get_header(headers, "Subject")
                    date_str = _get_header(headers, "Date")
                    gmail_id = msg["id"]

                    body = _decode_body(payload)
                    url = _extract_mt_url(body)

                    if url:
                        results.append({
                            "gmail_id": gmail_id,
                            "subject": subject,
                            "date": date_str,
                            "newsletter_url": url,
                        })
                        logger.info(f"Found newsletter: {subject!r} → {url}")
                    else:
                        logger.warning(
                            f"Email {gmail_id!r} ({subject!r}) had no MT URL — skipping"
                        )

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        except HttpError as e:
            logger.error(f"Gmail API error: {e}")

        return results
