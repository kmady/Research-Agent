"""Ligne de commande.

    python -m research_agent "Quels sont les effets de la caféine sur le sommeil ?"
    python -m research_agent "..." --out reports/ --no-cache -v
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import warnings
from dataclasses import replace

# Avant les imports : trafilatura charge urllib3, qui avertit de l'absence de PySocks (sans conséquence ici).
warnings.filterwarnings("ignore", message=".*SOCKS support.*")

from .agent import build_agent  # noqa: E402
from .config import CONFIG  # noqa: E402
from .llm import LLMError  # noqa: E402
from .report import save_outputs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research-agent", description="Agent autonome de recherche documentaire")
    ap.add_argument("question", help="la question de recherche")
    ap.add_argument("--out", default="reports", help="dossier de sortie (défaut : reports/)")
    ap.add_argument("--model", default=None, help=f"modèle Ollama (défaut : {CONFIG.model})")
    ap.add_argument("--max-rounds", type=int, default=None,
                    help=f"nombre maximal de tours de recherche (défaut : {CONFIG.max_rounds})")
    ap.add_argument("--no-synthesis", action="store_true", help="sans synthèse rédigée (rapport brut seul)")
    ap.add_argument("--no-verify", action="store_true", help="sauter la vérification croisée (plus rapide)")
    ap.add_argument("--no-cache", action="store_true", help="ignorer le cache de recherche et de pages")
    ap.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # avertissements de structure PDF sans conséquence

    overrides = {}
    if args.model:
        overrides["model"] = args.model
    if args.max_rounds is not None:
        overrides["max_rounds"] = max(1, args.max_rounds)
    if args.no_verify:
        overrides["verify"] = False
    if args.no_synthesis:
        overrides["synthesize"] = False
    cfg = replace(CONFIG, **overrides)
    agent = build_agent(cfg, use_cache=not args.no_cache)

    t0 = time.time()
    try:
        state = agent.run(args.question)
    except LLMError as exc:
        logging.error("Échec : %s", exc)
        return 1
    meta = {"model": cfg.model, "seconds": time.time() - t0, "llm": agent.llm.stats.as_dict()}
    paths = save_outputs(state, args.out, meta)
    for label, path in paths.items():
        print(f"{label:<8}: {path}")
    if "moteur de recherche indisponible" in state.stop_reason:
        logging.error("Recherche impossible : les moteurs de SearXNG sont bloqués ou injoignables. Réessayez plus tard.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
