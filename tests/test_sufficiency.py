"""Tests de la décision « suffisant ou recherche complémentaire » (fonctions pures)."""

from research_agent.schemas import SubQuestionAssessment
from research_agent.state import ResearchState
from research_agent.sufficiency import SubQuestionMetrics, Thresholds, compute_metrics, decide

TH = Thresholds(max_rounds=3, min_domains_per_subquestion=2, min_total_sources=4,
                max_queries_per_round=4, strong_evidence_domains=3, strong_evidence_claims=4)
SQS = [("Q1", "Quel est le mécanisme ?"), ("Q2", "Quelle est la durée de l'effet ?")]


def m(sq, domains, claims=3, low=()):
    return SubQuestionMetrics(sub_question_id=sq, claims=claims, reliable_domains=list(domains), low_domains=list(low))


def a(status, queries=(), rationale="r"):
    return SubQuestionAssessment(status=status, rationale=rationale, missing_aspects=[],
                                 follow_up_queries=list(queries))


def run(metrics, assessments, round_no=1, covered=(), queries_run=(), read=5, claims=5, reliable=5, th=TH):
    return decide(round_no=round_no, sub_questions=SQS, metrics=metrics, assessments=assessments,
                  already_covered=set(covered), queries_run=list(queries_run), last_round_read=read,
                  last_round_claims=claims, reliable_read=reliable, th=th)


def by_id(decision):
    return {v.sub_question_id: v for v in decision.verdicts}


def test_all_covered_with_two_domains_stops_sufficient():
    d = run({"Q1": m("Q1", ["a.org", "b.org"]), "Q2": m("Q2", ["a.org", "c.org"])},
            {"Q1": a("covered"), "Q2": a("covered")})
    assert d.stop and d.sufficient
    assert d.next_queries == []


def test_model_says_covered_but_single_domain_continues():
    d = run({"Q1": m("Q1", ["a.org"]), "Q2": m("Q2", ["a.org", "b.org"])},
            {"Q1": a("covered"), "Q2": a("covered")})
    assert not d.stop and not d.sufficient
    v = by_id(d)["Q1"]
    assert not v.covered and "corroboration est insuffisante" in v.reasons[0]
    assert d.focus == ["Q1"]
    assert d.next_queries == ["mécanisme"]   # repli : mots-clés de la sous-question (aucune requête proposée)


def test_low_reliability_domains_do_not_count():
    st = ResearchState(question="q")

    class Plan:
        sub_questions = [type("SQ", (), {"id": "Q1", "question": "x"})()]
    st.plan = Plan()
    s1, _ = st.registry.add("https://shop.com/a")
    s2, _ = st.registry.add("https://inst.gouv.fr/b")
    s1.reliability, s2.reliability = "low", "high"
    for s in (s1, s2):
        st.add_claim(statement="c", sub_question_id="Q1", source_id=s.id, passage_ids=[f"{s.id}:p1"],
                     quote="", quote_verified=False, round_found=1)
    met = compute_metrics(st)["Q1"]
    assert met.reliable_domains == ["inst.gouv.fr"] and met.low_domains == ["shop.com"]
    assert met.claims == 2 and met.high_sources == 1


def test_partial_is_accepted_only_with_strong_evidence_after_a_targeted_round():
    strong = run({"Q1": m("Q1", ["a", "b", "c"], claims=6), "Q2": m("Q2", ["a", "b"])},
                 {"Q1": a("partial"), "Q2": a("covered")}, round_no=2)
    assert by_id(strong)["Q1"].covered and strong.sufficient
    weak = run({"Q1": m("Q1", ["a", "b"], claims=6), "Q2": m("Q2", ["a", "b"])},
               {"Q1": a("partial", ["mécanisme adénosine récepteurs"]), "Q2": a("covered")}, round_no=2)
    assert not by_id(weak)["Q1"].covered
    assert weak.next_queries == ["mécanisme adénosine récepteurs"]


def test_partial_on_first_round_always_triggers_a_targeted_search():
    d = run({"Q1": m("Q1", ["a", "b", "c", "d"], claims=10), "Q2": m("Q2", ["a", "b"])},
            {"Q1": a("partial", ["impact chiffré 2030"]), "Q2": a("covered")}, round_no=1)
    assert not by_id(d)["Q1"].covered and not d.stop
    assert d.next_queries == ["impact chiffré 2030"]


def test_missing_sub_question_without_assessment_gets_fallback_query():
    d = run({"Q1": m("Q1", [], claims=0), "Q2": m("Q2", ["a", "b"])}, {"Q1": None, "Q2": a("covered")})
    v = by_id(d)["Q1"]
    assert v.llm_status == "missing" and not v.covered
    assert d.next_queries == ["mécanisme"]


def test_stagnation_stops_insufficient():
    d = run({"Q1": m("Q1", ["a"]), "Q2": m("Q2", ["a", "b"])}, {"Q1": a("partial", ["x y z"]), "Q2": a("covered")},
            round_no=2, read=0, claims=0)
    assert d.stop and not d.sufficient and "stagnation" in d.reason


def test_stagnation_not_checked_on_first_round():
    d = run({"Q1": m("Q1", ["a"]), "Q2": m("Q2", ["a", "b"])}, {"Q1": a("partial", ["x y z"]), "Q2": a("covered")},
            round_no=1, read=0, claims=0)
    assert not d.stop


def test_max_rounds_stops_insufficient():
    d = run({"Q1": m("Q1", ["a"]), "Q2": m("Q2", ["a", "b"])}, {"Q1": a("partial", ["x y z"]), "Q2": a("covered")},
            round_no=3)
    assert d.stop and not d.sufficient and "nombre maximal de tours" in d.reason


def test_queries_already_run_are_not_relaunched():
    d = run({"Q1": m("Q1", ["a"]), "Q2": m("Q2", ["a", "b"])},
            {"Q1": a("partial", ["Déjà Faite", "nouvelle requête"]), "Q2": a("covered")},
            queries_run=["déjà faite"])
    assert d.next_queries == ["nouvelle requête"]


def test_no_new_query_available_stops():
    d = run({"Q1": m("Q1", ["a"]), "Q2": m("Q2", ["a", "b"])},
            {"Q1": a("partial", ["déjà faite"]), "Q2": a("covered")},
            queries_run=["déjà faite", "mécanisme"])
    assert d.stop and not d.sufficient and "plus aucune requête" in d.reason


def test_previously_covered_sub_question_stays_covered():
    d = run({"Q1": m("Q1", ["a"]), "Q2": m("Q2", ["a", "b"])}, {"Q2": a("covered")}, covered={"Q1"})
    assert by_id(d)["Q1"].covered and by_id(d)["Q1"].llm_status == "déjà couverte"
    assert d.sufficient


def test_too_few_sources_overall_continues_even_if_all_covered():
    d = run({"Q1": m("Q1", ["a", "b"]), "Q2": m("Q2", ["a", "b"])}, {"Q1": a("covered"), "Q2": a("covered")},
            reliable=2)
    assert not d.stop and "sources fiables lues" in d.reason
    assert len(d.next_queries) == 1


def test_queries_are_shared_between_uncovered_sub_questions():
    th = Thresholds(max_queries_per_round=3)
    d = run({"Q1": m("Q1", ["a"]), "Q2": m("Q2", ["a"])},
            {"Q1": a("partial", ["q1 a", "q1 b"]), "Q2": a("partial", ["q2 a", "q2 b"])}, th=th)
    assert d.next_queries == ["q1 a", "q2 a", "q1 b"]
    assert d.focus == ["Q1", "Q2"]
