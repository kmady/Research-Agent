"""Étape 7 — Synthèse référencée.

Le modèle rédige ; le code garantit les références :
- une section par sous-question (un appel chacune : le contexte de 8k ne permet pas tout d'un coup),
  à partir des seules affirmations utilisables, classées par solidité ;
- une réponse courte, rédigée à partir des sections ;
- les références écrites par le modèle ([S3], [S3:p2], [C12]) sont contrôlées : seules restent
  celles qui désignent une source effectivement fournie pour ce texte, puis elles sont
  renumérotées [1], [2]... dans l'ordre d'apparition dans le rapport.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import prompts
from .llm import LLMError
from .state import Claim, ResearchState

if TYPE_CHECKING:
    from .agent import ResearchAgent

log = logging.getLogger("research_agent")

# [S3] [S3:p2] [C12] [S3, S5] [S3; C4] …
_REF_BLOCK = re.compile(r"\[((?:\s*(?:S\d+(?::p\d+)?|C\d+)\s*[,;]?)+)\]")
_REF_ID = re.compile(r"S\d+(?::p\d+)?|C\d+")
_CLAIM_ID = re.compile(r"(?<![\w\[])C\d+\b")  # « C6 » nu dans un texte du modèle

STATUS_LABEL = {
    "corroborated": "corroborée par plusieurs sources",
    "confirmed": "confirmée par une autre source",
    "unchecked": "source unique",
    "unverifiable": "source unique, non confirmée ailleurs",
    "contested": "contestée par d'autres sources",
}
_STATUS_ORDER = {"corroborated": 0, "confirmed": 1, "contested": 2, "unchecked": 3, "unverifiable": 4}


class CitationMapper:
    """Renumérote les références dans l'ordre d'apparition, sur tout le rapport."""

    def __init__(self, state: ResearchState) -> None:
        self.state = state
        self.numbers: dict[str, int] = {}  # S3 → 1
        self.claim_source = {c.id: c.source_id for c in state.claims}
        self.dropped: list[str] = []

    def _source_of(self, ref: str) -> str | None:
        if ref.startswith("C"):
            return self.claim_source.get(ref)
        return ref.split(":", 1)[0]

    def number(self, source_id: str) -> int:
        if source_id not in self.numbers:
            self.numbers[source_id] = len(self.numbers) + 1
        return self.numbers[source_id]

    def render(self, text: str, allowed: set[str]) -> str:
        """Remplace les références par des numéros ; supprime celles qui ne sont pas autorisées."""

        def repl(m: re.Match) -> str:
            out: list[int] = []
            for ref in _REF_ID.findall(m.group(1)):
                sid = self._source_of(ref)
                if sid and sid in allowed and sid in self.state.registry:
                    n = self.number(sid)
                    if n not in out:
                        out.append(n)
                else:
                    self.dropped.append(ref)
            return "".join(f"[{n}]" for n in out)

        text = _REF_BLOCK.sub(repl, text)
        text = re.sub(r"(\[\d+\])(?:\s*,?\s*\1)+", r"\1", text)  # « [4], [4] » → « [4] »
        return re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()

    def cited_sources(self) -> list[str]:
        return sorted(self.numbers, key=self.numbers.get)


def usable_claims(state: ResearchState, sq_id: str, limit: int = 15) -> list[Claim]:
    """Affirmations d'une sous-question utilisables dans la synthèse, les plus solides d'abord.

    Une source peu fiable n'est retenue que si l'affirmation est corroborée ou confirmée ailleurs.
    """
    reg = state.registry
    out = []
    for c in state.claims_for(sq_id):
        low = reg.get(c.source_id).reliability == "low"
        if low and c.status not in ("corroborated", "confirmed"):
            continue
        out.append(c)
    rel_rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(
        key=lambda c: (_STATUS_ORDER[c.status], rel_rank[reg.get(c.source_id).reliability], int(c.id[1:]))
    )
    # Un seul représentant par groupe d'affirmations équivalentes (les autres sources restent citables).
    seen_groups, unique = set(), []
    for c in out:
        if c.group_id and c.group_id in seen_groups:
            continue
        seen_groups.add(c.group_id)
        unique.append(c)
    return unique[:limit]


def _group_sources(state: ResearchState, claim: Claim) -> list[str]:
    """Sources citables pour une affirmation : celles de son groupe, en écartant les sources peu fiables
    dès qu'au moins une source fiable rapporte la même chose."""
    if not claim.group_id:
        return [claim.source_id]
    reg = state.registry
    sources = list(dict.fromkeys(c.source_id for c in state.claims if c.group_id == claim.group_id))
    reliable = [s for s in sources if reg.get(s).reliability in ("medium", "high")]
    return reliable or sources


def _clean_section(text: str) -> str:
    """Retire les titres que le modèle ajouterait malgré la consigne."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()


@dataclass
class Synthesis:
    short_answer: str = ""
    sections: dict[str, str] = field(default_factory=dict)  # Q1 → texte avec [n]
    divergences: list[dict] = field(default_factory=list)  # divergences avec refs numérotées
    cited_sources: list[str] = field(default_factory=list)  # S-ids dans l'ordre des numéros
    numbers: dict[str, int] = field(default_factory=dict)
    dropped_refs: list[str] = field(default_factory=list)
    failed_sections: list[str] = field(default_factory=list)


class Synthesizer:
    def __init__(self, agent: ResearchAgent) -> None:
        self.agent = agent
        self.llm = agent.llm

    def _chat(self, user: str) -> str:
        return self.llm.chat(
            [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": user}], think=False
        )

    def _section_input(self, state: ResearchState, sq) -> tuple[str, str, set[str]]:
        reg = state.registry
        claims = usable_claims(state, sq.id)
        allowed: set[str] = set()
        lines = []
        for c in claims:
            sources = _group_sources(state, c)
            allowed.update(sources)
            refs = "".join(f"[{s}]" for s in sources)
            doms = ", ".join(dict.fromkeys(reg.get(s).domain for s in sources))
            lines.append(f"- {c.statement} {refs} ({STATUS_LABEL[c.status]} ; {doms})")
        div_lines = []
        for d in state.divergences:
            if d["sub_question_id"] != sq.id:
                continue
            by_id = {c.id: c for c in state.claims}
            sides = []
            for key in ("side_a", "side_b"):
                parts = []
                for cid in d[key]:
                    c = by_id.get(cid)
                    if c:
                        allowed.add(c.source_id)
                        parts.append(f"{c.statement} [{c.source_id}]")
                sides.append(" ; ".join(parts))
            div_lines.append(f"- {d['point']} : d'un côté {sides[0]} ; de l'autre {sides[1]}")
        return "\n".join(lines), "\n".join(div_lines), allowed

    def run(self, state: ResearchState) -> Synthesis:
        syn = Synthesis()
        mapper = CitationMapper(state)
        raw_sections: dict[str, tuple[str, set[str]]] = {}

        for sq in state.plan.sub_questions:
            claims_text, div_text, allowed = self._section_input(state, sq)
            if not claims_text:
                raw_sections[sq.id] = ("", set())
                continue
            try:
                text = self._chat(
                    prompts.section_prompt(state.question, sq.id, sq.question, claims_text, div_text)
                )
            except LLMError as exc:
                log.warning("  rédaction de %s en échec : %s", sq.id, exc)
                syn.failed_sections.append(sq.id)
                raw_sections[sq.id] = ("", set())
                continue
            raw_sections[sq.id] = (_clean_section(text), allowed)
            log.info("  ✎ %s rédigée (%d caractères)", sq.id, len(text))

        # Réponse courte à partir des sections (références encore au format S3).
        all_allowed = set().union(*(a for _, a in raw_sections.values())) if raw_sections else set()
        body = "\n\n".join(
            f"{sq.id} — {sq.question}\n{raw_sections[sq.id][0]}"
            for sq in state.plan.sub_questions
            if raw_sections[sq.id][0]
        )
        if body:
            try:
                short = self._chat(prompts.short_answer_prompt(state.question, body))
                syn.short_answer = mapper.render(_clean_section(short), all_allowed)
            except LLMError as exc:
                log.warning("  réponse courte en échec : %s", exc)

        # Numérotation dans l'ordre du rapport : réponse courte, sections, puis divergences.
        for sq in state.plan.sub_questions:
            text, allowed = raw_sections[sq.id]
            syn.sections[sq.id] = mapper.render(text, allowed) if text else ""
        by_id = {c.id: c for c in state.claims}
        for d in state.divergences:
            sides = {}
            for key in ("side_a", "side_b"):
                sides[key] = [
                    (by_id[c].statement, mapper.number(by_id[c].source_id)) for c in d[key] if c in by_id
                ]
            explanation = _CLAIM_ID.sub(lambda m: f"[{m.group(0)}]", d["explanation"])
            allowed = {by_id[c].source_id for key in ("side_a", "side_b") for c in d[key] if c in by_id}
            syn.divergences.append(
                {**d, "rendered": sides, "explanation": mapper.render(explanation, allowed)}
            )

        syn.cited_sources = mapper.cited_sources()
        syn.numbers = dict(mapper.numbers)
        syn.dropped_refs = mapper.dropped
        if mapper.dropped:
            log.info(
                "  références retirées (non fournies au modèle) : %s", ", ".join(sorted(set(mapper.dropped)))
            )
        state.synthesis = syn
        state.log(
            "synthesis",
            sections={k: len(v) for k, v in syn.sections.items()},
            cited=syn.cited_sources,
            dropped_refs=syn.dropped_refs,
            failed=syn.failed_sections,
        )
        return syn
