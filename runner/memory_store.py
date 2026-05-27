"""Vector RAG memory backed by chromadb.

Single collection `agent001_memory`. Embedder is chromadb's default
(sentence-transformers all-MiniLM-L6-v2, ~80MB, downloaded on first
use and cached). Persistent storage at the path supplied by the
caller (typically `memory/chroma_db/`).

Auto-save policy: every chat turn (user + assistant) and every
terminal-state spawned-agent result are written by the callers in
server.py — this module just exposes save/recall/list primitives.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("agent-001.memory")

COLLECTION_NAME = "agent001_memory"


class MemoryStore:
    """Thin wrapper around a chromadb persistent client + single collection."""

    def __init__(self, persist_dir: Path) -> None:
        self._dir = persist_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._client = None
        self._collection = None

    def _ensure(self) -> None:
        if self._collection is not None:
            return
        with self._lock:
            if self._collection is not None:
                return
            import chromadb
            from chromadb.config import Settings
            self._client = chromadb.PersistentClient(
                path=str(self._dir),
                settings=Settings(anonymized_telemetry=False, allow_reset=False),
            )
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            count = self._collection.count()
            log.info("MemoryStore ready | dir=%s | collection=%s | entries=%d",
                     self._dir, COLLECTION_NAME, count)

    def save(self, text: str, metadata: dict[str, Any] | None = None) -> str | None:
        """Persist a single text entry. Returns the new entry id, or None if text is empty."""
        text = (text or "").strip()
        if not text:
            return None
        try:
            self._ensure()
            entry_id = uuid.uuid4().hex
            meta = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "chat",
                **{k: str(v) for k, v in (metadata or {}).items() if v is not None},
            }
            self._collection.add(ids=[entry_id], documents=[text], metadatas=[meta])
            return entry_id
        except Exception as e:
            log.warning("MemoryStore.save failed: %s", e)
            return None

    def recall(self, query: str, k: int = 5, where: dict | None = None) -> list[dict]:
        """Return up to k most-relevant entries for the query.

        Each result: {id, text, metadata, distance}. Lower distance = more relevant.
        """
        query = (query or "").strip()
        if not query:
            return []
        try:
            self._ensure()
            count = self._collection.count()
            if count == 0:
                return []
            n = max(1, min(k, count))
            kwargs = {"query_texts": [query], "n_results": n}
            if where:
                kwargs["where"] = where
            res = self._collection.query(**kwargs)
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[None] * len(ids)])[0]
            out = []
            for i, _id in enumerate(ids):
                out.append({
                    "id": _id,
                    "text": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else None,
                })
            return out
        except Exception as e:
            log.warning("MemoryStore.recall failed: %s", e)
            return []

    def list(self, where: dict | None = None, limit: int = 50) -> list[dict]:
        """Return up to `limit` entries matching `where` (newest first by timestamp metadata)."""
        try:
            self._ensure()
            kwargs = {"limit": limit}
            if where:
                kwargs["where"] = where
            res = self._collection.get(**kwargs)
            ids = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            out = []
            for i, _id in enumerate(ids):
                out.append({
                    "id": _id,
                    "text": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                })
            out.sort(key=lambda e: e.get("metadata", {}).get("timestamp", ""), reverse=True)
            return out
        except Exception as e:
            log.warning("MemoryStore.list failed: %s", e)
            return []

    def count(self) -> int:
        try:
            self._ensure()
            return self._collection.count()
        except Exception:
            return 0
