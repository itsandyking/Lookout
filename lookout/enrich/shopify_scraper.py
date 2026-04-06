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
            images.append(
                ImageInfo(
                    url=src,
                    alt_text=img.get("alt") or "",
                    source_hint="shopify_json",
                    width=img.get("width"),
                    height=img.get("height"),
                )
            )

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


async def _search_shopify_by_title(
    domain: str,
    title: str,
    http_client: httpx.AsyncClient,
) -> dict | None:
    """Search a Shopify store's products by title.

    Uses Shopify's search suggest API first (most accurate), then
    falls back to scanning /products.json with Jaccard similarity.
    """
    # Try 1: Shopify search suggest API (fast, accurate)
    try:
        resp = await http_client.get(
            f"https://{domain}/search/suggest.json",
            params={"q": title, "resources[type]": "product"},
        )
        if resp.status_code == 200:
            results = resp.json().get("resources", {}).get("results", {}).get("products", [])
            if results:
                # Pick the best match by title similarity
                title_lower = title.lower()
                for result in results:
                    result_title = result.get("title", "").lower()
                    # Check for significant word overlap
                    title_words = set(title_lower.split())
                    result_words = set(result_title.split())
                    if title_words and result_words:
                        overlap = len(title_words & result_words) / len(title_words | result_words)
                        if overlap > 0.3:
                            # Got a match — fetch the full product JSON
                            matched_handle = result.get("handle", "")
                            if matched_handle:
                                full_resp = await http_client.get(
                                    f"https://{domain}/products/{matched_handle}.json"
                                )
                                if full_resp.status_code == 200:
                                    product = full_resp.json().get("product")
                                    if product:
                                        logger.info(
                                            "Shopify search matched '%s' → '%s' (handle=%s)",
                                            title,
                                            result["title"],
                                            matched_handle,
                                        )
                                        return product
    except Exception:
        logger.debug("Shopify search suggest failed for %s, trying products.json", domain)

    # Try 2: Scan /products.json with title similarity
    try:
        import re as _re

        resp = await http_client.get(
            f"https://{domain}/products.json",
            params={"limit": 50},
        )
        if resp.status_code != 200:
            return None

        products = resp.json().get("products", [])
        title_lower = title.lower()
        title_words = set(_re.findall(r"[a-z0-9]+", title_lower))
        filler = {
            "the",
            "a",
            "an",
            "by",
            "for",
            "in",
            "of",
            "and",
            "with",
            "mens",
            "womens",
            "men",
            "women",
            "kids",
        }
        title_words -= filler

        best_match = None
        best_score = 0

        for product in products:
            product_title = product.get("title", "").lower()
            product_words = set(_re.findall(r"[a-z0-9]+", product_title)) - filler

            if not title_words or not product_words:
                continue
            intersection = title_words & product_words
            union = title_words | product_words
            score = len(intersection) / len(union)

            # Penalize candidates with foreign product names
            # e.g., "Dancer 1 Verbier" has "verbier" which isn't in "Dancer 1 Skis"
            generic = {
                "ski",
                "skis",
                "boot",
                "boots",
                "shoe",
                "shoes",
                "jacket",
                "rope",
                "helmet",
                "goggles",
                "sunglasses",
                "new",
                "sale",
                "2024",
                "2025",
                "2026",
                "2027",
            }
            foreign = product_words - title_words - generic
            missing = title_words - product_words - generic
            if foreign and missing:
                # Different product — has words we don't expect AND
                # is missing words we do expect
                score *= 0.5  # Halve the score

            if score > best_score:
                best_score = score
                best_match = product

        if best_match and best_score > 0.3:
            logger.info(
                "Shopify products.json matched '%s' → '%s' (score=%.2f)",
                title,
                best_match["title"],
                best_score,
            )
            return best_match

    except Exception:
        logger.exception("Shopify title search failed for %s", domain)

    return None


async def scrape_shopify_product(
    domain: str,
    handle: str,
    http_client: httpx.AsyncClient | None = None,
    title: str | None = None,
) -> ExtractedFacts | None:
    """Fetch product data from a Shopify store's JSON API.

    Tries exact handle match first, then falls back to title search
    if the handle doesn't match the vendor's store.

    Args:
        domain: The vendor's Shopify domain (e.g., "us.factionskis.com")
        handle: The product handle (e.g., "dancer-1-ski-2024")
        http_client: Optional shared HTTP client.
        title: Optional product title for fallback search.

    Returns:
        ExtractedFacts if successful, None if the product isn't found.
    """
    owns_client = http_client is None

    try:
        if owns_client:
            http_client = httpx.AsyncClient(timeout=15, follow_redirects=True)

        # Try 1: Exact handle match
        url = f"https://{domain}/products/{handle}.json"
        resp = await http_client.get(url)

        if resp.status_code == 200:
            data = resp.json()
            product = data.get("product")
            if product:
                product_url = f"https://{domain}/products/{handle}"
                return _shopify_json_to_facts(product, product_url)

        # Try 1b: Strip common product-type suffixes and retry
        # Your store: "strand-sunglasses", vendor store: "strand"
        suffixes_to_strip = [
            "-sunglasses",
            "-goggles",
            "-helmet",
            "-ski",
            "-skis",
            "-snowboard",
            "-jacket",
            "-boot",
            "-boots",
            "-shoe",
            "-shoes",
            "-gloves",
            "-pants",
            "-rope",
        ]
        for suffix in suffixes_to_strip:
            if handle.endswith(suffix):
                short_handle = handle[: -len(suffix)]
                url = f"https://{domain}/products/{short_handle}.json"
                resp = await http_client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    product = data.get("product")
                    if product:
                        logger.info(
                            "Shopify handle suffix strip matched: %s → %s",
                            handle,
                            short_handle,
                        )
                        product_url = f"https://{domain}/products/{short_handle}"
                        return _shopify_json_to_facts(product, product_url)
                break  # Only try one suffix

        # Try 2: Title-based search
        if title:
            logger.info("Handle mismatch on %s, searching by title: %s", domain, title)
            product = await _search_shopify_by_title(domain, title, http_client)
            if product:
                matched_handle = product.get("handle", handle)
                product_url = f"https://{domain}/products/{matched_handle}"
                return _shopify_json_to_facts(product, product_url)

        logger.info("Shopify product not found on %s: handle=%s title=%s", domain, handle, title)
        return None

    except Exception:
        logger.exception("Shopify JSON fetch failed for %s/%s", domain, handle)
        return None
    finally:
        if owns_client and http_client:
            await http_client.aclose()
