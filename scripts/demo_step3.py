"""Étape 3 — Teste chaque schéma une fois sur un exemple réel.

    python scripts/demo_step3.py ["question"]

Enchaîne à la main : plan → sélection → extraction → évaluation d'une sous-question → verdict.
Affiche pour chaque appel : validité, durée, et tokens réels comparés à l'estimation
(pour calibrer `chars_per_token`).
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
from research_agent.llm import OllamaLLM  # noqa: E402
from research_agent.schemas import (  # noqa: E402
    ClaimVerdict,
    Extraction,
    ResearchPlan,
    SubQuestionAssessment,
    UrlSelection,
)
from research_agent.search import SearxngProvider  # noqa: E402
from research_agent.state import SourceRegistry  # noqa: E402

SYSTEM = (
    "Tu es un assistant de recherche documentaire rigoureux. Tu t'appuies uniquement sur les "
    "informations fournies, tu cites les identifiants donnés et tu réponds en français."
)


def run(llm: OllamaLLM, label: str, user: str, schema, think: bool):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    before = (llm.stats.prompt_tokens, llm.stats.calls)
    t0 = time.time()
    out = llm.structured(msgs, schema, think=think)
    real = llm.stats.prompt_tokens - before[0]
    est = llm.estimate_tokens(msgs)
    extra = llm.stats.calls - before[1] - 1
    print(
        f"\n✔ {label} (think={think}) : {time.time() - t0:.1f}s, tokens réels {real} / estimés ~{est}"
        + (f", {extra} nouvel(s) essai(s)" if extra else "")
    )
    print("  " + out.model_dump_json(indent=1)[:900].replace("\n", "\n  "))
    return out


def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "Quels sont les effets de la caféine sur le sommeil ?"
    llm = OllamaLLM(
        CONFIG.ollama_url,
        CONFIG.model,
        num_ctx=CONFIG.num_ctx,
        temperature=CONFIG.temperature,
        timeout_s=CONFIG.llm_timeout_s,
        max_output_tokens=CONFIG.max_output_tokens,
        max_output_tokens_thinking=CONFIG.max_output_tokens_thinking,
        chars_per_token=CONFIG.chars_per_token,
    )
    search = SearxngProvider(CONFIG.searxng_url, cache=DiskCache(CONFIG.cache_dir, "search"))
    fetcher = Fetcher(user_agent=CONFIG.user_agent, cache=DiskCache(CONFIG.cache_dir, "pages"))
    embedder = OllamaEmbedder(CONFIG.ollama_url, CONFIG.embed_model)
    reg = SourceRegistry()
    t_start = time.time()

    # 1. Plan
    plan = run(
        llm,
        "ResearchPlan",
        f"Question : {question}\n\nDécompose la question en 2 à 4 sous-questions "
        "et propose 3 à 5 requêtes de recherche web.",
        ResearchPlan,
        think=False,
    )

    # 2. Sélection
    for h in search.search(plan.initial_queries[0], n=CONFIG.results_per_query):
        reg.add(h.url, h.title, snippet=h.snippet)
    listing = "\n".join(f"[{s.id}] {s.title} — {s.domain}\n    {s.snippet[:200]}" for s in reg.all())
    sel = run(
        llm,
        "UrlSelection",
        f"Question : {question}\n\nRésultats de recherche :\n{listing}\n\n"
        "Choisis les 3 à 5 sources les plus utiles et fiables à lire.",
        UrlSelection,
        think=False,
    )

    # 3. Extraction sur la première source sélectionnée lisible
    q1 = plan.sub_questions[0]
    src, passages = None, []
    for choice in sel.selected:
        cand = reg.get(choice.source_id)
        res = fetcher.fetch(cand.url) if cand else None
        if res and res.ok:
            src = cand
            all_p = reg.set_passages(src.id, chunk_text(res.text, CONFIG.passage_tokens))
            top = rank_passages(question, [p.text for p in all_p], embedder, top_k=CONFIG.passages_per_call)
            passages = [all_p[i] for i, _ in top]
            break
    if not src:
        sys.exit("aucune source sélectionnée n'a pu être lue")
    subqs = "\n".join(f"{s.id} : {s.question}" for s in plan.sub_questions)
    ptext = "\n\n".join(f"[{p.id}] {p.text}" for p in passages)
    ext = run(
        llm,
        f"Extraction ({src.id} {src.domain})",
        f"Question : {question}\nSous-questions :\n{subqs}\n\nSource [{src.id}] {src.title} — {src.url}\n\n"
        f"Passages :\n{ptext}\n\nExtrais les affirmations factuelles utiles, chacune reliée à ses passages. "
        "Évalue aussi le type et la fiabilité de la source.",
        Extraction,
        think=False,
    )

    # 4. Évaluation d'une sous-question
    claims = [c for c in ext.claims if c.sub_question_id == q1.id] or ext.claims
    ctext = "\n".join(f"- {c.statement} {c.passage_ids}" for c in claims) or "(aucune)"
    run(
        llm,
        f"SubQuestionAssessment ({q1.id})",
        f"Sous-question {q1.id} : {q1.question}\n\nAffirmations recueillies :\n{ctext}\n\n"
        f"Faits mesurés par le code : 1 source, 1 domaine ({src.domain}).\n\n"
        "La sous-question est-elle couverte ? Qu'est-ce qui manque ? Propose des requêtes complémentaires.",
        SubQuestionAssessment,
        think=True,
    )

    # 5. Verdict sur la première affirmation, avec les passages d'une autre source
    if ext.claims:
        claim = ext.claims[0]
        other_passages = []
        for s in reg.all():
            if s.id == src.id:
                continue
            res = fetcher.fetch(s.url)
            if res.ok:
                all_p = reg.set_passages(s.id, chunk_text(res.text, CONFIG.passage_tokens))
                top = rank_passages(claim.statement, [p.text for p in all_p], embedder, top_k=3)
                other_passages = [all_p[i] for i, _ in top]
                break
        otext = "\n\n".join(f"[{p.id}] {p.text}" for p in other_passages)
        run(
            llm,
            "ClaimVerdict",
            f"Affirmation : {claim.statement}\n\nPassages d'une source indépendante :\n{otext}"
            "\n\nCes passages confirment-ils, contredisent-ils l'affirmation, ou ne permettent-ils pas de conclure ?",
            ClaimVerdict,
            think=True,
        )

    s = llm.stats
    print(
        f"\nTotal : {time.time() - t_start:.0f}s, {s.calls} appels, {s.retries} nouveaux essais, "
        f"{s.failures} échecs, {s.prompt_tokens} tokens en entrée, {s.output_tokens} en sortie."
    )


if __name__ == "__main__":
    main()
