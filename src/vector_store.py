"""
ChromaDB-backed vector store for newsletter content.
Embeds newsletter chunks and retrieves relevant passages for Q&A.
"""

import email.utils
import logging
import os
import re
from datetime import datetime
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions
from google import genai
from google.genai import types

from src.config import GEMINI_API_KEY, CHROMA_PATH

logger = logging.getLogger(__name__)

COLLECTION_NAME = "newsletters"
EMBED_MODEL = "gemini-embedding-001"
QA_MODEL = "gemini-flash-latest"
TOP_K = 5  # Number of chunks to retrieve per question
GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"

# The band's newsletter year runs April 1 -> March 31. Most questions mean
# "this season" implicitly, so retrieval defaults to it and only widens to
# the full archive when the question is clearly asking about the past.
SEASON_START_MONTH = 4

HISTORICAL_KEYWORDS = (
    "last year", "last season", "previous year", "previous season",
    "prior year", "prior season", "past season", "in the past",
    "used to", "history", "historically", "old newsletter", "years ago",
)


def _season_start_year(dt: datetime) -> int:
    """Which season a date falls in, keyed by the calendar year it starts in."""
    return dt.year if dt.month >= SEASON_START_MONTH else dt.year - 1


def _parse_email_date(date_str: str) -> Optional[datetime]:
    try:
        return email.utils.parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return None


def _looks_historical(question: str) -> bool:
    """Heuristic: does this question seem to be asking about a past season?"""
    lower = question.lower()
    if any(keyword in lower for keyword in HISTORICAL_KEYWORDS):
        return True

    current = _season_start_year(datetime.now())
    current_season_years = {current, current + 1}
    for year_str in re.findall(r"\b(20\d{2})\b", question):
        if int(year_str) not in current_season_years:
            return True

    return False


class VectorStore:
    def __init__(self):
        os.makedirs(CHROMA_PATH, exist_ok=True)
        self._client = chromadb.PersistentClient(path=CHROMA_PATH)

        # Gemini embeddings are asymmetric: documents and queries should be
        # embedded with different task types for good retrieval quality.
        self._doc_embed_fn = embedding_functions.GoogleGeminiEmbeddingFunction(
            model_name=EMBED_MODEL,
            task_type="RETRIEVAL_DOCUMENT",
            api_key_env_var=GEMINI_API_KEY_ENV_VAR,
        )
        self._query_embed_fn = embedding_functions.GoogleGeminiEmbeddingFunction(
            model_name=EMBED_MODEL,
            task_type="RETRIEVAL_QUERY",
            api_key_env_var=GEMINI_API_KEY_ENV_VAR,
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._doc_embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        self._genai = genai.Client(api_key=GEMINI_API_KEY)
        logger.info(
            f"VectorStore ready — {self._collection.count()} chunks in collection"
        )

    def add_newsletter(
        self,
        gmail_id: str,
        subject: str,
        url: str,
        date_str: str,
        chunks: list[str],
    ):
        """Embed and store all chunks from a newsletter."""
        if not chunks:
            logger.warning(f"No chunks to store for {gmail_id!r}")
            return

        parsed_date = _parse_email_date(date_str)
        season_start_year = (
            _season_start_year(parsed_date) if parsed_date else _season_start_year(datetime.now())
        )

        ids = [f"{gmail_id}_{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "gmail_id": gmail_id,
                "subject": subject,
                "url": url,
                "date": date_str,
                "chunk_index": i,
                "season_start_year": season_start_year,
            }
            for i in range(len(chunks))
        ]

        # ChromaDB batches embeddings automatically
        self._collection.add(
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
        )
        logger.info(f"Stored {len(chunks)} chunks for newsletter {gmail_id!r} ({subject!r})")

    def newsletter_already_indexed(self, gmail_id: str) -> bool:
        """Check if any chunks for this gmail_id already exist."""
        results = self._collection.get(
            where={"gmail_id": gmail_id},
            limit=1,
        )
        return len(results["ids"]) > 0

    def answer_question(self, question: str) -> str:
        """
        Retrieve the most relevant newsletter chunks and ask GPT to answer the question.
        Returns a formatted answer string suitable for posting in Slack.
        """
        if self._collection.count() == 0:
            return "I don't have any newsletters in my archive yet. Check back after the next newsletter arrives!"

        # Retrieve relevant chunks. Embed the question with the query-side
        # embedding function so it matches document embeddings correctly.
        query_embedding = self._query_embed_fn([question])[0].tolist()

        current_season = _season_start_year(datetime.now())
        scope_to_season = not _looks_historical(question)

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=TOP_K,
            where={"season_start_year": current_season} if scope_to_season else None,
            include=["documents", "metadatas", "distances"],
        )

        docs = results["documents"][0]
        metas = results["metadatas"][0]
        distances = results["distances"][0]

        if not docs and scope_to_season:
            # Nothing from this season yet (e.g. early in a new one) — fall
            # back to the full archive rather than saying nothing was found.
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=TOP_K,
                include=["documents", "metadatas", "distances"],
            )
            docs = results["documents"][0]
            metas = results["metadatas"][0]
            distances = results["distances"][0]

        if not docs:
            return "I couldn't find anything relevant in the newsletters for that question."

        # Build context block for GPT
        context_parts = []
        sources_seen = set()
        for doc, meta, dist in zip(docs, metas, distances):
            subject = meta.get("subject", "Newsletter")
            date = meta.get("date", "")
            url = meta.get("url", "")
            source_key = meta.get("gmail_id", "")

            context_parts.append(
                f"[From: {subject} ({date})]\n{doc}"
            )
            if source_key not in sources_seen:
                sources_seen.add(source_key)

        context = "\n\n---\n\n".join(context_parts)

        # Build source footer
        source_lines = []
        for meta in metas:
            key = meta.get("gmail_id", "")
            if key in sources_seen:
                source_lines.append(
                    f"• {meta.get('subject', 'Newsletter')} — <{meta.get('url', '')}|View>"
                )
                sources_seen.discard(key)

        today_str = datetime.now().strftime("%A, %B %d, %Y")

        prompt = f"""You are a helpful assistant for a high school band program.
Today's date is {today_str}. Use this to correctly reason about relative
time references in the question and excerpts (e.g. "this week", "last
year", "next game") — do not guess or assume which year is "current".
Answer the following question using ONLY the newsletter excerpts provided below.
Be concise and specific. If the answer isn't in the excerpts, say so honestly.
If times, dates, or locations are mentioned, highlight them clearly.

QUESTION: {question}

NEWSLETTER EXCERPTS:
{context}
"""

        response = self._genai.models.generate_content(
            model=QA_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=500,
                # This is a plain extractive RAG answer, not a reasoning task.
                # Without this, the model's internal "thinking" tokens can eat
                # the whole max_output_tokens budget, truncating the visible
                # answer mid-sentence (reproduced against the real archive).
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )

        answer = response.text.strip()

        # Append source links
        if source_lines:
            unique_sources = list(dict.fromkeys(source_lines))  # deduplicate
            answer += "\n\n_Sources:_\n" + "\n".join(unique_sources[:3])

        return answer
