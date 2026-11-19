"""Petites fonctions de texte partagées par l'orchestrateur et la décision de suffisance."""

from __future__ import annotations

import re
import unicodedata

from .state import Passage

_LANG_WORDS = re.compile(r"\s*\(?\b(english|in english|en anglais|anglais)\b\)?\s*$", re.IGNORECASE)


_STOPWORDS = set("""
quel quels quelle quelles est sont a ont été être les la le l l' d d' des du de un une et ou en sur dans
pour par avec lors qui que quoi qu qu' comment pourquoi combien se sa son ses leur leurs ce cette ces il
elle ils elles on y aux au fait font peut peuvent t-il t-elle donné donne
what which how why who when where are is was were the of a an in on for to and or do does did can with by
""".split())


def keywords_query(question: str, max_words: int = 8) -> str:
    """Transforme une sous-question en requête de mots-clés (repli quand le modèle n'en propose pas).
    « Quels sont les résultats des expérimentations de la semaine de quatre jours ? »
    → « résultats expérimentations semaine quatre jours »."""
    words = re.findall(r"[\w’'-]+", question.lower())
    kept = []
    for w in words:
        w = re.sub(r"^(l|d|qu|n|s|c|j)['’]", "", w)
        if w and w not in _STOPWORDS:
            kept.append(w)
    return " ".join(kept[:max_words])


def clean_query(query: str, max_words: int = 10) -> str:
    """Nettoie une requête proposée par le modèle : « ? » final, mention parasite de la langue,
    longueur limitée (le modèle propose parfois des phrases entières)."""
    q = query.strip().rstrip("?").strip()
    q = _LANG_WORDS.sub("", q).strip()
    return " ".join(q.split()[:max_words])


# Médias connus : le modèle les classe parfois « academic » ou « official » parce qu'ils rapportent une étude.
_PRESS_DOMAINS = (
    "lemonde.fr", "lefigaro.fr", "liberation.fr", "francetvinfo.fr", "franceinfo.fr", "radiofrance.fr",
    "radio-canada.ca", "lapresse.ca", "ledevoir.com", "euronews.com", "bbc.com", "bbc.co.uk", "nytimes.com",
    "theguardian.com", "reuters.com", "apnews.com", "ouest-france.fr", "20minutes.fr", "leparisien.fr",
    "lepoint.fr", "lexpress.fr", "nouvelobs.com", "huffingtonpost.fr", "rfi.fr", "france24.com", "lesechos.fr",
    "la-croix.com", "geo.fr", "science-et-vie.com", "sciencesetavenir.fr", "courrierinternational.com",
    "tf1info.fr", "bfmtv.com", "rtl.fr", "europe1.fr", "challenges.fr", "latribune.fr", "lci.fr",
    "theconversation.com", "washingtonpost.com", "cnn.com", "lesoir.be", "rts.ch", "letemps.ch",
    "noovo.info", "tvanouvelles.ca", "journaldemontreal.com", "journaldequebec.com", "lactualite.com",
    "canadafrancais.com", "huffpost.com", "slate.fr", "marianne.net", "mediapart.fr", "humanite.fr",
)
# Correspondance par suffixe de domaine (« un.org » ne doit pas reconnaître « fun.org »).
_OFFICIAL_SUFFIXES = ("gouv.fr", "gov", "gov.uk", "gc.ca", "canada.ca", "europa.eu", "who.int", "un.org",
                      "oecd.org", "senat.fr", "assemblee-nationale.fr", "insee.fr", "service-public.fr", "admin.ch")
_ACADEMIC_SUFFIXES = ("edu", "ac.uk", "hal.science", "theses.fr", "cnrs.fr", "inserm.fr", "inrae.fr",
                      "pasteur.fr", "cairn.info", "persee.fr", "erudit.org", "nature.com", "sciencedirect.com",
                      "springer.com", "wiley.com", "thelancet.com", "bmj.com", "plos.org", "frontiersin.org")
_GOUV_LABEL = re.compile(r"(^|\.)gouv\.")            # sante.gouv.qc.ca, bretagne.developpement-durable.gouv.fr
_UNIV_LABEL = re.compile(r"(^|[.-])univ")              # univ-lyon1.fr, universite-paris-saclay.fr


def _has_suffix(domain: str, suffixes: tuple[str, ...]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def domain_kind(domain: str) -> tuple[str, str] | None:
    """(type, fiabilité) quand le domaine ne laisse pas de doute, sinon None."""
    if _has_suffix(domain, _PRESS_DOMAINS):
        return "press", "medium"
    if _has_suffix(domain, _OFFICIAL_SUFFIXES) or _GOUV_LABEL.search(domain):
        return "official", "high"
    if _has_suffix(domain, _ACADEMIC_SUFFIXES) or _UNIV_LABEL.search(domain):
        return "academic", "high"
    return None


def correct_source_kind(domain: str, kind: str, reliability: str) -> tuple[str, str]:
    """Corrige l'appréciation du modèle quand le domaine ne laisse pas de doute
    (un média qui rapporte une étude reste de la presse ; un site .gouv est officiel)."""
    known = domain_kind(domain)
    return known if known else (kind, reliability)


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[’‘`´]", "'", text)
    text = re.sub(r"[«»“”„]", '"', text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in_passages(quote: str, passages: list[Passage]) -> bool:
    """La citation figure-t-elle telle quelle (aux espaces, à la casse et aux guillemets près) dans un passage ?"""
    q = normalize_for_match(quote).strip(' ".…')
    return bool(q) and any(q in normalize_for_match(p.text) for p in passages)
