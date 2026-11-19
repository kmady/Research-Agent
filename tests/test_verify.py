"""Tests de la vérification croisée (étape 6), avec des faux services."""

from dataclasses import replace

from research_agent.agent import ResearchAgent
from research_agent.config import Config
from research_agent.fetch import FetchResult
from research_agent.llm import LLMStats
from research_agent.schemas import ClaimVerdict, ResearchPlan, SubQuestion
from research_agent.search import SearchHit
from research_agent.state import Claim, ResearchState
from research_agent.verify import (
    Verifier, count_independent_origins, group_claims, heuristic_reliability, shorten_query,
)

# Faux embedder : chaque texte est projeté sur des « thèmes » repérés par mot-clé.
TOPICS = ["demi-vie", "adénosine", "mélatonine", "dépêche", "fusion"]


class TopicEmbedder:
    """Même thème, textes différents → similarité ≈ 0,92 (au-dessus de 0,91, sous le seuil de reprise 0,95).
    Textes identiques → 1,0."""
    BUCKETS = 512

    def _vec(self, text):
        import hashlib
        t = text.lower()
        topics = [1.0 if k in t else 0.0 for k in TOPICS]
        if not any(topics):
            topics[-1] = 0.01
        own = [0.0] * self.BUCKETS
        own[int(hashlib.md5(text.encode()).hexdigest(), 16) % self.BUCKETS] = 0.3
        return topics + own

    def embed_query(self, text):
        return self._vec(text)

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]


def claim(cid, sq, sid, text="x", pids=None):
    return Claim(id=cid, statement=text, sub_question_id=sq, source_id=sid, passage_ids=pids or [f"{sid}:p1"],
                 quote="", quote_verified=False, round_found=1)


def make_state(sources):
    """sources : liste de (url, fiabilité, [textes des passages])."""
    st = ResearchState(question="Caféine et sommeil ?")
    st.plan = ResearchPlan(sub_questions=[SubQuestion(id="Q1", question="Durée ?"),
                                          SubQuestion(id="Q2", question="Mécanisme ?")], initial_queries=[])
    for url, rel, texts in sources:
        src, _ = st.registry.add(url, url)
        src.reliability = rel
        st.registry.set_passages(src.id, texts)
    return st


# --- Fonctions pures ---------------------------------------------------------

def test_group_claims_respects_sub_questions_and_threshold():
    claims = [claim("C1", "Q1", "S1"), claim("C2", "Q1", "S2"), claim("C3", "Q2", "S3"), claim("C4", "Q1", "S4")]
    vecs = [[1, 0], [0.99, 0.14], [1, 0], [0, 1]]
    groups = group_claims(claims, vecs, threshold=0.91)
    assert groups == [[0, 1], [2], [3]]   # C3 identique mais autre sous-question ; C4 différente


def test_group_claims_compares_to_first_member_to_avoid_chains():
    claims = [claim(f"C{i}", "Q1", f"S{i}") for i in range(3)]
    vecs = [[1, 0], [0.94, 0.34], [0.77, 0.64]]   # C0≈C1 (0,94), C1≈C2 (0,94), mais C0 vs C2 = 0,77
    assert group_claims(claims, vecs, threshold=0.91) == [[0, 1], [2]]


def test_syndicated_copies_count_as_one_origin():
    st = make_state([("https://lapresse.ca/a", "medium", ["Texte de la dépêche sur la demi-vie."]),
                     ("https://canadafrancais.com/b", "medium", ["Texte de la dépêche sur la demi-vie."]),
                     ("https://inserm.fr/c", "high", ["Étude sur l'adénosine."])])
    emb = TopicEmbedder()
    same = [claim("C1", "Q1", "S1"), claim("C2", "Q1", "S2")]
    assert count_independent_origins(same, st, emb.embed_documents, 0.95) == 1
    indep = [claim("C1", "Q1", "S1"), claim("C3", "Q1", "S3")]
    assert count_independent_origins(indep, st, emb.embed_documents, 0.95) == 2


def test_low_reliability_sources_are_not_origins():
    st = make_state([("https://shop.com/a", "low", ["demi-vie"]), ("https://blog.net/b", "low", ["fusion"])])
    members = [claim("C1", "Q1", "S1"), claim("C2", "Q1", "S2")]
    assert count_independent_origins(members, st, TopicEmbedder().embed_documents, 0.95) == 0


def test_apply_enforces_rules_on_model_verdict():
    shown = [type("P", (), {"id": "S2:p1"})(), type("P", (), {"id": "S3:p1"})()]

    def run(verdict, support, contra):
        c = claim("C1", "Q1", "S1")
        v = ClaimVerdict(verdict=verdict, supporting_passage_ids=support, contradicting_passage_ids=contra, note="n")
        return Verifier._apply([c], v, shown, "corpus", None)["status"], c

    assert run("confirmed", [], [])[0] == "unverifiable"              # confirmée sans appui : refusé
    assert run("confirmed", ["S9:p9"], [])[0] == "unverifiable"       # identifiant inventé : ignoré
    assert run("contested", ["S2:p1"], [])[0] == "confirmed"          # « contestée » sans contradiction
    assert run("confirmed", ["S2:p1"], ["S3:p1"])[0] == "contested"   # appuis et contradictions → divergence
    status, c = run("confirmed", ["S2:p1", "S2:p1"], [])
    assert status == "confirmed" and c.supporting_passage_ids == ["S2:p1"]


def test_heuristic_reliability_and_query_shortening():
    assert heuristic_reliability("sante.gouv.fr") == ("official", "high")
    assert heuristic_reliability("univ-lyon1.fr") == ("academic", "high")
    assert heuristic_reliability("exemple.com") == ("other", "medium")
    assert heuristic_reliability("lemonde.fr") == ("press", "medium")
    assert shorten_query("La demi-vie (environ 5 h) est « longue ».", max_words=4) == "La demi-vie environ 5"


# --- Vérification complète avec faux services --------------------------------

class VerdictLLM:
    def __init__(self, verdict_fn):
        self.stats = LLMStats()
        self.prompts = []
        self.verdict_fn = verdict_fn

    def structured(self, messages, schema, think=False):
        from research_agent.schemas import DivergenceReport
        if schema is DivergenceReport:
            return DivergenceReport(divergences=[])
        assert schema is ClaimVerdict, schema
        self.prompts.append(messages[-1]["content"])
        return self.verdict_fn(messages[-1]["content"])


class FakeSearch:
    def __init__(self, urls):
        self.urls, self.queries = urls, []

    def search(self, query, n=8):
        self.queries.append(query)
        return [SearchHit(url=u, title=u, snippet="s") for u in self.urls]


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url):
        text = self.pages.get(url)
        return FetchResult(url=url, ok=bool(text), text=text or "", error="" if text else "HTTP 404")


def make_verifier(llm, search_urls=(), pages=None, **cfg):
    cfg.setdefault("min_hit_relevance", 0.0)   # filtre de pertinence testé dans test_agent.py
    config = replace(Config(), passage_tokens=1000, **cfg)
    agent = ResearchAgent(config, llm, FakeSearch(list(search_urls)), FakeFetcher(pages or {}), TopicEmbedder())
    return Verifier(agent), agent


def confirm_first(prompt):
    first = prompt.split("[", 1)[1].split("]", 1)[0]
    return ClaimVerdict(verdict="confirmed", supporting_passage_ids=[first], contradicting_passage_ids=[], note="ok")


def test_corroborated_groups_skip_the_model_and_fragile_ones_use_the_corpus():
    st = make_state([("https://a.org/1", "high", ["La demi-vie de la caféine est de 5 h."]),
                     ("https://b.org/1", "high", ["Demi-vie : cinq heures environ, comme pour la fusion des données."]),
                     ("https://c.org/1", "high", ["L'adénosine s'accumule pendant la journée."]),
                     ("https://d.org/1", "medium", ["Les récepteurs de l'adénosine sont bloqués."]),
                     ("https://e.org/1", "medium", ["Rôle de l'adénosine dans le sommeil."])])
    st.claims = [claim("C1", "Q1", "S1", "La demi-vie est de 5 h."),
                 claim("C2", "Q1", "S2", "La demi-vie est d'environ cinq heures."),
                 claim("C3", "Q2", "S3", "L'adénosine favorise le sommeil.")]
    llm = VerdictLLM(confirm_first)
    verifier, agent = make_verifier(llm)
    verifier.run(st)

    c1, c2, c3 = st.claims
    assert c1.group_id == c2.group_id != c3.group_id
    assert c1.status == c2.status == "corroborated" and c1.independent_origins == 2
    # C3 n'a qu'une origine : vérifiée avec des passages d'autres domaines du corpus, sans recherche web.
    assert c3.status == "confirmed" and c3.supporting_passage_ids[0] in ("S4:p1", "S5:p1")
    assert len(llm.prompts) == 1 and agent.search.queries == []
    assert "S3:p1" not in llm.prompts[0]                         # la source de l'affirmation n'est pas une preuve


def test_fragile_claim_without_corpus_evidence_is_searched_on_the_web():
    st = make_state([("https://a.org/1", "high", ["La mélatonine est produite la nuit."])])
    st.claims = [claim("C1", "Q1", "S1", "La mélatonine est produite la nuit.")]
    llm = VerdictLLM(confirm_first)
    verifier, agent = make_verifier(
        llm, search_urls=["https://a.org/2", "https://sante.gouv.fr/x", "https://www.facebook.com/p"],
        pages={"https://sante.gouv.fr/x": "La mélatonine, hormone du sommeil, est sécrétée la nuit."})
    verifier.run(st)
    c = st.claims[0]
    assert agent.search.queries == ["La mélatonine est produite la nuit"]
    new = st.registry.find_by_url("https://sante.gouv.fr/x")
    assert new.fetched and new.reliability == "high"              # appréciée par l'heuristique de domaine
    assert st.registry.find_by_url("https://a.org/2").fetched is False   # même domaine que l'affirmation
    assert st.registry.find_by_url("https://www.facebook.com/p") is None   # domaine exclu
    assert c.status == "confirmed" and c.supporting_passage_ids == [f"{new.id}:p1"]


def test_no_evidence_anywhere_gives_unverifiable_without_calling_the_model():
    st = make_state([("https://a.org/1", "high", ["La mélatonine est produite la nuit."])])
    st.claims = [claim("C1", "Q1", "S1", "La mélatonine est produite la nuit.")]
    llm = VerdictLLM(confirm_first)
    verifier, _ = make_verifier(llm, search_urls=[])
    verifier.run(st)
    assert st.claims[0].status == "unverifiable" and llm.prompts == []


def test_budget_limits_verifications_and_numbers_go_first():
    st = make_state([("https://a.org/1", "high", ["texte source"]), ("https://b.org/1", "high", ["fusion"])])
    st.claims = [claim("C1", "Q1", "S1", "La mélatonine agit."),          # pas de chiffre
                 claim("C2", "Q2", "S1", "La fusion produit 4 fois plus d'énergie."),   # chiffré
                 claim("C3", "Q2", "S1", "L'adénosine s'accumule.")]
    llm = VerdictLLM(confirm_first)
    verifier, _ = make_verifier(llm, max_claims_to_verify=1)
    verifier.run(st)
    statuses = {c.id: c.status for c in st.claims}
    assert statuses["C2"] == "confirmed"
    assert statuses["C1"] == statuses["C3"] == "unchecked"
    log = next(t for t in st.trace if t["step"] == "verification")
    assert sorted(log["unchecked"]) == ["C1", "C3"]


def test_compatible_blocks_merges_that_embeddings_cannot_see():
    from research_agent.verify import compatible
    # Cas réel (similarité 0,94) : « probable » ≠ « certain ».
    assert not compatible("L'OMS a classé le glyphosate comme « probablement cancérogène pour l'humain ».",
                          "Le glyphosate est classé comme substance cancérogène pour l'homme par l'OMS.")
    assert compatible("L'OMS a classé le glyphosate comme « probablement cancérogène pour l'humain ».",
                      "L'OMS estime que le glyphosate est un cancérogène probable.")
    assert not compatible("La demi-vie est de 5 heures.", "La demi-vie est de 15 heures.")
    assert compatible("La demi-vie est de 5 heures.", "Demi-vie : 5 h environ.")
    assert compatible("La demi-vie est de 5,5 heures.", "La demi-vie est d'environ 5.5 heures.")
    assert not compatible("L'Efsa n'a pas identifié de risque.", "L'Efsa a identifié un risque.")


def test_incompatible_claims_are_not_grouped_even_if_similar():
    claims = [claim("C1", "Q1", "S1", "Le glyphosate est probablement cancérogène."),
              claim("C2", "Q1", "S2", "Le glyphosate est cancérogène.")]
    assert group_claims(claims, [[1, 0], [1, 0]], threshold=0.91) == [[0], [1]]


def test_reliable_single_origin_claims_are_verified_before_low_reliability_ones():
    st = make_state([("https://blog.net/a", "low", ["texte un"]), ("https://inst.org/b", "high", ["texte deux"]),
                     ("https://c.org/c", "medium", ["fusion"])])
    st.claims = [claim("C1", "Q1", "S1", "La mélatonine agit."), claim("C2", "Q1", "S2", "La fusion est propre.")]
    llm = VerdictLLM(confirm_first)
    verifier, _ = make_verifier(llm, max_claims_to_verify=1)
    verifier.run(st)
    assert st.claims[1].status == "confirmed" and st.claims[0].status == "unchecked"


# --- Divergences entre sources -----------------------------------------------

class DivergenceLLM(VerdictLLM):
    def __init__(self, report):
        super().__init__(confirm_first)
        self.report = report

    def structured(self, messages, schema, think=False):
        from research_agent.schemas import DivergenceReport
        if schema is DivergenceReport:
            self.prompts.append(messages[-1]["content"])
            return self.report
        return super().structured(messages, schema, think)


def test_divergences_are_validated_by_the_code():
    from research_agent.schemas import Divergence, DivergenceReport
    st = make_state([("https://iarc.who.int/a", "high", ["Le CIRC classe le glyphosate en 2A."]),
                     ("https://bfr.bund.de/b", "high", ["Le BfR juge un risque peu probable."]),
                     ("https://blog.net/c", "low", ["Le glyphosate est un poison."])])
    st.claims = [claim("C1", "Q1", "S1", "Le CIRC juge le glyphosate cancérogène probable."),
                 claim("C2", "Q1", "S2", "Le BfR juge la cancérogénicité peu probable."),
                 claim("C3", "Q1", "S1", "Le CIRC a publié en 2015."),
                 claim("C4", "Q1", "S3", "Le glyphosate est un poison.")]
    report = DivergenceReport(divergences=[
        Divergence(point="Cancérogénicité", side_a_claim_ids=["C1", "C99"], side_b_claim_ids=["C2"], explanation="e"),
        Divergence(point="Même source", side_a_claim_ids=["C1"], side_b_claim_ids=["C3"], explanation="e"),
        Divergence(point="Côté vide", side_a_claim_ids=["C1"], side_b_claim_ids=["C42"], explanation="e"),
    ])
    llm = DivergenceLLM(report)
    verifier, _ = make_verifier(llm)
    found = verifier.find_divergences(st)
    assert found == [{"sub_question_id": "Q1", "point": "Cancérogénicité", "side_a": ["C1"], "side_b": ["C2"],
                      "explanation": "e"}]
    assert "C4" not in llm.prompts[0]   # les sources peu fiables ne sont pas comparées


def test_no_comparison_when_a_single_domain():
    st = make_state([("https://a.org/1", "high", ["x"])])
    st.claims = [claim("C1", "Q1", "S1", "a"), claim("C2", "Q1", "S1", "b")]
    llm = DivergenceLLM(None)
    verifier, _ = make_verifier(llm)
    assert verifier.find_divergences(st) == [] and llm.prompts == []


def test_diverse_sample_alternates_domains_high_reliability_first():
    st = make_state([("https://a.org/1", "medium", ["x"]), ("https://b.gouv.fr/1", "high", ["y"])])
    st.claims = [claim(f"C{i}", "Q1", "S1") for i in range(1, 6)] + [claim("C6", "Q1", "S2"), claim("C7", "Q1", "S2")]
    sample = Verifier._diverse_sample(st.claims, st, 4)
    assert [c.id for c in sample] == ["C1", "C2", "C6", "C7"]


def test_same_opposition_is_reported_once_across_sub_questions():
    from research_agent.schemas import Divergence, DivergenceReport
    st = make_state([("https://iarc.who.int/a", "high", ["x"]), ("https://epa.gov/b", "high", ["y"]),
                     ("https://lemonde.fr/c", "medium", ["z"])])
    st.claims = [claim("C1", "Q1", "S1", "CIRC : probable."), claim("C2", "Q1", "S2", "EPA : pas de risque."),
                 claim("C3", "Q2", "S1", "Classé 2A."), claim("C4", "Q2", "S2", "Aucun risque."),
                 claim("C5", "Q2", "S3", "Étude : +41 % de lymphome.")]
    reports = iter([
        DivergenceReport(divergences=[Divergence(point="A", side_a_claim_ids=["C1"], side_b_claim_ids=["C2"],
                                                 explanation="e")]),
        DivergenceReport(divergences=[
            Divergence(point="A bis", side_a_claim_ids=["C4"], side_b_claim_ids=["C3"], explanation="e"),  # inversée
            Divergence(point="B", side_a_claim_ids=["C5"], side_b_claim_ids=["C4"], explanation="e")]),
    ])

    class SeqLLM(DivergenceLLM):
        def structured(self, messages, schema, think=False):
            return next(reports)

    verifier, _ = make_verifier(SeqLLM(None))
    found = verifier.find_divergences(st)
    assert [d["point"] for d in found] == ["A", "B"]
