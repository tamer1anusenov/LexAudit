"""LexAudit `adilet` module: Adilet (adilet.zan.kz) API client, tools, cache."""
from lexaudit.adilet.client import (
    AdiletClient,
    AdiletClientError,
    AdiletError,
    AdiletUnavailable,
    ActCache,
    Article,
    FullAct,
    SearchCache,
    SearchHit,
    parse_search_html,
    get_article,
    get_full_text,
    search_laws,
)

__all__ = [
    "AdiletClient",
    "AdiletClientError",
    "AdiletError",
    "AdiletUnavailable",
    "ActCache",
    "Article",
    "FullAct",
    "SearchCache",
    "SearchHit",
    "parse_search_html",
    "get_article",
    "get_full_text",
    "search_laws",
]
