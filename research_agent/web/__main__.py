"""Lancement du service web.

    python -m research_agent.web                          # http://127.0.0.1:8000, sans mot de passe
    RA_WEB_PASSWORD=... python -m research_agent.web --host 0.0.0.0   # accessible sur le réseau
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
import warnings

warnings.filterwarnings("ignore", message=".*SOCKS support.*")

import uvicorn  # noqa: E402

from .app import create_app  # noqa: E402
from .settings import WebSettings  # noqa: E402


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research-agent-web", description="Interface web de Research-Agent")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--root-path", default="", help="préfixe d'URL derrière un proxy inverse (ex. /agent)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    settings = WebSettings()
    if not _is_loopback(args.host) and not settings.password:
        logging.error("Refus de démarrer sur %s sans mot de passe : définissez RA_WEB_PASSWORD "
                      "(ou restez sur 127.0.0.1).", args.host)
        return 2
    # Un seul processus : la file d'attente et le GPU sont partagés par toutes les requêtes.
    uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1,
                root_path=args.root_path, proxy_headers=True, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
