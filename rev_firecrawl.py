"""
Rev Firecrawl — Self-Hosted Web Search & Scrape Layer
======================================================
External intelligence layer for the Rev M2M bridge.
Firecrawl runs as a self-hosted Docker service, providing:
  - Web search (SERP-like results)
  - Web scraping (URL → markdown/structured data)
  - Content extraction (LLM-ready format)

Architecture:
  ┌──────────┐     ┌─────────────────┐     ┌──────────────┐
  │ Rev CLI  │────▶│  Firecrawl API  │────▶│  Web / SERP  │
  │  / M2M   │     │  (self-hosted)  │     │  (internet)  │
  └──────────┘     └─────────────────┘     └──────────────┘
                          │
                          ▼
                   ┌─────────────┐
                   │  Playwright  │ (headless browser)
                   │  + Scraping  │
                   └─────────────┘

Setup:
  docker compose up -d   # starts Firecrawl on :3002
  export FIRECRAWL_URL=http://localhost:3002
"""

import json
import os
from typing import Optional, List, Dict, Any

FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "http://localhost:3002")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "fc-self-hosted")


class FirecrawlClient:
    """Client for self-hosted Firecrawl instance."""

    def __init__(self, base_url: Optional[str] = None,
                 api_key: Optional[str] = None):
        self.base_url = (base_url or FIRECRAWL_URL).rstrip("/")
        self.api_key = api_key or FIRECRAWL_API_KEY
        self._session = None

    def _get_session(self):
        if self._session is None:
            from curl_cffi import requests as cr
            self._session = cr.Session(impersonate="chrome131")
        return self._session

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def health(self) -> bool:
        """Check if Firecrawl is running."""
        try:
            s = self._get_session()
            r = s.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def search(self, query: str, limit: int = 5,
               timeout: int = 30) -> List[Dict[str, Any]]:
        """Web search — returns SERP-like results.

        Returns:
            [{"title": ..., "url": ..., "description": ..., "content": ...}]
        """
        s = self._get_session()
        r = s.post(
            f"{self.base_url}/v1/search",
            headers=self._headers(),
            json={"query": query, "limit": limit},
            timeout=timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"firecrawl search: HTTP {r.status_code} — {r.text[:200]}")
        data = r.json()
        results = data.get("data", [])
        return [{
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
            "content": item.get("markdown", item.get("content", "")),
        } for item in results]

    def scrape(self, url: str, formats: Optional[List[str]] = None,
               timeout: int = 60) -> Dict[str, Any]:
        """Scrape a URL → structured content.

        Args:
            url: Target URL
            formats: ["markdown", "html", "rawHtml", "links", "screenshot"]

        Returns:
            {"markdown": ..., "html": ..., "metadata": {...}}
        """
        s = self._get_session()
        body = {"url": url}
        if formats:
            body["formats"] = formats
        r = s.post(
            f"{self.base_url}/v1/scrape",
            headers=self._headers(),
            json=body,
            timeout=timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"firecrawl scrape: HTTP {r.status_code} — {r.text[:200]}")
        data = r.json()
        return data.get("data", {})

    def crawl(self, url: str, max_depth: int = 2,
              limit: int = 10, timeout: int = 120) -> Dict[str, Any]:
        """Crawl a website (multi-page).

        Returns:
            {"job_id": ..., "status": ..., "pages": [...]}
        """
        s = self._get_session()
        r = s.post(
            f"{self.base_url}/v1/crawl",
            headers=self._headers(),
            json={
                "url": url,
                "maxDepth": max_depth,
                "limit": limit,
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"firecrawl crawl: HTTP {r.status_code} — {r.text[:200]}")
        return r.json()

    def extract(self, url: str, prompt: str,
                schema: Optional[Dict] = None,
                timeout: int = 60) -> Dict[str, Any]:
        """LLM-powered extraction from a URL.

        Args:
            url: Target URL
            prompt: What to extract
            schema: Optional JSON schema for structured output

        Returns:
            {"data": {...}, "usage": {...}}
        """
        s = self._get_session()
        body = {
            "urls": [url],
            "prompt": prompt,
        }
        if schema:
            body["schema"] = schema
        r = s.post(
            f"{self.base_url}/v1/extract",
            headers=self._headers(),
            json=body,
            timeout=timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"firecrawl extract: HTTP {r.status_code} — {r.text[:200]}")
        return r.json()

    def map(self, url: str, timeout: int = 30) -> List[str]:
        """Map a website — discover all URLs.

        Returns:
            ["https://example.com/page1", ...]
        """
        s = self._get_session()
        r = s.post(
            f"{self.base_url}/v1/map",
            headers=self._headers(),
            json={"url": url},
            timeout=timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"firecrawl map: HTTP {r.status_code} — {r.text[:200]}")
        return r.json().get("links", [])


# Singleton
_client = None


def get_firecrawl() -> FirecrawlClient:
    global _client
    if _client is None:
        _client = FirecrawlClient()
    return _client
