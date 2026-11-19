"""Exécution d'une recherche pour le service web : l'agent réel, branché sur la progression."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path

from ..agent import build_agent
from ..config import Config
from ..report import save_outputs
from .jobs import Cancelled, Outcome, Runner
from .progress import describe

log = logging.getLogger(__name__)


def agent_runner(cfg: Config, reports_dir: Path, *, use_cache: bool = True) -> Runner:
    def run(job_id, question, options, emit, should_cancel) -> Outcome:
        overrides: dict = {}
        if options.get("max_rounds"):
            overrides["max_rounds"] = int(options["max_rounds"])
        if options.get("verify") is False:
            overrides["verify"] = False
        job_cfg = replace(cfg, **overrides)
        agent = build_agent(job_cfg, use_cache=use_cache)

        def listener(state, event):
            if should_cancel():
                raise Cancelled()
            try:
                described = describe(state, event)
            except Exception:  # noqa: BLE001 — un message raté ne doit pas interrompre la recherche
                log.exception("message de progression impossible pour %s", event.get("step"))
                return
            if described:
                emit(*described)

        t0 = time.time()
        state = agent.run(question, listener=listener)
        seconds = time.time() - t0
        meta = {"model": job_cfg.model, "seconds": seconds, "llm": agent.llm.stats.as_dict()}
        paths = save_outputs(state, Path(reports_dir) / job_id, meta)
        reg = state.registry
        return Outcome(
            files={"report": str(paths["Rapport"]), "details": str(paths["Détail"]), "trace": str(paths["Trace"])},
            sufficient=state.sufficient,
            stop_reason=state.stop_reason,
            search_outage="moteur de recherche indisponible" in (state.stop_reason or ""),
            stats={"seconds": round(seconds), "rounds": len(state.decisions),
                   "sources_read": sum(s.fetched for s in reg.all()), "claims": len(state.claims),
                   "divergences": len(state.divergences), "model": job_cfg.model},
        )

    return run
