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


def _firecrawl_json_to_facts(data: dict, url: str) -> ExtractedFacts:
    """Convert Firecrawl structured extraction output to ExtractedFacts."""
    images = []
    for img_url in data.get("images", []):
        if isinstance(img_url, str) and img_url.startswith("http"):
            images.append(ImageInfo(url=img_url, source_hint="firecrawl"))

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
                final_url = getattr(doc.metadata, "source_url", None) or getattr(doc.metadata, "sourceURL", None) or url

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
                final_url = getattr(doc.metadata, "source_url", None) or getattr(doc.metadata, "sourceURL", None) or url
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
    async def scrape_markdown(self, url: str) -> str | None:
        """Markdown mode — returns clean markdown text."""
        await self._polite_delay()
        try:
            doc = await self._client.scrape(url, formats=["markdown"])
            return doc.markdown
        except Exception:
            logger.exception("Firecrawl markdown scrape failed for %s", url)
            return None
