"""Tests de la synthèse référencée (étape 7)."""

from research_agent.llm import LLMError, LLMStats
from research_agent.report import render_final_report
from research_agent.schemas import ResearchPlan, SubQuestion
from research_agent.state import Claim, ResearchState
from research_agent.synthesis import CitationMapper, Synthesizer, usable_claims


def claim(cid, sq, sid, text, status="unchecked", group=""):
    return Claim(
        id=cid,
        statement=text,
        sub_question_id=sq,
        source_id=sid,
        passage_ids=[f"{sid}:p1"],
        quote="",
        quote_verified=False,
        round_found=1,
        status=status,
        group_id=group,
    )


def make_state():
    st = ResearchState(question="Le glyphosate est-il cancérogène ?")
    st.plan = ResearchPlan(
        sub_questions=[
            SubQuestion(id="Q1", question="Que disent les agences ?"),
            SubQuestion(id="Q2", question="Que disent les études ?"),
        ],
        initial_queries=[],
    )
    for url, rel in [
        ("https://iarc.who.int/a", "high"),
        ("https://efsa.europa.eu/b", "high"),
        ("https://lemonde.fr/c", "medium"),
        ("https://blog.net/d", "low"),
    ]:
        src, _ = st.registry.add(url, f"Titre {url}")
        src.reliability, src.fetched = rel, True
    st.claims = [
        claim("C1", "Q1", "S1", "Le CIRC classe le glyphosate en 2A.", "corroborated", "G1"),
        claim("C2", "Q1", "S3", "Le CIRC a classé le glyphosate cancérogène probable.", "corroborated", "G1"),
        claim("C3", "Q1", "S2", "L'EFSA juge le risque improbable.", "unchecked", "G2"),
        claim("C4", "Q1", "S4", "Le glyphosate est un poison.", "unchecked", "G3"),
        claim("C5", "Q1", "S4", "Le glyphosate est classé 2A.", "confirmed", "G4"),
    ]
    st.divergences = [
        {
            "sub_question_id": "Q1",
            "point": "Cancérogénicité",
            "side_a": ["C1"],
            "side_b": ["C3"],
            "explanation": "Le CIRC et l'EFSA concluent différemment.",
        }
    ]
    return st


# --- Références ----------------------------------------------------------------


def test_citations_are_renumbered_in_order_and_invalid_ones_dropped():
    st = make_state()
    m = CitationMapper(st)
    allowed = {"S1", "S2", "S3"}
    text = m.render(
        "Fait A [S2]. Fait B [S1, S2]. Fait C [S3:p4][C1]. Inventé [S9]. Interdit [S4] .", allowed
    )
    assert text == "Fait A [1]. Fait B [2][1]. Fait C [3][2]. Inventé. Interdit."
    assert m.cited_sources() == ["S2", "S1", "S3"]
    assert sorted(m.dropped) == ["S4", "S9"]


def test_usable_claims_filters_low_reliability_and_keeps_one_per_group():
    st = make_state()
    ids = [c.id for c in usable_claims(st, "Q1")]
    # C4 (blog, non vérifiée) écartée ; C5 (blog mais confirmée) gardée ; C2 même groupe que C1.
    assert ids == ["C1", "C5", "C3"]


# --- Rédaction -----------------------------------------------------------------


class ChatLLM:
    def __init__(self, fail_on=None):
        self.stats = LLMStats()
        self.prompts = []
        self.fail_on = fail_on

    def chat(self, messages, think=False):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt:
            raise LLMError("délai dépassé")
        if "réponse courte" in prompt:
            return "Les agences divergent [S2][S1]. Source inventée [S77]."
        return "## Titre parasite\nLe CIRC classe le glyphosate en 2A [S1][S3]. L'EFSA juge le risque improbable [S2]."


class FakeAgent:
    def __init__(self, llm):
        self.llm = llm


def test_synthesis_renumbers_across_the_whole_report():
    st = make_state()
    llm = ChatLLM()
    syn = Synthesizer(FakeAgent(llm)).run(st)
    # La réponse courte vient en premier : S2 → 1, S1 → 2 ; puis la section : S3 → 3.
    assert syn.short_answer == "Les agences divergent [1][2]. Source inventée."
    assert (
        syn.sections["Q1"]
        == "Le CIRC classe le glyphosate en 2A [2][3]. L'EFSA juge le risque improbable [1]."
    )
    assert syn.sections["Q2"] == ""  # aucune affirmation : pas d'appel au modèle
    assert syn.cited_sources == ["S2", "S1", "S3"]
    assert "S77" in syn.dropped_refs
    assert syn.divergences[0]["rendered"]["side_b"] == [("L'EFSA juge le risque improbable.", 1)]
    section_prompt = next(p for p in llm.prompts if "Sous-question Q1" in p)
    assert "Divergences repérées" in section_prompt
    assert "poison" not in section_prompt  # source peu fiable non vérifiée
    assert "corroborée par plusieurs sources" in section_prompt
    assert "[S1][S3]" in section_prompt  # toutes les sources du groupe sont citables


def test_failed_section_is_reported_not_fatal():
    st = make_state()
    syn = Synthesizer(FakeAgent(ChatLLM(fail_on="Sous-question Q1"))).run(st)
    assert syn.failed_sections == ["Q1"] and syn.sections["Q1"] == "" and syn.short_answer == ""


def test_final_report_structure():
    st = make_state()
    st.sufficient, st.stop_reason, st.decisions = True, "suffisant", [{"round_no": 1}]
    Synthesizer(FakeAgent(ChatLLM())).run(st)
    md = render_final_report(st, {"model": "m"})
    for heading in (
        "## Réponse courte",
        "## Développement",
        "### Que disent les agences ?",
        "## Points de divergence",
        "## Limites",
        "## Sources",
    ):
        assert heading in md
    assert "_Aucune information exploitable trouvée dans les sources._" in md  # Q2
    assert "1. [Titre https://efsa.europa.eu/b](https://efsa.europa.eu/b)" in md
    assert "3. [Titre https://lemonde.fr/c]" in md
    assert "Titre https://blog.net/d" not in md  # jamais citée → absente de la bibliographie
    assert "référence(s) proposées par le modèle ont été retirées" in md
    assert "**Q2** (Que disent les études ?) : aucune information exploitable trouvée." in md


def test_citation_coverage_metric():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "evaluate", pathlib.Path(__file__).parent.parent / "scripts" / "evaluate.py"
    )
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    text = (
        "La caféine réduit le sommeil profond [1]. Elle agit sur l'adénosine selon plusieurs études. Court."
    )
    assert ev.citation_coverage(text) == (1, 2)


def test_low_reliability_member_of_a_reliable_group_is_not_cited():
    from research_agent.synthesis import _group_sources

    st = make_state()
    st.claims.append(
        claim("C6", "Q1", "S4", "Le CIRC classe le glyphosate en 2A selon un blog.", "corroborated", "G1")
    )
    assert _group_sources(st, st.claims[0]) == ["S1", "S3"]  # S4 (blog) écarté
    only_low = claim("C7", "Q2", "S4", "x", "confirmed", "G9")
    st.claims.append(only_low)
    assert _group_sources(st, only_low) == ["S4"]  # aucune source fiable : on garde


def test_claim_ids_in_divergence_analysis_become_numbered_references():
    st = make_state()
    st.divergences[0]["explanation"] = "C1 classe en 2A alors que C3 juge le risque improbable."
    syn = Synthesizer(FakeAgent(ChatLLM())).run(st)
    text = syn.divergences[0]["explanation"]
    assert "C1" not in text and "C3" not in text
    assert (
        text
        == f"[{syn.numbers['S1']}] classe en 2A alors que [{syn.numbers['S2']}] juge le risque improbable."
    )


def test_repeated_adjacent_references_are_collapsed():
    st = make_state()
    m = CitationMapper(st)
    assert (
        m.render("Les sources [S1], [S1] et [S2] l'affirment.", {"S1", "S2"})
        == "Les sources [1] et [2] l'affirment."
    )


def test_outage_reason_is_shown_even_without_any_decision():
    st = make_state()
    st.claims, st.divergences = [], []
    st.sufficient, st.stop_reason = False, "arrêt : moteur de recherche indisponible au tour 1 (…)"
    Synthesizer(FakeAgent(ChatLLM())).run(st)
    md = render_final_report(st)
    assert "⚠️ Couverture incomplète" in md and "moteur de recherche indisponible" in md
    assert "Couverture jugée **incomplète** par l'agent : arrêt : moteur de recherche indisponible" in md
