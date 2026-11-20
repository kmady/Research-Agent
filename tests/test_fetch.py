import httpx

from research_agent.cache import DiskCache
from research_agent.fetch import Fetcher, chunk_text, extract_html, rank_passages

ARTICLE = (
    "<html><head><title>Titre</title></head><body><article><h1>La photosynthèse</h1>"
    + "".join(
        f"<p>{f'Paragraphe numéro {i} qui explique un aspect du sujet en détail. ' * 4}</p>" for i in range(6)
    )
    + "</article></body></html>"
)


# --- Découpage ---------------------------------------------------------------


def test_chunks_respect_max_size_and_never_cut_words():
    words = [f"mot{i}" for i in range(2000)]
    text = " ".join(words)
    chunks = chunk_text(text, target_tokens=50, chars_per_token=4)
    assert all(len(c) <= 200 * 1.25 for c in chunks)
    # Recoller les passages redonne exactement la suite de mots : aucun mot coupé ni perdu.
    assert " ".join(chunks).split() == words


def test_chunks_group_short_paragraphs():
    text = "\n".join(["Un paragraphe court mais pas trop, avec du contenu utile."] * 10)
    chunks = chunk_text(text, target_tokens=100, chars_per_token=4)
    assert len(chunks) < 10
    assert all(len(c) <= 400 for c in chunks)


def test_long_paragraph_is_split_on_sentences_first():
    sentence = "Ceci est une phrase complète de longueur moyenne."
    text = " ".join([sentence] * 30)
    chunks = chunk_text(text, target_tokens=40, chars_per_token=4)
    assert len(chunks) > 1
    assert all(c.endswith(".") for c in chunks)


def test_tiny_trailing_fragment_is_merged_not_lost():
    body = " ".join(["mot"] * 75)  # 299 caractères → 2 passages
    chunks = chunk_text(body + "\nFin.", target_tokens=50, chars_per_token=4, min_chars=80)
    assert len(chunks) == 2
    assert chunks[-1].endswith("\nFin.")


def test_overlong_single_word_is_kept_whole():
    url = "https://exemple.org/" + "a" * 300
    assert chunk_text(url, target_tokens=50, chars_per_token=4) == [url]


def test_single_short_text_is_kept():
    assert chunk_text("Bonjour.") == ["Bonjour."]


# --- Classement --------------------------------------------------------------


class FakeEmbedder:
    """Embedding jouet : compte des mots-clés, suffisant pour tester le classement."""

    KEYS = ["plante", "soleil", "voiture"]

    def _vec(self, text):
        return [text.lower().count(k) + 0.01 for k in self.KEYS]

    def embed_query(self, text):
        return self._vec(text)

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]


def test_rank_passages_orders_by_similarity():
    passages = ["La voiture roule.", "La plante utilise le soleil.", "Le soleil brille."]
    ranked = rank_passages("plante soleil", passages, FakeEmbedder(), top_k=2)
    assert [i for i, _ in ranked] == [1, 2]
    assert ranked[0][1] > ranked[1][1]


def test_rank_passages_empty():
    assert rank_passages("q", [], FakeEmbedder()) == []


# --- Extraction et téléchargement -------------------------------------------


def test_extract_html_returns_main_text():
    title, _, text = extract_html(ARTICLE.encode(), "https://a.org/x")
    assert "Paragraphe numéro 3" in text
    assert title


def make_fetcher(routes, tmp_path=None, **kw):
    def handler(request):
        resp = routes.get(str(request.url))
        return resp() if callable(resp) else (resp or httpx.Response(404))

    cache = DiskCache(tmp_path, "pages") if tmp_path else None
    return Fetcher(
        user_agent="test", cache=cache, client=httpx.Client(transport=httpx.MockTransport(handler)), **kw
    )


def html_response():
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=ARTICLE.encode())


def test_fetch_success_and_cache(tmp_path):
    calls = {"n": 0}

    def page():
        calls["n"] += 1
        return html_response()

    fetcher = make_fetcher({"https://a.org/x": page}, tmp_path)
    r1 = fetcher.fetch("https://a.org/x")
    r2 = fetcher.fetch("https://a.org/x")
    assert r1.ok and "Paragraphe" in r1.text
    assert r2.ok and calls["n"] == 1


def test_fetch_http_error_is_reported_not_raised():
    fetcher = make_fetcher({"https://a.org/x": httpx.Response(403)})
    r = fetcher.fetch("https://a.org/x")
    assert not r.ok and r.error == "HTTP 403"


def test_fetch_respects_robots_txt():
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /prive/\n")
    fetcher = make_fetcher(
        {
            "https://a.org/robots.txt": robots,
            "https://a.org/prive/x": html_response,
            "https://a.org/public": html_response,
        }
    )
    assert fetcher.fetch("https://a.org/prive/x").error == "interdit par robots.txt"
    assert fetcher.fetch("https://a.org/public").ok


def test_fetch_rejects_unsupported_type_and_oversized_pages():
    img = httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG....")
    big = httpx.Response(200, headers={"content-type": "text/html"}, content=b"a" * 2000)
    fetcher = make_fetcher({"https://a.org/img": img, "https://a.org/big": big}, max_bytes=1000)
    assert fetcher.fetch("https://a.org/img").error.startswith("type non pris en charge")
    assert fetcher.fetch("https://a.org/big").error == "page trop volumineuse"


def test_fetch_rejects_pages_with_too_little_text():
    empty = httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"<html><body><p>Hi</p></body></html>"
    )
    fetcher = make_fetcher({"https://a.org/e": empty})
    assert fetcher.fetch("https://a.org/e").error == "texte extrait trop court"
