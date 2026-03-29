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
        sku: str | None = None,
        catalog_price: float | None = None,
    ) -> ResolverOutput:
        """
        Resolve a product handle to a vendor URL.

        Search strategies (in order of precision):
        1. Barcode on vendor site — most precise
        2. SKU on vendor site — very precise (vendor SKUs appear on product pages)
        3. Title on vendor site — primary discovery
        4. Handle on vendor site — fallback discovery
        5. Vendor + Title broad search — validation / confidence signal
        6. Direct URL probe — last resort construction

        Args:
            handle: The Shopify product handle.
            vendor: The vendor name.
            vendor_config: Configuration for the vendor.
            hints: Optional hints from gaps/suggestions.
            title: Optional product title (better for searching).
            barcode: Optional barcode/UPC (for exact matching).
            sku: Optional vendor SKU (for precise matching).

        Returns:
            ResolverOutput with candidates and selected URL.
        """
        all_candidates: list[URLCandidate] = []
        queries_used: list[str] = []
        domain = vendor_config.domain

        # Clean title once (remove vendor prefix if present)
        clean_title = ""
        if title and title.strip():
            clean_title = title.strip()
            vendor_lower = vendor.lower()
            if clean_title.lower().startswith(vendor_lower):
                clean_title = clean_title[len(vendor):].strip(" -")

        # Strategy 1: Barcode on vendor site (most precise — but only if barcode appears in results)
        if barcode and barcode.strip():
            barcode_clean = barcode.strip()
            query = f"site:{domain} {barcode_clean}"
            queries_used.append(f"barcode: {query}")
            try:
                candidates = await self._search_candidates(query, domain, vendor_config, product_title=clean_title)
                for c in candidates:
                    # Only boost if barcode actually appears in the result
                    if barcode_clean in c.snippet or barcode_clean in c.url or barcode_clean in c.title:
                        c.confidence = min(100, c.confidence + 15)
                        c.reasoning = f"Barcode MATCH: {c.reasoning}"
                    else:
                        # Barcode search returned results but barcode isn't in them
                        # These are generic results, don't boost — actually penalize slightly
                        c.confidence = max(0, c.confidence - 5)
                        c.reasoning = f"Barcode search (no match in result): {c.reasoning}"
                all_candidates.extend(candidates)
            except Exception as e:
                logger.warning(f"Barcode search failed for {handle}: {e}")

        # Strategy 2: SKU on vendor site (very precise — same verification logic)
        if sku and sku.strip():
            sku_clean = sku.strip()
            # Also try SKU prefix (vendor style code) which is more likely to appear on pages
            sku_prefix = sku_clean.split("-")[0] if "-" in sku_clean else sku_clean[:8]
            query = f"site:{domain} {sku_clean}"
            queries_used.append(f"sku: {query}")
            try:
                candidates = await self._search_candidates(query, domain, vendor_config, product_title=clean_title)
                for c in candidates:
                    if sku_clean in c.snippet or sku_clean in c.url or sku_prefix in c.url:
                        c.confidence = min(100, c.confidence + 12)
                        c.reasoning = f"SKU MATCH: {c.reasoning}"
                    else:
                        c.confidence = max(0, c.confidence - 5)
                        c.reasoning = f"SKU search (no match in result): {c.reasoning}"
                all_candidates.extend(candidates)
            except Exception as e:
                logger.warning(f"SKU search failed for {handle}: {e}")

        # Strategy 3: Title on vendor site (primary discovery)
        if clean_title:
            query = f"site:{domain} {clean_title}"
            queries_used.append(f"title: {query}")
            try:
                candidates = await self._search_candidates(query, domain, vendor_config, product_title=clean_title)
                for c in candidates:
                    c.confidence = min(100, c.confidence + 10)
                    c.reasoning = f"Title search: {c.reasoning}"
                all_candidates.extend(candidates)
            except Exception as e:
                logger.warning(f"Title search failed for {handle}: {e}")

        # Strategy 4: Handle on vendor site (fallback discovery — handle is unreliable
        # since it's our Shopify store's handle, not the vendor's)
        if not all_candidates:
            handle_query = self._build_query(handle, vendor, domain, hints)
            queries_used.append(f"handle: {handle_query}")
            try:
                candidates = await self._search_candidates(handle_query, domain, vendor_config, product_title=clean_title)
                for c in candidates:
                    c.reasoning = f"Handle search: {c.reasoning}"
                all_candidates.extend(candidates)
            except Exception as e:
                logger.warning(f"Handle search failed for {handle}: {e}")

        # Strategy 5: Broad vendor+title search (validation signal)
        # Not restricted to vendor site — cross-retailer presence boosts confidence,
        # absence or conflicting results reduces it
        if clean_title and all_candidates:
            search_vendor = vendor_config.search_brand_name or vendor
            broad_query = f"{search_vendor} {clean_title}"
            queries_used.append(f"broad: {broad_query}")
            try:
                broad_html = await asyncio.to_thread(self._sync_ddg_search, broad_query)
                if not broad_html:
                    # Try Brave if DDG fails
                    import os
                    if os.environ.get("BRAVE_SEARCH_API_KEY"):
                        resp = await self._client.get(
                            BRAVE_SEARCH_URL,
                            params={"q": broad_query, "count": 5},
                            headers={
                                "Accept": "application/json",
                                "X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"],
                            },
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            broad_count = len(data.get("web", {}).get("results", []))
                        else:
                            broad_count = 0
                    else:
                        broad_count = 0
                else:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(broad_html, "lxml")
                    broad_count = len(soup.select(".result__a"))

                if broad_count >= 3:
                    # Multiple retailers confirm this product exists
                    for c in all_candidates[:1]:
                        c.confidence = min(100, c.confidence + 5)
                        c.reasoning += f" +broad_validated({broad_count} results)"
                elif broad_count == 0 and all_candidates:
                    # No broad results — product may not exist or name is wrong
                    for c in all_candidates:
                        c.confidence = max(0, c.confidence - 10)
                        c.reasoning += " -no_broad_validation"
            except Exception as e:
                logger.debug(f"Broad validation search failed: {e}")

        # Deduplicate candidates by URL, keeping highest confidence
        seen_urls: dict[str, URLCandidate] = {}
        for candidate in all_candidates:
            url_key = candidate.url.lower().rstrip("/")
            if url_key not in seen_urls or candidate.confidence > seen_urls[url_key].confidence:
                seen_urls[url_key] = candidate

        # Title-match scoring: boost candidates that match the product title,
        # penalize those that don't. This is the strongest signal for
        # distinguishing product pages from category pages.
        if clean_title:
            from difflib import SequenceMatcher

            import re as _re

            title_lower = clean_title.lower()
            # Normalize: strip punctuation, split into words
            title_words = set(_re.findall(r'[a-z0-9]+', title_lower))
            # Remove common filler words
            filler = {"the", "a", "an", "by", "for", "in", "of", "and", "with"}
            title_words -= filler

            for candidate in seen_urls.values():
                candidate_title = candidate.title.lower()
                candidate_words = set(_re.findall(r'[a-z0-9]+', candidate_title)) - filler

                if not title_words or not candidate_words:
                    continue

                overlap = title_words & candidate_words
                overlap_ratio = len(overlap) / len(title_words)
                seq_ratio = SequenceMatcher(None, title_lower, candidate_title).ratio()

                # Check for critical word mismatches — model numbers and
                # product types that distinguish similar products
                # e.g., "99Ti Skis" vs "95 Boots"
                critical_mismatch = False
                # Words in expected title NOT in candidate
                missing_words = title_words - candidate_words
                # Words in candidate NOT in expected title
                extra_words = candidate_words - title_words
                # Product type words that indicate wrong product entirely
                type_words = {"ski", "skis", "boot", "boots", "shoe", "shoes",
                             "jacket", "pants", "helmet", "goggles", "sunglasses",
                             "gloves", "pole", "poles", "binding", "bindings",
                             "board", "snowboard",
                             "pad", "bundle", "pack", "kit", "set", "system", "combo"}
                missing_types = missing_words & type_words
                extra_types = extra_words & type_words
                if missing_types and extra_types:
                    # Candidate has a different product type than expected
                    critical_mismatch = True

                # Height/fit words — "Low" vs "Mid" is a different product
                height_fit_words = {"low", "mid", "high", "tall", "short",
                                    "wide", "narrow"}
                missing_height = missing_words & height_fit_words
                extra_height = extra_words & height_fit_words
                if missing_height and extra_height:
                    critical_mismatch = True
                elif extra_height and not (title_words & height_fit_words):
                    candidate.confidence = max(0, candidate.confidence - 15)
                    candidate.reasoning += " -asymmetric_height"

                # Edition/variant words — "Pro" vs "Lite" is a different SKU
                edition_words = {"standard", "pro", "plus", "lite", "max",
                                 "mini", "ultra", "evo", "comp"}
                missing_edition = missing_words & edition_words
                extra_edition = extra_words & edition_words
                if missing_edition and extra_edition:
                    critical_mismatch = True

                # Collab/special edition detection — if candidate has collab
                # markers not present in our title, penalize heavily
                collab_markers = {"shf", "collab", "collaboration", "limited",
                                  "edition", "special"}
                # Also check for "×" or " x " crossover pattern in candidate but not title
                candidate_has_collab = bool(extra_words & collab_markers)
                if not candidate_has_collab:
                    collab_pattern = r'(?:\s[x×]\s|×)'
                    if (_re.search(collab_pattern, candidate_title)
                            and not _re.search(collab_pattern, title_lower)):
                        candidate_has_collab = True
                if candidate_has_collab:
                    candidate.confidence = max(0, candidate.confidence - 25)
                    candidate.reasoning += " -collab_mismatch"

                demographics = {"youth", "kids", "boys", "girls", "mens", "men",
                                "womens", "women", "unisex", "junior", "jr"}
                expected_demos = title_words & demographics
                candidate_demos = candidate_words & demographics
                if expected_demos and candidate_demos:
                    if not expected_demos & candidate_demos:
                        candidate.confidence = max(0, candidate.confidence - 15)
                        candidate.reasoning += " -demographic_mismatch"

                # Model numbers — if expected has a number not in candidate
                _year_pattern = _re.compile(r"20[2-3]\d")
                expected_years = set(_year_pattern.findall(title_lower))
                candidate_years = set(_year_pattern.findall(candidate_title))
                expected_numbers = set(_re.findall(r"\d+", title_lower)) - expected_years
                candidate_numbers = set(_re.findall(r"\d+", candidate_title)) - candidate_years

                if expected_numbers and candidate_numbers:
                    if not expected_numbers & candidate_numbers:
                        critical_mismatch = True

                if expected_years and candidate_years:
                    if not expected_years & candidate_years:
                        candidate.confidence = max(0, candidate.confidence - 5)
                        candidate.reasoning += " -year_mismatch"

                # Check for foreign product names — words in the candidate
                # that aren't in our title and aren't generic. If a candidate
                # says "Protac" and our product is "BWII", that's a different product.
                generic_words = type_words | height_fit_words | edition_words | demographics | {
                    "rope", "ropes", "cord", "ski", "skis", "boot", "boots",
                    "new", "sale",
                    "2024", "2025", "2026", "2027",
                }
                # Also treat the vendor name and common size/measurement words as generic
                vendor_words = set(_re.findall(r'[a-z0-9]+', vendor.lower())) if vendor else set()
                generic_words |= vendor_words

                foreign_names = extra_words - generic_words - set(_re.findall(r'\d+', candidate_title))
                missing_names = missing_words - generic_words - set(_re.findall(r'\d+', title_lower))

                if foreign_names and missing_names:
                    # Candidate has a different product name AND is missing ours
                    # e.g., candidate="Protac" but we want "BWII"
                    candidate.confidence = max(0, candidate.confidence - 20)
                    candidate.reasoning += f" -foreign_product({','.join(sorted(foreign_names)[:2])})"

                if critical_mismatch:
                    candidate.confidence = max(0, candidate.confidence - 30)
                    candidate.reasoning += f" -critical_mismatch(type/model)"
                elif overlap_ratio >= 0.6 or seq_ratio >= 0.5:
                    # Strong title match — boost based on word overlap
                    boost = int(20 * overlap_ratio)
                    # Check URL for title words too — product URLs often contain
                    # the model name (e.g., /product/10-5mm-2764-bwii/)
                    url_words = set(_re.findall(r'[a-z0-9]+', candidate.url.lower()))
                    url_title_overlap = title_words & url_words
                    title_in_url = len(url_title_overlap) / len(title_words) if title_words else 0
                    if title_in_url > 0.5:
                        boost += 5
                    candidate.confidence = min(100, candidate.confidence + boost)
                    candidate.reasoning += f" +title_match({overlap_ratio:.0%})"
                elif overlap_ratio < 0.2 and seq_ratio < 0.3:
                    # Weak match — likely a category page or wrong product
                    candidate.confidence = max(0, candidate.confidence - 25)
                    candidate.reasoning += f" -title_mismatch({seq_ratio:.0%})"
                elif overlap_ratio < 0.3:
                    # Moderate mismatch
                    candidate.confidence = max(0, candidate.confidence - 10)
                    candidate.reasoning += f" -title_weak({seq_ratio:.0%})"

        # Price comparison scoring: compare catalog price to prices found in
        # candidate snippets/titles (search results often include "$XX.XX")
        if catalog_price and catalog_price > 0:
            import re as _re_price

            price_pattern = _re_price.compile(r'\$(\d+(?:[.,]\d{2})?)')

            for candidate in seen_urls.values():
                # Try to extract a price from snippet or title
                candidate_price = None
                for text in (candidate.snippet, candidate.title):
                    if not text:
                        continue
                    matches = price_pattern.findall(text)
                    if matches:
                        try:
                            candidate_price = float(matches[0].replace(",", ""))
                            break
                        except (ValueError, TypeError):
                            continue

                if candidate_price is None or candidate_price <= 0:
                    continue  # No price data — don't penalize

                price_diff = abs(candidate_price - catalog_price) / catalog_price
                if price_diff <= 0.20:
                    candidate.confidence = min(100, candidate.confidence + 10)
                    candidate.reasoning += f" +price_match(${candidate_price:.0f}≈${catalog_price:.0f})"
                elif price_diff > 0.50:
                    candidate.confidence = max(0, candidate.confidence - 15)
                    candidate.reasoning += f" -price_mismatch(${candidate_price:.0f}vs${catalog_price:.0f})"

        # Strategy 6: Direct URL probe (last resort — only works when our handle
        # happens to match the vendor's URL structure, e.g., Patagonia)
        if not seen_urls:
            queries_used.append("direct_probe")
            try:
                probe_candidates = await self._probe_direct_urls(handle, vendor_config)
                for c in probe_candidates:
                    c.confidence = 75
                    c.reasoning = f"Direct URL probe: {c.reasoning}"
                for c in probe_candidates:
                    url_key = c.url.lower().rstrip("/")
                    if url_key not in seen_urls:
                        seen_urls[url_key] = c
            except Exception as e:
                logger.debug(f"Direct URL probe failed for {handle}: {e}")

        deduplicated = sorted(seen_urls.values(), key=lambda c: c.confidence, reverse=True)

        output = ResolverOutput(
            handle=handle,
            vendor=vendor,
            query_used=" | ".join(queries_used),
            candidates=deduplicated[:5],
        )

        # Select best candidate, verifying URL is live
        if deduplicated:
            selected = None
            for candidate in deduplicated:
                # HEAD-check to verify the URL is live (not 404/redirect-to-404)
                try:
                    resp = await self._client.head(
                        candidate.url, follow_redirects=True, timeout=10
                    )
                    if resp.status_code == 200:
                        selected = candidate
                        break
                    else:
                        logger.info(
                            "URL probe returned %d, skipping: %s",
                            resp.status_code, candidate.url[:80],
                        )
                        output.warnings.append(
                            f"URL_NOT_LIVE: {candidate.url[:80]} → {resp.status_code}"
                        )
                except Exception as e:
                    logger.debug("URL probe failed for %s: %s", candidate.url[:80], e)
                    # If HEAD fails, still try this candidate (might work with full GET)
                    selected = candidate
                    break

            if selected:
                output.selected_url = selected.url
                output.selected_confidence = selected.confidence

                # Add warnings based on confidence
                if selected.confidence < 70:
                    output.warnings.append("LOW_MATCH_CONFIDENCE")
                elif selected.confidence < 85:
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
        product_title: str = "",
    ) -> list[URLCandidate]:
        """Search for candidate URLs. Tries Brave, SearXNG, then DuckDuckGo."""
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

        # Try Brave Search first (if API key available)
        candidates = await self._search_brave(query, domain, vendor_config, product_title=product_title)
        if candidates:
            return candidates

        # Fall back to SearXNG (self-hosted metasearch)
        candidates = await self._search_searxng(query, domain, vendor_config, product_title=product_title)
        if candidates:
            return candidates

        # Last resort: DuckDuckGo HTML scraping
        candidates = await self._search_duckduckgo(query, domain, vendor_config)
        return candidates

    async def _search_brave(
        self,
        query: str,
        domain: str,
        vendor_config: VendorConfig,
        product_title: str = "",
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

                confidence = self._score_candidate(url, title, snippet, domain, product_title=product_title)
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

    async def _search_searxng(
        self,
        query: str,
        domain: str,
        vendor_config: VendorConfig,
        product_title: str = "",
        searxng_url: str = "http://localhost:8080",
    ) -> list[URLCandidate]:
        """Search using self-hosted SearXNG metasearch engine."""
        candidates: list[URLCandidate] = []
        try:
            response = await self._client.get(
                f"{searxng_url}/search",
                params={"q": query, "format": "json"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            for result in data.get("results", []):
                url = result.get("url", "")
                title = result.get("title", "")
                snippet = result.get("content", "")

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

                confidence = self._score_candidate(url, title, snippet, domain, product_title=product_title)
                candidates.append(
                    URLCandidate(
                        url=url,
                        confidence=confidence,
                        title=title,
                        snippet=snippet,
                        reasoning=f"SearXNG ({result.get('engine', '?')}): score={confidence}",
                    )
                )

            candidates.sort(key=lambda c: c.confidence, reverse=True)
            if candidates:
                logger.info(f"SearXNG found {len(candidates)} candidates")
            return candidates[:5]

        except Exception as e:
            logger.warning(f"SearXNG search failed: {e}")
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
        product_title: str = "",
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
            confidence = self._score_candidate(url, title, snippet, domain, product_title=product_title)

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
        product_title: str = "",
    ) -> int:
        """
        Score a candidate URL for relevance.

        Args:
            url: The candidate URL.
            title: The page title.
            snippet: The search snippet.
            domain: The vendor domain.
            product_title: Our product title (for word overlap scoring).

        Returns:
            Confidence score 0-100.
        """
        score = 50  # Base score

        parsed = urlparse(url)
        path = parsed.path.lower()

        # Strong boost for product-specific path patterns (has a slug/ID after the pattern)
        # e.g., /product/alpine-jacket or /products/alpine-jacket.html
        product_page_patterns = ["/product/", "/products/", "/p/", "/item/"]
        path_segments = [s for s in path.split("/") if s]

        is_product_page = False
        for pattern in product_page_patterns:
            if pattern in path:
                # Check there's a slug after the pattern (not just /products/ alone)
                idx = path.find(pattern)
                after = path[idx + len(pattern):]
                if after and after.strip("/"):
                    is_product_page = True
                    score += 20
                    break

        if not is_product_page:
            # /shop/ with a deep path could be a product or category
            if "/shop/" in path:
                if len(path_segments) >= 4:
                    # Likely a category: /shop/womens/tops/t-shirts
                    score -= 10
                else:
                    score += 5

        # Penalize category/listing patterns
        category_patterns = [
            "/blog", "/news", "/support", "/help", "/about",
            "/category", "/collection", "/collections",
            "/search", "/tag", "/tags",
        ]
        if any(p in path for p in category_patterns):
            score -= 20

        # Penalize URLs that look like category browsing paths
        # e.g., /shop/womens/tops/t-shirts, /shop/mens/jackets
        category_segments = {"shop", "mens", "womens", "tops", "bottoms",
                           "jackets", "shoes", "accessories", "gear", "all",
                           "new", "sale", "clearance"}
        if path_segments:
            category_count = sum(1 for s in path_segments if s in category_segments)
            if category_count >= 2:
                score -= 15

        # Boost for reasonable path depth
        if 1 <= len(path_segments) <= 3:
            score += 10
        elif len(path_segments) > 5:
            score -= 10

        # Penalize generic titles (category pages)
        title_lower = title.lower()
        generic_markers = [
            "by patagonia", "by altra", "by burton", "by rossignol",
            "| official", "shop all", "all products",
        ]
        if any(m in title_lower for m in generic_markers):
            score -= 15

        generic_titles = ["home", "shop", "search", "products", "all products"]
        if title_lower.strip() in generic_titles:
            score -= 25

        # Boost if title has specific product words (model names, numbers)
        title_words = title.split()
        if len(title_words) >= 2 and len(title_words) <= 10:
            score += 5
        # Extra boost for titles with model numbers or specific identifiers
        if any(c.isdigit() for c in title):
            score += 5

        # Penalize if URL has query parameters suggesting search/filter
        if "?" in url and any(p in url.lower() for p in ["search=", "filter=", "page=", "sort="]):
            score -= 15

        # Boost if URL path contains a product-like slug (has hyphens, alphanumeric)
        if path_segments:
            last_segment = path_segments[-1]
            if "-" in last_segment and len(last_segment) > 10:
                score += 5  # Looks like a product slug

        # Boost for product name word overlap with URL slug and page title
        if product_title:
            import re
            product_words = {
                w.lower() for w in re.split(r"[\s\-/]+", product_title)
                if len(w) > 2 and w.lower() not in {"the", "and", "for", "ski", "skis"}
            }
            if product_words:
                # Check overlap with URL slug
                url_words = {
                    w.lower() for w in re.split(r"[\s\-/]+", path)
                    if len(w) > 2
                }
                url_overlap = len(product_words & url_words) / len(product_words)
                if url_overlap >= 0.5:
                    score += 10
                elif url_overlap >= 0.3:
                    score += 5

        # Ensure score is in valid range
        return max(0, min(100, score))

    async def search_color_images(
        self,
        vendor_config: VendorConfig,
        product_name: str,
        colors: list[str],
        domain: str | None = None,
    ) -> dict[str, list[str]]:
        """Search for color-specific product images on the vendor site.

        For each color, searches "{product_name} {color}" on the vendor domain
        and extracts image URLs from results that match the color.

        Args:
            vendor_config: Vendor configuration.
            product_name: Product name (e.g., "Nano Puff Jacket").
            colors: List of color names to search for.
            domain: Override domain (defaults to vendor_config.domain).

        Returns:
            Dict mapping color name to list of image URLs found.
        """
        domain = domain or vendor_config.domain
        color_images: dict[str, list[str]] = {}

        for color in colors[:5]:  # Limit to 5 colors to control API costs
            query = f"site:{domain} {product_name} {color}"
            try:
                candidates = await self._search_candidates(query, domain, vendor_config, product_title=product_name)
                # Extract image-like URLs from snippets/results
                for candidate in candidates[:2]:
                    # The candidate URL itself might be a color-specific product page
                    if color.lower().replace(" ", "-") in candidate.url.lower():
                        if candidate.url not in color_images.get(color, []):
                            color_images.setdefault(color, []).append(candidate.url)
            except Exception as e:
                logger.debug(f"Color search failed for {color}: {e}")

        return color_images

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
