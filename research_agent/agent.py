"""Orchestrateur : c'est le code qui pilote, le modèle exécute des tâches courtes.

Boucle agentique (étape 5) :

    plan → [ tour de recherche → évaluation par sous-question → décision ] × N
                                                  │
                     suffisant, budget épuisé, stagnation → arrêt

La vérification (étape 6) et la synthèse (étape 7) viendront après la boucle.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field

from . import prompts
from .agent_utils import clean_query, correct_source_kind, quote_in_passages  # noqa: F401  (réexportés)
from .cache import DiskCache
from .config import Config
from .embeddings import Embedder, OllamaEmbedder
from .fetch import Fetcher, chunk_text, max_similarity, rank_passages_multi
from .llm import ContextOverflowError, LLMError, OllamaLLM
from .schemas import Extraction, ResearchPlan, SubQuestionAssessment, UrlSelection
from .search import SearchError, SearchProvider, SearxngProvider
from .state import Passage, ResearchState, Source, domain_of
from .sufficiency import Decision, Thresholds, compute_metrics, decide, metrics_text, reliable_sources_read
from .synthesis import Synthesizer
from .verify import Verifier

log = logging.getLogger("research_agent")


@dataclass
class RoundResult:
    round_no: int
    queries: list[str]
    focus: list[str] = field(default_factory=list)
    new_source_ids: list[str] = field(default_factory=list)
    read_source_ids: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    search_errors: list[str] = field(default_factory=list)
    claims_added: int = 0
    seconds: float = 0.0


class ResearchAgent:
    def __init__(
        self, config: Config, llm: OllamaLLM, search: SearchProvider, fetcher: Fetcher, embedder: Embedder
    ) -> None:
        self.cfg = config
        self.llm = llm
        self.search = search
        self.fetcher = fetcher
        self.embedder = embedder
        self.th = Thresholds(
            max_rounds=config.max_rounds,
            min_domains_per_subquestion=config.min_domains_per_subquestion,
            min_total_sources=config.min_total_sources,
            max_queries_per_round=config.max_queries_per_round,
        )

    # --- Utilitaires ---------------------------------------------------------

    def _ask(self, user: str, schema, *, think: bool):
        return self.llm.structured(
            [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": user}],
            schema,
            think=think,
        )

    @staticmethod
    def _sub_questions_text(state: ResearchState, only: list[str] | None = None) -> str:
        return "\n".join(
            f"{sq.id} : {sq.question}" for sq in state.plan.sub_questions if only is None or sq.id in only
        )

    # --- Étape 1 : plan ------------------------------------------------------

    def plan(self, state: ResearchState) -> ResearchPlan:
        plan = self._ask(prompts.plan_prompt(state.question), ResearchPlan, think=False)
        # Le code renumérote : on ne dépend pas des identifiants inventés par le modèle.
        for i, sq in enumerate(plan.sub_questions[:4], start=1):
            sq.id = f"Q{i}"
        plan.sub_questions = plan.sub_questions[:4]
        plan.initial_queries = [clean_query(q) for q in plan.initial_queries if clean_query(q)][:5]
        state.plan = plan
        state.log(
            "plan", sub_questions=[sq.model_dump() for sq in plan.sub_questions], queries=plan.initial_queries
        )
        log.info("Plan : %d sous-questions, %d requêtes", len(plan.sub_questions), len(plan.initial_queries))
        for sq in plan.sub_questions:
            log.info("  %s %s", sq.id, sq.question)
        return plan

    # --- Étape 2a : recherche ------------------------------------------------

    def _search(self, state: ResearchState, queries: list[str], round_no: int, result: RoundResult) -> None:
        for q in queries:
            try:
                hits = self.search.search(q, n=self.cfg.results_per_query)
            except SearchError as exc:
                # Requête non comptée comme « déjà faite » : elle pourra être relancée.
                log.warning("Recherche en échec : %s", exc)
                state.log("search_error", round=round_no, query=q, error=str(exc))
                result.search_errors.append(str(exc))
                continue
            state.queries_run.append(q)
            new = off_topic = 0
            kept = [h for h in hits if not self._is_blocked(h.url)]
            blocked, hits = len(hits) - len(kept), kept
            relevance = self._relevance(state, hits)
            for h, score in zip(hits, relevance, strict=True):
                if score < self.cfg.min_hit_relevance:
                    off_topic += 1
                    continue
                src, is_new = state.registry.add(h.url, h.title, round_no=round_no, snippet=h.snippet)
                if is_new:
                    result.new_source_ids.append(src.id)
                    new += 1
            notes = ([f"{off_topic} hors sujet"] if off_topic else []) + (
                [f"{blocked} domaines exclus"] if blocked else []
            )
            log.info(
                "  « %s » → %d résultats, %d nouvelles sources%s",
                q,
                len(hits),
                new,
                f" ({', '.join(notes)})" if notes else "",
            )

    def _relevance(self, state: ResearchState, hits: list) -> list[float]:
        """Pertinence de chaque résultat (titre + extrait) pour la question ou l'une des sous-questions."""
        if not hits:
            return []
        queries = [state.question] + ([sq.question for sq in state.plan.sub_questions] if state.plan else [])
        return max_similarity(queries, [f"{h.title} — {h.snippet}" for h in hits], self.embedder)

    def _is_blocked(self, url: str) -> bool:
        dom = domain_of(url)
        return any(dom == b or dom.endswith("." + b) for b in self.cfg.blocked_domains)

    # --- Étape 2b : sélection ------------------------------------------------

    def _select(
        self, state: ResearchState, candidates: list[Source], n: int, focus: list[str]
    ) -> list[Source]:
        read_domains = {s.domain for s in state.registry.all() if s.fetched}
        reads_per_domain = Counter(s.domain for s in state.registry.all() if s.fetched)
        cap = self.cfg.max_pages_per_domain
        candidates = [s for s in candidates if reads_per_domain[s.domain] < cap]
        # Les sources trouvées par les requêtes de ce tour passent en premier, puis les domaines encore inconnus.
        # (« vue au dernier tour » : une source retrouvée par une requête ciblée compte comme récente)
        latest = max((s.last_seen_round for s in candidates), default=0)
        candidates = sorted(candidates, key=lambda s: (s.last_seen_round != latest, s.domain in read_domains))
        if len(candidates) <= n:
            return self._cap_domains(candidates, reads_per_domain, n)
        shown = candidates[:20]
        listing = "\n".join(
            f"[{s.id}] {s.title} — {s.domain}{' (domaine déjà lu)' if s.domain in read_domains else ''}"
            f"\n    {s.snippet[:200]}"
            for s in shown
        )
        by_id = {s.id: s for s in shown}
        focus_text = self._sub_questions_text(state, focus) if focus else ""
        try:
            sel = self._ask(
                prompts.selection_prompt(
                    state.question, self._sub_questions_text(state), listing, n, focus_text
                ),
                UrlSelection,
                think=False,
            )
            # Une sélection vide est une réponse légitime (« rien de pertinent ») : pas de repli.
            chosen = list(dict.fromkeys(c.source_id for c in sel.selected if c.source_id in by_id))
            reasons = {c.source_id: c.reason for c in sel.selected if c.source_id in by_id}
        except LLMError as exc:
            log.warning("Sélection par le modèle en échec (%s) : ordre du moteur conservé", exc)
            chosen, reasons = [s.id for s in shown], {}
        state.log("selection", candidates=[s.id for s in shown], chosen=chosen, reasons=reasons, focus=focus)
        return self._cap_domains([by_id[sid] for sid in chosen], reads_per_domain, n)

    def _cap_domains(self, sources: list[Source], reads_per_domain: Counter, n: int) -> list[Source]:
        """Garde au plus `max_pages_per_domain` pages par domaine (lectures passées comprises), puis les n premières."""
        counts = Counter(reads_per_domain)
        out = []
        for s in sources:
            if counts[s.domain] < self.cfg.max_pages_per_domain:
                counts[s.domain] += 1
                out.append(s)
            if len(out) == n:
                break
        return out

    # --- Étape 2c : lecture --------------------------------------------------

    def _read(self, state: ResearchState, src: Source) -> list[Passage] | None:
        res = self.fetcher.fetch(src.url)
        if not res.ok:
            src.fetch_error = res.error
            return None
        if res.title and src.title == src.url:
            src.title = res.title
        if res.date:
            src.publication_date = res.date[:10]
        return state.registry.set_passages(src.id, chunk_text(res.text, self.cfg.passage_tokens))

    def _best_passages(
        self, state: ResearchState, passages: list[Passage], k: int, focus: list[str]
    ) -> list[Passage]:
        sqs = [sq.question for sq in state.plan.sub_questions if not focus or sq.id in focus]
        ranked = rank_passages_multi(
            [state.question, *sqs], [p.text for p in passages], self.embedder, top_k=k
        )
        # Remis dans l'ordre de la page : plus lisible pour le modèle.
        return sorted((passages[i] for i, _ in ranked), key=lambda p: p.index)

    # --- Étape 3 : extraction ------------------------------------------------

    def _extract(
        self, state: ResearchState, src: Source, passages: list[Passage], round_no: int, focus: list[str]
    ) -> int:
        header = f"[{src.id}] {src.title} — {src.url} ({src.domain})"
        k = self.cfg.passages_per_call
        while True:
            chosen = self._best_passages(state, passages, k, focus)
            ptext = "\n\n".join(f"[{p.id}] {p.text}" for p in chosen)
            try:
                ext = self._ask(
                    prompts.extraction_prompt(state.question, self._sub_questions_text(state), header, ptext),
                    Extraction,
                    think=False,
                )
                break
            except ContextOverflowError:
                if k <= 1:
                    raise
                k -= 1
                log.info("  %s : message trop long, nouvel essai avec %d passages", src.id, k)

        src.kind, src.reliability = correct_source_kind(src.domain, ext.source_kind, ext.reliability)
        if src.kind in ("company", "blog"):
            src.reliability = "low"  # règle de la consigne, imposée par le code (le modèle l'oublie parfois)
        if ext.publication_date and not src.publication_date:
            src.publication_date = ext.publication_date[:10]

        valid_sq = set(state.sub_question_ids())
        shown = {p.id: p for p in chosen}
        added, rejected = 0, []
        for c in ext.claims[:8]:
            pids = [pid for pid in dict.fromkeys(c.passage_ids) if pid in shown]
            if c.sub_question_id not in valid_sq or not pids or not c.statement.strip():
                rejected.append(
                    {
                        "statement": c.statement,
                        "sub_question_id": c.sub_question_id,
                        "passage_ids": c.passage_ids,
                    }
                )
                continue
            verified = quote_in_passages(c.quote, [shown[p] for p in pids]) if c.quote else False
            state.add_claim(
                statement=c.statement.strip(),
                sub_question_id=c.sub_question_id,
                source_id=src.id,
                passage_ids=pids,
                quote=c.quote if verified else "",
                quote_verified=verified,
                round_found=round_no,
            )
            added += 1
        state.log(
            "extraction",
            round=round_no,
            source=src.id,
            passages=list(shown),
            kind=src.kind,
            reliability=src.reliability,
            claims_added=added,
            rejected=rejected,
        )
        return added

    # --- Un tour de recherche ------------------------------------------------

    def research_round(
        self, state: ResearchState, queries: list[str], round_no: int, focus: list[str] | None = None
    ) -> RoundResult:
        focus = focus or []
        t0 = time.time()
        result = RoundResult(round_no=round_no, queries=queries, focus=focus)
        log.info(
            "Tour %d : %d requêtes%s",
            round_no,
            len(queries),
            f" (ciblées sur {', '.join(focus)})" if focus else "",
        )
        self._search(state, queries, round_no, result)

        candidates = [s for s in state.registry.all() if not s.fetched and not s.fetch_error]
        for src in self._select(state, candidates, self.cfg.urls_to_read_per_round, focus):
            passages = self._read(state, src)
            if passages is None:
                result.failed[src.id] = src.fetch_error
                log.info("  ✘ %s %s : %s", src.id, src.domain, src.fetch_error)
                continue
            result.read_source_ids.append(src.id)
            try:
                added = self._extract(state, src, passages, round_no, focus)
            except LLMError as exc:
                log.warning("  Extraction %s en échec : %s", src.id, exc)
                state.log("extraction_error", round=round_no, source=src.id, error=str(exc))
                continue
            result.claims_added += added
            log.info(
                "  ✔ %s %s (%s, %s) : %d passages, %d affirmations",
                src.id,
                src.domain,
                src.kind,
                src.reliability,
                len(passages),
                added,
            )

        result.seconds = time.time() - t0
        state.log(
            "round",
            round=round_no,
            queries=queries,
            focus=focus,
            new_sources=result.new_source_ids,
            read=result.read_source_ids,
            failed=result.failed,
            claims_added=result.claims_added,
            seconds=round(result.seconds, 1),
        )
        return result

    # --- Étape 4 : évaluation de la suffisance -------------------------------

    def _assess_sub_question(self, state: ResearchState, sq, metrics) -> SubQuestionAssessment | None:
        reg = state.registry
        claims = state.claims_for(sq.id)
        if not claims:
            return None  # rien à juger : « missing » sans appeler le modèle
        lines = []
        for c in claims[:15]:
            s = reg.get(c.source_id)
            lines.append(f"- {c.statement} [{c.source_id}, {s.domain}, fiabilité {s.reliability}]")
        try:
            return self._ask(
                prompts.assessment_prompt(
                    state.question, sq.id, sq.question, "\n".join(lines), metrics_text(metrics, self.th)
                ),
                SubQuestionAssessment,
                think=True,
            )
        except LLMError as exc:
            log.warning("  Évaluation de %s en échec : %s", sq.id, exc)
            return None

    def assess(self, state: ResearchState, last: RoundResult) -> Decision:
        metrics = compute_metrics(state)
        assessments = {}
        for sq in state.plan.sub_questions:
            if sq.id in state.covered:
                continue
            assessments[sq.id] = self._assess_sub_question(state, sq, metrics[sq.id])

        decision = decide(
            round_no=last.round_no,
            sub_questions=[(sq.id, sq.question) for sq in state.plan.sub_questions],
            metrics=metrics,
            assessments=assessments,
            already_covered=set(state.covered),
            queries_run=state.queries_run,
            last_round_read=len(last.read_source_ids),
            last_round_claims=last.claims_added,
            reliable_read=reliable_sources_read(state),
            th=self.th,
        )
        state.covered.update(v.sub_question_id for v in decision.verdicts if v.covered)

        for v in decision.verdicts:
            m = metrics[v.sub_question_id]
            mark = "✔" if v.covered else "…"
            log.info(
                "  %s %s [%s] %d affirmations, %d domaines fiables — %s",
                mark,
                v.sub_question_id,
                v.llm_status,
                m.claims,
                len(m.reliable_domains),
                "; ".join(v.reasons),
            )
        log.info("Décision tour %d : %s", decision.round_no, decision.reason)
        if not decision.stop:
            log.info("  Requêtes suivantes : %s", " | ".join(decision.next_queries))

        state.log(
            "assessment",
            round=last.round_no,
            metrics={k: v.__dict__ for k, v in metrics.items()},
            llm={k: (a.model_dump() if a is not None else None) for k, a in assessments.items()},
        )
        state.log("decision", **decision.as_dict())
        state.decisions.append(decision.as_dict())
        return decision

    # --- Point d'entrée ------------------------------------------------------

    def run(self, question: str, listener=None) -> ResearchState:
        """Lance la recherche complète. `listener(state, event)` est appelé à chaque étape tracée."""
        state = ResearchState(question=question, listener=listener)
        t0 = time.time()
        state.log("phase", name="plan")
        self.plan(state)

        queries, focus, round_no = state.new_queries(state.plan.initial_queries), [], 1
        while True:
            state.log("round_start", round=round_no, queries=queries)
            result = self.research_round(state, queries, round_no, focus)
            if queries and len(result.search_errors) == len(queries):
                # Panne du moteur de recherche : ne pas la confondre avec un manque d'information.
                state.sufficient = False
                state.stop_reason = (
                    f"arrêt : moteur de recherche indisponible au tour {round_no} ({result.search_errors[0]})"
                )
                log.error("%s", state.stop_reason)
                state.log("outage", round=round_no, reason=state.stop_reason)
                if round_no == 1:
                    break
                state.decisions.append(
                    {
                        "round_no": round_no,
                        "stop": True,
                        "sufficient": False,
                        "reason": state.stop_reason,
                        "next_queries": [],
                        "focus": [],
                        "verdicts": [],
                        "reliable_sources_read": reliable_sources_read(state),
                    }
                )
                break
            decision = self.assess(state, result)
            if decision.stop:
                state.sufficient = decision.sufficient
                state.stop_reason = decision.reason
                break
            queries, focus, round_no = state.new_queries(decision.next_queries), decision.focus, round_no + 1

        if self.cfg.verify:
            state.log("phase", name="verification")
            Verifier(self).run(state)
        if self.cfg.synthesize:
            state.log("phase", name="synthesis")
            log.info("Rédaction de la synthèse")
            Synthesizer(self).run(state)

        state.log(
            "done",
            seconds=round(time.time() - t0, 1),
            rounds=round_no,
            sufficient=state.sufficient,
            llm=self.llm.stats.as_dict(),
        )
        return state


def build_agent(cfg: Config, use_cache: bool = True) -> ResearchAgent:
    llm = OllamaLLM(
        cfg.ollama_url,
        cfg.model,
        num_ctx=cfg.num_ctx,
        temperature=cfg.temperature,
        timeout_s=cfg.llm_timeout_s,
        max_output_tokens=cfg.max_output_tokens,
        max_output_tokens_thinking=cfg.max_output_tokens_thinking,
        chars_per_token=cfg.chars_per_token,
    )
    search = SearxngProvider(
        cfg.searxng_url,
        min_interval_s=cfg.search_interval_s,
        categories=cfg.search_categories,
        fallback_categories=cfg.search_fallback_categories,
        cache=DiskCache(cfg.cache_dir, "search", enabled=use_cache),
    )
    fetcher = Fetcher(
        user_agent=cfg.user_agent,
        timeout_s=cfg.fetch_timeout_s,
        max_bytes=cfg.max_page_bytes,
        cache=DiskCache(cfg.cache_dir, "pages", enabled=use_cache),
    )
    embedder = OllamaEmbedder(cfg.ollama_url, cfg.embed_model)
    return ResearchAgent(cfg, llm, search, fetcher, embedder)
