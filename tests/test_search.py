import httpx
import pytest

from research_agent.cache import DiskCache
from research_agent.search import SearchError, SearxngProvider

PAYLOAD = {
    "results": [
        {"url": "https://a.org/x", "title": " A ", "content": "extrait A", "engines": ["ddg"]},
        {"url": "https://www.a.org/x/?utm_source=1", "title": "doublon", "content": ""},
        {"url": "ftp://bad.org/f", "title": "pas http"},
        {"url": "http:///go/v1/AS8HVepn86", "title": "redirection sans hôte"},
        {"title": "sans url"},
        {"url": "https://b.org/y", "title": "B", "content": "extrait B", "publishedDate": "2024-02-01"},
        {"url": "https://c.org/z", "title": "C"},
    ]
}


def make_provider(handler, cache=None):
    return SearxngProvider(
        "http://searx.test",
        cache=cache,
        min_interval_s=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_search_parses_filters_and_deduplicates():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=PAYLOAD)

    hits = make_provider(handler).search("photosynthèse", n=10)
    assert [h.url for h in hits] == ["https://a.org/x", "https://b.org/y", "https://c.org/z"]
    assert hits[0].title == "A" and hits[0].engines == ["ddg"]
    assert hits[1].published_date == "2024-02-01"
    assert calls[0].url.params["format"] == "json"


def test_search_respects_n():
    provider = make_provider(lambda r: httpx.Response(200, json=PAYLOAD))
    assert len(provider.search("q", n=2)) == 2


def test_search_uses_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=PAYLOAD)

    cache = DiskCache(tmp_path, "search")
    provider = make_provider(handler, cache)
    provider.search("q")
    provider.search("q")
    assert len(calls) == 1


def test_empty_results_with_failing_engines_is_an_outage_not_an_empty_answer():
    payload = {"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["brave", "timeout"]]}
    provider = make_provider(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(SearchError, match="duckduckgo \\(CAPTCHA\\)"):
        provider.search("q")
    empty = make_provider(lambda r: httpx.Response(200, json={"results": [], "unresponsive_engines": []}))
    assert empty.search("q") == []  # vraiment rien trouvé : pas une erreur


def test_search_raises_search_error_on_http_failure():
    provider = make_provider(lambda r: httpx.Response(500))
    with pytest.raises(SearchError):
        provider.search("q")


def test_requests_are_spaced_but_cache_hits_are_not(tmp_path, monkeypatch):
    import research_agent.search as search_mod

    sleeps = []
    monkeypatch.setattr(search_mod.time, "sleep", lambda s: sleeps.append(s))
    provider = SearxngProvider(
        "http://searx.test",
        min_interval_s=5,
        cache=DiskCache(tmp_path, "s"),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=PAYLOAD))),
    )
    provider.search("a")
    provider.search("b")  # requête réelle juste après : attente
    provider.search("a")  # en cache : aucune attente
    assert len(sleeps) == 1 and sleeps[0] > 4


OUTAGE = {"results": [], "unresponsive_engines": [["google cse", "too many requests"]]}
SCIENCE = {"results": [{"url": "https://arxiv.org/abs/1", "title": "Tropical geometry", "content": "x"}]}


def fallback_provider(responses, calls, cache=None):
    def handler(request):
        cat = request.url.params["categories"]
        calls.append(cat)
        return httpx.Response(200, json=responses[cat])

    return SearxngProvider(
        "http://searx.test",
        min_interval_s=0,
        categories="general",
        fallback_categories="science",
        cache=cache,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_fallback_categories_used_when_general_engines_are_blocked():
    calls = []
    hits = fallback_provider({"general": OUTAGE, "science": SCIENCE}, calls).search("géométrie tropicale")
    assert [h.url for h in hits] == ["https://arxiv.org/abs/1"]
    assert calls == ["general", "science"]


def test_fallback_not_queried_when_general_answers():
    calls = []
    hits = fallback_provider({"general": PAYLOAD, "science": SCIENCE}, calls).search("q")
    assert len(hits) == 3 and calls == ["general"]


def test_outage_still_reported_when_fallback_is_empty_too():
    calls = []
    provider = fallback_provider({"general": OUTAGE, "science": {"results": []}}, calls)
    with pytest.raises(SearchError, match="google cse"):
        provider.search("q")
    assert calls == ["general", "science"]


def test_fallback_results_cached_separately(tmp_path):
    calls = []
    provider = fallback_provider(
        {"general": OUTAGE, "science": SCIENCE}, calls, cache=DiskCache(tmp_path, "s")
    )
    provider.search("q")
    provider.search("q")
    assert calls == ["general", "science", "general"]  # la panne n'est pas mise en cache, le secours oui
