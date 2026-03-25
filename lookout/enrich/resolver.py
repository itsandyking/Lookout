"""
URL Resolver for finding vendor product pages.

This module handles:
1. Building search queries from product handles
2. Searching vendor domains (via site-restricted search)
3. Scoring and ranking candidate URLs
4. Selecting the best match with confidence scoring
"""

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import ResolverOutput, URLCandidate, VendorConfig
from .utils.helpers import handle_to_query, is_product_url

logger = logging.getLogger(__name__)

# Search endpoints
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class URLResolver:
    """
    Resolves product handles to vendor product URLs.

    Uses site-restricted search to find product pages on vendor domains.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        min_delay_ms: int = 500,
        max_delay_ms: int = 2000,
    ) -> None:
        """
        Initialize the resolver.

        Args:
            http_client: Optional shared HTTP client.
            min_delay_ms: Minimum delay between requests.
            max_delay_ms: Maximum delay between requests.
        """
        self._client = http_client
        self._owns_client = http_client is None
        self._min_delay = min_delay_ms / 1000
        self._max_delay = max_delay_ms / 1000

    async def __aenter__(self) -> "URLResolver":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()

    async def resolve(
        self,
        handle: str,
        vendor: str,
        vendor_config: VendorConfig,
        hints: str = "",
        title: str | None = None,
        barcode: str | None = None,
    ) -> ResolverOutput:
        """
        Resolve a product handle to a vendor URL.

        Args:
            handle: The Shopify product handle.
            vendor: The vendor name.
            vendor_config: Configuration for the vendor.
            hints: Optional hints from gaps/suggestions.
            title: Optional product title (better for searching).
            barcode: Optional barcode/UPC (for exact matching).

        Returns:
            ResolverOutput with candidates and selected URL.
        """
        all_candidates: list[URLCandidate] = []
        queries_used: list[str] = []

        # Strategy 1: Search by barcode (most precise)
        if barcode and barcode.strip():
            barcode_query = f"site:{vendor_config.domain} {barcode.strip()}"
            queries_used.append(f"barcode: {barcode_query}")
            try:
                candidates = await self._search_candidates(
                    barcode_query, vendor_config.domain, vendor_config
                )
                # Boost confidence for barcode matches
                for c in candidates:
                    c.confidence = min(100, c.confidence + 15)
                    c.reasoning = f"Barcode search: {c.reasoning}"
                all_candidates.extend(candidates)
            except Exception as e:
                logger.warning(f"Barcode search failed for {handle}: {e}")

        # Strategy 2: Search by product title (more accurate than handle)
        if title and title.strip():
            # Clean up the title - remove vendor name if present
            clean_title = title.strip()
            vendor_lower = vendor.lower()
            if clean_title.lower().startswith(vendor_lower):
                clean_title = clean_title[len(vendor) :].strip(" -")

            title_query = f"site:{vendor_config.domain} {clean_title}"
            queries_used.append(f"title: {title_query}")
            try:
                candidates = await self._search_candidates(
                    title_query, vendor_config.domain, vendor_config
                )
                # Boost confidence for title matches
                for c in candidates:
                    c.confidence = min(100, c.confidence + 10)
                    c.reasoning = f"Title search: {c.reasoning}"
                all_candidates.extend(candidates)
            except Exception as e:
                logger.warning(f"Title search failed for {handle}: {e}")

        # Strategy 3: Search by handle (fallback)
        handle_query = self._build_query(handle, vendor, vendor_config.domain, hints)
        queries_used.append(f"handle: {handle_query}")
        try:
            candidates = await self._search_candidates(
                handle_query, vendor_config.domain, vendor_config
            )
            for c in candidates:
                c.reasoning = f"Handle search: {c.reasoning}"
            all_candidates.extend(candidates)
        except Exception as e:
            logger.warning(f"Handle search failed for {handle}: {e}")

        # Strategy 4: Direct URL probe (fallback when search fails)
        if not all_candidates:
            queries_used.append("direct_probe")
            try:
                probe_candidates = await self._probe_direct_urls(
                    handle, vendor_config
                )
                all_candidates.extend(probe_candidates)
            except Exception as e:
                logger.warning(f"Direct URL probe failed for {handle}: {e}")

        # Deduplicate candidates by URL, keeping highest confidence
        seen_urls: dict[str, URLCandidate] = {}
        for candidate in all_candidates:
            url_key = candidate.url.lower().rstrip("/")
            if url_key not in seen_urls or candidate.confidence > seen_urls[url_key].confidence:
                seen_urls[url_key] = candidate

        deduplicated = sorted(seen_urls.values(), key=lambda c: c.confidence, reverse=True)

        output = ResolverOutput(
            handle=handle,
            vendor=vendor,
            query_used=" | ".join(queries_used),
            candidates=deduplicated[:5],
        )

        # Select best candidate
        if deduplicated:
            best = deduplicated[0]
            output.selected_url = best.url
            output.selected_confidence = best.confidence

            # Add warnings based on confidence
            if best.confidence < 70:
                output.warnings.append("LOW_MATCH_CONFIDENCE")
            elif best.confidence < 85:
                output.warnings.append("MODERATE_MATCH_CONFIDENCE")

        return output

    def _build_query(
        self,
        handle: str,
        vendor: str,
        domain: str,
        hints: str = "",
    ) -> str:
        """
        Build a search query from handle and hints.

        Args:
            handle: The product handle.
            vendor: The vendor name.
            domain: The vendor domain.
            hints: Optional hints.

        Returns:
            Search query string.
        """
        # Convert handle to words
        base_query = handle_to_query(handle)

        # Add hints if present (clean them up)
        if hints:
            # Extract useful terms from hints
            hint_words = re.findall(r"\b[a-zA-Z]{3,}\b", hints)
            # Filter out common words
            stopwords = {
                "the",
                "and",
                "for",
                "this",
                "that",
                "with",
                "from",
                "has",
                "have",
                "are",
                "was",
                "were",
                "been",
                "being",
                "missing",
                "needs",
                "add",
                "update",
                "description",
                "image",
                "images",
                "variant",
                "variants",
                "product",
            }
            hint_words = [w.lower() for w in hint_words if w.lower() not in stopwords]
            if hint_words:
                base_query += " " + " ".join(hint_words[:3])

        # Build site-restricted query
        query = f"site:{domain} {base_query}"

        return query

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    )
    async def _search_candidates(
        self,
        query: str,
        domain: str,
        vendor_config: VendorConfig,
    ) -> list[URLCandidate]:
        """Search for candidate URLs. Tries Brave Search first, falls back to DuckDuckGo."""
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

        # Try Brave Search first (if API key available)
        candidates = await self._search_brave(query, domain, vendor_config)
        if candidates:
            return candidates

        # Fall back to DuckDuckGo
        candidates = await self._search_duckduckgo(query, domain, vendor_config)
        return candidates

    async def _search_brave(
        self,
        query: str,
        domain: str,
        vendor_config: VendorConfig,
    ) -> list[URLCandidate]:
        """Search using Brave Search API."""
        import os

        api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not api_key:
            return []

        candidates: list[URLCandidate] = []
        try:
            response = await self._client.get(
                BRAVE_SEARCH_URL,
                params={"q": query, "count": 10},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                },
            )
            response.raise_for_status()
            data = response.json()

            for result in data.get("web", {}).get("results", []):
                url = result.get("url", "")
                title = result.get("title", "")
                snippet = result.get("description", "")

                if not url:
                    continue

                # Filter to target domain
                parsed = urlparse(url)
                result_domain = parsed.netloc.lower().replace("www.", "")
                if domain.lower() not in result_domain and result_domain not in domain.lower():
                    continue

                # Filter blocked paths and non-product URLs
                if not is_product_url(
                    url, vendor_config.blocked_paths, vendor_config.product_url_patterns
                ):
                    continue

                confidence = self._score_candidate(url, title, snippet, domain)
                candidates.append(
                    URLCandidate(
                        url=url,
                        confidence=confidence,
                        title=title,
                        snippet=snippet,
                        reasoning=f"Brave search: score={confidence}",
                    )
                )

            candidates.sort(key=lambda c: c.confidence, reverse=True)
            if candidates:
                logger.info(f"Brave search found {len(candidates)} candidates")
            return candidates[:5]

        except Exception as e:
            logger.warning(f"Brave search failed: {e}")
            return []

    async def _search_duckduckgo(
        self,
        query: str,
        domain: str,
        vendor_config: VendorConfig,
    ) -> list[URLCandidate]:
        """Search using DuckDuckGo HTML endpoint (fallback)."""
        try:
            html = await asyncio.to_thread(self._sync_ddg_search, query)
            if html:
                return self._parse_duckduckgo_results(html, domain, vendor_config)
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
        return []

    async def _probe_direct_urls(
        self,
        handle: str,
        vendor_config: VendorConfig,
    ) -> list[URLCandidate]:
        """Construct and probe candidate URLs directly on the vendor domain.

        Tries common URL patterns like:
        - {domain}/product/{handle}
        - {domain}/products/{handle}
        - {domain}/p/{handle}
        - {domain}/shop/{handle}

        Only returns candidates that respond with HTTP 200.
        """
        domain = vendor_config.domain
        candidates: list[URLCandidate] = []

        # Build candidate URLs from product_url_patterns
        patterns = vendor_config.product_url_patterns or ["/product/", "/products/"]
        urls_to_try: list[str] = []

        for pattern in patterns:
            # Try handle as-is and with .html suffix
            base = f"https://www.{domain}{pattern}{handle}"
            urls_to_try.append(base)
            if not base.endswith(".html"):
                urls_to_try.append(f"{base}.html")

        for url in urls_to_try:
            try:
                await asyncio.sleep(random.uniform(0.3, 0.8))
                response = await self._client.head(url, follow_redirects=True)
                if response.status_code == 200:
                    final_url = str(response.url)
                    candidates.append(
                        URLCandidate(
                            url=final_url,
                            confidence=70,
                            title="",
                            snippet="",
                            reasoning=f"Direct URL probe: {url} → {response.status_code}",
                        )
                    )
                    logger.info(f"Direct probe hit: {final_url}")
                    break  # Take the first hit
            except Exception as e:
                logger.debug(f"Direct probe failed for {url}: {e}")
                continue

        return candidates

    @staticmethod
    def _sync_ddg_search(query: str) -> str | None:
        """Run DuckDuckGo HTML search synchronously (avoids 202 JS challenge)."""
        try:
            with httpx.Client(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            ) as client:
                response = client.post(
                    DUCKDUCKGO_HTML_URL,
                    data={"q": query, "b": ""},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                return response.text
        except Exception as e:
            logger.warning(f"DuckDuckGo sync search failed: {e}")
            return None

    def _parse_duckduckgo_results(
        self,
        html: str,
        domain: str,
        vendor_config: VendorConfig,
    ) -> list[URLCandidate]:
        """
        Parse DuckDuckGo HTML search results.

        Args:
            html: The HTML response.
            domain: The vendor domain.
            vendor_config: Vendor configuration.

        Returns:
            List of URL candidates.
        """
        from bs4 import BeautifulSoup

        candidates: list[URLCandidate] = []
        soup = BeautifulSoup(html, "lxml")

        # Find result links
        for result in soup.select(".result__a"):
            url = result.get("href", "")
            title = result.get_text(strip=True)

            if not url or not title:
                continue

            # Skip if not on the target domain
            parsed = urlparse(url)
            result_domain = parsed.netloc.lower().replace("www.", "")
            if domain.lower() not in result_domain and result_domain not in domain.lower():
                continue

            # Check if it's a valid product URL
            if not is_product_url(
                url,
                vendor_config.blocked_paths,
                vendor_config.product_url_patterns,
            ):
                continue

            # Get snippet if available
            snippet_elem = result.find_next(".result__snippet")
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

            # Score the candidate
            confidence = self._score_candidate(url, title, snippet, domain)

            candidates.append(
                URLCandidate(
                    url=url,
                    confidence=confidence,
                    title=title,
                    snippet=snippet,
                    reasoning=f"Title match + URL structure scoring = {confidence}",
                )
            )

        # Sort by confidence
        candidates.sort(key=lambda c: c.confidence, reverse=True)

        # Return top 5
        return candidates[:5]

    def _score_candidate(
        self,
        url: str,
        title: str,
        snippet: str,
        domain: str,
    ) -> int:
        """
        Score a candidate URL for relevance.

        Args:
            url: The candidate URL.
            title: The page title.
            snippet: The search snippet.
            domain: The vendor domain.

        Returns:
            Confidence score 0-100.
        """
        score = 50  # Base score

        parsed = urlparse(url)
        path = parsed.path.lower()

        # Boost for product path patterns
        product_patterns = ["/product/", "/products/", "/p/", "/shop/", "/item/"]
        if any(p in path for p in product_patterns):
            score += 15

        # Penalize non-product patterns
        non_product_patterns = [
            "/blog",
            "/news",
            "/support",
            "/help",
            "/about",
            "/category",
            "/collection",
            "/search",
            "/tag",
        ]
        if any(p in path for p in non_product_patterns):
            score -= 20

        # Boost for reasonable path depth (product pages usually 2-4 segments)
        path_segments = [s for s in path.split("/") if s]
        if 1 <= len(path_segments) <= 4:
            score += 10
        elif len(path_segments) > 6:
            score -= 10

        # Boost if title looks like a product name (not too generic)
        title_lower = title.lower()
        if len(title.split()) >= 2 and len(title.split()) <= 10:
            score += 10

        # Penalize generic titles
        generic_titles = ["home", "shop", "search", "products", "all products"]
        if title_lower in generic_titles:
            score -= 25

        # Penalize if URL has query parameters suggesting search/filter
        if "?" in url and any(p in url.lower() for p in ["search=", "filter=", "page=", "sort="]):
            score -= 15

        # Ensure score is in valid range
        return max(0, min(100, score))

    async def save_output(
        self,
        output: ResolverOutput,
        artifacts_dir: Path,
    ) -> None:
        """
        Save resolver output to artifacts directory.

        Args:
            output: The resolver output.
            artifacts_dir: Path to the artifacts directory.
        """
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        resolver_path = artifacts_dir / "resolver.json"
        with open(resolver_path, "w") as f:
            json.dump(output.model_dump(mode="json"), f, indent=2, default=str)


async def resolve_product_url(
    handle: str,
    vendor: str,
    vendor_config: VendorConfig,
    hints: str = "",
    title: str | None = None,
    barcode: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ResolverOutput:
    """
    Convenience function to resolve a single product URL.

    Args:
        handle: The Shopify product handle.
        vendor: The vendor name.
        vendor_config: Configuration for the vendor.
        hints: Optional hints from gaps/suggestions.
        title: Optional product title (better for searching).
        barcode: Optional barcode/UPC (for exact matching).
        http_client: Optional shared HTTP client.

    Returns:
        ResolverOutput with candidates and selected URL.
    """
    async with URLResolver(http_client=http_client) as resolver:
        return await resolver.resolve(
            handle, vendor, vendor_config, hints, title=title, barcode=barcode
        )
