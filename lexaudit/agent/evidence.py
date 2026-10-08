"""Fetch and rank real Adilet articles for draft hypotheses."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from lexaudit.adilet.client import get_full_text, search_laws
from lexaudit.agent.draft import DraftFinding
from lexaudit.config.settings import get_settings
from lexaudit.llm import LLMClient
from lexaudit.prompts.registry import load

logger = logging.getLogger("lexaudit.evidence")
_STOPWORDS = set("который которая которые договор условия условие сторона стороны может могут риск возможный возможное законодательство нарушение является также имеет этого этом данной данный должны должен только между право права".split())


class CandidateArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    act_id: str
    act_title: str
    url: str
    article_number: str
    article_text: str


def _keywords(finding: DraftFinding) -> set[str]:
    words = re.findall(r"[а-яёa-z]{4,}",
                       (finding.risk_hypothesis + " " + finding.law_area).casefold())
    # A short suffix reduction covers common Russian case inflections.
    return {re.sub(r"(?:иями|ами|ого|ему|ыми|ий|ый|ая|ое|ые|ов|ам|ах|ом|ие|ия|ию|ии|ы|а|у|е|и)$", "", word)
            for word in words if word not in _STOPWORDS}


def gather_evidence(draft_findings: list[DraftFinding], *,
                    client_factory: Callable[[], LLMClient] = LLMClient,
                    on_tool: Callable[[str, str], None] | None = None) -> dict[str, list[CandidateArticle]]:
    """Run independent findings concurrently; share identical fetches per run."""
    settings = get_settings()
    if settings.EVIDENCE_CONCURRENCY < 1 or settings.EVIDENCE_ACTS < 1:
        raise ValueError("EVIDENCE_CONCURRENCY and EVIDENCE_ACTS must be positive")
    if len({f.draft_id for f in draft_findings}) != len(draft_findings):
        raise ValueError("draft_id must be unique")
    cache: dict[tuple[str, str], Future[Any]] = {}
    lock = Lock()

    def cached(kind: str, key: str, fetch: Callable[[], Any]) -> Any:
        with lock:
            future = cache.get((kind, key))
            owner = future is None
            if future is None:
                future = Future()
                cache[kind, key] = future
        if owner:
            try:
                if on_tool:
                    on_tool(kind, key)
                future.set_result(fetch())
            except Exception as exc:
                future.set_exception(exc)
        return future.result()

    def search(finding: DraftFinding, queries: list[str]) -> list[CandidateArticle]:
        keywords = _keywords(finding)
        ranked: dict[tuple[str, str], tuple[int, CandidateArticle]] = {}
        seen: set[str] = set()
        for query in queries:
            try:
                hits = cached("search", query, lambda: search_laws(query, limit=settings.EVIDENCE_ACTS))
                logger.info("evidence_search", extra={"draft_id": finding.draft_id, "query": query, "hits": len(hits)})
            except Exception as exc:
                if "evidence_search_error" not in finding.warnings:
                    finding.warnings.append("evidence_search_error")
                logger.warning("evidence_search_failed", extra={"draft_id": finding.draft_id, "error_type": type(exc).__name__})
                continue
            for hit in hits[:settings.EVIDENCE_ACTS]:
                if hit.act_id in seen:
                    continue
                seen.add(hit.act_id)
                try:
                    act = cached("act", hit.act_id, lambda: get_full_text(hit.act_id))
                    if not act.in_force:
                        continue
                    for article in act.articles:
                        text = "\n\n".join(filter(None, [article.heading, *article.paragraphs]))
                        lowered = text.casefold()
                        score = sum(bool(re.search(r"\b" + re.escape(word) + r"[а-яёa-z]*\b", lowered))
                                    for word in keywords if len(word) >= 4)
                        if article.number in finding.suggested_article_numbers:
                            score += 1
                        if not score:
                            continue
                        url = act.url.split("#", 1)[0]
                        if article.anchor:
                            url += "#" + article.anchor.lstrip("#")
                        candidate = CandidateArticle(act_id=act.act_id, act_title=act.title,
                            url=url, article_number=article.number, article_text=text)
                        ranked[act.act_id, article.number] = (score, candidate)
                except Exception as exc:
                    if "evidence_act_error" not in finding.warnings:
                        finding.warnings.append("evidence_act_error")
                    logger.warning("evidence_act_failed", extra={"draft_id": finding.draft_id,
                        "act_id": hit.act_id, "error_type": type(exc).__name__})
        ordered = sorted(ranked.values(), key=lambda item: (-item[0], item[1].act_id, item[1].article_number))
        return [item[1] for item in ordered[:3]]

    def worker(finding: DraftFinding) -> list[CandidateArticle]:
        finding.grounding = "pending"
        try:
            candidates = search(finding, finding.search_queries)
            if not candidates:
                logger.info("evidence_reformulate", extra={"draft_id": finding.draft_id})
                with client_factory() as client:
                    response = client.chat([
                        {"role": "system", "content": load(None, "evidence_queries")},
                        {"role": "user", "content": json.dumps({
                            "risk_hypothesis": finding.risk_hypothesis, "law_area": finding.law_area,
                            "previous_queries": finding.search_queries}, ensure_ascii=False)},
                    ], temperature=0.2, max_tokens=300)
                queries = json.loads(response.content)
                if response.tool_calls or not isinstance(queries, list) or not 2 <= len(queries) <= 3 or not all(
                    isinstance(q, str) and q.strip() for q in queries
                ):
                    raise ValueError("invalid reformulated queries")
                candidates = search(finding, queries)
            finding.grounding = "candidates_found" if candidates else "ungrounded"
            logger.info("evidence_complete", extra={"draft_id": finding.draft_id,
                "grounding": finding.grounding, "candidate_count": len(candidates)})
            return candidates
        except Exception as exc:
            finding.grounding = "ungrounded"
            if "evidence_error" not in finding.warnings:
                finding.warnings.append("evidence_error")
            logger.warning("evidence_failed", extra={"draft_id": finding.draft_id, "error_type": type(exc).__name__})
            return []

    with ThreadPoolExecutor(max_workers=settings.EVIDENCE_CONCURRENCY) as executor:
        results = list(executor.map(worker, draft_findings))
    return {f.draft_id: candidates for f, candidates in zip(draft_findings, results)}
