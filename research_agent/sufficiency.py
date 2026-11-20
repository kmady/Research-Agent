"""Décision « suffisant ou recherche complémentaire ? » — le cœur agentique.

La décision combine deux avis :
  - le jugement du modèle, sous-question par sous-question (covered / partial / missing) ;
  - des règles dures calculées par le code (corroboration par des domaines fiables distincts).

Les règles ont le dernier mot : un modèle 8B peut se dire satisfait trop tôt, ou au
contraire inventer des manques (observé à l'étape 3). Tout ici est pur et testé.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .agent_utils import clean_query, keywords_query
from .state import ResearchState


@dataclass(frozen=True)
class Thresholds:
    max_rounds: int = 3
    min_domains_per_subquestion: int = 2  # domaines fiables (fiabilité medium ou high) distincts
    min_total_sources: int = 4  # sources fiables lues au total
    max_queries_per_round: int = 4
    max_queries_per_subquestion: int = 2
    # « Preuve forte » : on accepte un avis « partial » du modèle si la corroboration est nettement au-dessus du seuil.
    strong_evidence_domains: int = 3
    strong_evidence_claims: int = 4


@dataclass
class SubQuestionMetrics:
    sub_question_id: str
    claims: int = 0
    sources: list[str] = field(default_factory=list)
    reliable_domains: list[str] = field(default_factory=list)  # fiabilité medium ou high
    low_domains: list[str] = field(default_factory=list)  # fiabilité low : non comptés
    high_sources: int = 0


@dataclass
class SubQuestionVerdict:
    sub_question_id: str
    covered: bool
    llm_status: str  # covered / partial / missing / « déjà couverte »
    reasons: list[str]
    queries: list[str]


@dataclass
class Decision:
    round_no: int
    stop: bool
    sufficient: bool
    reason: str
    next_queries: list[str]
    focus: list[str]  # sous-questions visées par le tour suivant
    verdicts: list[SubQuestionVerdict]
    reliable_sources_read: int

    def as_dict(self) -> dict:
        return asdict(self)


# --- Mesures objectives ------------------------------------------------------


def compute_metrics(state: ResearchState) -> dict[str, SubQuestionMetrics]:
    reg = state.registry
    out: dict[str, SubQuestionMetrics] = {}
    for sq_id in state.sub_question_ids():
        claims = state.claims_for(sq_id)
        source_ids = list(dict.fromkeys(c.source_id for c in claims))
        sources = [reg.get(s) for s in source_ids if reg.get(s)]
        reliable = sorted({s.domain for s in sources if s.reliability in ("medium", "high")})
        low = sorted({s.domain for s in sources if s.reliability == "low"} - set(reliable))
        out[sq_id] = SubQuestionMetrics(
            sub_question_id=sq_id,
            claims=len(claims),
            sources=source_ids,
            reliable_domains=reliable,
            low_domains=low,
            high_sources=sum(s.reliability == "high" for s in sources),
        )
    return out


def reliable_sources_read(state: ResearchState) -> int:
    return sum(1 for s in state.registry.all() if s.fetched and s.reliability in ("medium", "high"))


def metrics_text(m: SubQuestionMetrics, th: Thresholds) -> str:
    """Faits mesurés, présentés au modèle avant son jugement."""
    doms = ", ".join(m.reliable_domains) or "aucun"
    lines = [
        f"- Affirmations recueillies : {m.claims}",
        f"- Domaines fiables distincts : {len(m.reliable_domains)} ({doms}) — seuil requis : "
        f"{th.min_domains_per_subquestion}",
        f"- Sources de fiabilité haute : {m.high_sources}",
    ]
    if m.low_domains:
        lines.append(f"- Sources peu fiables (non comptées) : {', '.join(m.low_domains)}")
    return "\n".join(lines)


# --- Décision ----------------------------------------------------------------


def _fallback_query(question: str) -> str:
    return keywords_query(question)


def decide(
    *,
    round_no: int,
    sub_questions: list[tuple[str, str]],  # [(id, texte)]
    metrics: dict[str, SubQuestionMetrics],
    assessments: dict[str, object],  # id → SubQuestionAssessment (None si non évaluée)
    already_covered: set[str],
    queries_run: list[str],
    last_round_read: int,
    last_round_claims: int,
    reliable_read: int,
    th: Thresholds,
) -> Decision:
    seen = {q.strip().lower() for q in queries_run}
    verdicts: list[SubQuestionVerdict] = []

    for sq_id, sq_text in sub_questions:
        m = metrics[sq_id]
        if sq_id in already_covered:
            verdicts.append(
                SubQuestionVerdict(sq_id, True, "déjà couverte", ["couverte à un tour précédent"], [])
            )
            continue

        a = assessments.get(sq_id)
        status = getattr(a, "status", "missing") if a is not None else "missing"
        n_dom = len(m.reliable_domains)
        rules_ok = n_dom >= th.min_domains_per_subquestion
        # La « preuve forte » ne vaut qu'après au moins une recherche ciblée (tour ≥ 2) :
        # au tour 1, un avis « partial » signale un aspect manquant qu'il faut d'abord chercher.
        strong = (
            round_no >= 2 and n_dom >= th.strong_evidence_domains and m.claims >= th.strong_evidence_claims
        )

        reasons = []
        if status == "covered" and rules_ok:
            covered = True
            reasons.append(
                f"modèle : couverte ; règles : {n_dom} domaines fiables ≥ {th.min_domains_per_subquestion}"
            )
        elif status == "partial" and strong:
            covered = True
            reasons.append(
                f"modèle : partielle, mais preuve forte après recherche ciblée ({n_dom} domaines fiables, "
                f"{m.claims} affirmations) → acceptée par les règles"
            )
        else:
            covered = False
            if status != "covered":
                reasons.append(
                    f"modèle : {status}" + (f" — {a.rationale}" if a is not None and a.rationale else "")
                )
            if not rules_ok:
                reasons.append(
                    f"règles : {n_dom} domaine(s) fiable(s) < {th.min_domains_per_subquestion} requis"
                )
            if status == "covered" and not rules_ok:
                reasons.insert(
                    0, "le modèle juge la sous-question couverte, mais la corroboration est insuffisante"
                )

        queries: list[str] = []
        if not covered:
            proposed = [
                clean_query(q) for q in (getattr(a, "follow_up_queries", []) if a is not None else [])
            ]
            if not proposed or not any(q.lower() not in seen for q in proposed):
                proposed.append(_fallback_query(sq_text))  # repli : la sous-question elle-même
            for q in proposed:
                if q and q.lower() not in seen and len(queries) < th.max_queries_per_subquestion:
                    seen.add(q.lower())
                    queries.append(q)
        verdicts.append(SubQuestionVerdict(sq_id, covered, status, reasons, queries))

    uncovered = [v for v in verdicts if not v.covered]
    total_ok = reliable_read >= th.min_total_sources

    # Requêtes du tour suivant : réparties entre les sous-questions non couvertes (tour de rôle).
    next_queries: list[str] = []
    pools = [list(v.queries) for v in uncovered]
    while pools and len(next_queries) < th.max_queries_per_round:
        pools = [p for p in pools if p]
        for p in pools:
            if len(next_queries) < th.max_queries_per_round:
                next_queries.append(p.pop(0))
    if not uncovered and not total_ok:
        # Tout est couvert mais trop peu de sources au total : on élargit sur la sous-question la moins étayée.
        weakest = min(sub_questions, key=lambda sq: len(metrics[sq[0]].reliable_domains))
        q = _fallback_query(weakest[1])
        if q.lower() not in seen:
            next_queries.append(q)
    focus = [v.sub_question_id for v in uncovered]

    def make(stop: bool, sufficient: bool, reason: str) -> Decision:
        return Decision(
            round_no,
            stop,
            sufficient,
            reason,
            [] if stop else next_queries,
            [] if stop else focus,
            verdicts,
            reliable_read,
        )

    if not uncovered and total_ok:
        return make(
            True,
            True,
            f"suffisant : toutes les sous-questions sont couvertes et {reliable_read} "
            f"sources fiables lues (≥ {th.min_total_sources})",
        )
    gap = (
        f"non couvertes : {', '.join(focus)}"
        if focus
        else f"seulement {reliable_read} sources fiables lues (< {th.min_total_sources})"
    )
    if round_no >= th.max_rounds:
        return make(True, False, f"arrêt : nombre maximal de tours atteint ({th.max_rounds}) ; {gap}")
    if round_no > 1 and last_round_read == 0 and last_round_claims == 0:
        return make(
            True, False, f"arrêt : stagnation (aucune nouvelle source exploitable au tour {round_no}) ; {gap}"
        )
    if not next_queries:
        return make(True, False, f"arrêt : plus aucune requête nouvelle à lancer ; {gap}")
    return make(False, False, f"recherche complémentaire : {gap}")
