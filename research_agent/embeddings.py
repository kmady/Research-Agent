"""Embeddings via Ollama (`/api/embed`), utilisés pour classer les passages.

nomic-embed-text attend des préfixes de tâche : « search_query: » pour la requête
et « search_document: » pour les passages.
"""

from __future__ import annotations

from typing import Protocol

import httpx


class Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        query_prefix: str = "search_query: ",
        document_prefix: str = "search_document: ",
        batch_size: int = 32,
        timeout_s: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.batch_size = batch_size
        self.timeout_s = timeout_s
        self.client = client or httpx.Client()
        self._memo: dict[str, list[float]] = {}

    def _embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._memo]
        for i in range(0, len(missing), self.batch_size):
            batch = missing[i : i + self.batch_size]
            r = self.client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": batch, "truncate": True},
                timeout=self.timeout_s,
            )
            r.raise_for_status()
            for text, vec in zip(batch, r.json()["embeddings"], strict=True):
                self._memo[text] = vec
        return [self._memo[t] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.query_prefix + text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.document_prefix + t for t in texts])
