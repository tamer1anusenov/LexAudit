"""Tests for the Adilet client (T1.2).

Offline by design: HTTP is mocked with httpx.MockTransport and the HTML
fixtures in tests/adilet_fixtures/ are real responses captured from the
live legacy portal on 2026-10-06 (see docs/adilet-api.md).
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from lexaudit.adilet.client import (
    AdiletClient,
    AdiletClientError,
    AdiletUnavailable,
    SearchCache,
    SearchHit,
    parse_search_html,
)

FIXTURES = Path(__file__).resolve().parent / "adilet_fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _transport_with(html_by_page: dict[int, str] | str) -> httpx.MockTransport:
    """MockTransport returning fixture HTML; records every request URL."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if isinstance(html_by_page, str):
            body = html_by_page
        else:
            page = request.url.params.get("page", "1")
            body = html_by_page.get(int(page), "")
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=UTF-8"})

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


# --- HTML parser (offline fixtures) ------------------------------------------


def test_parse_salary_rus_fixture():
    hits = parse_search_html(_fixture("search_salary_rus_10.html"), "rus")
    assert len(hits) == 10
    # Real ground truth from the captured page (2026-10-06).
    assert [h["act_id"] for h in hits[:3]] == ["V11CB000106", "V10CL000110", "V2300032055"]
    # status_yts («Утративший силу») -> in_force False; status_new -> True
    assert hits[0]["in_force"] is False
    assert hits[2]["in_force"] is True
    for h in hits:
        assert set(h) == {"act_id", "title", "snippet", "url", "in_force"}
        assert h["url"] == f"https://adilet.zan.kz/rus/docs/{h['act_id']}"
        assert h["title"]
        assert isinstance(h["snippet"], str)


def test_parse_kazakh_fixture():
    hits = parse_search_html(_fixture("search_salary_kaz_10.html"), "kaz")
    assert len(hits) == 10
    assert hits[0]["act_id"] == "U910000541_"
    assert all(h["url"].startswith("https://adilet.zan.kz/kaz/docs/") for h in hits)
    # Kazakh text survives intact (title + snippet)
    assert "Жалақыға" in hits[0]["title"]
    assert "Қазақстан" in hits[0]["snippet"]


def test_parse_labor_and_lease_fixtures():
    assert len(parse_search_html(_fixture("search_labor_code_rus_10.html"), "rus")) == 10
    assert len(parse_search_html(_fixture("search_lease_rus_10.html"), "rus")) == 10


def test_parse_page_without_results():
    html = '<html><body><div class="serp">ничего не найдено</div></body></html>'
    assert parse_search_html(html, "rus") == []


# --- SearchHit strictness (T1.2 rule 6) ---------------------------------------


def test_search_hit_forbids_extra_and_missing_fields():
    with pytest.raises(ValueError):
        SearchHit(
            act_id="A1", title="t", snippet="s", url="u", in_force=True, extra="nope"
        )
    with pytest.raises(ValueError):
        SearchHit(act_id="A1", title="t", snippet="s", url="u")  # missing in_force
    hit = SearchHit(act_id="A1", title="t", snippet="s", url="u", in_force=True)
    assert set(hit.model_dump()) == {"act_id", "title", "snippet", "url", "in_force"}
    with pytest.raises(ValueError):  # frozen
        hit.title = "other"  # type: ignore[misc]


# --- SQLite cache --------------------------------------------------------------


def test_cache_roundtrip_and_normalization(tmp_path):
    cache = SearchCache(tmp_path / "test.db", ttl_days=7)
    assert cache.get("зарплата", "rus") is None
    cache.set("ЗАРПЛАТА", "rus", [{"a": 1}])
    # normalized key: case + whitespace independent
    assert cache.get(" зарплата ", "rus") == [{"a": 1}]
    # different language -> different key
    assert cache.get("зарплата", "kaz") is None
    # table name check
    import sqlite3

    conn = sqlite3.connect(tmp_path / "test.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "search_cache" in tables


def test_cache_ttl_expiry(tmp_path):
    import sqlite3

    cache = SearchCache(tmp_path / "test.db", ttl_days=7)
    cache.set("q", "rus", [{"a": 1}])
    assert cache.get("q", "rus") == [{"a": 1}]
    # Age the row past its TTL directly (deterministic, no sleep).
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("UPDATE search_cache SET created_at = created_at - 8 * 86400")
    conn.commit()
    conn.close()
    assert cache.get("q", "rus") is None


def test_cache_ttl_zero_disables(tmp_path):
    cache = SearchCache(tmp_path / "test.db", ttl_days=0)
    cache.set("q", "rus", [{"a": 1}])
    assert cache.get("q", "rus") is None


# --- client with mocked HTTP -----------------------------------------------------


def test_client_search_returns_hits_and_logs(caplog):
    transport = _transport_with(_fixture("search_salary_rus_10.html"))
    client = AdiletClient(transport=transport, backoff=0, cache=None)
    with caplog.at_level(logging.INFO, logger="lexaudit.adilet"):
        hits = client.search("зарплата", limit=10, language="rus")
    assert len(hits) == 10
    assert all(isinstance(h, SearchHit) for h in hits)
    assert hits[0].act_id == "V11CB000106"
    assert hits[0].url == "https://adilet.zan.kz/rus/docs/V11CB000106"
    # one request to the legacy portal, path-style URL, query percent-encoded
    assert transport.calls == [
        "https://old.adilet.zan.kz/rus/search/docs/"
        "fulltext=%D0%B7%D0%B0%D1%80%D0%BF%D0%BB%D0%B0%D1%82%D0%B0&pagesize=10&page=1"
    ]
    # structured logging (working rule 5): endpoint, latency_ms, status on the record
    rec = next(r for r in caplog.records if r.name == "lexaudit.adilet" and "endpoint" in r.__dict__)
    assert rec.__dict__["endpoint"].startswith("https://old.adilet.zan.kz/rus/search/docs/")
    assert isinstance(rec.__dict__["latency_ms"], float)
    assert rec.__dict__["status"] == 200


def test_client_search_cache_hits_no_second_request():
    transport = _transport_with(_fixture("search_salary_rus_10.html"))
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        client = AdiletClient(transport=transport, backoff=0, cache=SearchCache(f"{td}/c.db", 7))
        first = client.search("зарплата")
        second = client.search("зарплата")
    assert len(first) == 10
    assert second == first  # same hits
    assert len(transport.calls) == 1  # second call served from SQLite


def test_client_search_limit_15_fetches_two_pages():
    transport = _transport_with(
        {1: _fixture("search_salary_rus_10.html"), 2: _fixture("search_lease_rus_10.html")}
    )
    client = AdiletClient(transport=transport, backoff=0, cache=None)
    hits = client.search("зарплата", limit=15)
    assert len(hits) == 15
    assert "page=1" in transport.calls[0]
    assert "page=2" in transport.calls[1]


def test_client_retries_then_succeeds():
    bodies: list[str] = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] <= 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(
            200, text=_fixture("search_salary_rus_10.html"), headers={"content-type": "text/html"}
        )

    client = AdiletClient(transport=httpx.MockTransport(handler), backoff=0, cache=None)
    hits = client.search("зарплата")
    assert len(hits) == 10
    assert state["n"] == 3  # 2 failures + 1 success


def test_client_unavailable_after_retries():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(503, text="busy")

    client = AdiletClient(transport=httpx.MockTransport(handler), backoff=0, retries=2, cache=None)
    with pytest.raises(AdiletUnavailable):
        client.search("зарплата")
    assert state["n"] == 3  # initial + 2 retries


def test_client_network_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = AdiletClient(transport=httpx.MockTransport(handler), backoff=0, retries=1, cache=None)
    with pytest.raises(AdiletUnavailable, match="connection refused"):
        client.search("зарплата")


def test_client_404_raises_without_retry():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(404, text="not found")

    client = AdiletClient(transport=httpx.MockTransport(handler), backoff=0, retries=2, cache=None)
    with pytest.raises(AdiletClientError, match="404"):
        client.search("зарплата")
    assert state["n"] == 1


def test_client_zero_results_empty_list():
    transport = _transport_with("<html><body>ничего</body></html>")
    client = AdiletClient(transport=transport, backoff=0, cache=None)
    assert client.search("несуществующийтермин12345") == []


def test_client_argument_validation():
    client = AdiletClient(transport=_transport_with(""), backoff=0, cache=None)
    with pytest.raises(ValueError):
        client.search("   ")
    with pytest.raises(ValueError):
        client.search("зарплата", language="fre")


def test_user_agent_header_set():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    client = AdiletClient(transport=httpx.MockTransport(handler), backoff=0, cache=None)
    client.search("зарплата")
    assert seen["ua"].startswith("Mozilla/5.0")


# --- module-level contract function ---------------------------------------------


def test_search_laws_contract(monkeypatch, tmp_path):
    from lexaudit import adilet as adilet_mod
    from lexaudit.adilet import client as client_mod

    transport = _transport_with(_fixture("search_salary_rus_10.html"))
    monkeypatch.setattr(
        client_mod,
        "_default_client",
        AdiletClient(transport=transport, backoff=0, cache=SearchCache(tmp_path / "c.db", 7)),
    )
    hits = adilet_mod.search_laws("зарплата", limit=10, language="rus")
    assert isinstance(hits, list) and len(hits) == 10
    assert all(type(h) is SearchHit for h in hits)
    assert all(set(h.model_dump()) == {"act_id", "title", "snippet", "url", "in_force"} for h in hits)
