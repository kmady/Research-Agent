"""Tests de l'orchestrateur avec de faux services : aucun réseau, aucun modèle."""

from dataclasses import replace

from research_agent.agent import ResearchAgent, quote_in_passages
from research_agent.config import Config
from research_agent.fetch import FetchResult
from research_agent.llm import ContextOverflowError, LLMError, LLMStats
from research_agent.report import render_raw_report, slugify, state_to_dict
from research_agent.schemas import (
    ExtractedClaim, Extraction, ResearchPlan, SelectedSource, SubQuestion, SubQuestionAssessment, UrlSelection,
)
from research_agent.search import SearchHit
from research_agent.state import Passage

PAGE = "\n".join(f"Paragraphe {i} sur la caféine et le sommeil profond, avec des détails utiles." for i in range(12))


class FakeLLM:
    def __init__(self, extraction_factory=None, selection=None, overflow_once=False, assessment=None):
        self.stats = LLMStats()
        self.assessment = assessment or (lambda prompt: SubQuestionAssessment(
            status="covered", rationale="ok", missing_aspects=[], follow_up_queries=[]))
        self.calls = []
        self.extraction_factory = extraction_factory
        self.selection = selection
        self.overflow_once = overflow_once

    def structured(self, messages, schema, think=False):
        self.calls.append(schema.__name__)
        if schema is ResearchPlan:
            return ResearchPlan(
                sub_questions=[SubQuestion(id="A", question="Mécanisme ?"), SubQuestion(id="B", question="Durée ?")],
                initial_queries=["caféine sommeil ?", "caféine demi-vie"])
        if schema is UrlSelection:
            if self.selection is None:
                raise LLMError("pas de sélection")
            return self.selection
        if schema is Extraction:
            if self.overflow_once:
                self.overflow_once = False
                raise ContextOverflowError("trop long")
            return self.extraction_factory(messages[-1]["content"])
        if schema is SubQuestionAssessment:
            return self.assessment(messages[-1]["content"])
        raise AssertionError(schema)


class FakeSearch:
    def __init__(self, urls):
        self.urls = urls

    def search(self, query, n=8):
        return [SearchHit(url=u, title=f"Titre {u}", snippet="extrait") for u in self.urls][:n]


class FakeFetcher:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        if url in self.failing:
            return FetchResult(url=url, ok=False, error="HTTP 403")
        return FetchResult(url=url, ok=True, final_url=url, title="", date="2025-01-02", text=PAGE)


class FakeEmbedder:
    def embed_query(self, text):
        return [1.0, 0.0]

    def embed_documents(self, texts):
        return [[1.0, float(i)] for i, _ in enumerate(texts)]  # les premiers passages sont les plus proches


def source_id_in(prompt: str) -> str:
    return prompt.split("Source : [", 1)[1].split("]", 1)[0]


def good_extraction(prompt: str) -> Extraction:
    sid = source_id_in(prompt)
    return Extraction(
        claims=[
            ExtractedClaim(statement="La caféine réduit le sommeil profond.", sub_question_id="Q1",
                           passage_ids=[f"{sid}:p1", f"{sid}:p99"], quote="Paragraphe 0 sur la caféine"),
            ExtractedClaim(statement="Affirmation sur une sous-question inconnue.", sub_question_id="Q9",
                           passage_ids=[f"{sid}:p1"], quote=""),
            ExtractedClaim(statement="Affirmation sans passage valide.", sub_question_id="Q2",
                           passage_ids=["S99:p1"], quote=""),
            ExtractedClaim(statement="Citation inventée.", sub_question_id="Q2",
                           passage_ids=[f"{sid}:p1"], quote="phrase absente du texte"),
        ],
        source_kind="academic", reliability="high", publication_date="")


def make_agent(urls, llm, failing=(), **cfg_overrides):
    cfg_overrides.setdefault("max_rounds", 1)   # un seul tour, sauf pour les tests de la boucle
    cfg_overrides.setdefault("urls_to_read_per_round", 3)
    cfg_overrides.setdefault("verify", False)   # la vérification a ses propres tests (test_verify.py)
    cfg_overrides.setdefault("synthesize", False)   # idem (test_synthesis.py)
    cfg_overrides.setdefault("min_hit_relevance", 0.0)   # filtre testé à part (faux embeddings arbitraires)
    cfg = replace(Config(), passage_tokens=20, passages_per_call=3, **cfg_overrides)
    return ResearchAgent(cfg, llm, FakeSearch(urls), FakeFetcher(failing), FakeEmbedder())


URLS = [f"https://site{i}.org/page" for i in range(5)]


def test_full_round_validates_claims_produced_by_the_model():
    agent = make_agent(URLS, FakeLLM(good_extraction, selection=UrlSelection(selected=[
        SelectedSource(source_id="S3", reason="r"), SelectedSource(source_id="S42", reason="inexistante"),
        SelectedSource(source_id="S1", reason="r"), SelectedSource(source_id="S3", reason="doublon")])))
    state = agent.run("Effets de la caféine sur le sommeil ?")

    # Le plan est renuméroté par le code, les « ? » retirés des requêtes.
    assert state.sub_question_ids() == ["Q1", "Q2"]
    assert state.queries_run == ["caféine sommeil", "caféine demi-vie"]
    # Sources dédupliquées entre les deux requêtes ; sélection filtrée (S42 inexistante, doublon S3).
    assert len(state.registry) == 5
    round_log = next(t for t in state.trace if t["step"] == "round")
    assert round_log["read"] == ["S3", "S1"]   # ordre de priorité choisi par le modèle
    # Par source : 1 affirmation valide (p99 retiré) + 1 avec citation inventée (gardée, citation effacée).
    assert len(state.claims) == 4
    c = state.claims[0]
    assert c.passage_ids == [f"{c.source_id}:p1"] and c.quote_verified
    invented = [x for x in state.claims if x.statement == "Citation inventée."]
    assert all(not x.quote_verified and x.quote == "" for x in invented)
    # Appréciation de la source et date issue de la page.
    s3 = state.registry.get("S3")
    assert (s3.kind, s3.reliability, s3.publication_date) == ("academic", "high", "2025-01-02")
    # Les rejets sont tracés.
    ext_logs = [t for t in state.trace if t["step"] == "extraction"]
    assert len(ext_logs[0]["rejected"]) == 2


def test_selection_failure_falls_back_to_search_order():
    agent = make_agent(URLS, FakeLLM(good_extraction, selection=None))
    state = agent.run("q")
    assert [s.id for s in state.registry.all() if s.fetched] == ["S1", "S2", "S3"]


def test_failed_fetch_is_recorded_and_round_continues():
    agent = make_agent(URLS, FakeLLM(good_extraction, selection=None), failing={URLS[1]})
    state = agent.run("q")
    assert state.registry.get("S2").fetch_error == "HTTP 403"
    assert [s.id for s in state.registry.all() if s.fetched] == ["S1", "S3"]


def test_selection_is_skipped_when_few_candidates():
    llm = FakeLLM(good_extraction, selection=None)
    agent = make_agent(URLS[:2], llm)
    agent.run("q")
    assert "UrlSelection" not in llm.calls


def test_context_overflow_retries_with_fewer_passages():
    llm = FakeLLM(good_extraction, selection=None, overflow_once=True)
    agent = make_agent(URLS[:1], llm)
    state = agent.run("q")
    assert llm.calls.count("Extraction") == 2
    assert state.claims


def test_quote_matching_tolerates_spacing_case_and_quotes():
    p = [Passage(id="S1:p1", source_id="S1", index=1, text="L’adénosine   s'accumule\npendant la journée.")]
    assert quote_in_passages("l'adénosine s'accumule pendant", p)
    assert quote_in_passages("« L’adénosine s'accumule »", p)
    assert not quote_in_passages("la mélatonine", p)
    assert not quote_in_passages("", p)


def test_report_and_export():
    agent = make_agent(URLS, FakeLLM(good_extraction, selection=None), failing={URLS[1]})
    state = agent.run("Effets de la caféine ?")
    md = render_raw_report(state, {"model": "m", "seconds": 3, "llm": agent.llm.stats.as_dict()})
    assert "### Q1 — Mécanisme ?" in md
    assert "La caféine réduit le sommeil profond. [S1]" in md
    assert "échec : HTTP 403" in md
    exported = state_to_dict(state)
    s1 = next(s for s in exported["sources"] if s["id"] == "S1")
    assert [p["id"] for p in s1["passages"]] == ["S1:p1"]   # seuls les passages cités gardent leur texte
    assert slugify("Effets de la caféine ?") == "effets-de-la-cafeine"


def test_blocked_domains_never_enter_the_registry():
    urls = ["https://www.facebook.com/post/1", "https://m.youtube.com/watch?v=1", "https://ok.org/a"]
    agent = make_agent(urls, FakeLLM(good_extraction, selection=None))
    state = agent.run("q")
    assert [s.domain for s in state.registry.all()] == ["ok.org"]


def test_company_and_blog_sources_are_forced_to_low_reliability():
    def company_extraction(prompt):
        ext = good_extraction(prompt)
        return ext.model_copy(update={"source_kind": "company", "reliability": "medium"})

    agent = make_agent(URLS[:1], FakeLLM(company_extraction, selection=None))
    state = agent.run("q")
    assert state.registry.get("S1").reliability == "low"


def test_clean_query():
    from research_agent.agent import clean_query
    assert clean_query("decline bee population causes english") == "decline bee population causes"
    assert clean_query("caffeine sleep (in English)") == "caffeine sleep"
    assert clean_query("  effets caféine ?  ") == "effets caféine"
    assert clean_query("english grammar rules") == "english grammar rules"   # seulement en fin de requête


# --- Boucle agentique --------------------------------------------------------

class SearchByQuery:
    """Chaque requête renvoie ses propres URLs : permet de simuler un tour complémentaire utile."""
    def __init__(self, mapping, default=()):
        self.mapping, self.default, self.queries = mapping, list(default), []

    def search(self, query, n=8):
        self.queries.append(query)
        return [SearchHit(url=u, title=u, snippet="s") for u in self.mapping.get(query, self.default)][:n]


def q1_only_extraction(prompt):
    sid = source_id_in(prompt)
    return Extraction(claims=[ExtractedClaim(statement=f"Fait de {sid}.", sub_question_id="Q1",
                                             passage_ids=[f"{sid}:p1"], quote="")],
                      source_kind="official", reliability="high", publication_date="")


def test_loop_runs_a_targeted_round_then_stops_when_sufficient():
    # Tour 1 : 2 sources → Q1 couverte (2 domaines), Q2 sans aucune affirmation.
    # Le modèle propose une requête pour Q2 ; le tour 2 trouve 2 nouvelles sources qui couvrent Q2.
    def extraction(prompt):
        sid = source_id_in(prompt)
        sq = "Q1" if sid in ("S1", "S2") else "Q2"
        return Extraction(claims=[ExtractedClaim(statement=f"Fait {sq} de {sid}.", sub_question_id=sq,
                                                 passage_ids=[f"{sid}:p1"], quote="")],
                          source_kind="official", reliability="high", publication_date="")

    search = SearchByQuery({"caféine sommeil": ["https://a.org/1", "https://b.org/1"],
                            "durée": ["https://c.org/1", "https://d.org/1"]})
    llm = FakeLLM(extraction, selection=None)
    cfg = replace(Config(), urls_to_read_per_round=3, passage_tokens=20, passages_per_call=3, verify=False, synthesize=False, min_hit_relevance=0.0, max_rounds=3)
    agent = ResearchAgent(cfg, llm, search, FakeFetcher(), FakeEmbedder())
    state = agent.run("Effets ?")

    assert len(state.decisions) == 2
    first, second = state.decisions
    assert not first["stop"] and first["focus"] == ["Q2"]
    assert first["next_queries"] == ["durée"]                 # repli : mots-clés de Q2 (« Durée ? »)
    assert second["stop"] and second["sufficient"]
    assert state.sufficient and "suffisant" in state.stop_reason
    assert state.covered == {"Q1", "Q2"}
    # Q1, couverte dès le tour 1, n'est pas réévaluée au tour 2.
    assert llm.calls.count("SubQuestionAssessment") == 2
    round2 = [t for t in state.trace if t["step"] == "round"][1]
    assert round2["focus"] == ["Q2"] and round2["read"] == ["S3", "S4"]


def test_loop_stops_on_stagnation_and_reports_insufficient():
    llm = FakeLLM(q1_only_extraction, selection=None)
    search = SearchByQuery({}, default=["https://a.org/1"])  # toujours la même source
    cfg = replace(Config(), urls_to_read_per_round=3, passage_tokens=20, passages_per_call=3, verify=False, synthesize=False, min_hit_relevance=0.0, max_rounds=5)
    agent = ResearchAgent(cfg, llm, search, FakeFetcher(), FakeEmbedder())
    state = agent.run("q")
    assert not state.sufficient
    assert "stagnation" in state.stop_reason
    assert len(state.decisions) == 2


def test_loop_respects_max_rounds():
    counter = {"n": 0}

    def fresh_urls(query, n=8):
        counter["n"] += 1
        return [SearchHit(url=f"https://site{counter['n']}.org/x", title="t", snippet="s")]

    search = SearchByQuery({})
    search.search = fresh_urls
    # Le modèle juge toujours « partial » avec une nouvelle requête : seule la limite de tours arrête la boucle.
    calls = {"n": 0}

    def never_satisfied(prompt):
        calls["n"] += 1
        return SubQuestionAssessment(status="partial", rationale="manque", missing_aspects=["x"],
                                     follow_up_queries=[f"requête {calls['n']}"])

    def low_quality_extraction(prompt):
        # Sources peu fiables, affirmations sur les deux sous-questions : jamais de corroboration suffisante.
        sid = source_id_in(prompt)
        return Extraction(claims=[ExtractedClaim(statement=f"Fait {sq} de {sid}.", sub_question_id=sq,
                                                 passage_ids=[f"{sid}:p1"], quote="") for sq in ("Q1", "Q2")],
                          source_kind="blog", reliability="medium", publication_date="")

    llm = FakeLLM(low_quality_extraction, selection=None, assessment=never_satisfied)
    cfg = replace(Config(), urls_to_read_per_round=3, passage_tokens=20, passages_per_call=3, verify=False, synthesize=False, min_hit_relevance=0.0, max_rounds=3)
    state = ResearchAgent(cfg, llm, search, FakeFetcher(), FakeEmbedder()).run("q")
    assert len(state.decisions) == 3
    assert not state.sufficient and "nombre maximal de tours atteint (3)" in state.stop_reason


def test_at_most_two_pages_per_domain_are_read():
    urls = [f"https://gouv.fr/page{i}" for i in range(4)] + ["https://autre.org/a"]
    agent = make_agent(urls, FakeLLM(good_extraction, selection=None), urls_to_read_per_round=5)
    state = agent.run("q")
    read = [s.domain for s in state.registry.all() if s.fetched]
    assert read.count("gouv.fr") == 2 and "autre.org" in read


def test_source_kind_is_corrected_by_domain():
    from research_agent.agent_utils import correct_source_kind
    assert correct_source_kind("fr.euronews.com", "academic", "high") == ("press", "medium")
    assert correct_source_kind("lemonde.fr", "press", "medium") == ("press", "medium")
    assert correct_source_kind("bretagne.developpement-durable.gouv.fr", "other", "medium") == ("official", "high")
    assert correct_source_kind("sante.gouv.qc.ca", "blog", "low") == ("official", "high")
    assert correct_source_kind("univ-lyon1.fr", "other", "medium") == ("academic", "high")
    assert correct_source_kind("fun.org", "blog", "low") == ("blog", "low")      # « un.org » ne correspond pas
    assert correct_source_kind("ouraring.com", "company", "low") == ("company", "low")


def test_queries_are_capped_to_ten_words():
    from research_agent.agent_utils import clean_query
    long_q = "Retours qualitatifs des agents sur la semaine de 4 jours en France métropolitaine"
    assert clean_query(long_q) == "Retours qualitatifs des agents sur la semaine de 4 jours"


def test_source_found_again_counts_as_recent():
    from research_agent.state import SourceRegistry
    reg = SourceRegistry()
    src, _ = reg.add("https://a.org/x", round_no=1)
    reg.add("https://a.org/x", round_no=2)
    assert (src.round_found, src.last_seen_round) == (1, 2)


def test_search_outage_stops_with_an_honest_reason():
    from research_agent.search import SearchError

    class DownSearch:
        def search(self, query, n=8):
            raise SearchError("moteurs de recherche indisponibles : duckduckgo (CAPTCHA)")

    llm = FakeLLM(good_extraction, selection=None)
    cfg = replace(Config(), verify=False, synthesize=False, min_hit_relevance=0.0, max_rounds=3)
    state = ResearchAgent(cfg, llm, DownSearch(), FakeFetcher(), FakeEmbedder()).run("q")
    assert not state.sufficient and "moteur de recherche indisponible" in state.stop_reason
    assert state.queries_run == []          # les requêtes en échec pourront être relancées


def test_off_topic_search_hits_are_filtered_by_relevance():
    class TopicEmbedder:
        def embed_query(self, text):
            return [1.0, 0.0]

        def embed_documents(self, texts):
            return [[1.0, 0.0] if "caféine" in t.lower() else [0.0, 1.0] for t in texts]

    class Search:
        def search(self, query, n=8):
            return [SearchHit(url="https://speedtest.net/", title="Speedtest by Ookla", snippet="débit internet"),
                    SearchHit(url="https://inserm.fr/cafeine", title="Caféine et sommeil", snippet="étude")]

    cfg = replace(Config(), verify=False, synthesize=False, max_rounds=1, passage_tokens=20, passages_per_call=3)
    state = ResearchAgent(cfg, FakeLLM(good_extraction, selection=None), Search(), FakeFetcher(), TopicEmbedder()).run("q")
    assert [s.domain for s in state.registry.all()] == ["inserm.fr"]


def test_empty_model_selection_is_respected():
    agent = make_agent(URLS, FakeLLM(good_extraction, selection=UrlSelection(selected=[])))
    state = agent.run("q")
    assert not any(s.fetched for s in state.registry.all())


def test_keywords_query():
    from research_agent.agent_utils import keywords_query
    assert keywords_query("Quels sont les résultats des expérimentations de la semaine de quatre jours ?") == \
        "résultats expérimentations semaine quatre jours"
    assert keywords_query("What are the key challenges of nuclear fission?") == "key challenges nuclear fission"
    assert keywords_query("Comment l'adénosine agit-elle sur le sommeil ?") == "adénosine agit-elle sommeil"
