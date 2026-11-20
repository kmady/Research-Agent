"""Étape 2 — Démonstration des outils déterministes sur de vrais services.

    python scripts/demo_step2.py "Comment fonctionne la photosynthèse ?"

Recherche → enregistrement des sources → lecture de 3 pages → découpage → meilleurs passages.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research_agent.cache import DiskCache  # noqa: E402
from research_agent.config import CONFIG  # noqa: E402
from research_agent.embeddings import OllamaEmbedder  # noqa: E402
from research_agent.fetch import Fetcher, chunk_text, rank_passages  # noqa: E402
from research_agent.search import SearxngProvider  # noqa: E402
from research_agent.state import SourceRegistry  # noqa: E402


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "Comment fonctionne la photosynthèse ?"
    t0 = time.time()

    search = SearxngProvider(CONFIG.searxng_url, cache=DiskCache(CONFIG.cache_dir, "search"))
    fetcher = Fetcher(
        user_agent=CONFIG.user_agent,
        timeout_s=CONFIG.fetch_timeout_s,
        max_bytes=CONFIG.max_page_bytes,
        cache=DiskCache(CONFIG.cache_dir, "pages"),
    )
    embedder = OllamaEmbedder(CONFIG.ollama_url, CONFIG.embed_model)
    registry = SourceRegistry()

    hits = search.search(query, n=CONFIG.results_per_query)
    print(f"Recherche « {query} » : {len(hits)} résultats ({time.time() - t0:.1f}s)\n")
    for h in hits:
        registry.add(h.url, h.title, round_no=1, snippet=h.snippet)
    print(registry.catalog(), "\n")

    read = 0
    for src in registry.all():
        if read == 3:
            break
        res = fetcher.fetch(src.url)
        if not res.ok:
            src.fetch_error = res.error
            print(f"✘ {src.id} ignorée : {res.error}")
            continue
        read += 1
        src.publication_date = res.date
        passages = registry.set_passages(src.id, chunk_text(res.text, CONFIG.passage_tokens))
        ranked = rank_passages(query, [p.text for p in passages], embedder, top_k=2)
        print(f"✔ {src.id} {src.domain} : {len(res.text)} caractères → {len(passages)} passages")
        for i, score in ranked:
            p = passages[i]
            print(f"   [{p.id}] score {score:.3f} : {p.text[:200].replace(chr(10), ' ')}…")
        print()

    print(f"Terminé en {time.time() - t0:.1f}s — {read} pages lues sur {len(registry)} sources.")


if __name__ == "__main__":
    main()
