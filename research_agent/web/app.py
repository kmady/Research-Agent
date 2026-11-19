"""Application FastAPI : API REST + progression en direct (SSE) + interface web.

    python -m research_agent.web                 # http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from ..config import CONFIG, Config
from .jobs import TERMINAL, JobManager, JobStore, QueueFull
from .render import markdown_to_html
from .settings import WebSettings

STATIC = Path(__file__).parent / "static"
_SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class JobRequest(BaseModel):
    question: str
    max_rounds: int = Field(default=CONFIG.max_rounds, ge=1, le=CONFIG.max_rounds)
    verify: bool = True

    @field_validator("question")
    @classmethod
    def _clean(cls, v: str) -> str:
        return " ".join(v.split())


def _public(job: dict, manager: JobManager) -> dict:
    """Vue d'une recherche pour l'API : jamais les chemins de fichiers du serveur."""
    out = {k: job[k] for k in ("id", "question", "options", "status", "progress", "created_at", "started_at",
                               "finished_at", "error", "sufficient", "search_outage", "stop_reason", "stats")}
    out["available"] = sorted(job["files"])
    out["queue_position"] = manager.queue_position(job["id"]) if job["status"] == "queued" else None
    return out


def create_app(settings: WebSettings | None = None, *, cfg: Config = CONFIG,
               manager: JobManager | None = None) -> FastAPI:
    settings = settings or WebSettings()
    if manager is None:
        from .runner import agent_runner   # import tardif : les tests injectent leur propre exécutant
        manager = JobManager(JobStore(settings.db_path), agent_runner(cfg, settings.reports_dir),
                             max_queue=settings.max_queue, reports_dir=settings.reports_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start()
        yield
        manager.stop()

    app = FastAPI(title="Research-Agent", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.manager = manager
    store = manager.store

    # --- Authentification et en-têtes de sécurité ----------------------------

    def _authorized(request: Request) -> bool:
        if not settings.password:
            return True
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        return (secrets.compare_digest(user.encode(), settings.user.encode())
                and secrets.compare_digest(pwd.encode(), settings.password.encode()))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path != "/api/health/live" and not _authorized(request):
            response: Response = JSONResponse({"detail": "authentification requise"}, status_code=401,
                                              headers={"WWW-Authenticate": 'Basic realm="Research-Agent"'})
        else:
            response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def _job_or_404(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "recherche introuvable")
        return job

    # --- Santé ---------------------------------------------------------------

    health_cache: dict = {"at": 0.0, "value": None}

    @app.get("/api/health/live", include_in_schema=False)
    async def live():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        if health_cache["value"] and time.time() - health_cache["at"] < 10:
            return health_cache["value"]
        async with httpx.AsyncClient(timeout=3) as client:
            async def check(url):
                try:
                    r = await client.get(url)
                    return r if r.status_code == 200 else None
                except httpx.HTTPError:
                    return None
            ollama, searx = await asyncio.gather(check(f"{cfg.ollama_url}/api/tags"),
                                                 check(f"{cfg.searxng_url}/healthz"))
        models = {m.get("name") for m in (ollama.json().get("models", []) if ollama else [])}
        value = {
            "ollama": ollama is not None,
            "model": cfg.model,
            "model_available": cfg.model in models,
            "embed_model_available": any(n.split(":")[0] == cfg.embed_model.split(":")[0] for n in models),
            "searxng": searx is not None,
            "queued": len(store.by_status("queued")),
            "running": len(store.by_status("running")),
        }
        value["ok"] = value["ollama"] and value["model_available"] and value["searxng"]
        health_cache.update(at=time.time(), value=value)
        return value

    # --- Recherches ----------------------------------------------------------

    @app.post("/api/jobs", status_code=201)
    async def create_job(req: JobRequest):
        if not (settings.min_question_chars <= len(req.question) <= settings.max_question_chars):
            raise HTTPException(422, f"la question doit faire entre {settings.min_question_chars} et "
                                     f"{settings.max_question_chars} caractères")
        try:
            job = manager.submit(req.question, {"max_rounds": req.max_rounds, "verify": req.verify})
        except QueueFull as exc:
            raise HTTPException(429, str(exc)) from exc
        return _public(job, manager)

    @app.get("/api/jobs")
    async def list_jobs(limit: int = Query(50, ge=1, le=200)):
        return [_public(j, manager) for j in store.list(limit)]

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        return _public(_job_or_404(job_id), manager)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        _job_or_404(job_id)
        if not manager.cancel(job_id):
            raise HTTPException(409, "la recherche est déjà terminée")
        return _public(store.get(job_id), manager)

    @app.delete("/api/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: str):
        _job_or_404(job_id)
        if not manager.delete(job_id):
            raise HTTPException(409, "annulez la recherche avant de la supprimer")
        return Response(status_code=204)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, after: int = Query(0, ge=0)):
        """Progression en direct (Server-Sent Events). Rejoue l'historique puis suit la recherche."""
        _job_or_404(job_id)
        last = request.headers.get("last-event-id", "")
        cursor = int(last) if last.isdigit() else after

        async def stream():
            nonlocal cursor
            last_status, idle = None, 0.0
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                for ev in store.events(job_id, cursor):
                    cursor = ev["seq"]
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                job = store.get(job_id)
                if job is None:
                    return
                status = (job["status"], job["progress"], manager.queue_position(job_id))
                if status != last_status:
                    last_status = status
                    yield f"event: status\ndata: {json.dumps(_public(job, manager), ensure_ascii=False)}\n\n"
                if job["status"] in TERMINAL and not store.events(job_id, cursor):
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)
                idle += 0.5
                if idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _file(job: dict, kind: str) -> Path:
        path = job["files"].get(kind)
        if not path or not Path(path).is_file():
            raise HTTPException(404, "fichier non disponible")
        return Path(path)

    @app.get("/api/jobs/{job_id}/report")
    async def job_report(job_id: str, view: str = Query("report", pattern="^(report|details)$")):
        text = _file(_job_or_404(job_id), view).read_text(encoding="utf-8")
        return {"view": view, "markdown": text, "html": markdown_to_html(text)}

    @app.get("/api/jobs/{job_id}/download/{kind}")
    async def download(job_id: str, kind: str):
        if kind not in ("report", "details", "trace"):
            raise HTTPException(404, "fichier inconnu")
        path = _file(_job_or_404(job_id), kind)
        media = "application/json" if kind == "trace" else "text/markdown; charset=utf-8"
        return FileResponse(path, media_type=media, filename=path.name)

    # --- Interface -----------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
