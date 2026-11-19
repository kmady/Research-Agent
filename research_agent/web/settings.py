"""Réglages du service web (variables d'environnement RA_WEB_* / RA_DATA_DIR)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("RA_DATA_DIR", "data")))
    # Authentification HTTP Basic : active dès qu'un mot de passe est défini.
    user: str = field(default_factory=lambda: os.environ.get("RA_WEB_USER", "admin"))
    password: str = field(default_factory=lambda: os.environ.get("RA_WEB_PASSWORD", ""))
    # Un seul GPU : les recherches passent une par une ; au-delà de cette file, on refuse.
    max_queue: int = field(default_factory=lambda: int(os.environ.get("RA_MAX_QUEUE", "10")))
    min_question_chars: int = 8
    max_question_chars: int = 500

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"
