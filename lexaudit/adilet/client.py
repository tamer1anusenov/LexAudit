"""Adilet (adilet.zan.kz) legal-document search client.

Public contract (T1.2):
    search_laws(query: str, limit: int = 10, language: str = "rus") -> list[SearchHit]

Transport reality (verified 2026-10-06, see docs/adilet-api.md):
* The new SPA at adilet.zan.kz calls ``/api/documents/search`` but that API
  requires a user token (``Authorization: Bearer``) — there is no public
  anonymous JSON endpoint.
* The legacy portal at ``old.adilet.zan.kz`` serves server-rendered HTML
  search results: ``GET /{lang}/search/docs?fulltext=...&page=N&pagesize=M``.
  This module parses that HTML with a small regex parser (stdlib only).

Reliability (per task brief):
* httpx client, 15 s timeout, 2 retries with exponential backoff,
  browser-like User-Agent (old.adilet.zan.kz drops non-browser UAs with a
  redirect loop that never yields a 200 — see docs/adilet-api.md);
* every call is logged to the structured logger (working rule 5);
* results are cached in the SQLite table ``search_cache``
  (normalized query + language -> response, TTL via SEARCH_CACHE_TTL_DAYS);
* failure after all retries raises :class:`AdiletUnavailable`.

SearchHit is strict (T1.2 rule 6): exactly the five documented fields.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from lexaudit.config.settings import get_settings

logger = logging.getLogger("lexaudit.adilet")

# Default retry backoff in seconds (doubled per retry; 2 retries max).
_RETRY_BACKOFF_S = 1.5
# Retry on transient statuses only; 4xx (except 429) is a hard failure.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Default cache TTL in days, used only when SEARCH_CACHE_TTL_DAYS is unset.
_DEFAULT_TTL_DAYS = 7

_LANGS = frozenset({"rus", "kaz", "eng"})

# --- parsing -----------------------------------------------------------------

_DOC_LINK_RE = re.compile(
    r'<h4 class="post_header">.*?<a href="/(?:rus|kaz|eng)/docs/([A-Z0-9_]+)"[^>]*>(.*?)</a>',
    re.S,
)
_STATUS_RE = re.compile(r'<span class="status (status_[a-z]+)">([^<]*)</span>')
_PARA_RE = re.compile(r"<p>(.*?)</p>", re.S)
_BLOCKQUOTE_RE = re.compile(r"<blockquote>(.*?)</blockquote>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Only these statuses mean "no longer in force" (verified on the live portal:
# status_yts = «Утративший силу» / «Күшін жойған»). Everything else
# (status_new, status_upd, status_err or no status span) is treated as
# in_force — verified against the Labor Code K1500000375 (status_upd).
_YTS_CLASSES = frozenset({"status_yts"})


def _strip_tags(fragment: str) -> str:
    return _WHITESPACE_RE.sub(" ", html_mod.unescape(_TAG_RE.sub("", fragment))).strip()


def _normalize_query(query: str) -> str:
    """Cache key: casefolded, whitespace-normalized, trimmed."""
    return _WHITESPACE_RE.sub(" ", query.strip()).casefold()


def parse_search_html(html: str, language: str, doc_base: str | None = None) -> list[dict[str, Any]]:
    """Parse a legacy-portal search-results page into raw hit dicts.

    ``language`` and ``doc_base`` build the absolute SearchHit.url
    (document pages are public on the canonical adilet.zan.kz host).
    """
    base = (doc_base or get_settings().ADILET_BASE_URL or "https://adilet.zan.kz").rstrip("/")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    # post_holder blocks: one <h4 class="post_header"> per result; the block
    # extends until the next post_holder start or the end of the <div class="serp">.
    blocks = re.split(r'<div class="post_holder">', html)
    for block in blocks[1:]:
        block = block.split('<div class="post_holder">', 1)[0]
        m = _DOC_LINK_RE.search(block)
        if not m:
            continue
        act_id, title = m.group(1), _strip_tags(m.group(2))
        if act_id in seen or not title:
            continue
        seen.add(act_id)
        sm = _STATUS_RE.search(block)
        in_force = (sm.group(1) not in _YTS_CLASSES) if sm else True
        pm = _PARA_RE.search(block)
        snippet = _strip_tags(pm.group(1)) if pm else ""
        if not snippet:  # fall back to the excerpt blockquote
            qm = _BLOCKQUOTE_RE.search(block)
            snippet = _strip_tags(qm.group(1)) if qm else ""
        hits.append(
            {
                "act_id": act_id,
                "title": title,
                "snippet": snippet,
                "url": f"{base}/{language}/docs/{act_id}",
                "in_force": in_force,
            }
        )
    return hits


# --- models / exceptions -------------------------------------------------------


class SearchHit(BaseModel):
    """One search result. STRICT shape (T1.2 rule 6): no extra/missing fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    act_id: str
    title: str
    snippet: str
    url: str
    in_force: bool


class Article(BaseModel):
    """One article from an Adilet act."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str
    heading: str | None
    paragraphs: list[str]
    anchor: str | None


class FullAct(BaseModel):
    """Cached full text and metadata for an Adilet act."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    act_id: str
    title: str
    url: str
    in_force: bool
    fetched_at: str
    articles: list[Article]


class AdiletError(RuntimeError):
    """Base class for Adilet client errors."""


class AdiletUnavailable(AdiletError):
    """Raised when the Adilet portal is unreachable after all retries."""


class AdiletClientError(AdiletError):
    """Raised on an unusable non-transient response (4xx) or unparseable page."""


# --- cache ---------------------------------------------------------------------


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key  TEXT PRIMARY KEY,
            language   TEXT NOT NULL,
            query      TEXT NOT NULL,
            payload    TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


class SearchCache:
    """SQLite cache for search responses: normalized query + language -> hits.

    TTL comes from settings.SEARCH_CACHE_TTL_DAYS (env SEARCH_CACHE_TTL_DAYS).
    A TTL of 0 disables caching (reads miss, writes are skipped).
    """

    def __init__(self, db_path: Path | str, ttl_days: float) -> None:
        self._db_path = Path(db_path)
        self._ttl_s = ttl_days * 86400
        _init_db(self._db_path)

    @property
    def ttl_days(self) -> float:
        return self._ttl_s / 86400

    def _key(self, query: str, language: str) -> str:
        return hashlib.sha256(f"{language}\u0000{_normalize_query(query)}".encode("utf-8")).hexdigest()

    def get(self, query: str, language: str) -> list[dict[str, Any]] | None:
        if self._ttl_s <= 0:
            return None
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT payload, created_at FROM search_cache WHERE cache_key = ?",
                (self._key(query, language),),
            ).fetchone()
            if not row:
                return None
            payload, created_at = row
            if time.time() - created_at > self._ttl_s:
                conn.execute("DELETE FROM search_cache WHERE cache_key = ?", (self._key(query, language),))
                conn.commit()
                return None
            return _loads(payload)
        finally:
            conn.close()

    def set(self, query: str, language: str, hits: list[dict[str, Any]]) -> None:
        if self._ttl_s <= 0:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO search_cache (cache_key, language, query, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    self._key(query, language),
                    language,
                    _normalize_query(query),
                    _dumps(hits),
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


class ActCache:
    """SQLite mirror for full act metadata and article text."""

    def __init__(self, db_path: Path | str, refresh_days: float) -> None:
        self._db_path = Path(db_path)
        self._refresh_s = refresh_days * 86400
        _init_act_db(self._db_path)

    def get(self, act_id: str, language: str, *, force: bool = False) -> FullAct | None:
        if force or self._refresh_s <= 0:
            return None
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT title, url, in_force, fetched_at FROM acts WHERE act_id=? AND language=?",
                (act_id, language),
            ).fetchone()
            if row is None:
                return None
            title, url, in_force, fetched_at = row
            try:
                fetched_epoch = datetime.fromisoformat(fetched_at).timestamp()
            except ValueError as exc:
                raise AdiletClientError(f"invalid fetched_at in acts cache for {act_id}") from exc
            if time.time() - fetched_epoch > self._refresh_s:
                return None
            article_rows = conn.execute(
                "SELECT number, heading, paragraphs, anchor FROM articles "
                "WHERE act_id=? AND language=? ORDER BY ordinal",
                (act_id, language),
            ).fetchall()
            return FullAct(
                act_id=act_id,
                title=title,
                url=url,
                in_force=bool(in_force),
                fetched_at=fetched_at,
                articles=[Article(number=n, heading=h, paragraphs=_loads(p), anchor=a) for n, h, p, a in article_rows],
            )
        finally:
            conn.close()

    def set(self, act: FullAct, language: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO acts(act_id, language, title, url, in_force, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (act.act_id, language, act.title, act.url, int(act.in_force), act.fetched_at),
                )
                conn.execute("DELETE FROM articles WHERE act_id=? AND language=?", (act.act_id, language))
                conn.executemany(
                    "INSERT INTO articles(act_id, language, ordinal, number, heading, paragraphs, anchor) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (act.act_id, language, i, a.number, a.heading, _dumps(a.paragraphs), a.anchor)
                        for i, a in enumerate(act.articles)
                    ],
                )
        finally:
            conn.close()


def _init_act_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS acts (act_id TEXT NOT NULL, language TEXT NOT NULL, "
            "title TEXT NOT NULL, url TEXT NOT NULL, in_force INTEGER NOT NULL, fetched_at TEXT NOT NULL, "
            "PRIMARY KEY(act_id, language));"
            "CREATE TABLE IF NOT EXISTS articles (act_id TEXT NOT NULL, language TEXT NOT NULL, "
            "ordinal INTEGER NOT NULL, number TEXT NOT NULL, heading TEXT, paragraphs TEXT NOT NULL, anchor TEXT, "
            "PRIMARY KEY(act_id, language, ordinal), "
            "FOREIGN KEY(act_id, language) REFERENCES acts(act_id, language) ON DELETE CASCADE);"
        )
    finally:
        conn.close()


class _ActHTMLParser(HTMLParser):
    """Extract paragraph text/ids and page title/status from legacy HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[str, str | None]] = []
        self.title_parts: list[str] = []
        self.status_classes: set[str] = set()
        self._tag_stack: list[str] = []
        self._paragraph: list[str] | None = None
        self._paragraph_id: str | None = None
        self._title_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        self._tag_stack.append(tag)
        if tag == "title":
            self._title_depth += 1
        if tag == "span" and attrs_d.get("class"):
            self.status_classes.update(attrs_d["class"].split())
        if tag == "p":
            self._paragraph = []
            self._paragraph_id = attrs_d.get("id")
        if self._paragraph is not None and tag == "a":
            anchor = attrs_d.get("name") or attrs_d.get("id")
            if anchor and self._paragraph_id is None:
                self._paragraph_id = anchor

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._paragraph is not None:
            text = _WHITESPACE_RE.sub(" ", "".join(self._paragraph)).strip()
            self.paragraphs.append((text, self._paragraph_id))
            self._paragraph = None
            self._paragraph_id = None
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if self._tag_stack:
            # Tolerate malformed legacy markup by dropping through to matching tag.
            try:
                index = len(self._tag_stack) - 1 - self._tag_stack[::-1].index(tag)
                del self._tag_stack[index:]
            except ValueError:
                pass

    def handle_data(self, data: str) -> None:
        if self._paragraph is not None:
            self._paragraph.append(data)
        if self._title_depth:
            self.title_parts.append(data)


_ARTICLE_HEAD_RE = re.compile(r"^Статья\s+([\w./-]+)\.?\s*(.*)$", re.I)


def parse_act_html(html: str, act_id: str, language: str, url: str) -> FullAct:
    """Parse legacy Adilet document HTML into the FullAct contract."""
    parser = _ActHTMLParser()
    parser.feed(html)
    articles: list[Article] = []
    current_number: str | None = None
    heading: str | None = None
    paragraphs: list[str] = []
    anchor: str | None = None

    def flush() -> None:
        if current_number is not None:
            articles.append(Article(number=current_number, heading=heading, paragraphs=paragraphs, anchor=anchor))

    for text, paragraph_id in parser.paragraphs:
        match = _ARTICLE_HEAD_RE.match(text)
        if match:
            flush()
            current_number = match.group(1).rstrip(".")
            heading = match.group(2).strip() or None
            paragraphs = []
            anchor = None
            # Article heading anchors are not paragraph anchors.
            continue
        if current_number is not None and text:
            paragraphs.append(text)
            if anchor is None and paragraph_id:
                anchor = paragraph_id.lstrip("#")
    flush()
    if not articles:
        raise AdiletClientError(f"Adilet document contains no parseable articles: {act_id}")
    title = _WHITESPACE_RE.sub(" ", "".join(parser.title_parts)).strip()
    title = re.sub(r"\s*-\s*ИПС\s*[«\"]?.*$", "", title).strip()
    if not title:
        raise AdiletClientError(f"Adilet document has no title: {act_id}")
    return FullAct(
        act_id=act_id,
        title=title,
        url=url,
        in_force=not bool(parser.status_classes & _YTS_CLASSES),
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        articles=articles,
    )

def _dumps(hits: list[dict[str, Any]]) -> str:
    import json

    return json.dumps(hits, ensure_ascii=False)


def _loads(payload: str) -> list[dict[str, Any]]:
    import json

    return json.loads(payload)


# --- HTTP ----------------------------------------------------------------------


class AdiletClient:
    """HTTP client for the legacy Adilet search portal.

    Every request is logged (working rule 5) with endpoint, latency and
    HTTP status. Retries: 2 extra attempts with exponential backoff on
    network errors, timeouts, and 429/5xx.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        search_base_url: str | None = None,
        timeout: float = 15.0,
        retries: int = 2,
        user_agent: str | None = None,
        backoff: float = _RETRY_BACKOFF_S,
        transport: httpx.BaseTransport | None = None,
        cache: SearchCache | None = None,
        act_cache: ActCache | None = None,
    ) -> None:
        settings = get_settings()
        # Canonical base for SearchHit.url (public document pages).
        self.base_url = (base_url or settings.ADILET_BASE_URL or "https://adilet.zan.kz").rstrip("/")
        # Legacy portal that actually serves the anonymous HTML search.
        self.search_base_url = (
            search_base_url or settings.ADILET_SEARCH_BASE_URL or "https://old.adilet.zan.kz"
        ).rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._transport = transport
        self._cache = cache
        self._act_cache = act_cache
        headers = {
            "User-Agent": user_agent
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,kk;q=0.8,en;q=0.5",
        }
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AdiletClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level -------------------------------------------------------------

    def get(self, url: str) -> httpx.Response:
        """GET with timeout, retries + backoff, and per-call structured logging."""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                response = self._client.get(url)
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                status = response.status_code
                outcome = "ok" if status < 500 else "error"
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                status = 0
                outcome = "error"
                last_exc = exc
                self._log_call(url, latency_ms, status, attempt, outcome)
                if attempt < self.retries:
                    self._sleep_backoff(attempt)
                    continue
                raise AdiletUnavailable(
                    f"Adilet request failed after {self.retries + 1} attempts: {url} ({exc})"
                ) from exc

            self._log_call(url, latency_ms, status, attempt, outcome)

            if 200 <= status < 300:
                return response
            if status in _RETRYABLE_STATUS and attempt < self.retries:
                self._sleep_backoff(attempt)
                continue
            # Non-retryable HTTP error (4xx or exhausted retries): raise typed.
            if status in _RETRYABLE_STATUS:
                raise AdiletUnavailable(
                    f"Adilet unavailable: {status} after {self.retries + 1} attempts: {url}"
                )
            raise AdiletClientError(f"Adilet error {status}: {url}")

    def _sleep_backoff(self, attempt: int) -> None:
        delay = self.backoff * (2**attempt)
        logger.warning(
            "adilet retry %d/%d after %s s", attempt + 1, self.retries, delay
        )
        time.sleep(delay)

    @staticmethod
    def _log_call(url: str, latency_ms: float, status: int, attempt: int, outcome: str) -> None:
        logger.info(
            "adilet GET %s status=%d attempt=%d outcome=%s",
            url,
            status,
            attempt + 1,
            outcome,
            extra={"endpoint": url, "latency_ms": latency_ms, "status": status, "tokens": None},
        )

    # -- search ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10, language: str = "rus") -> list[SearchHit]:
        """Search legal acts; returns strict SearchHit dicts (no extras)."""
        if language not in _LANGS:
            raise ValueError(f"unsupported language: {language!r} (expected one of {sorted(_LANGS)})")
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string")
        limit = max(1, int(limit))

        if self._cache is not None:
            cached = self._cache.get(query, language)
            if cached is not None:
                logger.info("adilet cache hit: %r (%d hits)", query, len(cached), extra={"endpoint": "search_cache"})
                return [SearchHit(**h) for h in cached[:limit]]

        pages_needed = (limit + 9) // 10  # page size 10 is fixed by the portal
        collected: list[dict[str, Any]] = []
        for page in range(1, pages_needed + 1):
            if page > 1:
                time.sleep(0.5)  # small courtesy delay between pages
            # Path-style URL on the legacy portal (query-style 302-redirects
            # to exactly this form; the portal also sets a JSESSIONID cookie).
            url = (
                f"{self.search_base_url}/{language}/search/docs/"
                f"fulltext={quote(query)}&pagesize=10&page={page}"
            )
            response = self.get(url)
            html = response.text
            hits = parse_search_html(html, language)
            if page == 1 and not hits:
                # No results at all — do not cache (transient or zero hits)
                return []
            collected.extend(hits)
            if len(collected) >= limit:
                break

        hits = collected[:limit]
        if self._cache is not None and hits:
            self._cache.set(query, language, hits)
        return [SearchHit(**h) for h in hits]

    def get_full_text(self, act_id: str, language: str = "rus", force: bool = False) -> FullAct:
        """Fetch or return the cached full text of an Adilet act."""
        if language not in _LANGS:
            raise ValueError(f"unsupported language: {language!r} (expected one of {sorted(_LANGS)})")
        if not act_id or not act_id.strip():
            raise ValueError("act_id must be a non-empty string")
        cache_started = time.perf_counter()
        if self._act_cache is not None:
            cached = self._act_cache.get(act_id, language, force=force)
            if cached is not None:
                logger.info(
                    "adilet act cache hit: %s (%d articles)", act_id, len(cached.articles),
                    extra={
                        "endpoint": "act_cache",
                        "act_id": act_id,
                        "latency_ms": round((time.perf_counter() - cache_started) * 1000, 1),
                        "status": "hit",
                        "tokens": None,
                    },
                )
                return cached
        url = f"{self.search_base_url}/{language}/docs/{act_id}"
        response = self.get(url)
        act = parse_act_html(response.text, act_id, language, f"{self.base_url}/{language}/docs/{act_id}")
        if self._act_cache is not None:
            self._act_cache.set(act, language)
        return act

    def get_article(self, act_id: str, article_number: str, language: str = "rus") -> Article | None:
        """Return one exact article from the local act mirror, fetching it if needed."""
        act = self.get_full_text(act_id, language=language)
        wanted = str(article_number).strip().rstrip(".")
        return next((article for article in act.articles if article.number == wanted), None)


# --- module-level default client (the contract function) -------------------------

_default_client: AdiletClient | None = None


def _get_default_client() -> AdiletClient:
    global _default_client
    if _default_client is None:
        settings = get_settings()
        _default_client = AdiletClient(
            base_url=settings.ADILET_BASE_URL,
            cache=SearchCache(settings.DATABASE_PATH, settings.SEARCH_CACHE_TTL_DAYS),
            act_cache=ActCache(settings.DATABASE_PATH, settings.ACT_REFRESH_DAYS),
        )
    return _default_client


def search_laws(query: str, limit: int = 10, language: str = "rus") -> list[SearchHit]:
    """Search Adilet for legal acts matching ``query``.

    Contract (T1.2): returns ``list[SearchHit]`` with the strict five-field
    shape. Caches results in SQLite (TTL SEARCH_CACHE_TTL_DAYS). Raises
    :class:`AdiletUnavailable` when the portal is unreachable after retries,
    :class:`AdiletClientError` on hard HTTP errors, :class:`ValueError` on
    bad arguments.
    """
    return _get_default_client().search(query, limit=limit, language=language)


def get_full_text(act_id: str, language: str = "rus", force: bool = False) -> FullAct:
    """Get full act text, using the SQLite mirror with ACT_REFRESH_DAYS TTL."""
    return _get_default_client().get_full_text(act_id, language=language, force=force)


def get_article(act_id: str, article_number: str, language: str = "rus") -> Article | None:
    """Look up an article from the mirrored Adilet act text."""
    return _get_default_client().get_article(act_id, article_number, language=language)
