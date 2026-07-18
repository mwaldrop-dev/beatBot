"""
ChromaDB-backed vector store for newsletter content.
Embeds newsletter chunks and retrieves relevant passages for Q&A.
"""

import logging
import os
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions
from openai import OpenAI

from src.config import OPENAI_API_KEY, CHROMA_PATH

logger = logging.getLogger(__name__)

COLLECTION_NAME = "newsletters"
EMBED_MODEL = "text-embedding-3-small"
QA_MODEL = "gpt-4o-mini"
TOP_K = 5  # Number of chunks to retrieve per question


class VectorStore:
    def __init__(self):
        os.makedirs(CHROMA_PATH, exist_ok=True)
        self._client = chromadb.PersistentClient(path=CHROMA_PATH)
        self._embed_fn = embedding_functions.OpenAIEmbeddingFunction(
            api_key=OPENAI_API_KEY,
            model_name=EMBED_MODEL,
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        self._openai = OpenAI(api_key=OPENAI_API_KEY)
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

        ids = [f"{gmail_id}_{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "gmail_id": gmail_id,
                "subject": subject,
                "url": url,
                "date": date_str,
                "chunk_index": i,
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

        # Retrieve relevant chunks
        results = self._collection.query(
            query_texts=[question],
            n_results=min(TOP_K, self._collection.count()),
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

        prompt = f"""You are a helpful assistant for a high school band program.
Answer the following question using ONLY the newsletter excerpts provided below.
Be concise and specific. If the answer isn't in the excerpts, say so honestly.
If times, dates, or locations are mentioned, highlight them clearly.

QUESTION: {question}

NEWSLETTER EXCERPTS:
{context}
"""

        response = self._openai.chat.completions.create(
            model=QA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=500,
        )

        answer = response.choices[0].message.content.strip()

        # Append source links
        if source_lines:
            unique_sources = list(dict.fromkeys(source_lines))  # deduplicate
            answer += "\n\n_Sources:_\n" + "\n".join(unique_sources[:3])

        return answer
