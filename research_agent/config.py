"""Configuration centrale de l'agent.

Chaque valeur peut être surchargée par une variable d'environnement
(préfixe RA_), par exemple : RA_MODEL=llama3.1:8b RA_NUM_CTX=8192.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"RA_{name}", default)


@dataclass(frozen=True)
class Config:
    # --- Serveurs ---------------------------------------------------------
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    searxng_url: str = field(default_factory=lambda: _env("SEARXNG_URL", "http://localhost:8080"))

    # --- Modèles ----------------------------------------------------------
    model: str = field(default_factory=lambda: _env("MODEL", "qwen3:8b"))
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "nomic-embed-text"))
    # Taille de contexte : à ajuster après la mesure de l'étape 1 (doit tenir dans les 8 Go de VRAM).
    num_ctx: int = field(default_factory=lambda: int(_env("NUM_CTX", "8192")))
    temperature: float = 0.2
    llm_timeout_s: float = 300.0
    # Tokens réservés à la sortie (la réflexion compte dedans) ; le reste du contexte va au message.
    max_output_tokens: int = 2048
    max_output_tokens_thinking: int = 4096
    chars_per_token: float = 3.0  # estimation prudente, calibrée à l'étape 3

    # --- Recherche et lecture --------------------------------------------
    results_per_query: int = 8
    search_interval_s: float = 2.0  # délai entre deux recherches réelles (limite les blocages des moteurs)
    # Catégories SearXNG : les moteurs généralistes d'abord ; si aucun ne répond (blocage, CAPTCHA),
    # les bases scientifiques interrogées par API (arXiv, Crossref, OpenAlex, Semantic Scholar…).
    search_categories: str = field(default_factory=lambda: _env("SEARCH_CATEGORIES", "general"))
    search_fallback_categories: str = field(
        default_factory=lambda: _env("SEARCH_FALLBACK_CATEGORIES", "science")
    )
    # Résultats du moteur écartés s'ils sont hors sujet (titre + extrait vs question), calibré à l'étape 8 :
    # sources utiles ≥ 0,57 (95 % ≥ 0,63) ; bruit des moteurs (tests de débit, Gmail…) 0,32–0,53.
    min_hit_relevance: float = 0.55
    urls_to_read_per_round: int = 6
    max_pages_per_domain: int = 2  # sur toute la recherche : force la diversité des sources
    fetch_timeout_s: float = 15.0
    # Certains sites (ex. Wikimedia) exigent un user-agent identifiant l'outil et un contact :
    # RA_USER_AGENT="research-agent/0.1 (contact: vous@exemple.org)"
    user_agent: str = field(
        default_factory=lambda: _env("USER_AGENT", "research-agent/0.1 (local research tool)")
    )
    max_page_bytes: int = 15_000_000  # les thèses et rapports en PDF dépassent souvent 5 Mo
    # Domaines jamais lus (réseaux sociaux, vidéo) : écartés par le code, avant toute sélection.
    blocked_domains: tuple[str, ...] = (
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "threads.net",
        "linkedin.com",
        "pinterest.com",
        "pinterest.fr",
        "youtube.com",
        "youtu.be",
    )
    passage_tokens: int = 450  # taille cible d'un passage
    passages_per_call: int = 5  # passages envoyés au LLM par appel d'extraction
    cache_dir: str = field(default_factory=lambda: _env("CACHE_DIR", ".cache"))

    # --- Boucle agentique (étape 5) ---------------------------------------
    max_rounds: int = 3
    min_domains_per_subquestion: int = 2
    min_total_sources: int = 4  # sources fiables (medium/high) lues au total
    max_queries_per_round: int = 4  # tours complémentaires : requêtes ciblées

    # --- Vérification (étape 6) -------------------------------------------
    verify: bool = True
    synthesize: bool = True
    max_claims_to_verify: int = 8
    max_claims_per_comparison: int = 12  # affirmations comparées par sous-question (20 : délai dépassé)
    # Seuils calibrés sur nomic-embed-text (étape 6) :
    claim_similarity: float = 0.91  # deux affirmations disent la même chose (prudent : mieux vaut vérifier)
    duplicate_passage_similarity: float = 0.95  # deux passages sont le même texte repris (dépêche syndiquée)
    evidence_similarity: float = 0.70  # un passage du corpus est une preuve pertinente pour l'affirmation
    verification_pages: int = 2  # pages lues sur le web par affirmation si le corpus ne suffit pas


CONFIG = Config()
