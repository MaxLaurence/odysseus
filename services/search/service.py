# services/search/service.py
"""Search service — clean interface for web search."""

from dataclasses import dataclass
import inspect
from typing import List, Optional, Dict, Any

from . import (
    comprehensive_web_search,
    fetch_webpage_content,
    get_search_config,
)


@dataclass
class SearchResult:
    """A single search result."""
    url: str
    title: str
    snippet: str
    content: Optional[str] = None


@dataclass
class SearchResponse:
    """Response from a search query."""
    query: str
    results: List[SearchResult]
    total: int
    cached: bool = False


class SearchService:
    """
    Web search service.

    Usage:
        service = SearchService()
        result = await service.search("python async patterns")
        for r in result.results:
            print(f"{r.title}: {r.url}")
    """

    def __init__(self, default_depth: int = 1, fetch_content: bool = True):
        self.default_depth = default_depth
        self.fetch_content = fetch_content

    async def search(
        self,
        query: str,
        depth: Optional[int] = None,
        fetch_content: Optional[bool] = None,
    ) -> SearchResponse:
        """
        Search the web.

        Args:
            query: Search query
            depth: Search depth (1=quick, 2=thorough, 3=comprehensive)
            fetch_content: Whether to fetch full page content

        Returns:
            SearchResponse with results
        """
        depth = depth or self.default_depth

        # comprehensive_web_search is synchronous and, with return_sources=True,
        # returns (context_str, [{"url", "title"}, ...]). Run it off the event
        # loop so we don't block it, and use the source list as the result rows.
        # Some tests and older integrations still provide the legacy async
        # max_results/fetch_content shape; tolerate that wrapper shape too.
        import asyncio
        max_results = 10 * depth

        if inspect.iscoroutinefunction(comprehensive_web_search):
            search_payload = await comprehensive_web_search(
                query,
                max_results=max_results,
                fetch_content=fetch_content if fetch_content is not None else self.fetch_content,
            )
        else:
            def _run_search():
                try:
                    return comprehensive_web_search(
                        query,
                        max_pages=max_results,
                        return_sources=True,
                    )
                except TypeError as exc:
                    if "unexpected keyword argument" not in str(exc):
                        raise
                    return comprehensive_web_search(
                        query,
                        max_results=max_results,
                        fetch_content=fetch_content if fetch_content is not None else self.fetch_content,
                    )

            search_payload = await asyncio.to_thread(_run_search)

        if inspect.isawaitable(search_payload):
            search_payload = await search_payload
        if isinstance(search_payload, tuple) and len(search_payload) >= 2:
            raw_results = search_payload[1]
        else:
            raw_results = search_payload or []

        results = []
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            results.append(SearchResult(
                url=r.get("url", ""),
                title=r.get("title", ""),
                snippet=r.get("snippet", ""),
                content=r.get("content"),
            ))

        return SearchResponse(
            query=query,
            results=results,
            total=len(results),
        )

    async def fetch_content(self, url: str) -> Optional[str]:
        """Fetch content from a URL."""
        return await fetch_webpage_content(url)

    def get_config(self) -> Dict[str, Any]:
        """Get current search configuration."""
        return get_search_config()
