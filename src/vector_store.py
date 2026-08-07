"""
ChromaDB-backed vector store for newsletter content.
Embeds newsletter chunks and retrieves relevant passages for Q&A.
"""

import email.utils
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

import chromadb
from chromadb.utils import embedding_functions
from google import genai
from google.genai import types

from src.calendar_client import CALENDAR_TZ, CalendarEvent, event_url, format_event_date
from src.config import GEMINI_API_KEY, CHROMA_PATH, CALENDAR_INFO_URL
from src.newsletter_parser import _chunk_text

logger = logging.getLogger(__name__)

COLLECTION_NAME = "newsletters"
EMBED_MODEL = "gemini-embedding-001"
# Use the floating alias, not a pinned version: pinned Gemini versions get
# retired ("no longer available to new users" -> 404), while the alias always
# resolves to a current, available model. To survive model-generation changes we
# also send NO thinking_config below (2.5 used thinking_budget, 3.x uses a
# thinking_level enum — passing the wrong one 400s), and give generateContent a
# roomy token budget so default "thinking" can't truncate the visible answer.
QA_MODEL = "gemini-flash-latest"
GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"
EMBED_BATCH_SIZE = 100  # Gemini's batch embedding endpoint's per-call cap

# Retrieved separately per source (rather than one combined top-K ranking)
# so a cluster of near-duplicate calendar entries (e.g. one per band camp
# day) can't crowd newsletter prose out of the context entirely.
SOURCE_TOP_K = 4

# The band's season runs June 1 -> May 31. Most questions mean "this season"
# implicitly, so retrieval defaults to it and only widens to the full archive
# when the question is clearly asking about the past.
SEASON_START_MONTH = 6

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


def _parse_stored_date(date_str: str) -> Optional[datetime]:
    """Parse a chunk's stored `date` — newsletters use RFC-2822 email dates,
    manual notes use "%A, %B %d, %Y" — for recomputing its season on demand."""
    parsed = _parse_email_date(date_str)
    if parsed:
        return parsed
    try:
        return datetime.strptime(date_str, "%A, %B %d, %Y")
    except (TypeError, ValueError):
        return None


def _detect_window(question: str, now: datetime) -> Optional[Tuple[float, float]]:
    """If the question is time-scoped (this week, next week, upcoming, next game,
    …), return the (start_ts, end_ts) unix range to pull calendar events from.
    None means 'not time-scoped' → fall back to semantic calendar search."""
    q = question.lower()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def span(start: datetime, end: datetime) -> Tuple[float, float]:
        return start.timestamp(), end.timestamp()

    if "today" in q or "tonight" in q:
        return span(today, today + timedelta(days=1))
    if "tomorrow" in q:
        d = today + timedelta(days=1)
        return span(d, d + timedelta(days=1))
    if "this weekend" in q:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        return span(sat, sat + timedelta(days=2))
    if "next week" in q:
        mon = today + timedelta(days=7 - today.weekday())
        return span(mon, mon + timedelta(days=7))
    if "this week" in q:
        mon = today - timedelta(days=today.weekday())
        return span(mon, mon + timedelta(days=7))
    if "next month" in q:
        first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return span(first, (first + timedelta(days=32)).replace(day=1))
    if "this month" in q:
        first = today.replace(day=1)
        return span(first, (first + timedelta(days=32)).replace(day=1))
    # "upcoming", "coming up", "what's on the schedule", "next game/rehearsal/…"
    if any(k in q for k in ("upcoming", "coming up", "what's next", "whats next", "schedule")):
        return span(today, today + timedelta(days=30))
    if re.search(r"\bnext\s+(game|rehearsal|practice|competition|event|show|performance|meeting|concert)", q):
        return span(today, today + timedelta(days=45))
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

    def add_manual_entry(self, title: str, url: str, body: str) -> int:
        """
        Index an ad-hoc admin-provided note (e.g. pasted from an email or
        flyer) alongside newsletters and calendar events. Keyed by title,
        so re-adding the same title overwrites the old content — the
        expected way to fix a typo'd entry, matching how calendar events
        are upserted by identity rather than accumulated as new copies.
        """
        chunks = _chunk_text(body)
        if not chunks:
            return 0

        entry_id = hashlib.sha256(title.strip().lower().encode()).hexdigest()[:16]
        date_str = datetime.now().strftime("%A, %B %d, %Y")

        ids = [f"manual_{entry_id}_{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "source": "manual",
                "gmail_id": f"manual_{entry_id}",
                "subject": title,
                "url": url,
                "date": date_str,
                "chunk_index": i,
                "season_start_year": _season_start_year(datetime.now()),
            }
            for i in range(len(chunks))
        ]

        # Delete-then-add rather than upsert: if a correction has a
        # different chunk count than the original, upserting by index
        # would leave the extra old chunks behind as stale duplicates.
        self._collection.delete(where={"gmail_id": f"manual_{entry_id}"})
        self._collection.add(ids=ids, documents=chunks, metadatas=metadatas)
        logger.info(f"Stored {len(chunks)} manual chunk(s): {title!r}")
        return len(chunks)

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
                    # Unix start time, so time-scoped questions ("next week")
                    # can retrieve events by date range instead of by embedding.
                    "start_ts": int(e.start.timestamp()),
                    # Use local calendar-day, not UTC, so events near
                    # midnight don't get misfiled across the June 1 cutoff.
                    "season_start_year": _season_start_year(e.start.astimezone(CALENDAR_TZ)),
                },
            }

        ids = list(by_id.keys())

        # Only (re-)embed events whose text is new or changed. Expanding
        # recurrences multiplies the event count, and re-embedding every one on
        # every 15-min poll would be slow and costly. When only metadata changed
        # (e.g. start_ts added, season re-tag), update it without re-embedding.
        existing = self._collection.get(where={"source": "calendar"}, include=["documents", "metadatas"])
        existing_docs = dict(zip(existing["ids"], existing["documents"]))
        existing_metas = dict(zip(existing["ids"], existing["metadatas"]))

        embed_ids, embed_docs, embed_metas = [], [], []
        meta_ids, meta_metas = [], []
        for eid in ids:
            doc = by_id[eid]["document"]
            meta = by_id[eid]["metadata"]
            if existing_docs.get(eid) != doc:
                embed_ids.append(eid); embed_docs.append(doc); embed_metas.append(meta)
            elif existing_metas.get(eid) != meta:
                meta_ids.append(eid); meta_metas.append(meta)

        # Gemini's batch embedding endpoint caps at 100 requests per call.
        for i in range(0, len(embed_ids), EMBED_BATCH_SIZE):
            self._collection.upsert(
                ids=embed_ids[i:i + EMBED_BATCH_SIZE],
                documents=embed_docs[i:i + EMBED_BATCH_SIZE],
                metadatas=embed_metas[i:i + EMBED_BATCH_SIZE],
            )
        if meta_ids:
            self._collection.update(ids=meta_ids, metadatas=meta_metas)

        stale_ids = set(existing_docs) - set(ids)
        if stale_ids:
            self._collection.delete(ids=list(stale_ids))
            logger.info(f"Removed {len(stale_ids)} stale calendar event(s)")

        logger.info(
            f"Synced {len(events)} calendar event(s) "
            f"({len(embed_ids)} embedded, {len(meta_ids)} meta-updated)"
        )

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

    def _calendar_in_window(self, start_ts: float, end_ts: float, current_season: int,
                            scope_to_season: bool, limit: int = 15):
        """Fetch calendar events whose start falls in [start_ts, end_ts] by date
        (not embedding), soonest first — so time-scoped questions get the events
        that are actually in that window. Returns (documents, metadatas)."""
        conds = [
            {"source": "calendar"},
            {"start_ts": {"$gte": int(start_ts)}},
            {"start_ts": {"$lte": int(end_ts)}},
        ]
        if scope_to_season:
            conds.append({"season_start_year": current_season})
        got = self._collection.get(where={"$and": conds}, include=["documents", "metadatas"])
        docs = got.get("documents", []) or []
        metas = got.get("metadatas", []) or []
        order = sorted(range(len(docs)), key=lambda i: metas[i].get("start_ts", 0))[:limit]
        return [docs[i] for i in order], [metas[i] for i in order]

    def retag_seasons(self) -> int:
        """Recompute season_start_year for stored newsletter/note chunks from
        their date, so a change to SEASON_START_MONTH takes effect on data that
        was indexed under the old boundary. Calendar events re-tag themselves on
        the next sync; this covers the ingest-once sources. Returns # updated."""
        got = self._collection.get(
            where={"source": {"$in": ["newsletter", "manual"]}},
            include=["metadatas"],
        )
        ids = got.get("ids", []) or []
        metas = got.get("metadatas", []) or []
        upd_ids, upd_metas = [], []
        for cid, meta in zip(ids, metas):
            parsed = _parse_stored_date(meta.get("date", ""))
            if not parsed:
                continue
            season = _season_start_year(parsed)
            if meta.get("season_start_year") != season:
                new_meta = dict(meta)
                new_meta["season_start_year"] = season
                upd_ids.append(cid)
                upd_metas.append(new_meta)
        if upd_ids:
            self._collection.update(ids=upd_ids, metadatas=upd_metas)
        return len(upd_ids)

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
        # Newsletters + notes always come from semantic search. The calendar is
        # special: for a time-scoped question ("next week", "upcoming", "next
        # game") we pull events by DATE RANGE (embeddings are date-blind), else
        # fall back to semantic. Windowed events get distance -1 so they lead.
        for source in ("newsletter", "manual"):
            d, m, dist = self._query_source(query_embedding, source, current_season, scope_to_season)
            docs.extend(d)
            metas.extend(m)
            distances.extend(dist)

        window = _detect_window(question, datetime.now(CALENDAR_TZ))
        if window:
            wdocs, wmetas = self._calendar_in_window(window[0], window[1], current_season, scope_to_season)
            docs.extend(wdocs)
            metas.extend(wmetas)
            distances.extend([-1.0] * len(wdocs))
        else:
            d, m, dist = self._query_source(query_embedding, "calendar", current_season, scope_to_season)
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
            source_label = {"calendar": "Calendar", "manual": "Note"}.get(meta.get("source"), "Newsletter")

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
come from the newsletter archive, the band calendar, or additional notes.
Be concise and specific. If the answer isn't in the excerpts, say so honestly.
If times, dates, or locations are mentioned, highlight them clearly.
If the calendar and newsletter excerpts describe the same event
differently (a different date, time, or location), do not silently pick
one — explicitly flag the discrepancy so the reader knows to double check.

QUESTION: {question}

EXCERPTS:
{context}
"""

        response = self._genai.models.generate_content(
            model=QA_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                # Roomy budget so the model's internal "thinking" tokens can't
                # eat the whole allotment and truncate the visible answer. We
                # deliberately omit thinking_config: the knob differs by model
                # generation (2.5 thinking_budget vs 3.x thinking_level), and
                # sending the wrong one fails with 400 INVALID_ARGUMENT.
                max_output_tokens=2048,
            ),
        )

        answer = response.text.strip()

        # Append source links
        if source_lines:
            unique_sources = list(dict.fromkeys(source_lines))  # deduplicate
            answer += "\n\n_Sources:_\n" + "\n".join(unique_sources[:3])

        return answer
