"""
Extracts plain text from uploaded document files (PDF, DOCX, plain text)
so their content can be chunked and indexed as FAQ entries.
"""

import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = ("pdf", "docx", "txt", "md")


def extract_text(content: bytes, filetype: str) -> Optional[str]:
    """
    Extract plain text from file bytes. `filetype` is Slack's lowercase
    file extension (e.g. "pdf", "docx", "txt"). Returns None if the type
    is unsupported or extraction fails.
    """
    filetype = (filetype or "").lower()
    try:
        if filetype == "pdf":
            return _extract_pdf(content)
        if filetype == "docx":
            return _extract_docx(content)
        if filetype in ("txt", "md", "text"):
            return content.decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"Failed to extract text from .{filetype} file: {e}")
        return None
    return None


def _extract_pdf(content: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(content: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs)
