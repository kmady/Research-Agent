"""Traduit les événements de la trace de l'agent en messages de progression lisibles."""

from __future__ import annotations

from ..state import ResearchState

# Avancement approximatif (%) à chaque étape : la durée réelle dépend du nombre de tours.
_PHASE_PROGRESS = {"plan": 3, "verification": 72, "synthesis": 86}


def _n(x) -> int:
    return len(x) if isinstance(x, (list, tuple, dict, set)) else int(x or 0)


def _domain(state: ResearchState, source_id: str) -> str:
    src = state.registry.get(source_id)
    return src.domain if src else source_id


def describe(state: ResearchState, event: dict) -> tuple[str, str, int | None] | None:
    """(type, message, avancement %) pour un événement, ou None s'il n'intéresse pas l'utilisateur.

    Types : phase (nouvelle étape), info, decision (choix de l'agent), warn (problème non bloquant).
    """
    step = event.get("step")
    if step == "phase":
        name = event.get("name")
        label = {"plan": "Planification de la recherche", "verification": "Vérification croisée des affirmations",
                 "synthesis": "Rédaction de la synthèse"}.get(name, name)
        return "phase", label, _PHASE_PROGRESS.get(name)
    if step == "plan":
        sqs = event.get("sub_questions") or []
        lines = "; ".join(f"{sq.get('id')} {sq.get('question')}" for sq in sqs)
        return "info", f"Plan : {len(sqs)} sous-questions — {lines}", 6
    if step == "round_start":
        r = int(event.get("round", 1))
        queries = event.get("queries") or []
        return "phase", f"Tour de recherche {r} : {len(queries)} requêtes ({' | '.join(queries)})", 8 + (r - 1) * 22
    if step == "search_error":
        return "warn", f"Recherche « {event.get('query')} » en échec", None
    if step == "selection":
        chosen = event.get("chosen") or []
        domains = ", ".join(dict.fromkeys(_domain(state, s) for s in chosen))
        return "info", f"{len(chosen)} sources retenues sur {_n(event.get('candidates'))} : {domains}", None
    if step == "extraction":
        src = state.registry.get(event.get("source", ""))
        where = src.domain if src else event.get("source")
        return "info", f"Lu : {where} — {_n(event.get('claims_added'))} affirmations extraites", None
    if step == "extraction_error":
        return "warn", f"Extraction impossible pour {_domain(state, event.get('source', ''))}", None
    if step == "round":
        r = int(event.get("round", 1))
        return ("info", f"Tour {r} terminé : {_n(event.get('read'))} sources lues, "
                f"{_n(event.get('claims_added'))} affirmations", 8 + r * 22 - 4)
    if step == "decision":
        verb = "Arrêt" if event.get("stop") else "Recherche complémentaire"
        msg = f"{verb} — {event.get('reason', '')}"
        if not event.get("stop") and event.get("next_queries"):
            msg += f" → {' | '.join(event['next_queries'])}"
        return "decision", msg, None
    if step == "outage":
        return "warn", event.get("reason", "moteur de recherche indisponible"), None
    if step == "verification":
        return ("info", f"Vérification : {_n(event.get('corroborated'))} groupes corroborés, "
                f"{_n(event.get('verified'))} affirmations contrôlées, {_n(event.get('divergences'))} divergences", 84)
    if step == "synthesis":
        return "info", f"Synthèse rédigée : {_n(event.get('cited'))} sources citées", 97
    if step == "done":
        return "phase", f"Recherche terminée en {float(event.get('seconds', 0)) / 60:.1f} min", 99
    return None
