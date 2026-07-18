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

from src.calendar_client import CALENDAR_TZ, CalendarEvent, event_url, format_event_date
from src.config import GEMINI_API_KEY, CHROMA_PATH, CALENDAR_INFO_URL

logger = logging.getLogger(__name__)

COLLECTION_NAME = "newsletters"
EMBED_MODEL = "gemini-embedding-001"
QA_MODEL = "gemini-flash-latest"
GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"
EMBED_BATCH_SIZE = 100  # Gemini's batch embedding endpoint's per-call cap

# Retrieved separately per source (rather than one combined top-K ranking)
# so a cluster of near-duplicate calendar entries (e.g. one per band camp
# day) can't crowd newsletter prose out of the context entirely.
SOURCE_TOP_K = 4

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
                "source": "newsletter",
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

    def sync_calendar_events(self, events: list[CalendarEvent]):
        """
        Upsert calendar events into the vector store (unlike newsletters,
        events can be edited in place — e.g. a game time changing — so this
        always overwrites by UID), and remove any previously-indexed events
        that no longer exist in the fetched set (cancelled/deleted).
        """
        if not events:
            return

        # UID alone isn't a unique key: recurring events can have multiple
        # VEVENT components (recurrence exceptions) sharing the same UID
        # with different start times. Keying by dict also absorbs any exact
        # duplicate entries a feed might contain.
        by_id = {}
        for e in events:
            event_id = f"cal_{e.uid}_{int(e.start.timestamp())}"
            date_str = format_event_date(e)
            parts = [f"{e.summary} — {date_str}"]
            if e.location:
                parts.append(f"Location: {e.location}")
            if e.description:
                parts.append(e.description)
            by_id[event_id] = {
                "document": "\n".join(parts),
                "metadata": {
                    "source": "calendar",
                    "gmail_id": event_id,
                    "subject": e.summary or e.calendar_name,
                    "url": event_url(e) or CALENDAR_INFO_URL,
                    "date": date_str,
                    "chunk_index": 0,
                    # Use local calendar-day, not UTC, so events near
                    # midnight don't get misfiled across the April 1 cutoff.
                    "season_start_year": _season_start_year(e.start.astimezone(CALENDAR_TZ)),
                },
            }

        ids = list(by_id.keys())
        documents = [v["document"] for v in by_id.values()]
        metadatas = [v["metadata"] for v in by_id.values()]

        # Gemini's batch embedding endpoint caps at 100 requests per call.
        for i in range(0, len(ids), EMBED_BATCH_SIZE):
            self._collection.upsert(
                ids=ids[i:i + EMBED_BATCH_SIZE],
                documents=documents[i:i + EMBED_BATCH_SIZE],
                metadatas=metadatas[i:i + EMBED_BATCH_SIZE],
            )

        existing = self._collection.get(where={"source": "calendar"}, include=[])
        stale_ids = set(existing["ids"]) - set(ids)
        if stale_ids:
            self._collection.delete(ids=list(stale_ids))
            logger.info(f"Removed {len(stale_ids)} stale calendar event(s)")

        logger.info(f"Synced {len(events)} calendar event(s)")

    def _query_source(
        self, query_embedding, source: str, current_season: int, scope_to_season: bool
    ):
        """Query one source (newsletter/calendar) for its own top-K, with the
        same season-scoping + full-archive fallback as the overall search."""
        source_filter = {"source": source}
        where = (
            {"$and": [source_filter, {"season_start_year": current_season}]}
            if scope_to_season else source_filter
        )
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=SOURCE_TOP_K,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        docs = results["documents"][0]
        if not docs and scope_to_season:
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=SOURCE_TOP_K,
                where=source_filter,
                include=["documents", "metadatas", "distances"],
            )
            docs = results["documents"][0]
        return docs, results["metadatas"][0], results["distances"][0]

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

        docs, metas, distances = [], [], []
        for source in ("newsletter", "calendar"):
            d, m, dist = self._query_source(query_embedding, source, current_season, scope_to_season)
            docs.extend(d)
            metas.extend(m)
            distances.extend(dist)

        if not docs:
            return "I couldn't find anything relevant in the newsletters or calendar for that question."

        # Sort by relevance across both sources combined (ascending distance
        # = most similar first). Without this, newsletter results — always
        # queried first above — fill the citation list's truncation below
        # before a more-relevant calendar result is ever reached.
        order = sorted(range(len(docs)), key=lambda i: distances[i])
        docs = [docs[i] for i in order]
        metas = [metas[i] for i in order]
        distances = [distances[i] for i in order]

        # Build context block for GPT
        context_parts = []
        sources_seen = set()
        for doc, meta, dist in zip(docs, metas, distances):
            subject = meta.get("subject", "Newsletter")
            date = meta.get("date", "")
            url = meta.get("url", "")
            source_key = meta.get("gmail_id", "")
            source_label = "Calendar" if meta.get("source") == "calendar" else "Newsletter"

            context_parts.append(
                f"[{source_label}: {subject} ({date})]\n{doc}"
            )
            if source_key not in sources_seen:
                sources_seen.add(source_key)

        context = "\n\n---\n\n".join(context_parts)

        # Build source footer
        source_lines = []
        for meta in metas:
            key = meta.get("gmail_id", "")
            if key in sources_seen:
                subject = meta.get("subject", "Newsletter")
                url = meta.get("url", "")
                source_lines.append(f"• {subject} — <{url}|View>" if url else f"• {subject}")
                sources_seen.discard(key)

        today_str = datetime.now().strftime("%A, %B %d, %Y")

        prompt = f"""You are a helpful assistant for a high school band program.
Today's date is {today_str}. Use this silently to resolve relative time
references in the question and excerpts (e.g. "this week", "last year",
"next game") — do not guess or assume which year is "current". Do not
explain this reasoning or mention today's date in your answer; just give
the resolved answer directly.
Answer the following question using ONLY the excerpts provided below, which
come from either the newsletter archive or the band calendar.
Be concise and specific. If the answer isn't in the excerpts, say so honestly.
If times, dates, or locations are mentioned, highlight them clearly.

QUESTION: {question}

EXCERPTS:
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
