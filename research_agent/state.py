"""État de la recherche tenu par le code : registre de sources et passages.

Chaque source reçoit un identifiant stable (S1, S2...) dans l'ordre de découverte,
et chaque passage lu un identifiant dérivé (S3:p1, S3:p2...). Le modèle ne voit
que ces identifiants : il ne peut donc citer que ce qui existe réellement.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "_hs", "yclid")


def normalize_url(url: str) -> str:
    """Forme canonique d'une URL pour dédupliquer.

    Hôte en minuscules sans « www. », sans fragment, sans paramètres de suivi,
    paramètres triés, sans « / » final. Le schéma http/https est unifié.
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.removesuffix(":80").removesuffix(":443")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PREFIXES)
        )
    )
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, query, ""))


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


@dataclass
class Passage:
    id: str  # "S3:p2"
    source_id: str  # "S3"
    index: int  # position dans la page (1, 2, ...)
    text: str


@dataclass
class Source:
    id: str
    url: str
    title: str
    domain: str
    round_found: int
    last_seen_round: int = 0  # dernier tour où une requête l'a renvoyée
    snippet: str = ""  # extrait renvoyé par le moteur de recherche
    kind: str = "other"  # rempli par l'extraction (étape 4)
    reliability: str = "medium"
    publication_date: str = ""
    fetched: bool = False
    fetch_error: str = ""
    passages: list[Passage] = field(default_factory=list)


class SourceRegistry:
    """Registre dédupliqué des sources et de leurs passages."""

    def __init__(self) -> None:
        self._by_id: dict[str, Source] = {}
        self._by_key: dict[str, str] = {}
        self._passages: dict[str, Passage] = {}

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._by_id

    # --- Sources -------------------------------------------------------------

    def add(self, url: str, title: str = "", round_no: int = 1, snippet: str = "") -> tuple[Source, bool]:
        """Ajoute une source ou renvoie l'existante. Renvoie (source, est_nouvelle)."""
        key = normalize_url(url)
        if key in self._by_key:
            src = self._by_id[self._by_key[key]]
            if title and src.title == src.url:
                src.title = title
            if snippet and not src.snippet:
                src.snippet = snippet
            src.last_seen_round = max(src.last_seen_round, round_no)
            return src, False
        sid = f"S{len(self._by_id) + 1}"
        src = Source(
            id=sid,
            url=url,
            title=title or url,
            domain=domain_of(url),
            round_found=round_no,
            last_seen_round=round_no,
            snippet=snippet,
        )
        self._by_id[sid] = src
        self._by_key[key] = sid
        return src, True

    def get(self, source_id: str) -> Source | None:
        return self._by_id.get(source_id)

    def find_by_url(self, url: str) -> Source | None:
        sid = self._by_key.get(normalize_url(url))
        return self._by_id.get(sid) if sid else None

    def all(self) -> list[Source]:
        return list(self._by_id.values())

    def domains(self, source_ids: list[str]) -> set[str]:
        return {self._by_id[s].domain for s in source_ids if s in self._by_id}

    # --- Passages ------------------------------------------------------------

    def set_passages(self, source_id: str, texts: list[str]) -> list[Passage]:
        src = self._by_id[source_id]
        for old in src.passages:
            self._passages.pop(old.id, None)
        src.passages = [
            Passage(id=f"{source_id}:p{i}", source_id=source_id, index=i, text=t)
            for i, t in enumerate(texts, start=1)
        ]
        for p in src.passages:
            self._passages[p.id] = p
        src.fetched = True
        return src.passages

    def get_passage(self, passage_id: str) -> Passage | None:
        return self._passages.get(passage_id)

    @staticmethod
    def source_id_of(passage_id: str) -> str:
        return passage_id.split(":", 1)[0]

    # --- Rendu pour le modèle -----------------------------------------------

    def catalog(self, source_ids: list[str] | None = None) -> str:
        """« [S3] Titre — url (domaine, type, fiabilité, date) », une ligne par source."""
        ids = source_ids if source_ids is not None else list(self._by_id)
        lines = []
        for sid in ids:
            s = self._by_id.get(sid)
            if not s:
                continue
            meta = [s.domain, s.kind, f"fiabilité {s.reliability}"]
            if s.publication_date:
                meta.append(s.publication_date)
            lines.append(f"[{s.id}] {s.title} — {s.url} ({', '.join(meta)})")
        return "\n".join(lines) or "(aucune source)"


# --- Affirmations et état global ---------------------------------------------

ClaimStatus = Literal["unchecked", "corroborated", "confirmed", "contested", "unverifiable"]


@dataclass
class Claim:
    id: str  # "C1", "C2", ...
    statement: str
    sub_question_id: str  # "Q1", ...
    source_id: str  # une affirmation extraite vient toujours d'une seule source
    passage_ids: list[str]
    quote: str
    quote_verified: bool  # la citation figure-t-elle réellement dans les passages ?
    round_found: int
    status: ClaimStatus = "unchecked"
    # Vérification croisée (étape 6)
    group_id: str = ""  # affirmations équivalentes d'autres sources
    independent_origins: int = 0  # origines fiables indépendantes du groupe
    supporting_passage_ids: list[str] = field(default_factory=list)
    contradicting_passage_ids: list[str] = field(default_factory=list)
    verification_note: str = ""


@dataclass
class ResearchState:
    question: str
    plan: Any = None  # schemas.ResearchPlan (évite un import circulaire)
    registry: SourceRegistry = field(default_factory=SourceRegistry)
    claims: list[Claim] = field(default_factory=list)
    queries_run: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    # Boucle agentique (étape 5)
    covered: set[str] = field(default_factory=set)  # sous-questions jugées couvertes (définitif)
    decisions: list[dict] = field(default_factory=list)  # une décision par tour
    sufficient: bool = False
    stop_reason: str = ""
    # Comparaison des sources (étape 6)
    divergences: list[dict] = field(default_factory=list)
    # Synthèse (étape 7)
    synthesis: Any = None  # synthesis.Synthesis
    # Observateur appelé à chaque étape (progression dans l'interface web) ; hors de la trace exportée.
    listener: Callable[[ResearchState, dict], None] | None = field(default=None, repr=False, compare=False)

    def add_claim(self, **kwargs) -> Claim:
        claim = Claim(id=f"C{len(self.claims) + 1}", **kwargs)
        self.claims.append(claim)
        return claim

    def sub_question_ids(self) -> list[str]:
        return [sq.id for sq in self.plan.sub_questions] if self.plan else []

    def claims_for(self, sub_question_id: str) -> list[Claim]:
        return [c for c in self.claims if c.sub_question_id == sub_question_id]

    def new_queries(self, queries: list[str]) -> list[str]:
        """Filtre les requêtes déjà lancées (sans tenir compte de la casse ni des espaces)."""
        seen = {q.strip().lower() for q in self.queries_run}
        out = []
        for q in queries:
            key = q.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(q.strip())
        return out

    def log(self, step: str, **data) -> None:
        event = {"step": step, **data}
        self.trace.append(event)
        if self.listener is not None:
            self.listener(self, event)  # peut lever une exception pour interrompre la recherche
