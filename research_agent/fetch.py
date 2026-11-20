"""Lecture des pages : téléchargement, extraction du texte, découpage et classement des passages.

Tout ici est déterministe : aucun appel au LLM.
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import numpy as np
import trafilatura

from .cache import DiskCache
from .embeddings import Embedder

ROBOTS_TOKEN = "research-agent"


@dataclass
class FetchResult:
    url: str
    ok: bool
    final_url: str = ""
    title: str = ""
    date: str = ""
    text: str = ""
    content_type: str = ""
    error: str = ""


# --- Téléchargement ----------------------------------------------------------


class Fetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_s: float = 15.0,
        max_bytes: int = 5_000_000,
        min_text_chars: int = 200,
        respect_robots: bool = True,
        cache: DiskCache | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.headers = {"User-Agent": user_agent, "Accept-Language": "fr,en;q=0.8"}
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.min_text_chars = min_text_chars
        self.respect_robots = respect_robots
        self.cache = cache
        self.client = client or httpx.Client()
        self._robots: dict[str, RobotFileParser] = {}

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = RobotFileParser()
            try:
                r = self.client.get(
                    f"{base}/robots.txt",
                    headers=self.headers,
                    timeout=min(5.0, self.timeout_s),
                    follow_redirects=True,
                )
                if r.status_code >= 400:
                    rp.allow_all = (
                        True  # RFC 9309 : robots.txt indisponible (4xx/5xx) → pas de restriction déclarée
                    )
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp.allow_all = True
            self._robots[base] = rp
        return rp.can_fetch(ROBOTS_TOKEN, url)

    def fetch(self, url: str) -> FetchResult:
        if self.cache and (cached := self.cache.get(url)) is not None:
            return FetchResult(**cached)

        def fail(reason: str, ctype: str = "") -> FetchResult:
            return FetchResult(url=url, ok=False, error=reason, content_type=ctype)

        if not self.allowed_by_robots(url):
            return fail("interdit par robots.txt")

        try:
            with self.client.stream(
                "GET", url, headers=self.headers, timeout=self.timeout_s, follow_redirects=True
            ) as r:
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if r.status_code >= 400:
                    return fail(f"HTTP {r.status_code}", ctype)
                body = bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        return fail("page trop volumineuse", ctype)
                final_url = str(r.url)
                encoding = r.encoding or "utf-8"
        except httpx.HTTPError as exc:
            return fail(f"{type(exc).__name__}")

        data = bytes(body)
        if ctype == "application/pdf" or data[:5] == b"%PDF-":
            title, date, text = extract_pdf(data)
        elif ctype in ("text/html", "application/xhtml+xml", ""):
            title, date, text = extract_html(data, final_url)
        elif ctype.startswith("text/"):
            title, date, text = "", "", data.decode(encoding, errors="replace")
        else:
            return fail(f"type non pris en charge : {ctype}", ctype)

        text = text.strip()
        if len(text) < self.min_text_chars:
            return fail("texte extrait trop court", ctype)

        result = FetchResult(
            url=url, ok=True, final_url=final_url, title=title, date=date, text=text, content_type=ctype
        )
        if self.cache:
            self.cache.set(url, asdict(result))
        return result


def extract_html(data: bytes | str, url: str = "") -> tuple[str, str, str]:
    """Renvoie (titre, date, texte principal) d'une page HTML."""
    text = trafilatura.extract(data, url=url or None, include_comments=False, include_tables=True) or ""
    try:
        meta = trafilatura.extract_metadata(data, default_url=url or None)
    except Exception:  # métadonnées facultatives : une page mal formée ne doit pas faire échouer la lecture
        meta = None
    title = (meta.title or "") if meta else ""
    date = (meta.date or "") if meta else ""
    return title, date, text


def extract_pdf(data: bytes, max_pages: int = 40) -> tuple[str, str, str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
        meta = reader.metadata
        title = (meta.title or "") if meta else ""
    except Exception:  # PDF corrompu ou chiffré
        return "", "", ""
    return title, "", "\n\n".join(pages)


# --- Découpage en passages ---------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Découpe un paragraphe trop long par phrases, puis par mots si nécessaire."""
    out: list[str] = []
    cur = ""
    for sentence in _SENTENCE_END.split(paragraph):
        units = [sentence] if len(sentence) <= max_chars else sentence.split()
        for unit in units:
            if not cur:
                cur = unit
            elif len(cur) + 1 + len(unit) <= max_chars:
                cur += " " + unit
            else:
                out.append(cur)
                cur = unit
    if cur:
        out.append(cur)
    return out


def chunk_text(
    text: str, target_tokens: int = 450, chars_per_token: float = 4.0, min_chars: int = 80
) -> list[str]:
    """Découpe un texte en passages d'environ `target_tokens` tokens.

    Coupe en priorité entre paragraphes, puis entre phrases, puis entre mots :
    jamais au milieu d'un mot. Aucun contenu n'est perdu : un fragment plus court
    que `min_chars` est fusionné avec le passage précédent (dépassement toléré de 25 %).
    """
    max_chars = max(1, int(target_tokens * chars_per_token))
    pieces: list[str] = []
    for para in (p.strip() for p in re.split(r"\n+", text)):
        if not para:
            continue
        pieces.extend([para] if len(para) <= max_chars else _split_long(para, max_chars))

    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if not cur:
            cur = piece
        elif len(cur) + 1 + len(piece) <= max_chars:
            cur += "\n" + piece
        else:
            chunks.append(cur)
            cur = piece
    if cur:
        chunks.append(cur)

    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < min_chars and len(merged[-1]) + 1 + len(chunk) <= max_chars * 1.25:
            merged[-1] += "\n" + chunk
        else:
            merged.append(chunk)
    return merged


# --- Classement des passages -------------------------------------------------


def _unit_rows(vectors: list[list[float]]) -> np.ndarray:
    m = np.asarray(vectors, dtype=np.float32)
    return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-12, None)


def rank_passages_multi(
    queries: list[str], passages: list[str], embedder: Embedder, top_k: int = 5
) -> list[tuple[int, float]]:
    """Classe les passages selon leur meilleure similarité avec l'une des requêtes.

    Sert à choisir les passages d'une page utiles à *au moins une* sous-question.
    Renvoie [(indice, score cosinus)], du plus pertinent au moins pertinent.
    """
    if not passages or not queries:
        return []
    qs = _unit_rows([embedder.embed_query(q) for q in queries])
    docs = _unit_rows(embedder.embed_documents(passages))
    scores = (docs @ qs.T).max(axis=1)
    order = np.argsort(-scores, kind="stable")[:top_k]
    return [(int(i), float(scores[i])) for i in order]


def max_similarity(queries: list[str], texts: list[str], embedder: Embedder) -> list[float]:
    """Pour chaque texte, sa meilleure similarité avec l'une des requêtes."""
    if not texts or not queries:
        return [0.0] * len(texts)
    qs = _unit_rows([embedder.embed_query(q) for q in queries])
    docs = _unit_rows(embedder.embed_documents(texts))
    return [float(x) for x in (docs @ qs.T).max(axis=1)]


def rank_passages(
    query: str, passages: list[str], embedder: Embedder, top_k: int = 5
) -> list[tuple[int, float]]:
    """Renvoie les indices des `top_k` passages les plus proches de la requête, avec leur score cosinus."""
    return rank_passages_multi([query], passages, embedder, top_k)
