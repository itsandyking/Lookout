"""Shopify JSON API scraper for vendors running on Shopify.

For vendors with is_shopify=True, this provides a fast, reliable
data source that requires no browser, no bot evasion, and no LLM
extraction. The /products/{handle}.json endpoint is public and
returns structured product data.
"""

import logging
from html.parser import HTMLParser
from io import StringIO

import httpx

from .models import ExtractedFacts, ImageInfo

logger = logging.getLogger(__name__)


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and return plain text."""

    def __init__(self):
        super().__init__()
        self._text = StringIO()

    def handle_data(self, data):
        self._text.write(data)

    def get_text(self):
        return self._text.getvalue().strip()


def _strip_html(html: str) -> str:
    """Remove HTML tags from a string."""
    if not html:
        return ""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    return extractor.get_text()


def _shopify_json_to_facts(product: dict, url: str) -> ExtractedFacts:
    """Convert a Shopify product JSON object to ExtractedFacts."""
    # Images
    images = []
    for img in product.get("images", []):
        src = img.get("src", "")
        if src:
            images.append(ImageInfo(
                url=src,
                alt_text=img.get("alt", ""),
                source_hint="shopify_json",
                width=img.get("width"),
                height=img.get("height"),
            ))

    # Description — split body_html into paragraphs
    body_html = product.get("body_html", "")
    description_blocks = []
    if body_html:
        # Split on </p>, <br>, or double newlines
        plain = _strip_html(body_html)
        blocks = [b.strip() for b in plain.split("\n\n") if b.strip()]
        if not blocks:
            blocks = [b.strip() for b in plain.split("\n") if b.strip() and len(b.strip()) > 20]
        description_blocks = blocks[:5]  # Cap at 5 blocks

    # Variants → colors, sizes
    colors = set()
    for variant in product.get("variants", []):
        opt1 = variant.get("option1", "")
        # Option1 is typically color for apparel/gear
        if opt1 and opt1.lower() not in ("default title", "default"):
            colors.add(opt1)

    # Variant image candidates — map colors to image URLs
    variant_image_candidates = {}
    for img in product.get("images", []):
        variant_ids = img.get("variant_ids", [])
        if variant_ids:
            # Find which variant this image belongs to
            for variant in product.get("variants", []):
                if variant.get("id") in variant_ids:
                    color = variant.get("option1", "")
                    if color and color.lower() not in ("default title", "default"):
                        if color not in variant_image_candidates:
                            variant_image_candidates[color] = []
                        if img["src"] not in variant_image_candidates[color]:
                            variant_image_candidates[color].append(img["src"])

    # Tags
    tags = product.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    # Specs from tags (many Shopify vendors encode specs in tags)
    specs = {}
    product_type = product.get("product_type", "")
    if product_type:
        specs["Product Type"] = product_type

    return ExtractedFacts(
        canonical_url=url,
        product_name=product.get("title", ""),
        brand=product.get("vendor", ""),
        description_blocks=description_blocks,
        feature_bullets=[],  # Shopify JSON doesn't separate features
        specs=specs,
        materials="",  # Not in standard Shopify JSON
        images=images,
        variants=[],
        variant_image_candidates=variant_image_candidates,
        json_ld_data=None,
        evidence_snippets={},
        extraction_warnings=[],
    )


async def scrape_shopify_product(
    domain: str,
    handle: str,
    http_client: httpx.AsyncClient | None = None,
) -> ExtractedFacts | None:
    """Fetch product data from a Shopify store's JSON API.

    Args:
        domain: The vendor's Shopify domain (e.g., "us.factionskis.com")
        handle: The product handle (e.g., "dancer-1-ski-2024")
        http_client: Optional shared HTTP client.

    Returns:
        ExtractedFacts if successful, None if the product isn't found.
    """
    url = f"https://{domain}/products/{handle}.json"
    owns_client = http_client is None

    try:
        if owns_client:
            http_client = httpx.AsyncClient(timeout=15, follow_redirects=True)

        resp = await http_client.get(url)

        if resp.status_code == 404:
            logger.info("Shopify product not found: %s", url)
            return None

        if resp.status_code != 200:
            logger.warning("Shopify API error %d for %s", resp.status_code, url)
            return None

        data = resp.json()
        product = data.get("product")
        if not product:
            logger.warning("No product key in Shopify response for %s", url)
            return None

        product_url = f"https://{domain}/products/{handle}"
        return _shopify_json_to_facts(product, product_url)

    except Exception:
        logger.exception("Shopify JSON fetch failed for %s", url)
        return None
    finally:
        if owns_client and http_client:
            await http_client.aclose()
