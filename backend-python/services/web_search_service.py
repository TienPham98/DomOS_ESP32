"""Small real-time search adapter used as a server-side LLM tool."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
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


class _ReadableTextParser(HTMLParser):
    """Extract compact visible text from a search result page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())

    def text(self, limit: int = 6000) -> str:
        return plain_speech_text(" ".join(self.parts))[:limit]


def _is_public_result_url(url: str) -> bool:
    """Reject obvious local/private targets before fetching search result pages."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global


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
        if "gia vang" in fold_vietnamese(query):
            # The first focused SJC result normally contains the complete quote.
            # Fetching more pages adds several seconds without improving the answer.
            results = await self._enrich_results(results, limit=1)
        return {"query": query, "provider": provider, "results": results[:settings.WEB_SEARCH_MAX_RESULTS]}

    async def _enrich_results(
        self,
        results: list[dict[str, str]],
        *,
        limit: int,
    ) -> list[dict[str, str]]:
        """Fetch a few result pages when snippets do not contain complete prices."""
        selected = results[:limit]
        details = await asyncio.gather(
            *(self._fetch_result_text(item.get("url", "")) for item in selected),
            return_exceptions=True,
        )
        enriched: list[dict[str, str]] = []
        for item, detail in zip(selected, details):
            copy = dict(item)
            if isinstance(detail, str) and detail:
                copy["content"] = detail
            enriched.append(copy)
        enriched.extend(results[limit:])
        return enriched

    async def _fetch_result_text(self, url: str) -> str:
        if not _is_public_result_url(url):
            return ""
        # Resolve once and reject private answers. This is a defence-in-depth
        # guard for URLs returned by third-party search providers.
        host = urlparse(url).hostname or ""
        try:
            resolved = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError:
            return ""
        if not resolved or any(
            not ipaddress.ip_address(entry[4][0]).is_global for entry in resolved
        ):
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=min(float(settings.WEB_SEARCH_TIMEOUT_SEC), 3.0),
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; DomOS-Voice-Gateway/1.0)"},
            ) as client:
                response = await client.get(url)
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "").lower():
                return ""
            parser = _ReadableTextParser()
            parser.feed(response.text[:500_000])
            return parser.text()
        except (httpx.HTTPError, ValueError):
            logger.debug("Could not enrich search result url=%s", url, exc_info=True)
            return ""

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
            headers={"User-Agent": "Mozilla/5.0 (compatible; DomOS-Voice-Gateway/1.0)"},
        ) as client:
            # DuckDuckGo's HTML endpoint currently returns a 202 anti-bot page
            # for GET requests from cloud hosts. Its documented HTML form POST
            # still returns the server-rendered result list that this parser
            # consumes, without JavaScript or a local browser.
            response = await client.post(
                settings.WEB_SEARCH_BASE_URL,
                data={"q": query, "kl": "vn-vi"},
            )
        response.raise_for_status()
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        if not parser.results:
            logger.warning("DuckDuckGo returned no parseable results for query=%r", query)
        return parser.results


web_search_service = WebSearchService()
