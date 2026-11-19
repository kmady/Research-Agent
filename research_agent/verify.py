"""Étape 6 — Vérification croisée des affirmations.

1. Regroupement : les affirmations équivalentes extraites de sources différentes forment un groupe
   (similarité d'embeddings ≥ `claim_similarity`, dans la même sous-question).
2. Origines indépendantes : deux domaines dont les passages cités sont quasi identiques
   (dépêche reprise par plusieurs journaux) ne comptent que pour une seule origine.
   Seules les sources fiables (medium/high) comptent.
3. ≥ 2 origines fiables indépendantes → « corroborated », sans appel au modèle.
4. Les groupes fragiles (moins de 2 origines) sont vérifiés, dans la limite d'un budget :
   preuves cherchées d'abord dans le corpus déjà lu, puis sur le web ; verdict du modèle,
   contrôlé par le code (un verdict sans passage valide à l'appui devient « unverifiable »).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import numpy as np

from . import prompts
from .agent_utils import domain_kind
from .fetch import max_similarity, rank_passages
from .llm import ContextOverflowError, LLMError
from .schemas import ClaimVerdict, DivergenceReport
from .search import SearchError
from .state import Claim, Passage, ResearchState, Source

if TYPE_CHECKING:
    from .agent import ResearchAgent

log = logging.getLogger("research_agent")

def heuristic_reliability(domain: str) -> tuple[str, str]:
    """(type, fiabilité) d'une source lue seulement pour vérifier, donc sans appréciation par le modèle."""
    return domain_kind(domain) or ("other", "medium")


def _unit(vectors: list[list[float]]) -> np.ndarray:
    m = np.asarray(vectors, dtype=np.float32)
    return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-12, None)


_HEDGES = re.compile(
    r"\b(probabl\w*|possibl\w*|peut|peuvent|pourrai\w*|pourrait|semble\w*|susceptible\w*|risque\w*|"
    r"suspect\w*|potentiel\w*|may|might|could|likely|possibly|probably|suggest\w*)\b", re.IGNORECASE)
_NEGATIONS = re.compile(r"\b(ne|n'|n’|pas|aucun\w*|jamais|sans|not|no|never|none)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def compatible(a: str, b: str) -> bool:
    """Garde-fou : les embeddings ne distinguent pas « probablement cancérogène » de « cancérogène »
    (similarité 0,94 mesurée). On refuse de regrouper deux affirmations si l'une est nuancée et pas
    l'autre, si l'une est négative et pas l'autre, ou si leurs chiffres diffèrent."""
    if bool(_HEDGES.search(a)) != bool(_HEDGES.search(b)):
        return False
    if bool(_NEGATIONS.search(a)) != bool(_NEGATIONS.search(b)):
        return False
    na = {n.replace(",", ".") for n in _NUMBER.findall(a)}
    nb = {n.replace(",", ".") for n in _NUMBER.findall(b)}
    return not (na and nb and na != nb)


def group_claims(claims: list[Claim], vectors: list[list[float]], threshold: float) -> list[list[int]]:
    """Regroupe les affirmations équivalentes (même sous-question, similarité ≥ seuil avec le premier membre,
    et formulations compatibles : même nuance, même polarité, mêmes chiffres).

    Comparer au premier membre (et non à n'importe quel membre) évite les chaînes
    A≈B≈C où A et C ne disent plus la même chose.
    """
    if not claims:
        return []
    V = _unit(vectors)
    groups: list[list[int]] = []
    for i, c in enumerate(claims):
        best, best_sim = None, threshold
        for g in groups:
            head = g[0]
            if claims[head].sub_question_id != c.sub_question_id or not compatible(claims[head].statement,
                                                                                    c.statement):
                continue
            sim = float(V[i] @ V[head])
            if sim >= best_sim:
                best, best_sim = g, sim
        if best is None:
            groups.append([i])
        else:
            best.append(i)
    return groups


def count_independent_origins(members: list[Claim], state: ResearchState, embed_docs, dup_threshold: float) -> int:
    """Nombre d'origines fiables indépendantes : domaines distincts, reprises d'un même texte fusionnées."""
    reg = state.registry
    by_domain: dict[str, list[str]] = {}
    for c in members:
        src = reg.get(c.source_id)
        if not src or src.reliability not in ("medium", "high"):
            continue
        texts = by_domain.setdefault(src.domain, [])
        for pid in c.passage_ids:
            p = reg.get_passage(pid)
            if p and p.text not in texts:
                texts.append(p.text)
    domains = list(by_domain)
    if len(domains) <= 1:
        return len(domains)

    # Union-find : deux domaines sont fusionnés si l'un de leurs passages cités est quasi identique.
    parent = list(range(len(domains)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    vecs = {d: _unit(embed_docs(by_domain[d])) for d in domains if by_domain[d]}
    for i in range(len(domains)):
        for j in range(i + 1, len(domains)):
            a, b = vecs.get(domains[i]), vecs.get(domains[j])
            if a is not None and b is not None and float((a @ b.T).max()) >= dup_threshold:
                parent[find(i)] = find(j)
    return len({find(i) for i in range(len(domains))})


def shorten_query(statement: str, max_words: int = 12) -> str:
    words = re.sub(r"[«»\"“”()\[\]:;,.!?]", " ", statement).split()
    return " ".join(words[:max_words])


def _same_opposition(a: set[str], b: set[str], prev: tuple[set[str], set[str]]) -> bool:
    """Deux divergences opposent-elles les mêmes sources (dans un sens ou dans l'autre) ?"""
    pa, pb = prev
    return bool((a & pa and b & pb) or (a & pb and b & pa))


class Verifier:
    def __init__(self, agent: ResearchAgent) -> None:
        self.agent = agent
        self.cfg = agent.cfg
        self.embedder = agent.embedder

    # --- 1 à 3 : groupes, origines, corroboration -----------------------------

    def corroborate(self, state: ResearchState) -> list[list[Claim]]:
        claims = state.claims
        if not claims:
            return []
        vectors = self.embedder.embed_documents([c.statement for c in claims])
        groups = [[claims[i] for i in g] for g in group_claims(claims, vectors, self.cfg.claim_similarity)]
        for n, members in enumerate(groups, start=1):
            origins = count_independent_origins(members, state, self.embedder.embed_documents,
                                                self.cfg.duplicate_passage_similarity)
            for c in members:
                c.group_id = f"G{n}"
                c.independent_origins = origins
                if origins >= 2:
                    c.status = "corroborated"
        return groups

    @staticmethod
    def _priority(members: list[Claim], state: ResearchState, sq_order: dict[str, int]) -> tuple:
        reg = state.registry
        rels = {reg.get(c.source_id).reliability for c in members}
        has_number = any(re.search(r"\d", c.statement) for c in members)
        # D'abord les affirmations de sources fiables (ce sont elles qui nourriront la synthèse),
        # la fiabilité haute en tête, puis les faits chiffrés ; les sources peu fiables en dernier.
        rank = 0 if "high" in rels else 1 if "medium" in rels else 2
        return (rank, not has_number, sq_order.get(members[0].sub_question_id, 99), members[0].id)

    # --- 4 : vérification d'un groupe fragile -------------------------------

    def _own_passages(self, members: list[Claim], state: ResearchState) -> list[Passage]:
        return [p for c in members for pid in c.passage_ids if (p := state.registry.get_passage(pid))]

    def _evidence_from_corpus(self, state: ResearchState, claim: Claim, excluded_domains: set[str],
                              own: list[Passage]) -> list[tuple[Passage, float]]:
        reg = state.registry
        pool = [p for s in reg.all() if s.fetched and s.domain not in excluded_domains
                and s.reliability in ("medium", "high") for p in s.passages]
        if not pool:
            return []
        ranked = rank_passages(claim.statement, [p.text for p in pool], self.embedder, top_k=8)
        own_vecs = _unit(self.embedder.embed_documents([p.text for p in own])) if own else None
        out = []
        for i, score in ranked:
            if score < self.cfg.evidence_similarity:
                break
            p = pool[i]
            if own_vecs is not None:   # reprise du même texte : pas une preuve indépendante
                v = _unit(self.embedder.embed_documents([p.text]))
                if float((own_vecs @ v.T).max()) >= self.cfg.duplicate_passage_similarity:
                    continue
            out.append((p, score))
            if len(out) == 3:
                break
        return out

    def _evidence_from_web(self, state: ResearchState, claim: Claim, excluded_domains: set[str],
                           round_no: int) -> list[tuple[Passage, float]]:
        a = self.agent
        query = shorten_query(claim.statement)
        state.queries_run.append(query)
        try:
            hits = a.search.search(query, n=self.cfg.results_per_query)
        except SearchError as exc:
            log.warning("    recherche de vérification en échec : %s", exc)
            return []
        candidates: list[Source] = []
        hits = [h for h in hits if not a._is_blocked(h.url)]
        scores = max_similarity([claim.statement], [f"{h.title} — {h.snippet}" for h in hits], self.embedder)
        for h, score in zip(hits, scores):
            if score < self.cfg.min_hit_relevance:
                continue
            src, _ = state.registry.add(h.url, h.title, round_no=round_no, snippet=h.snippet)
            if src.domain not in excluded_domains and not src.fetched and not src.fetch_error:
                candidates.append(src)
        out: list[tuple[Passage, float]] = []
        read = 0
        for src in a._select(state, candidates, self.cfg.verification_pages + 1, [claim.sub_question_id]):
            if read == self.cfg.verification_pages:
                break
            passages = a._read(state, src)
            if passages is None:
                continue
            read += 1
            src.kind, src.reliability = heuristic_reliability(src.domain)
            for i, score in rank_passages(claim.statement, [p.text for p in passages], self.embedder, top_k=2):
                out.append((passages[i], score))
        return out

    def verify_group(self, state: ResearchState, members: list[Claim], round_no: int) -> dict:
        reg = state.registry
        claim = members[0]
        own_domains = {reg.get(c.source_id).domain for c in members}
        own = self._own_passages(members, state)

        evidence = self._evidence_from_corpus(state, claim, own_domains, own)
        origin = "corpus"
        if len(evidence) < 2:
            evidence += self._evidence_from_web(state, claim, own_domains, round_no)
            origin = "corpus+web" if evidence else "web"
        if not evidence:
            verdict = ClaimVerdict(verdict="unverifiable", supporting_passage_ids=[], contradicting_passage_ids=[],
                                   note="Aucune autre source pertinente trouvée.")
            return self._apply(members, verdict, [], origin, reg)

        k = min(len(evidence), 3)
        while True:
            shown = [p for p, _ in sorted(evidence, key=lambda e: -e[1])[:k]]
            etext = "\n\n".join(
                f"[{p.id}] ({reg.get(p.source_id).domain}, fiabilité {reg.get(p.source_id).reliability})\n{p.text}"
                for p in shown)
            src = reg.get(claim.source_id)
            try:
                verdict = self.agent._ask(prompts.verification_prompt(claim.statement, f"{src.domain}", etext),
                                          ClaimVerdict, think=True)
                break
            except ContextOverflowError:
                if k <= 1:
                    raise
                k -= 1
        return self._apply(members, verdict, shown, origin, reg)

    @staticmethod
    def _apply(members: list[Claim], v: ClaimVerdict, shown: list[Passage], origin: str, reg) -> dict:
        """Contrôle le verdict du modèle puis l'applique à tout le groupe."""
        valid = {p.id for p in shown}
        support = [pid for pid in dict.fromkeys(v.supporting_passage_ids) if pid in valid]
        contra = [pid for pid in dict.fromkeys(v.contradicting_passage_ids) if pid in valid]
        status = v.verdict
        if status == "confirmed" and not support:
            status = "unverifiable"            # « confirmée » sans passage à l'appui : refusé
        if status == "contested" and not contra:
            status = "confirmed" if support else "unverifiable"
        if status == "confirmed" and contra:
            status = "contested"              # des passages contredisent : c'est une divergence
        for c in members:
            c.status = status
            c.supporting_passage_ids = support
            c.contradicting_passage_ids = contra
            c.verification_note = v.note
        return {"group": members[0].group_id, "claim": members[0].id, "model_verdict": v.verdict,
                "status": status, "support": support, "contra": contra, "evidence_from": origin,
                "evidence": [p.id for p in shown], "note": v.note}

    # --- 5 : divergences entre sources (comparaison par sous-question) -------

    @staticmethod
    def _diverse_sample(claims: list[Claim], state: ResearchState, k: int) -> list[Claim]:
        """Au plus k affirmations, prises à tour de rôle dans chaque domaine (fiabilité haute d'abord) :
        une divergence n'apparaît qu'entre sources différentes, la diversité compte plus que le nombre."""
        reg = state.registry
        by_domain: dict[str, list[Claim]] = {}
        for c in claims:
            by_domain.setdefault(reg.get(c.source_id).domain, []).append(c)
        order = sorted(by_domain, key=lambda d: reg.get(by_domain[d][0].source_id).reliability != "high")
        out: list[Claim] = []
        while len(out) < k and any(by_domain[d] for d in order):
            for d in order:
                if by_domain[d] and len(out) < k:
                    out.append(by_domain[d].pop(0))
        return sorted(out, key=lambda c: int(c.id[1:]))

    def find_divergences(self, state: ResearchState) -> list[dict]:
        """Repère les conclusions incompatibles entre sources différentes, sous-question par sous-question.

        Complète la vérification affirmation par affirmation : « le CIRC juge le glyphosate cancérogène
        probable » et « le BfR le juge peu probable » sont deux affirmations vraies, mais les sources
        divergent. Le code ne garde qu'une divergence dont les deux côtés existent et viennent de
        domaines différents.
        """
        reg = state.registry
        found: list[dict] = []
        seen_oppositions: list[tuple[set[str], set[str]]] = []
        for sq in state.plan.sub_questions:
            claims = [c for c in state.claims_for(sq.id) if reg.get(c.source_id).reliability != "low"]
            if len({reg.get(c.source_id).domain for c in claims}) < 2:
                continue
            claims = self._diverse_sample(claims, state, self.cfg.max_claims_per_comparison)
            by_id = {c.id: c for c in claims}
            text = "\n".join(f"{c.id} : {c.statement} [{reg.get(c.source_id).domain}]" for c in claims)
            try:
                rep = self.agent._ask(prompts.divergence_prompt(state.question, sq.id, sq.question, text),
                                      DivergenceReport, think=True)
            except LLMError as exc:
                log.warning("  comparaison des sources de %s en échec : %s", sq.id, exc)
                continue
            for d in rep.divergences[:3]:
                a = [cid for cid in dict.fromkeys(d.side_a_claim_ids) if cid in by_id]
                b = [cid for cid in dict.fromkeys(d.side_b_claim_ids) if cid in by_id and cid not in a]
                dom_a = {reg.get(by_id[c].source_id).domain for c in a}
                dom_b = {reg.get(by_id[c].source_id).domain for c in b}
                if not a or not b or not (dom_a ^ dom_b):   # chaque côté doit exister, sources différentes
                    continue
                src_a = {by_id[c].source_id for c in a}
                src_b = {by_id[c].source_id for c in b}
                if any(_same_opposition(src_a, src_b, prev) for prev in seen_oppositions):
                    continue   # même opposition déjà signalée (souvent répétée d'une sous-question à l'autre)
                seen_oppositions.append((src_a, src_b))
                found.append({"sub_question_id": sq.id, "point": d.point, "side_a": a, "side_b": b,
                              "explanation": d.explanation})
                log.info("  ⇄ %s divergence : %s (%s / %s)", sq.id, d.point[:80], ", ".join(a), ", ".join(b))
        state.divergences = found
        return found

    # --- Point d'entrée -------------------------------------------------------

    def run(self, state: ResearchState) -> None:
        groups = self.corroborate(state)
        corroborated = [g for g in groups if g[0].status == "corroborated"]
        fragile = [g for g in groups if g[0].status != "corroborated"]
        sq_order = {sq: i for i, sq in enumerate(state.sub_question_ids())}
        fragile.sort(key=lambda g: self._priority(g, state, sq_order))
        budget = self.cfg.max_claims_to_verify
        log.info("Vérification : %d affirmations en %d groupes — %d corroborés, %d fragiles (%d vérifiés)",
                 len(state.claims), len(groups), len(corroborated), len(fragile), min(budget, len(fragile)))

        round_no = len(state.decisions) + 1
        results = []
        for members in fragile[:budget]:
            try:
                res = self.verify_group(state, members, round_no)
            except LLMError as exc:
                log.warning("  vérification de %s en échec : %s", members[0].id, exc)
                continue
            results.append(res)
            mark = {"confirmed": "✔", "contested": "⚠", "unverifiable": "?"}[res["status"]]
            log.info("  %s %s %s (%s) : %s", mark, res["claim"], res["status"], res["evidence_from"],
                     members[0].statement[:90])
        divergences = self.find_divergences(state)
        state.log("verification", groups=len(groups), corroborated=len(corroborated), fragile=len(fragile),
                  verified=results, unchecked=[g[0].id for g in fragile[budget:]], divergences=divergences)
