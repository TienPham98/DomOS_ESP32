"""Small real-time search adapter used as a server-side LLM tool."""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from config import settings
from services.text_normalization import fold_vietnamese, plain_speech_text

logger = logging.getLogger("domos.web-search")


def needs_web_search(text: str) -> bool:
    """Detect questions whose answer is likely to change after model training."""
    folded = fold_vietnamese(text)
    patterns = (
        r"\b(hom nay|hien tai|moi nhat|vua qua|toi qua|luc nay|bay gio)\b",
        r"\b(tin tuc|thoi su|thoi tiet|gia vang|gia xang|ty gia|chung khoan)\b",
        r"\b(ket qua|lich thi dau|bang xep hang|ti so|tran dau)\b",
        r"\b(today|current|latest|news|weather|price|score|schedule)\b",
    )
    return any(re.search(pattern, folded) for pattern in patterns)


def _result(title: Any, url: Any, snippet: Any) -> dict[str, str] | None:
    clean_title = plain_speech_text(title)
    clean_url = str(url or "").strip()
    clean_snippet = plain_speech_text(snippet)
    if not clean_title or not clean_url.startswith(("http://", "https://")):
        return None
    return {
        "title": clean_title[:180],
        "url": clean_url[:500],
        "snippet": clean_snippet[:600],
    }


def _unwrap_duckduckgo_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    return unquote(target) if target else url


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.capture = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self.current = {"title": "", "url": _unwrap_duckduckgo_url(attributes.get("href") or ""), "snippet": ""}
            self.capture = "title"
        elif self.current is not None and "result__snippet" in classes:
            self.capture = "snippet"

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture] += data

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag == "a" and self.capture == "title":
            self.capture = ""
        elif tag in {"a", "div"} and self.capture == "snippet":
            parsed = _result(**self.current)
            if parsed:
                self.results.append(parsed)
            self.current = None
            self.capture = ""


class WebSearchService:
    async def search(self, query: str) -> dict[str, Any]:
        query = plain_speech_text(query).strip()
        if not query:
            raise ValueError("query must not be empty")
        if not settings.WEB_SEARCH_BASE_URL:
            raise RuntimeError("WEB_SEARCH_BASE_URL is not configured")
        provider = settings.WEB_SEARCH_PROVIDER.lower()
        if provider == "auto":
            provider = "tavily" if settings.TAVILY_API_KEY else (
                "serper" if settings.SERPER_API_KEY else "duckduckgo"
            )
        if provider == "tavily":
            results = await self._tavily(query)
        elif provider == "serper":
            results = await self._serper(query)
        elif provider == "duckduckgo":
            results = await self._duckduckgo(query)
        else:
            raise RuntimeError("web search is disabled")
        return {"query": query, "provider": provider, "results": results[:settings.WEB_SEARCH_MAX_RESULTS]}

    async def _tavily(self, query: str) -> list[dict[str, str]]:
        if not settings.TAVILY_API_KEY:
            raise RuntimeError("TAVILY_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SEC) as client:
            response = await client.post(
                settings.WEB_SEARCH_BASE_URL,
                json={
                    "api_key": settings.TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": settings.WEB_SEARCH_MAX_RESULTS,
                    "include_answer": False,
                },
            )
        response.raise_for_status()
        return [
            parsed for item in response.json().get("results", [])
            if (parsed := _result(item.get("title"), item.get("url"), item.get("content")))
        ]

    async def _serper(self, query: str) -> list[dict[str, str]]:
        if not settings.SERPER_API_KEY:
            raise RuntimeError("SERPER_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SEC) as client:
            response = await client.post(
                settings.WEB_SEARCH_BASE_URL,
                headers={"X-API-KEY": settings.SERPER_API_KEY},
                json={"q": query, "num": settings.WEB_SEARCH_MAX_RESULTS},
            )
        response.raise_for_status()
        return [
            parsed for item in response.json().get("organic", [])
            if (parsed := _result(item.get("title"), item.get("link"), item.get("snippet")))
        ]

    async def _duckduckgo(self, query: str) -> list[dict[str, str]]:
        async with httpx.AsyncClient(
            timeout=settings.WEB_SEARCH_TIMEOUT_SEC,
            follow_redirects=True,
            headers={"User-Agent": "DomOS-Voice-Gateway/1.0"},
        ) as client:
            response = await client.get(settings.WEB_SEARCH_BASE_URL, params={"q": query})
        response.raise_for_status()
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        if not parser.results:
            logger.warning("DuckDuckGo returned no parseable results for query=%r", query)
        return parser.results


web_search_service = WebSearchService()
