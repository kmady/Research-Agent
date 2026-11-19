"""Tests du service web, avec un exécutant factice (ni modèle, ni réseau)."""

import base64
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from research_agent.state import ResearchState
from research_agent.web.app import create_app
from research_agent.web.jobs import FAILED, QUEUED, RUNNING, Cancelled, JobManager, JobStore, Outcome
from research_agent.web.progress import describe
from research_agent.web.render import markdown_to_html
from research_agent.web.settings import WebSettings

REPORT = "# Question\n\n## Réponse courte\n\nRéponse [1].\n\n## Sources\n\n1. [Source](https://a.org/x) — a.org\n"


def fake_runner(reports_dir: Path, *, gate: threading.Event | None = None, fail: bool = False):
    def run(job_id, question, options, emit, should_cancel):
        emit("phase", "Planification de la recherche", 3)
        if gate is not None:                       # attend un signal, en surveillant l'annulation
            while not gate.wait(0.02):
                if should_cancel():
                    raise Cancelled()
        if fail:
            raise RuntimeError("Ollama injoignable")
        out = reports_dir / job_id
        out.mkdir(parents=True)
        files = {}
        for kind, text in (("report", REPORT), ("details", "# Détail\n"), ("trace", "{}")):
            p = out / f"r.{kind}"
            p.write_text(text, encoding="utf-8")
            files[kind] = str(p)
        emit("decision", "Arrêt — suffisant", None)
        return Outcome(files=files, sufficient=True, stop_reason="suffisant", stats={"seconds": 1, "rounds": 1})
    return run


def make_client(tmp_path, *, password="", max_queue=10, **runner_kw):
    settings = WebSettings(data_dir=tmp_path, password=password, user="alice", max_queue=max_queue)
    manager = JobManager(JobStore(settings.db_path), fake_runner(settings.reports_dir, **runner_kw),
                         max_queue=max_queue, reports_dir=settings.reports_dir)
    return TestClient(create_app(settings, manager=manager)), manager


def wait_for(client, job_id, statuses, timeout=5.0, headers=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"statut {statuses} non atteint : {job['status']}")


def test_job_runs_and_report_is_served(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        r = client.post("/api/jobs", json={"question": "  Quels effets   de la caféine ?  ", "max_rounds": 2})
        assert r.status_code == 201
        job = r.json()
        assert job["question"] == "Quels effets de la caféine ?" and job["options"] == {"max_rounds": 2, "verify": True}
        done = wait_for(client, job["id"], {"done"})
        assert done["sufficient"] is True and done["progress"] == 100
        assert done["available"] == ["details", "report", "trace"]
        assert "files" not in done and str(tmp_path) not in str(done)   # aucun chemin serveur exposé

        rep = client.get(f"/api/jobs/{job['id']}/report").json()
        assert "<h2>Sources</h2>" in rep["html"] and 'target="_blank"' in rep["html"]
        assert client.get(f"/api/jobs/{job['id']}/report?view=details").json()["markdown"] == "# Détail\n"
        assert client.get(f"/api/jobs/{job['id']}/report?view=../../etc").status_code == 422
        dl = client.get(f"/api/jobs/{job['id']}/download/trace")
        assert dl.status_code == 200 and dl.text == "{}"
        assert client.get(f"/api/jobs/{job['id']}/download/autre").status_code == 404
        assert [j["id"] for j in client.get("/api/jobs").json()] == [job["id"]]


def test_events_stream_replays_progress_and_ends(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        job = client.post("/api/jobs", json={"question": "Une question assez longue"}).json()
        wait_for(client, job["id"], {"done"})
        with client.stream("GET", f"/api/jobs/{job['id']}/events") as r:
            body = "".join(r.iter_text())
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "Planification de la recherche" in body and "Arrêt — suffisant" in body
        assert body.rstrip().endswith("event: end\ndata: {}")
        with client.stream("GET", f"/api/jobs/{job['id']}/events", headers={"Last-Event-ID": "3"}) as r:
            resumed = "".join(r.iter_text())
        assert "Planification" not in resumed       # reprise après le dernier événement reçu


def test_question_is_validated(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert client.post("/api/jobs", json={"question": "court"}).status_code == 422
        assert client.post("/api/jobs", json={"question": "x" * 501}).status_code == 422
        assert client.post("/api/jobs", json={"question": "Une question valide", "max_rounds": 9}).status_code == 422


def test_cancel_running_job(tmp_path):
    gate = threading.Event()
    client, _ = make_client(tmp_path, gate=gate)
    with client:
        job = client.post("/api/jobs", json={"question": "Une question à annuler"}).json()
        wait_for(client, job["id"], {"running"})
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 409     # pas pendant l'exécution
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        assert wait_for(client, job["id"], {"cancelled"})["status"] == "cancelled"
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_queue_is_bounded_and_ordered(tmp_path):
    gate = threading.Event()
    client, manager = make_client(tmp_path, gate=gate, max_queue=2)
    with client:
        first = client.post("/api/jobs", json={"question": "Première question"}).json()
        wait_for(client, first["id"], {"running"})
        a = client.post("/api/jobs", json={"question": "Deuxième question"}).json()
        b = client.post("/api/jobs", json={"question": "Troisième question"}).json()
        assert (a["queue_position"], b["queue_position"]) == (1, 2)
        r = client.post("/api/jobs", json={"question": "Quatrième question"})
        assert r.status_code == 429 and "file d'attente pleine" in r.json()["detail"]
        assert client.post(f"/api/jobs/{a['id']}/cancel").json()["status"] == "cancelled"
        gate.set()
        wait_for(client, b["id"], {"done"})


def test_runner_failure_is_reported(tmp_path):
    client, _ = make_client(tmp_path, fail=True)
    with client:
        job = client.post("/api/jobs", json={"question": "Une question qui échoue"}).json()
        failed = wait_for(client, job["id"], {"failed"})
        assert "Ollama injoignable" in failed["error"]


def test_restart_marks_running_job_failed_and_resumes_queue(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    interrupted = store.create("Question interrompue", {})
    store.update(interrupted["id"], status=RUNNING)
    waiting = store.create("Question en attente", {})
    assert store.get(waiting["id"])["status"] == QUEUED
    manager = JobManager(store, fake_runner(tmp_path / "reports"), reports_dir=tmp_path / "reports")
    manager.start()
    try:
        deadline = time.time() + 5
        while store.get(waiting["id"])["status"] != "done" and time.time() < deadline:
            time.sleep(0.02)
        assert store.get(waiting["id"])["status"] == "done"
        assert store.get(interrupted["id"])["status"] == FAILED
        assert "redémarrage" in store.get(interrupted["id"])["error"]
    finally:
        manager.stop()


def test_basic_auth_when_password_is_set(tmp_path):
    client, _ = make_client(tmp_path, password="s3cret")
    with client:
        r = client.get("/api/jobs")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert client.get("/").status_code == 401
        bad = base64.b64encode(b"alice:faux").decode()
        assert client.get("/api/jobs", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        assert client.get("/api/jobs", headers={"Authorization": "Basic !!!"}).status_code == 401
        good = base64.b64encode(b"alice:s3cret").decode()
        assert client.get("/api/jobs", headers={"Authorization": f"Basic {good}"}).status_code == 200
        assert client.get("/api/health/live").status_code == 200    # sonde de vie sans authentification


def test_ui_and_security_headers(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        r = client.get("/")
        assert r.status_code == 200 and "Research-Agent" in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        assert client.get("/static/app.js").status_code == 200


def test_markdown_rendering_escapes_untrusted_html():
    html = markdown_to_html("Titre <script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://a.org)")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert '<a href="https://a.org" target="_blank" rel="noopener noreferrer nofollow">ok</a>' in html
    assert "<table>" in markdown_to_html("| a | b |\n|---|---|\n| 1 | 2 |")


def test_progress_messages():
    st = ResearchState(question="q")
    src, _ = st.registry.add("https://www.inserm.fr/a")
    assert describe(st, {"step": "phase", "name": "plan"})[0] == "phase"
    kind, msg, pct = describe(st, {"step": "round_start", "round": 2, "queries": ["a", "b"]})
    assert kind == "phase" and "Tour de recherche 2 : 2 requêtes" in msg and pct == 30
    assert "inserm.fr" in describe(st, {"step": "selection", "chosen": [src.id], "candidates": [src.id, "S9"]})[1]
    assert "inserm.fr — 3 affirmations" in describe(st, {"step": "extraction", "source": src.id, "claims_added": 3})[1]
    d = describe(st, {"step": "decision", "stop": False, "reason": "Q2 non couverte", "next_queries": ["x y"]})
    assert d[0] == "decision" and d[1] == "Recherche complémentaire — Q2 non couverte → x y"
    assert describe(st, {"step": "assessment"}) is None


def test_state_listener_receives_every_logged_event():
    seen = []
    st = ResearchState(question="q", listener=lambda state, ev: seen.append(ev["step"]))
    st.log("plan", sub_questions=[])
    st.log("done", seconds=1)
    assert seen == ["plan", "done"] and len(st.trace) == 2


def test_listener_exception_stops_the_agent():
    def stop(state, ev):
        raise Cancelled()
    st = ResearchState(question="q", listener=stop)
    with pytest.raises(Cancelled):
        st.log("plan")
