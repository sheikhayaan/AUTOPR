"""Qdrant-backed RAG for grounding agents in repo patterns.

Ingestion workflow (run once per repo or on repo updates):
  1. Chunk repo files into manageable pieces (e.g. functions, classes, or
     fixed-size overlapping windows).
  2. Embed each chunk via fastembed (local, fast, no API cost).
  3. Store vectors + metadata (file path, chunk text) in Qdrant.

Retrieval workflow (on every agent call that needs context):
  1. Query Qdrant with the changed file paths or a natural-language description
     of the change.
  2. Return the top-k most similar chunks.
  3. Inject that context into the agent's prompt.

This grounds Code Reviewer ("similar functions in this repo use X pattern")
and Fix Agent ("here's how the repo handles similar errors") without
hallucinating nonexistent patterns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.config import settings

try:
    # fastembed pulls onnxruntime (heavy), so it is the optional 'rag' extra. RAG is
    # off by default (the worker builds the graph with rag=None), so the app runs
    # without it — importing this module must not hard-require the dependency.
    from fastembed import TextEmbedding
except ImportError:  # pragma: no cover - depends on whether the 'rag' extra is installed
    TextEmbedding = None  # type: ignore[assignment,misc]


@dataclass(frozen=True)
class RepoChunk:
    """A single chunk of code from the repo."""

    file_path: str
    content: str
    start_line: int
    end_line: int

    @property
    def chunk_id(self) -> str:
        """Deterministic ID for deduplication.

        Qdrant requires point IDs to be an unsigned int or a UUID string — a
        raw hex digest is rejected. We derive a stable UUIDv5 from the
        file+line span so re-ingesting the same chunk upserts (not duplicates).
        """
        return str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{self.file_path}:{self.start_line}-{self.end_line}")
        )


class RepoRAG:
    """Qdrant-backed RAG for a single repo."""

    def __init__(
        self,
        qdrant_url: str | None = None,
        collection_name: str = "autopr_repo",
    ) -> None:
        self.client = QdrantClient(url=qdrant_url or settings.qdrant_url)
        self.collection = collection_name
        # fastembed: fast local embeddings. BAAI/bge-small-en-v1.5 is 384-dim,
        # runs offline, no API cost. Suitable for code similarity.
        if TextEmbedding is None:  # pragma: no cover - only when the 'rag' extra is absent
            raise RuntimeError(
                "RAG requires the optional 'fastembed' dependency. "
                "Install it with: uv sync --extra rag"
            )
        self.embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        self.embedding_dim = 384
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        """Create the collection if it doesn't exist. Idempotent."""
        existing = self.client.get_collections().collections
        if any(c.name == self.collection for c in existing):
            return
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.embedding_dim, distance=Distance.COSINE),
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per text."""
        # fastembed returns a generator of np arrays; convert to list of lists
        return [vec.tolist() for vec in self.embedding_model.embed(texts)]

    def ingest_repo(self, files: list[tuple[str, str]]) -> int:
        """Chunk and store a list of (file_path, file_content) tuples.

        Returns the number of chunks stored.
        """
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=512, chunk_overlap=50, length_function=len
        )
        chunks: list[RepoChunk] = []
        texts: list[str] = []

        for path, content in files:
            if not content.strip():
                continue
            # Split into chunks
            splits = splitter.split_text(content)
            for i, chunk_text in enumerate(splits):
                # Estimate line numbers (rough; a real impl would parse properly)
                start_line = i * 10 + 1
                end_line = start_line + 10
                chunk = RepoChunk(path, chunk_text, start_line, end_line)
                chunks.append(chunk)
                texts.append(chunk_text)

        if not chunks:
            return 0

        # Batch embed all chunks
        vectors = self._embed(texts)

        points: list[PointStruct] = []
        for chunk, vec in zip(chunks, vectors, strict=True):
            points.append(
                PointStruct(
                    id=chunk.chunk_id,
                    vector=vec,
                    payload={
                        "file_path": chunk.file_path,
                        "content": chunk.content,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                    },
                )
            )

        self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """Retrieve the top-k most similar chunks to the query.

        Returns a list of dicts with keys: file_path, content, start_line, end_line.
        """
        vec = self._embed([query])[0]
        # qdrant-client >=1.14 replaced .search() with .query_points(); the
        # response wraps hits in .points.
        response = self.client.query_points(collection_name=self.collection, query=vec, limit=top_k)
        return [hit.payload for hit in response.points if hit.payload]
