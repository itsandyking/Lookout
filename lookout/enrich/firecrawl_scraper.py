"""Firecrawl-based scraper for vendor product pages.

Replaces the Playwright WebScraper with self-hosted Firecrawl.
Supports three modes:
  - extract: structured JSON extraction (returns ExtractedFacts directly)
  - html: raw HTML (returns ScrapedPage for extractor compatibility)
  - markdown: clean markdown output
"""

import asyncio
import logging
import random
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from firecrawl import AsyncFirecrawl
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import ExtractedFacts, ImageInfo
from .scraper import ScrapedPage

logger = logging.getLogger(__name__)

# Markers that indicate bot protection blocked the request.
# These are checked against short content (<500 chars) or the first 2000 chars.
_BOT_BLOCK_MARKERS = [
    "verify you are a human",
    "access denied",
    "checking your browser",
    "perimeterx",
    "powered and protected by",
    "sit tight",
    "hands full at the moment",
    "just a moment",
    "ray id",
    "enable javascript",
    "document_antibot",
    "please complete the security check",
    "solve this captcha",
]

# Markers that only indicate a block on SHORT pages (<2000 chars).
# On longer pages, these are often just footer disclosures (e.g., "reCAPTCHA").
_SHORT_ONLY_MARKERS = [
    "captcha",
]


def is_bot_blocked(content: str) -> bool:
    """Check if scraped content is a bot protection page."""
    if not content or len(content) < 500:
        lower = (content or "").lower()
        all_markers = _BOT_BLOCK_MARKERS + _SHORT_ONLY_MARKERS
        return any(marker in lower for marker in all_markers)
    # For longer content, only check the first 2000 chars
    # (bot blocks are typically full-page replacements)
    lower = content[:2000].lower()
    return any(marker in lower for marker in _BOT_BLOCK_MARKERS)


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string", "description": "The product name/title"},
        "brand": {"type": "string", "description": "The brand or manufacturer"},
        "description_blocks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Product description paragraphs",
        },
        "feature_bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key feature bullet points",
        },
        "specs": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Product specifications as key-value pairs (e.g. Weight: 400g)",
        },
        "images": {
            "type": "array",
            "items": {"type": "string", "format": "uri"},
            "description": "All product image URLs (full size, not thumbnails)",
        },
        "colors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Available color options",
        },
        "materials": {
            "type": "string",
            "description": "Materials/fabric composition",
        },
        "price": {
            "type": "string",
            "description": "Product price as displayed",
        },
    },
}

EXTRACTION_PROMPT = (
    "Extract all product information from this page. "
    "Include every image URL you can find for the product (not icons or logos). "
    "For specs, include materials, weight, dimensions, ratings, and certifications."
)


# Query params that indicate resized/thumbnail images.
# Stripping these typically yields the full-size original.
_RESIZE_PARAMS = {
    "imwidth",
    "imheight",
    "impolicy",
    "width",
    "height",
    "w",
    "h",
    "resize",
    "size",
    "fit",
    "crop",
    "quality",
    "q",
    "fmt",
    "format",
    "auto",
    "dpr",
}


def _clean_image_url(url: str) -> str:
    """Strip resize/thumbnail query params from image URLs.

    Vendor CDNs often serve thumbnails via query params like
    ?imwidth=246 or ?w=300. Removing these gives the full-size image.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {k: v for k, v in params.items() if k.lower() not in _RESIZE_PARAMS}
    if cleaned:
        new_query = urlencode(cleaned, doseq=True)
    else:
        new_query = ""
    return urlunparse(parsed._replace(query=new_query))


def _firecrawl_json_to_facts(data: dict, url: str) -> ExtractedFacts:
    """Convert Firecrawl structured extraction output to ExtractedFacts."""
    images = []
    seen_urls = set()
    for img_url in data.get("images", []):
        if isinstance(img_url, str) and img_url.startswith("http"):
            cleaned = _clean_image_url(img_url)
            if cleaned not in seen_urls:
                seen_urls.add(cleaned)
                images.append(ImageInfo(url=cleaned, source_hint="firecrawl"))

    return ExtractedFacts(
        canonical_url=url,
        product_name=data.get("product_name", ""),
        brand=data.get("brand", ""),
        description_blocks=data.get("description_blocks", []),
        feature_bullets=data.get("feature_bullets", []),
        specs=data.get("specs", {}),
        materials=data.get("materials", ""),
        images=images,
        variant_image_candidates={},
        json_ld_data=None,
        evidence_snippets={},
        extraction_warnings=[],
    )


def _markdown_has_images(markdown: str) -> bool:
    """Check if markdown contains any image references.

    Looks for markdown image syntax ![...](url) and bare image URLs
    with common image extensions.
    """
    # Markdown image syntax
    if re.search(r"!\[.*?\]\(https?://[^)]+\)", markdown):
        return True
    # Bare image URLs with common extensions
    if re.search(r"https?://\S+\.(?:jpg|jpeg|png|webp|avif)", markdown, re.I):
        return True
    return False


def _extract_images_from_html(html: str, base_url: str) -> list[str]:
    """Extract product image URLs from rendered HTML.

    Uses BeautifulSoup to find images in the DOM that JS may have loaded.
    Filters out icons, logos, placeholders, and duplicates.
    Returns a list of absolute image URLs.
    """
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup(html, "lxml")

    # Focus on main content area
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", class_=re.compile(r"product|pdp|gallery|item", re.I))
    )
    search_area = main or soup.body or soup
    if not search_area:
        return []

    images: list[str] = []
    seen: set[str] = set()

    skip_patterns = {
        "placeholder",
        "loading",
        "spinner",
        "icon",
        "logo",
        "badge",
        "banner",
        "1x1",
        "pixel",
        "blank",
        "spacer",
        "transparent",
        "svg+xml",
    }

    for img in search_area.find_all("img"):
        if not isinstance(img, Tag):
            continue

        # Try multiple source attributes (handles lazy-load patterns)
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
        )

        # Also check srcset for highest-res image
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset and isinstance(srcset, str):
            parts = srcset.split(",")
            best_url, best_w = None, 0
            for part in parts:
                match = re.match(r"\s*(\S+)\s+(\d+)w", part.strip())
                if match:
                    w = int(match.group(2))
                    if w > best_w:
                        best_w = w
                        best_url = match.group(1)
            if best_url:
                src = best_url

        if not src or not isinstance(src, str) or src.startswith("data:"):
            continue

        full_url = urljoin(base_url, src)

        # Skip non-product images
        url_lower = full_url.lower()
        if any(p in url_lower for p in skip_patterns):
            continue
        # Skip tiny images (icons)
        if re.search(r"[/_-](\d{1,2})x(\d{1,2})[/_.]", url_lower):
            continue

        # Deduplicate by base URL (strip resize params)
        cleaned = _clean_image_url(full_url)
        normalized = cleaned.split("?")[0]
        if normalized in seen:
            continue
        seen.add(normalized)
        images.append(cleaned)

    return images[:20]


def _append_images_to_markdown(markdown: str, image_urls: list[str]) -> str:
    """Append extracted image URLs to markdown so the LLM can see them.

    Adds a section at the end with markdown image references.
    """
    lines = [
        "",
        "---",
        "",
        "**Product Images (extracted from page):**",
        "",
    ]
    for i, url in enumerate(image_urls, 1):
        lines.append(f"![Product image {i}]({url})")
    lines.append("")
    return markdown + "\n".join(lines)


class FirecrawlScraper:
    """Scraper that delegates to a self-hosted Firecrawl instance."""

    def __init__(
        self,
        base_url: str = "http://localhost:3002",
        client: AsyncFirecrawl | None = None,
        min_delay_ms: int = 500,
        max_delay_ms: int = 2000,
    ) -> None:
        self._client = client or AsyncFirecrawl(api_url=base_url)
        self._min_delay = min_delay_ms / 1000
        self._max_delay = max_delay_ms / 1000

    async def _polite_delay(self) -> None:
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def extract(self, url: str) -> ExtractedFacts | None:
        """Structured extraction — returns ExtractedFacts directly."""
        await self._polite_delay()
        try:
            doc = await self._client.scrape(
                url,
                formats=[
                    {
                        "type": "json",
                        "schema": EXTRACTION_SCHEMA,
                        "prompt": EXTRACTION_PROMPT,
                    }
                ],
            )
            if not doc.json:
                logger.warning("Firecrawl returned no JSON for %s", url)
                return None

            final_url = url
            if doc.metadata:
                final_url = (
                    getattr(doc.metadata, "source_url", None)
                    or getattr(doc.metadata, "sourceURL", None)
                    or url
                )

            return _firecrawl_json_to_facts(doc.json, final_url)

        except Exception:
            logger.exception("Firecrawl extract failed for %s", url)
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def scrape_html(self, url: str) -> ScrapedPage:
        """HTML mode — returns ScrapedPage for extractor compatibility."""
        await self._polite_delay()
        try:
            doc = await self._client.scrape(url, formats=["html"])
            final_url = url
            if doc.metadata:
                final_url = (
                    getattr(doc.metadata, "source_url", None)
                    or getattr(doc.metadata, "sourceURL", None)
                    or url
                )
            return ScrapedPage(
                url=url,
                html=doc.html or "",
                status_code=200,
                final_url=final_url,
            )
        except Exception as e:
            logger.exception("Firecrawl HTML scrape failed for %s", url)
            return ScrapedPage(url=url, html="", status_code=0, error=str(e))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def scrape_markdown(
        self,
        url: str,
        swatch_selector: str | None = None,
        gallery_selector: str | None = None,
        wait_after_click: int | None = None,
        wait_for: int | None = None,
    ) -> tuple[str | None, dict[str, list[str]] | None]:
        """Markdown mode — returns (markdown, variant_images).

        When swatch params are provided, calls the Firecrawl API directly
        (bypassing the SDK) to get variant_images alongside markdown.
        The SDK's Document model drops unknown fields, so we need the raw response.

        When wait_for is set, the scraper waits that many ms for JS rendering
        (useful for SPA sites). Also requests HTML alongside markdown so that
        images loaded by JS (but absent from the markdown conversion) can be
        extracted from the rendered DOM and appended to the markdown.
        """
        await self._polite_delay()
        has_swatch_params = bool(swatch_selector or gallery_selector)

        if has_swatch_params:
            return await self._scrape_markdown_with_swatches(
                url, swatch_selector, gallery_selector, wait_after_click
            )

        # Standard path via SDK (no swatch extraction)
        try:
            # Request HTML alongside markdown so we can extract images from the
            # rendered DOM when the markdown conversion drops them (common with
            # SPA frameworks like SuiteCommerce, React storefronts, etc.)
            formats = ["markdown", "html"] if wait_for else ["markdown"]

            kwargs: dict = {
                "formats": formats,
                "only_main_content": True,
                "exclude_tags": [
                    "nav",
                    "footer",
                    "header",
                    "[role='navigation']",
                    "[role='banner']",
                    "[role='contentinfo']",
                    ".site-footer",
                    ".site-header",
                    ".site-nav",
                    "#cookie-banner",
                    ".cookie-notice",
                    ".announcement-bar",
                ],
            }
            if wait_for:
                kwargs["wait_for"] = wait_for

            doc = await self._client.scrape(url, **kwargs)
            markdown = doc.markdown

            # If markdown has no image references, try to extract them from HTML
            if markdown and doc.html and not _markdown_has_images(markdown):
                html_images = _extract_images_from_html(doc.html, url)
                if html_images:
                    logger.info(
                        "Supplemented markdown with %d images from HTML for %s",
                        len(html_images),
                        url,
                    )
                    markdown = _append_images_to_markdown(markdown, html_images)

            return markdown, None
        except Exception:
            logger.exception("Firecrawl markdown scrape failed for %s", url)
            return None, None

    async def _scrape_markdown_with_swatches(
        self,
        url: str,
        swatch_selector: str | None = None,
        gallery_selector: str | None = None,
        wait_after_click: int | None = None,
    ) -> tuple[str | None, dict[str, list[str]] | None]:
        """Call Firecrawl API directly to get markdown + variant images.

        Bypasses the SDK because its Document model drops unknown fields.
        """
        import httpx

        api_url = self._client.api_url.rstrip("/")
        payload: dict = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "excludeTags": [
                "nav",
                "footer",
                "header",
                "[role='navigation']",
                "[role='banner']",
                "[role='contentinfo']",
                ".site-footer",
                ".site-header",
                ".site-nav",
                "#cookie-banner",
                ".cookie-notice",
                ".announcement-bar",
            ],
        }
        if swatch_selector:
            payload["swatchSelector"] = swatch_selector
        if gallery_selector:
            payload["gallerySelector"] = gallery_selector
        if wait_after_click:
            payload["waitAfterClick"] = wait_after_click

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{api_url}/v1/scrape",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            doc_data = data.get("data", data)
            markdown = doc_data.get("markdown")
            variant_images = doc_data.get("variantImages")

            if variant_images:
                logger.info(
                    "Firecrawl returned variant images for %d colors from %s",
                    len(variant_images),
                    url,
                )

            return markdown, variant_images
        except Exception:
            logger.exception("Firecrawl markdown+swatch scrape failed for %s", url)
            return None, None

    async def scrape_variant_images(
        self,
        url: str,
        swatch_selector: str | None = None,
        gallery_selector: str | None = None,
        playwright_url: str = "http://localhost:3003",
        timeout: int = 30000,
        wait_after_click: int = 1500,
    ) -> dict[str, list[str]] | None:
        """Scrape color variant images by clicking swatches.

        Calls the /scrape-variants endpoint on the Playwright service.
        Returns {color: [image_urls]} or None on failure.
        """
        import httpx

        payload: dict = {"url": url, "timeout": timeout, "wait_after_click": wait_after_click}
        if swatch_selector:
            payload["swatch_selector"] = swatch_selector
        if gallery_selector:
            payload["gallery_selector"] = gallery_selector

        try:
            async with httpx.AsyncClient(timeout=timeout / 1000 + 30) as client:
                resp = await client.post(
                    f"{playwright_url}/scrape-variants",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            variant_images = data.get("variant_images", {})
            swatch_count = data.get("swatch_count", 0)
            method = data.get("method", "unknown")

            if variant_images:
                logger.info(
                    "Swatch scrape found %d colors (%d swatches, method=%s) for %s",
                    len(variant_images),
                    swatch_count,
                    method,
                    url,
                )
                return variant_images
            else:
                logger.info(
                    "Swatch scrape found no variant images (%d swatches) for %s",
                    swatch_count,
                    url,
                )
                return None

        except Exception:
            logger.warning("Swatch scrape failed for %s", url, exc_info=True)
            return None
