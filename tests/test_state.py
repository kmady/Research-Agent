from research_agent.state import SourceRegistry, domain_of, normalize_url


def test_equivalent_urls_normalize_identically():
    variants = [
        "https://www.Example.org/page/?utm_source=x&b=2&a=1#section",
        "http://example.org/page?a=1&b=2",
        "https://example.org/page/?a=1&b=2&fbclid=abc",
    ]
    assert len({normalize_url(u) for u in variants}) == 1


def test_distinct_paths_stay_distinct():
    assert normalize_url("https://example.org/a") != normalize_url("https://example.org/b")
    assert normalize_url("https://example.org/a?id=1") != normalize_url("https://example.org/a?id=2")


def test_domain_strips_www_and_port():
    assert domain_of("https://www.lemonde.fr:443/x") == "lemonde.fr"
    assert domain_of("https://fr.wikipedia.org/wiki/X") == "fr.wikipedia.org"


def test_registry_ids_are_stable_and_deduplicated():
    reg = SourceRegistry()
    s1, new1 = reg.add("https://a.org/x", "A", round_no=1)
    s2, new2 = reg.add("https://b.org/y", "B", round_no=1)
    again, new3 = reg.add("https://www.a.org/x/?utm_medium=mail", "A bis", round_no=2)
    assert (s1.id, s2.id) == ("S1", "S2")
    assert new1 and new2 and not new3
    assert again is s1 and again.round_found == 1
    assert len(reg) == 2


def test_registry_fills_missing_title_and_snippet():
    reg = SourceRegistry()
    src, _ = reg.add("https://a.org/x")
    assert src.title == "https://a.org/x"
    reg.add("https://a.org/x", title="Vrai titre", snippet="extrait")
    assert src.title == "Vrai titre" and src.snippet == "extrait"


def test_domains_counts_distinct_domains():
    reg = SourceRegistry()
    reg.add("https://a.org/1")
    reg.add("https://a.org/2")
    reg.add("https://b.org/1")
    assert reg.domains(["S1", "S2", "S3", "S99"]) == {"a.org", "b.org"}


def test_passages_get_derived_ids_and_can_be_replaced():
    reg = SourceRegistry()
    src, _ = reg.add("https://a.org/x")
    passages = reg.set_passages(src.id, ["un", "deux"])
    assert [p.id for p in passages] == ["S1:p1", "S1:p2"]
    assert reg.get_passage("S1:p2").text == "deux"
    assert src.fetched
    reg.set_passages(src.id, ["nouveau"])
    assert reg.get_passage("S1:p2") is None
    assert SourceRegistry.source_id_of("S12:p3") == "S12"


def test_catalog_lists_sources():
    reg = SourceRegistry()
    src, _ = reg.add("https://a.org/x", "Titre A")
    src.publication_date = "2024-01-01"
    line = reg.catalog()
    assert line.startswith("[S1] Titre A — https://a.org/x (a.org")
    assert "2024-01-01" in line
