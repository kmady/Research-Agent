"""File d'attente des recherches, persistée dans SQLite.

Un seul GPU : un unique fil d'exécution traite les recherches une par une, dans l'ordre d'arrivée.
L'état (recherches, progression, chemins des rapports) survit à un redémarrage du service ;
une recherche interrompue par un arrêt est marquée en échec, celles en attente sont reprises.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
TERMINAL = {DONE, FAILED, CANCELLED}


class QueueFull(RuntimeError):
    pass


class Cancelled(BaseException):
    """Levée depuis l'observateur pour interrompre l'agent.

    Hérite de BaseException : les `except Exception` de l'agent (une page illisible ne doit pas
    arrêter la recherche) ne doivent pas l'intercepter.
    """


@dataclass
class Outcome:
    """Ce que renvoie l'exécution d'une recherche terminée."""

    files: dict[str, str]  # report / details / trace → chemin
    sufficient: bool
    stop_reason: str
    search_outage: bool = False
    stats: dict = field(default_factory=dict)


# runner(job_id, question, options, emit, should_cancel) -> Outcome
#   emit(kind, message, progress) publie un message de progression ;
#   should_cancel() indique si l'utilisateur a demandé l'annulation.
Runner = Callable[[str, str, dict, Callable[[str, str, int | None], None], Callable[[], bool]], Outcome]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    sufficient INTEGER,
    search_outage INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    files TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
"""


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in ("options", "files", "stats"):
            d[k] = json.loads(d[k] or "{}")
        d["sufficient"] = None if d["sufficient"] is None else bool(d["sufficient"])
        d["search_outage"] = bool(d["search_outage"])
        return d

    def create(self, question: str, options: dict) -> dict:
        job_id = uuid.uuid4().hex
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, question, options, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, question, json.dumps(options), QUEUED, time.time()),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def by_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at", statuses
            ).fetchall()
        return [self._row(r) for r in rows]

    def update(self, job_id: str, **fields) -> None:
        for k in ("options", "files", "stats"):
            if k in fields:
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def add_event(self, job_id: str, kind: str, message: str) -> int:
        with self._lock, self._conn() as c:
            seq = c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO events (job_id, seq, ts, kind, message) VALUES (?, ?, ?, ?, ?)",
                (job_id, seq, time.time(), kind, message),
            )
        return seq

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT seq, ts, kind, message FROM events WHERE job_id = ? AND seq > ? ORDER BY seq",
                (job_id, after),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, job_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


class JobManager:
    """Reçoit les demandes et les exécute une par une dans un fil dédié."""

    def __init__(
        self, store: JobStore, runner: Runner, *, max_queue: int = 10, reports_dir: Path | None = None
    ):
        self.store = store
        self.runner = runner
        self.max_queue = max_queue
        self.reports_dir = reports_dir
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel: set[str] = set()
        self._cancel_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # --- Cycle de vie --------------------------------------------------------

    def start(self) -> None:
        for job in self.store.by_status(RUNNING):  # arrêt brutal pendant une recherche
            self.store.update(
                job["id"],
                status=FAILED,
                finished_at=time.time(),
                error="interrompue par un redémarrage du service",
            )
            self.store.add_event(job["id"], "warn", "Recherche interrompue par un redémarrage du service")
        self._thread = threading.Thread(target=self._loop, name="research-worker", daemon=True)
        self._thread.start()
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._cancel_lock:  # interrompt proprement la recherche en cours
            self._cancel.update(j["id"] for j in self.store.by_status(RUNNING))
        if self._thread:
            self._thread.join(timeout)

    # --- Opérations ----------------------------------------------------------

    def submit(self, question: str, options: dict) -> dict:
        if len(self.store.by_status(QUEUED)) >= self.max_queue:
            raise QueueFull(f"file d'attente pleine ({self.max_queue} recherches en attente)")
        job = self.store.create(question, options)
        self.store.add_event(job["id"], "info", "Recherche mise en file d'attente")
        self._wake.set()
        return job

    def queue_position(self, job_id: str) -> int | None:
        """1 = prochaine à démarrer ; None si la recherche n'est pas en attente."""
        ids = [j["id"] for j in self.store.by_status(QUEUED)]
        return ids.index(job_id) + 1 if job_id in ids else None

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job is None or job["status"] in TERMINAL:
            return False
        if job["status"] == QUEUED:
            self.store.update(job_id, status=CANCELLED, finished_at=time.time())
            self.store.add_event(job_id, "warn", "Recherche annulée avant son démarrage")
            return True
        with self._cancel_lock:
            self._cancel.add(job_id)
        self.store.add_event(job_id, "warn", "Annulation demandée : arrêt à la prochaine étape")
        return True

    def delete(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job is None or job["status"] not in TERMINAL:
            return False
        if self.reports_dir is not None:
            folder = (self.reports_dir / job_id).resolve()
            if folder.parent == self.reports_dir.resolve() and folder.is_dir():
                shutil.rmtree(folder)
        self.store.delete(job_id)
        return True

    # --- Exécution -----------------------------------------------------------

    def _should_cancel(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancel

    def _loop(self) -> None:
        while not self._stop.is_set():
            queued = self.store.by_status(QUEUED)
            if not queued:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            self._run(queued[0])

    def _run(self, job: dict) -> None:
        job_id = job["id"]
        self.store.update(job_id, status=RUNNING, started_at=time.time(), progress=1)
        self.store.add_event(job_id, "phase", "Démarrage de la recherche")

        def emit(kind: str, message: str, progress: int | None = None) -> None:
            self.store.add_event(job_id, kind, message)
            if progress is not None:
                self.store.update(job_id, progress=progress)

        try:
            outcome = self.runner(
                job_id, job["question"], job["options"], emit, lambda: self._should_cancel(job_id)
            )
        except Cancelled:
            self.store.update(job_id, status=CANCELLED, finished_at=time.time())
            self.store.add_event(job_id, "warn", "Recherche annulée")
        except Exception as exc:  # noqa: BLE001 — l'erreur est montrée à l'utilisateur, le service continue
            log.exception("échec de la recherche %s", job_id)
            self.store.update(
                job_id, status=FAILED, finished_at=time.time(), error=f"{type(exc).__name__}: {exc}"[:500]
            )
            self.store.add_event(job_id, "warn", f"Échec : {type(exc).__name__}: {exc}"[:500])
        else:
            self.store.update(
                job_id,
                status=DONE,
                finished_at=time.time(),
                progress=100,
                files=outcome.files,
                sufficient=int(outcome.sufficient),
                search_outage=int(outcome.search_outage),
                stop_reason=outcome.stop_reason,
                stats=outcome.stats,
            )
        finally:
            with self._cancel_lock:
                self._cancel.discard(job_id)
