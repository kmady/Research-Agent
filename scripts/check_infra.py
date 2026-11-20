"""Étape 1 — Vérifie que chaque brique de l'infrastructure fonctionne seule.

    python scripts/check_infra.py              # les 4 vérifications
    python scripts/check_infra.py --ctx-sweep  # + mesure du contexte qui tient dans le GPU

Vérifications :
  1. recherche SearXNG (format JSON)
  2. téléchargement d'une page + extraction du texte (trafilatura)
  3. réponse JSON du LLM conforme à un schéma, validée par Pydantic
  4. embedding avec le modèle d'embeddings
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import trafilatura
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research_agent.config import CONFIG  # noqa: E402

UA = {"User-Agent": CONFIG.user_agent}
OK, KO = "\033[32m✔\033[0m", "\033[31m✘\033[0m"


def check_search() -> list[str]:
    r = httpx.get(f"{CONFIG.searxng_url}/search", params={"q": "photosynthèse", "format": "json"}, timeout=30)
    r.raise_for_status()
    results = r.json()["results"]
    print(f"{OK} SearXNG : {len(results)} résultats")
    for res in results[:3]:
        print(f"   - {res['title'][:70]} | {res['url']}")
    return [res["url"] for res in results]


def check_fetch(urls: list[str]) -> None:
    """Lit la première page accessible ; un site qui refuse est simplement ignoré (comme le fera l'agent)."""
    for url in urls[:6]:
        try:
            r = httpx.get(url, headers=UA, timeout=CONFIG.fetch_timeout_s, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            print(
                f"   (ignorée) {url} → {exc.__class__.__name__} {getattr(exc, 'response', None) and exc.response.status_code or ''}"
            )
            continue
        text = trafilatura.extract(r.text, url=url) or ""
        if len(text) < 200:
            print(f"   (ignorée) {url} → texte extrait trop court")
            continue
        print(f"{OK} Lecture : {len(text)} caractères extraits de {url}")
        print("   " + text[:300].replace("\n", " ") + "…")
        return
    raise RuntimeError("aucune des pages testées n'a pu être lue")


class Capital(BaseModel):
    country: str
    capital: str
    population_millions: float
    sources_needed: bool


def check_llm_json() -> None:
    t0 = time.time()
    r = httpx.post(
        f"{CONFIG.ollama_url}/api/chat",
        json={
            "model": CONFIG.model,
            "stream": False,
            "think": False,
            "format": Capital.model_json_schema(),
            "options": {"num_ctx": CONFIG.num_ctx, "temperature": 0},
            "messages": [
                {
                    "role": "user",
                    "content": "Donne la capitale du Japon et sa population approximative en JSON.",
                }
            ],
        },
        timeout=CONFIG.llm_timeout_s,
    )
    r.raise_for_status()
    data = r.json()
    parsed = Capital.model_validate_json(data["message"]["content"])
    tps = data["eval_count"] / (data["eval_duration"] / 1e9) if data.get("eval_duration") else 0
    print(f"{OK} LLM {CONFIG.model} : JSON valide en {time.time() - t0:.1f}s → {parsed.model_dump()}")
    print(f"   vitesse de génération : {tps:.1f} tokens/s")


def check_embed() -> None:
    r = httpx.post(
        f"{CONFIG.ollama_url}/api/embed",
        json={"model": CONFIG.embed_model, "input": ["La photosynthèse produit de l'oxygène."]},
        timeout=60,
    )
    r.raise_for_status()
    vec = r.json()["embeddings"][0]
    print(f"{OK} Embeddings {CONFIG.embed_model} : dimension {len(vec)}")


def ctx_sweep(values: list[int]) -> None:
    """Charge le modèle avec plusieurs tailles de contexte et regarde s'il tient dans le GPU."""
    print("\nMesure du contexte (le modèle doit être à 100 % en VRAM) :")
    for n in values:
        httpx.post(
            f"{CONFIG.ollama_url}/api/generate",
            json={
                "model": CONFIG.model,
                "prompt": "ok",
                "stream": False,
                "think": False,
                "options": {"num_ctx": n, "num_predict": 1},
            },
            timeout=CONFIG.llm_timeout_s,
        ).raise_for_status()
        ps = httpx.get(f"{CONFIG.ollama_url}/api/ps", timeout=10).json()["models"]
        m = next((m for m in ps if m["name"].startswith(CONFIG.model)), None)
        if not m:
            print(f"   num_ctx={n}: modèle introuvable dans /api/ps")
            continue
        total, vram = m["size"], m.get("size_vram", 0)
        pct = 100 * vram / total if total else 0
        mark = OK if pct >= 99.5 else KO
        print(f"   {mark} num_ctx={n:>6} : {total / 2**30:.2f} Go dont {pct:.0f} % en VRAM")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctx-sweep", action="store_true")
    args = ap.parse_args()

    failures = 0
    urls: list[str] = []
    for name, fn in [
        ("search", check_search),
        ("fetch", None),
        ("llm", check_llm_json),
        ("embed", check_embed),
    ]:
        try:
            if name == "search":
                urls = fn()
            elif name == "fetch":
                check_fetch(urls)
            else:
                fn()
        except Exception as exc:  # on veut voir toutes les vérifications, même si l'une échoue
            failures += 1
            print(f"{KO} {name} : {type(exc).__name__}: {exc}")

    if args.ctx_sweep:
        ctx_sweep([4096, 8192, 12288, 16384, 24576, 32768])

    print("\nRésultat :", "tout fonctionne" if not failures else f"{failures} vérification(s) en échec")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
