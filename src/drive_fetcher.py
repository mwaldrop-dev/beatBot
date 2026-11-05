"""
Fetches the text of a publicly shared Google Drive file or Google Doc from its
share link, so an admin can add a FAQ entry by pasting the link instead of
re-uploading the file. Only works for items shared as "Anyone with the link
can view" — no Google sign-in is involved.
"""

import logging
import re
from typing import Tuple

import requests

from src.document_extractor import extract_text

logger = logging.getLogger(__name__)

# drive.google.com/file/d/<id>/view, drive.google.com/open?id=<id>, /uc?id=<id>
_DRIVE_FILE_PATTERNS = (
    re.compile(r"drive\.google\.com/file/d/([\w-]+)"),
    re.compile(r"drive\.google\.com/(?:open|uc)\?(?:[^#\s]*&)?id=([\w-]+)"),
)
# A native Google Doc (not an uploaded PDF/Word file)
_DOC_PATTERN = re.compile(r"docs\.google\.com/document/d/([\w-]+)")

# usercontent host + confirm=t skips Google's virus-scan interstitial page,
# which otherwise replaces the file for anything over ~25 MB.
_FILE_DOWNLOAD_URL = "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t"
_DOC_EXPORT_URL = "https://docs.google.com/document/d/{id}/export?format=txt"

MAX_BYTES = 25 * 1024 * 1024
TIMEOUT_SECONDS = 30
USER_AGENT = "beatBot-document-fetcher"


class DriveFetchError(Exception):
    """Raised with a message that is safe to show the admin in Slack."""


def is_google_drive_url(url: str) -> bool:
    return bool(_DOC_PATTERN.search(url) or any(p.search(url) for p in _DRIVE_FILE_PATTERNS))


def _looks_like_html(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _get(url: str) -> requests.Response:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logger.error(f"Drive fetch failed for {url}: {e}")
        raise DriveFetchError("Couldn't reach Google Drive — please try again in a moment.")
    if len(resp.content) > MAX_BYTES:
        raise DriveFetchError(f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB — too big to index.")
    return resp


_ACCESS_HINT = (
    "Couldn't open that link without signing in. In Google Drive, set sharing to "
    "*Anyone with the link → Viewer* and try again."
)


def _filename_from(resp: requests.Response) -> str:
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', resp.headers.get("Content-Disposition", ""))
    return match.group(1).strip() if match else ""


def fetch_google_document(url: str) -> Tuple[str, str]:
    """
    Return (text, filename) for a public Google Doc or Drive file (PDF, Word,
    or plain text). Raises DriveFetchError with a user-facing message if the
    link isn't reachable, isn't public, or isn't a supported file type.
    """
    doc = _DOC_PATTERN.search(url)
    if doc:
        resp = _get(_DOC_EXPORT_URL.format(id=doc.group(1)))
        if resp.status_code != 200 or _looks_like_html(resp.content):
            raise DriveFetchError(_ACCESS_HINT)
        text = resp.content.decode("utf-8-sig", errors="replace")
        if not text.strip():
            raise DriveFetchError("That Google Doc appears to be empty.")
        return text, "Google Doc"

    file_id = None
    for pattern in _DRIVE_FILE_PATTERNS:
        match = pattern.search(url)
        if match:
            file_id = match.group(1)
            break
    if not file_id:
        raise DriveFetchError("That doesn't look like a Google Drive file or Google Doc link.")

    resp = _get(_FILE_DOWNLOAD_URL.format(id=file_id))
    content = resp.content
    if resp.status_code != 200 or _looks_like_html(content):
        raise DriveFetchError(_ACCESS_HINT)

    # Identify the type from the bytes themselves — Drive's Content-Type is
    # unreliable (often application/octet-stream).
    if content.startswith(b"%PDF"):
        filetype = "pdf"
    elif content.startswith(b"PK"):
        filetype = "docx"
    elif "text/plain" in resp.headers.get("Content-Type", ""):
        filetype = "txt"
    else:
        raise DriveFetchError(
            "I can read PDF, Word (.docx) and plain-text files from Drive, but that file is a different type."
        )

    text = extract_text(content, filetype)
    if not text or not text.strip():
        raise DriveFetchError("Couldn't extract any text from that file — it may be a scanned/image-only PDF, or a "
            "Word-style file that isn't a regular .docx (e.g. a spreadsheet or slide deck).")
    return text, _filename_from(resp)
