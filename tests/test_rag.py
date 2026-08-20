"""Tests for the Qdrant RAG pipeline.

We avoid two heavy dependencies so these run fast and offline:
  - the fastembed model download -> stub TextEmbedding + patch _embed with a
    deterministic hash-based vector,
  - a running Qdrant server -> use qdrant-client's in-memory mode.

This tests OUR plumbing (chunking, id determinism, upsert, retrieve wiring),
not the embedding model's semantic quality (that's fastembed's job).
"""

from __future__ import annotations

import hashlib

import pytest
from qdrant_client import QdrantClient

from app.agents import rag as rag_mod
from app.agents.rag import RepoChunk, RepoRAG

EMB_DIM = 384


def _fake_vector(text: str) -> list[float]:
    """Deterministic pseudo-embedding: identical text -> identical vector.

    Not semantic, but enough that an exact-text query retrieves its own chunk
    as the nearest neighbor under cosine distance.
    """
    h = hashlib.sha256(text.encode()).digest()
    # Tile the 32-byte digest out to EMB_DIM floats in [0, 1).
    return [(h[i % len(h)] / 255.0) for i in range(EMB_DIM)]


@pytest.fixture
def rag(monkeypatch):
    # Stub the embedding model so __init__ doesn't download anything.
    class _StubEmbed:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(rag_mod, "TextEmbedding", _StubEmbed)
    # Force in-memory Qdrant regardless of the url passed.
    monkeypatch.setattr(rag_mod, "QdrantClient", lambda *a, **k: QdrantClient(":memory:"))

    r = RepoRAG(collection_name="test_repo")
    # Replace the real embedder with our deterministic one.
    monkeypatch.setattr(r, "_embed", lambda texts: [_fake_vector(t) for t in texts])
    return r


def test_chunk_id_is_stable_uuid():
    c1 = RepoChunk("a.py", "code", 1, 11)
    c2 = RepoChunk("a.py", "code", 1, 11)
    c3 = RepoChunk("a.py", "code", 12, 22)
    # Same span -> same id (idempotent upsert); different span -> different id.
    assert c1.chunk_id == c2.chunk_id
    assert c1.chunk_id != c3.chunk_id
    # Must be a valid UUID string (Qdrant requirement), not a raw hex digest.
    import uuid

    uuid.UUID(c1.chunk_id)  # raises if invalid


def test_ingest_returns_chunk_count(rag):
    files = [("app/foo.py", "def foo():\n    return 1\n" * 50)]
    n = rag.ingest_repo(files)
    assert n > 0


def test_ingest_skips_empty_files(rag):
    assert rag.ingest_repo([("empty.py", "   \n  ")]) == 0


def test_retrieve_finds_ingested_content(rag):
    files = [
        ("auth.py", "def login(user, password): validate_credentials(user)"),
        ("math_utils.py", "def add(a, b): return a + b"),
    ]
    rag.ingest_repo(files)
    hits = rag.retrieve("def login(user, password): validate_credentials(user)", top_k=1)
    assert len(hits) == 1
    assert hits[0]["file_path"] == "auth.py"


def test_ingest_is_idempotent(rag):
    files = [("app/foo.py", "def foo(): return 1\n" * 30)]
    n1 = rag.ingest_repo(files)
    n2 = rag.ingest_repo(files)
    # Same content re-ingested upserts to the same ids -> collection size stable.
    assert n1 == n2
    info = rag.client.get_collection("test_repo")
    assert info.points_count == n1
