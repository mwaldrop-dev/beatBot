"""
Fetches a public Membership Toolkit newsletter page and extracts clean text.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BandNewsletterBot/1.0; "
        "+https://github.com/your-org/band-newsletter-bot)"
    )
}

# Tags that are never useful content
STRIP_TAGS = [
    "script", "style", "nav", "header", "footer",
    "aside", "form", "button", "input", "select",
    "noscript", "iframe", "svg",
]

# CSS classes / IDs that are navigation / chrome (common in MT pages)
NOISE_PATTERNS = re.compile(
    r"(nav|navigation|menu|sidebar|footer|header|cookie|banner|social|share|breadcrumb)",
    re.IGNORECASE,
)


@dataclass
class ParsedNewsletter:
    url: str
    title: str
    text: str          # Full clean text
    chunks: list[str]  # Split into ~500-word chunks for embedding


def _is_noise_element(tag) -> bool:
    """Return True if a tag looks like navigation / boilerplate."""
    for attr in ("class", "id"):
        value = " ".join(tag.get(attr, []) if attr == "class" else [tag.get(attr, "")])
        if NOISE_PATTERNS.search(value):
            return True
    return False


def fetch_and_parse(url: str) -> Optional[ParsedNewsletter]:
    """
    Download the Membership Toolkit newsletter page and return a ParsedNewsletter.
    Returns None if the page cannot be fetched or contains no useful content.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # Extract page title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Band Newsletter"

    # Remove noisy structural tags entirely
    for tag in soup(STRIP_TAGS):
        tag.decompose()

    # Remove noisy elements by class/id heuristic
    for tag in soup.find_all(True):
        if _is_noise_element(tag):
            tag.decompose()

    # Try to isolate the main content area (MT often uses <main> or a content div)
    main = (
        soup.find("main")
        or soup.find(id=re.compile(r"content|main|article", re.I))
        or soup.find(class_=re.compile(r"content|main|article|email-body", re.I))
        or soup.body
    )

    if not main:
        logger.warning(f"No content element found in {url}")
        return None

    # Get text, preserving paragraph breaks
    lines = []
    for elem in main.descendants:
        if isinstance(elem, str):
            text = elem.strip()
            if text:
                lines.append(text)
        elif elem.name in ("p", "br", "h1", "h2", "h3", "h4", "li", "tr"):
            lines.append("\n")

    raw_text = " ".join(lines)

    # Collapse excess whitespace / newlines
    clean = re.sub(r"\n{3,}", "\n\n", raw_text)
    clean = re.sub(r" {2,}", " ", clean).strip()

    if len(clean) < 100:
        logger.warning(f"Very short content ({len(clean)} chars) for {url} — may have failed to parse")

    chunks = _chunk_text(clean)
    logger.info(f"Parsed {url!r}: title={title!r}, {len(clean)} chars, {len(chunks)} chunks")

    return ParsedNewsletter(url=url, title=title, text=clean, chunks=chunks)


def _chunk_text(text: str, max_words: int = 400, overlap_words: int = 50) -> list[str]:
    """
    Split text into overlapping chunks of ~max_words words.
    Overlap helps the retriever find context that spans chunk boundaries.
    """
    words = text.split()
    if not words:
        return []

    chunks = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end == len(words):
            break
        start += max_words - overlap_words

    return chunks
