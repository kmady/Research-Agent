"""Rendu des résultats : rapport Markdown et export JSON de la trace.

Étape 4 : rapport brut (affirmations par sous-question, références S1, S2...).
L'étape 7 y ajoutera la synthèse rédigée et la renumérotation des références.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .state import ResearchState, Source


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "question"


def _source_status(s: Source) -> str:
    if s.fetched:
        return "lue"
    if s.fetch_error:
        return f"échec : {s.fetch_error}"
    return "non lue"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


STATUS_BADGE = {
    "corroborated": lambda c: f"✅ _corroborée ({c.independent_origins} origines)_",
    "confirmed": lambda c: "✔️ _confirmée_",
    "contested": lambda c: "⚠️ _contestée_",
    "unverifiable": lambda c: "❔ _invérifiable_",
    "unchecked": lambda c: "·",
}


def _contested_groups(state: ResearchState) -> list[list]:
    groups: dict[str, list] = {}
    for c in state.claims:
        if c.status == "contested":
            groups.setdefault(c.group_id or c.id, []).append(c)
    return list(groups.values())


def render_raw_report(state: ResearchState, meta: dict | None = None) -> str:
    meta = meta or {}
    reg = state.registry
    lines = [f"# {state.question}", ""]
    info = [f"Rapport brut — {datetime.now():%Y-%m-%d %H:%M}"]
    if meta.get("model"):
        info.append(f"modèle `{meta['model']}`")
    if meta.get("seconds") is not None:
        info.append(f"{meta['seconds']:.0f} s")
    lines += [f"_{' · '.join(info)}_", ""]

    if state.decisions or state.stop_reason:
        verdict = "✅ Couverture jugée suffisante" if state.sufficient else "⚠️ Couverture incomplète"
        lines += [f"> **{verdict}** après {len(state.decisions)} tour(s) — {state.stop_reason}", ""]

    lines += ["## Sous-questions", ""]
    for sq in state.plan.sub_questions if state.plan else []:
        n = len(state.claims_for(sq.id))
        doms = reg.domains([c.source_id for c in state.claims_for(sq.id)])
        lines.append(f"- **{sq.id}** — {sq.question} _(affirmations : {n}, domaines : {len(doms)})_")
    lines.append("")

    lines += ["## Affirmations par sous-question", ""]
    for sq in state.plan.sub_questions if state.plan else []:
        lines += [f"### {sq.id} — {sq.question}", ""]
        claims = state.claims_for(sq.id)
        if not claims:
            lines += ["_Aucune affirmation trouvée._", ""]
            continue
        for c in claims:
            line = f"- {STATUS_BADGE[c.status](c)} {c.statement} [{c.source_id}] `{', '.join(c.passage_ids)}`"
            if c.status in ("confirmed", "contested") and (c.supporting_passage_ids or c.contradicting_passage_ids):
                parts = []
                if c.supporting_passage_ids:
                    parts.append("appuyée par " + ", ".join(c.supporting_passage_ids))
                if c.contradicting_passage_ids:
                    parts.append("contredite par " + ", ".join(c.contradicting_passage_ids))
                line += f" — _{' ; '.join(parts)}_"
            if c.quote_verified:
                line += f"\n  > « {c.quote} »"
            lines.append(line)
        lines.append("")

    contested = _contested_groups(state)
    if contested or state.divergences:
        lines += ["## Points de divergence", ""]
    by_id = {c.id: c for c in state.claims}
    for d in state.divergences:
        lines += [f"### {d['sub_question_id']} — {d['point']}", ""]
        for label, ids in (("D'un côté", d["side_a"]), ("De l'autre", d["side_b"])):
            items = [f"{by_id[i].statement} [{by_id[i].source_id}, {reg.get(by_id[i].source_id).domain}]"
                     for i in ids if i in by_id]
            lines.append(f"- **{label}** : " + " ; ".join(items))
        lines += [f"- Analyse : {d['explanation']}", ""]
    if contested:
        for members in contested:
            c = members[0]
            lines.append(f"### {c.id} — {c.statement}")
            lines.append("")
            lines.append(f"- Affirmée par : {', '.join(sorted({reg.get(m.source_id).domain for m in members}))}")
            for label, pids in (("Appui", c.supporting_passage_ids), ("Contradiction", c.contradicting_passage_ids)):
                for pid in pids:
                    p = reg.get_passage(pid)
                    if p:
                        excerpt = _cell(p.text[:280]) + ("…" if len(p.text) > 280 else "")
                        lines.append(f"- {label} — [{pid}] {reg.get(p.source_id).domain} : « {excerpt} »")
            if c.verification_note:
                lines.append(f"- Analyse : {c.verification_note}")
            lines.append("")

    if state.decisions:
        lines += ["## Décisions de l'agent", ""]
        rounds = {t["round"]: t for t in state.trace if t["step"] == "round"}
        for d in state.decisions:
            r = rounds.get(d["round_no"], {})
            lines.append(f"### Tour {d['round_no']}")
            lines.append("")
            if r:
                focus = f" — ciblé sur {', '.join(r['focus'])}" if r.get("focus") else ""
                lines.append(f"- Requêtes{focus} : " + " · ".join(f"`{q}`" for q in r["queries"]))
                lines.append(f"- Sources lues : {len(r['read'])} ({', '.join(r['read']) or 'aucune'}) ; "
                             f"affirmations ajoutées : {r['claims_added']}")
            for v in d["verdicts"]:
                mark = "✔" if v["covered"] else "✘"
                lines.append(f"- {mark} **{v['sub_question_id']}** ({v['llm_status']}) : {'; '.join(v['reasons'])}")
            lines.append(f"- **Décision :** {d['reason']}")
            if not d["stop"]:
                lines.append("- Requêtes complémentaires : " + " · ".join(f"`{q}`" for q in d["next_queries"]))
            lines.append("")

    lines += ["## Sources", "", "| Id | Titre | Domaine | Type | Fiabilité | Date | Statut |",
              "|---|---|---|---|---|---|---|"]
    for s in reg.all():
        title = f"[{_cell(s.title[:80])}]({s.url})"
        lines.append(f"| {s.id} | {title} | {s.domain} | {s.kind if s.fetched else ''} | "
                     f"{s.reliability if s.fetched else ''} | {s.publication_date} | {_cell(_source_status(s))} |")
    lines.append("")

    if meta.get("llm"):
        st = meta["llm"]
        lines += ["## Statistiques", "",
                  f"- Appels au modèle : {st['calls']} (nouveaux essais : {st['retries']}, échecs : {st['failures']})",
                  f"- Tokens : {st['prompt_tokens']} en entrée, {st['output_tokens']} en sortie",
                  f"- Requêtes lancées : {len(state.queries_run)}",
                  f"- Sources : {len(reg)} trouvées, {sum(s.fetched for s in reg.all())} lues",
                  f"- Affirmations : {len(state.claims)} "
                  f"(citations vérifiées : {sum(c.quote_verified for c in state.claims)})"]
        counts = Counter(c.status for c in state.claims)
        if any(k != "unchecked" for k in counts):
            labels = [("corroborated", "corroborées"), ("confirmed", "confirmées"), ("contested", "contestées"),
                      ("unverifiable", "invérifiables"), ("unchecked", "non vérifiées (budget)")]
            lines.append("- Vérification : " + ", ".join(f"{counts[k]} {lab}" for k, lab in labels if counts[k]))
        lines.append("")
    return "\n".join(lines)


def state_to_dict(state: ResearchState) -> dict:
    """Export complet et lisible de l'état ; seuls les passages cités gardent leur texte."""
    cited = {pid for c in state.claims
             for pid in (*c.passage_ids, *c.supporting_passage_ids, *c.contradicting_passage_ids)}
    sources = []
    for s in state.registry.all():
        d = asdict(s)
        d["passages"] = [{"id": p.id, "text": p.text} for p in s.passages if p.id in cited]
        d["passage_count"] = len(s.passages)
        sources.append(d)
    return {
        "question": state.question,
        "plan": state.plan.model_dump() if state.plan else None,
        "queries_run": state.queries_run,
        "sufficient": state.sufficient,
        "stop_reason": state.stop_reason,
        "covered": sorted(state.covered),
        "decisions": state.decisions,
        "divergences": state.divergences,
        "synthesis": (asdict(state.synthesis) if state.synthesis is not None else None),
        "sources": sources,
        "claims": [asdict(c) for c in state.claims],
        "trace": state.trace,
    }


def _limits(state: ResearchState, syn) -> list[str]:
    """Section « Limites », produite par le code à partir de la trace (pas par le modèle)."""
    reg = state.registry
    out = []
    if state.stop_reason and not state.sufficient:
        out.append(f"Couverture jugée **incomplète** par l'agent : {state.stop_reason}.")
    # Aspects signalés comme manquants lors de la dernière évaluation de chaque sous-question.
    last_missing: dict[str, list[str]] = {}
    for t in state.trace:
        if t["step"] == "assessment":
            for sq_id, a in t["llm"].items():
                if a and a.get("missing_aspects"):
                    last_missing[sq_id] = a["missing_aspects"]
    for sq in state.plan.sub_questions:
        if syn and not syn.sections.get(sq.id):
            out.append(f"**{sq.id}** ({sq.question}) : aucune information exploitable trouvée.")
        elif sq.id in last_missing:
            out.append(f"**{sq.id}** : aspects peu documentés — {'; '.join(last_missing[sq.id])}.")
    counts = Counter(c.status for c in state.claims)
    single = counts["unchecked"] + counts["unverifiable"]
    if single:
        out.append(f"{single} affirmation(s) sur {len(state.claims)} ne reposent que sur une seule source "
                   f"(dont {counts['unverifiable']} non confirmée(s) ailleurs malgré une recherche).")
    failed = [s for s in reg.all() if s.fetch_error]
    if failed:
        out.append(f"{len(failed)} source(s) n'ont pas pu être lues (accès refusé, robots.txt, format…).")
    if syn and syn.dropped_refs:
        out.append(f"{len(syn.dropped_refs)} référence(s) proposées par le modèle ont été retirées "
                   "car elles ne correspondaient à aucune source fournie.")
    out.append("Rapport rédigé automatiquement par un modèle de langage local à partir des seules sources "
               "listées : vérifiez les sources avant toute utilisation.")
    return out


def render_final_report(state: ResearchState, meta: dict | None = None) -> str:
    meta = meta or {}
    syn = state.synthesis
    reg = state.registry
    lines = [f"# {state.question}", ""]
    info = [f"{datetime.now():%Y-%m-%d %H:%M}"]
    if meta.get("model"):
        info.append(f"modèle `{meta['model']}`")
    info.append(f"{len(state.decisions)} tour(s) de recherche")
    info.append(f"{sum(s.fetched for s in reg.all())} sources lues")
    lines += [f"_{' · '.join(info)}_", ""]
    if state.decisions or state.stop_reason:
        verdict = "✅ Couverture jugée suffisante" if state.sufficient else "⚠️ Couverture incomplète"
        lines += [f"> **{verdict}** — {state.stop_reason}", ""]

    lines += ["## Réponse courte", "", syn.short_answer or "_Réponse courte indisponible._", ""]

    lines += ["## Développement", ""]
    for sq in state.plan.sub_questions:
        lines += [f"### {sq.question}", ""]
        lines += [syn.sections.get(sq.id) or "_Aucune information exploitable trouvée dans les sources._", ""]

    if syn.divergences:
        lines += ["## Points de divergence", ""]
        for d in syn.divergences:
            lines += [f"**{d['point']}**", ""]
            for label, key in (("D'un côté", "side_a"), ("De l'autre", "side_b")):
                items = [f"{text} [{n}]" for text, n in d["rendered"][key]]
                lines.append(f"- *{label}* : " + " ; ".join(items))
            lines += [f"- *Analyse* : {d['explanation']}", ""]

    lines += ["## Limites", ""] + [f"- {x}" for x in _limits(state, syn)] + [""]

    lines += ["## Sources", ""]
    kind_fr = {"official": "officielle", "academic": "académique", "press": "presse", "encyclopedia": "encyclopédie",
               "company": "entreprise", "blog": "blog", "other": "autre"}
    rel_fr = {"high": "haute", "medium": "moyenne", "low": "faible"}
    for sid in syn.cited_sources:
        s = reg.get(sid)
        meta_parts = [s.domain, kind_fr.get(s.kind, s.kind), f"fiabilité {rel_fr.get(s.reliability, s.reliability)}"]
        if s.publication_date:
            meta_parts.insert(1, s.publication_date)
        lines.append(f"{syn.numbers[sid]}. [{_cell(s.title)}]({s.url}) — {' · '.join(meta_parts)}")
    lines.append("")
    return "\n".join(lines)


def save_outputs(state: ResearchState, out_dir: str | Path, meta: dict | None = None) -> dict[str, Path]:
    """Écrit le rapport (synthèse), le détail (rapport brut) et la trace JSON."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(state.question)}"
    paths: dict[str, Path] = {}
    if state.synthesis is not None:
        paths["Rapport"] = out / f"{stem}.md"
        paths["Rapport"].write_text(render_final_report(state, meta), encoding="utf-8")
        paths["Détail"] = out / f"{stem}.details.md"
    else:
        paths["Rapport"] = out / f"{stem}.md"
    paths.setdefault("Détail", paths["Rapport"])
    paths["Détail"].write_text(render_raw_report(state, meta), encoding="utf-8")
    paths["Trace"] = out / f"{stem}.trace.json"
    paths["Trace"].write_text(json.dumps({"meta": meta or {}, **state_to_dict(state)}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return paths
