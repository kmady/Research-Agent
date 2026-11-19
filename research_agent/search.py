"""Recherche web via un SearchProvider (implémentation : SearXNG auto-hébergé)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import logging

import httpx

from .cache import DiskCache
from .state import normalize_url


log = logging.getLogger(__name__)


class SearchError(RuntimeError):
    pass


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    engines: list[str] = field(default_factory=list)
    published_date: str = ""


class SearchProvider(Protocol):
    def search(self, query: str, n: int = 8) -> list[SearchHit]: ...


class SearxngProvider:
    """Interroge l'API JSON de SearXNG (`/search?format=json`)."""

    def __init__(
        self,
        base_url: str,
        *,
        language: str = "auto",
        timeout_s: float = 30.0,
        min_interval_s: float = 1.0,
        categories: str = "general",
        fallback_categories: str = "",
        cache: DiskCache | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.language = language
        # Catégories interrogées d'abord, puis en secours si elles ne donnent rien (moteurs bloqués).
        self.categories = categories
        self.fallback_categories = fallback_categories
        self.timeout_s = timeout_s
        self.cache = cache
        self.client = client or httpx.Client()
        # Délai minimal entre deux requêtes réelles : les moteurs publics bloquent les rafales (CAPTCHA, 429).
        self.min_interval_s = min_interval_s
        self._last_request = 0.0

    def _raw_results(self, query: str, categories: str) -> list[dict]:
        # Clé de cache inchangée pour « general » : les résultats déjà en cache restent valables.
        key = f"{self.language}|{query}" if categories == "general" else f"{self.language}|{categories}|{query}"
        if self.cache and (cached := self.cache.get(key)) is not None:
            return cached
        wait = self.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        try:
            r = self.client.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "language": self.language, "categories": categories},
                timeout=self.timeout_s,
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"recherche « {query} » en échec : {exc}") from exc
        failing = data.get("unresponsive_engines") or []
        if not results and failing:
            # SearXNG répond 200 avec 0 résultat quand les moteurs bloquent (CAPTCHA, délai) :
            # ce n'est pas « rien trouvé », c'est une panne — on la signale comme telle.
            names = ", ".join(f"{e[0]} ({e[1]})" if isinstance(e, list) and len(e) > 1 else str(e) for e in failing)
            raise SearchError(f"moteurs de recherche indisponibles pour « {query} » : {names}")
        if self.cache and results:  # on ne met pas en cache une réponse vide
            self.cache.set(key, results)
        return results

    def _results_with_fallback(self, query: str) -> list[dict]:
        """Catégories principales ; si elles sont en panne ou vides, catégories de secours."""
        try:
            results = self._raw_results(query, self.categories)
            error = None
        except SearchError as exc:
            results, error = [], exc
        if results or not self.fallback_categories:
            if error:
                raise error
            return results
        try:
            backup = self._raw_results(query, self.fallback_categories)
        except SearchError:
            backup = []
        if backup:
            log.info("  « %s » : moteurs généralistes %s, résultats pris dans « %s »", query,
                     "indisponibles" if error else "sans résultat", self.fallback_categories)
            return backup
        if error:
            raise error
        return []

    def search(self, query: str, n: int = 8) -> list[SearchHit]:
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in self._results_with_fallback(query):
            url = (item.get("url") or "").strip()
            # Certains moteurs renvoient des liens de redirection relatifs (« http:///go/v1/... ») : on les écarte.
            if not url.startswith(("http://", "https://")) or not urlsplit(url).netloc:
                continue
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            hits.append(SearchHit(
                url=url,
                title=(item.get("title") or "").strip(),
                snippet=(item.get("content") or "").strip(),
                engines=list(item.get("engines") or []),
                published_date=item.get("publishedDate") or "",
            ))
            if len(hits) >= n:
                break
        return hits
