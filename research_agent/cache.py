"""Petit cache disque clé → JSON, pour ne pas relancer les mêmes recherches et lectures."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class DiskCache:
    def __init__(self, root: str | Path, namespace: str, enabled: bool = True) -> None:
        self.dir = Path(root) / namespace
        self.enabled = enabled

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.dir / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # écriture atomique
