"""Étape 8 — Évaluation sur le jeu de questions de test.

    python scripts/evaluate.py --smoke              # une question par catégorie (~1 h)
    python scripts/evaluate.py                      # les 30 questions (~3 h 30)
    python scripts/evaluate.py --category controverse
    python scripts/evaluate.py --only glyphosate cafeine
    python scripts/evaluate.py --model llama3.1:8b --smoke   # comparer un autre modèle
    python scripts/evaluate.py --resume eval/runs/<dossier>  # reprendre après une panne des moteurs

Chaque question est lancée avec l'agent complet ; les rapports vont dans eval/runs/<horodatage>/,
et un tableau récapitulatif est écrit dans eval/runs/<horodatage>/RESULTATS.md (+ .json).

Une question interrompue par une panne des moteurs de recherche n'est pas comptée dans les moyennes
(ce serait mesurer les moteurs, pas l'agent) ; après 3 pannes consécutives, l'évaluation s'arrête
et --resume relance ensuite les seules questions manquantes ou en panne.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import warnings
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", message=".*SOCKS support.*")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from research_agent.agent import build_agent  # noqa: E402
from research_agent.config import CONFIG  # noqa: E402
from research_agent.llm import LLMError  # noqa: E402
from research_agent.report import save_outputs  # noqa: E402

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CITE = re.compile(r"\[\d+\]")


def citation_coverage(text: str) -> tuple[int, int]:
    """(phrases citées, phrases) — une phrase factuelle sans référence est un défaut de la synthèse."""
    sentences = [s for s in _SENTENCE.split(text.strip()) if len(s) > 25]
    return sum(bool(_CITE.search(s)) for s in sentences), len(sentences)


def metrics(state, agent, seconds: float) -> dict:
    reg = state.registry
    statuses = Counter(c.status for c in state.claims)
    syn = state.synthesis
    cited = total = 0
    if syn:
        for text in [syn.short_answer, *syn.sections.values()]:
            c, t = citation_coverage(text or "")
            cited, total = cited + c, total + t
    n_claims = len(state.claims) or 1
    return {
        "seconds": round(seconds),
        "rounds": len(state.decisions),
        "sufficient": state.sufficient,
        "search_outage": "moteur de recherche indisponible" in (state.stop_reason or ""),
        "sources_found": len(reg),
        "sources_read": sum(s.fetched for s in reg.all()),
        "reliable_read": sum(s.fetched and s.reliability in ("medium", "high") for s in reg.all()),
        "fetch_failures": sum(bool(s.fetch_error) for s in reg.all()),
        "claims": len(state.claims),
        "quotes_verified_pct": round(100 * sum(c.quote_verified for c in state.claims) / n_claims),
        "corroborated": statuses["corroborated"],
        "confirmed": statuses["confirmed"],
        "contested": statuses["contested"],
        "unverifiable": statuses["unverifiable"],
        "unchecked": statuses["unchecked"],
        "divergences": len(state.divergences),
        "cited_sources": len(syn.cited_sources) if syn else 0,
        "dropped_refs": len(syn.dropped_refs) if syn else 0,
        "failed_sections": len(syn.failed_sections) if syn else 0,
        "cited_sentences_pct": round(100 * cited / total) if total else 0,
        "llm_calls": agent.llm.stats.calls,
        "llm_retries": agent.llm.stats.retries,
        "llm_failures": agent.llm.stats.failures,
    }


COLUMNS = [
    ("id", "Question"),
    ("category", "Catégorie"),
    ("seconds", "Durée (s)"),
    ("rounds", "Tours"),
    ("sufficient", "Suffisant"),
    ("reliable_read", "Sources fiables lues"),
    ("claims", "Affirm."),
    ("quotes_verified_pct", "Citations vérif. %"),
    ("corroborated", "Corrob."),
    ("confirmed", "Confirm."),
    ("contested", "Contest."),
    ("divergences", "Diverg."),
    ("cited_sources", "Réf."),
    ("cited_sentences_pct", "Phrases citées %"),
    ("dropped_refs", "Réf. retirées"),
    ("llm_failures", "Échecs LLM"),
]
CATEGORIES = ["facile", "multi", "controverse", "pointue", "anglais"]


def is_valid(r: dict) -> bool:
    """Résultat exploitable : la question a tourné jusqu'au bout sans panne des moteurs."""
    return "metrics" in r and not r["metrics"].get("search_outage")


def aggregate(rs: list[dict]) -> dict:
    n = len(rs)

    def avg(k):
        return sum(r["metrics"][k] for r in rs) / n

    return {
        "n": n,
        "minutes": avg("seconds") / 60,
        "rounds": avg("rounds"),
        "sufficient": sum(r["metrics"]["sufficient"] for r in rs),
        "quotes_verified_pct": avg("quotes_verified_pct"),
        "cited_sentences_pct": avg("cited_sentences_pct"),
        "dropped_refs": avg("dropped_refs"),
        "with_divergence": sum(r["metrics"]["divergences"] > 0 for r in rs),
        "llm_failures": sum(r["metrics"]["llm_failures"] for r in rs),
    }


def write_summary(results: list[dict], out_dir: Path, model: str) -> Path:
    lines = [
        f"# Évaluation — {datetime.now():%Y-%m-%d %H:%M} — modèle `{model}`",
        "",
        "| " + " | ".join(h for _, h in COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    for r in results:
        if "error" in r or r["metrics"].get("search_outage"):
            why = f"échec : {r['error']}" if "error" in r else "panne des moteurs de recherche (non comptée)"
            lines.append(f"| {r['id']} | {r['category']} | {why} |" + " |" * (len(COLUMNS) - 3))
            continue
        row = []
        for key, _ in COLUMNS:
            v = r.get(key, r["metrics"].get(key))
            row.append("oui" if v is True else "non" if v is False else str(v))
        lines.append("| " + " | ".join(row) + " |")

    ok = [r for r in results if is_valid(r)]
    excluded = len(results) - len(ok)
    if ok:
        g = aggregate(ok)
        lines += [
            "",
            f"## Moyennes ({g['n']} questions exploitables, {excluded} exclues)",
            "",
            f"- Durée : {g['minutes']:.1f} min par question ; tours : {g['rounds']:.1f}",
            f"- Couverture jugée suffisante : {g['sufficient']}/{g['n']}",
            f"- Citations vérifiées : {g['quotes_verified_pct']:.0f} % ; "
            f"phrases de la synthèse avec référence : {g['cited_sentences_pct']:.0f} %",
            f"- Références retirées (inventées par le modèle) : {g['dropped_refs']:.1f} par rapport",
            f"- Échecs d'appel au modèle : {g['llm_failures']}",
            "",
            "## Par catégorie",
            "",
            "| Catégorie | Questions | Suffisant | Tours | Phrases citées % | Citations vérif. % "
            "| Avec divergence | Durée (min) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for cat in CATEGORIES + sorted({r["category"] for r in ok} - set(CATEGORIES)):
            rs = [r for r in ok if r["category"] == cat]
            if not rs:
                continue
            c = aggregate(rs)
            lines.append(
                f"| {cat} | {c['n']} | {c['sufficient']}/{c['n']} | {c['rounds']:.1f} "
                f"| {c['cited_sentences_pct']:.0f} | {c['quotes_verified_pct']:.0f} "
                f"| {c['with_divergence']}/{c['n']} | {c['minutes']:.1f} |"
            )
        lines += [
            "",
            "Attendu : « controverse » avec divergences ; « pointue » plus souvent jugée insuffisante "
            "(l'agent doit le dire plutôt que combler le vide).",
        ]
    lines.append("")
    path = out_dir / "RESULTATS.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "RESULTATS.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


MAX_CONSECUTIVE_OUTAGES = 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="une question par catégorie")
    ap.add_argument("--only", nargs="*", help="identifiants de questions")
    ap.add_argument("--category", nargs="*", choices=CATEGORIES, help="catégories à évaluer")
    ap.add_argument("--resume", type=Path, help="dossier d'une évaluation à compléter")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    questions = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
    elif args.smoke:
        questions = [q for q in questions if q.get("smoke")]
    if args.category:
        questions = [q for q in questions if q["category"] in args.category]
    cfg = replace(CONFIG, model=args.model) if args.model else CONFIG

    results: list[dict] = []
    if args.resume:
        out_dir = args.resume if args.resume.is_absolute() else ROOT / args.resume
        previous = json.loads((out_dir / "RESULTATS.json").read_text(encoding="utf-8"))
        results = [r for r in previous if is_valid(r)]  # on garde ce qui est exploitable
        done = {r["id"] for r in results}
        questions = [q for q in questions if q["id"] not in done]
        logging.info(
            "reprise de %s : %d questions déjà faites, %d à lancer", out_dir.name, len(done), len(questions)
        )
    else:
        out_dir = ROOT / "eval" / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{cfg.model.replace(':', '_')}"
        out_dir.mkdir(parents=True, exist_ok=True)

    outages = 0
    for i, q in enumerate(questions, 1):
        logging.info("===== [%d/%d] %s — %s", i, len(questions), q["id"], q["question"])
        agent = build_agent(cfg, use_cache=not args.no_cache)
        t0 = time.time()
        try:
            state = agent.run(q["question"])
        except (LLMError, Exception) as exc:  # une question en échec ne doit pas arrêter l'évaluation
            logging.exception("échec de %s", q["id"])
            results.append({**q, "error": f"{type(exc).__name__}: {exc}"[:200]})
            write_summary(results, out_dir, cfg.model)
            continue
        seconds = time.time() - t0
        meta = {"model": cfg.model, "seconds": seconds, "llm": agent.llm.stats.as_dict()}
        paths = save_outputs(state, out_dir, meta)
        m = metrics(state, agent, seconds)
        results.append({**q, "metrics": m, "report": str(paths["Rapport"].relative_to(ROOT))})
        logging.info("      → %s", json.dumps(m, ensure_ascii=False))
        write_summary(results, out_dir, cfg.model)  # écrit après chaque question : rien n'est perdu
        outages = outages + 1 if m["search_outage"] else 0
        if outages >= MAX_CONSECUTIVE_OUTAGES:
            rel = out_dir.relative_to(ROOT)
            logging.error(
                "%d pannes consécutives des moteurs de recherche : arrêt. "
                "Relancer plus tard avec : python scripts/evaluate.py --resume %s",
                outages,
                rel,
            )
            print(f"Résultats partiels : {out_dir / 'RESULTATS.md'}")
            return 2

    print(f"Résultats : {out_dir / 'RESULTATS.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
