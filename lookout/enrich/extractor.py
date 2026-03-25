"""
Content extractor for vendor product pages.

This module handles deterministic extraction of:
- Visible text blocks
- Bullet lists and feature sections
- Spec tables
- Images (including lazy-loaded)
- JSON-LD Product schema
- Variant/color swatches and image mappings
"""

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import ExtractedFacts, ImageInfo, SelectorsConfig, SourceText, VariantOption

logger = logging.getLogger(__name__)

# Tags to skip when extracting text
SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "header",
    "footer",
    "nav",
    "aside",
    "iframe",
    "svg",
    "canvas",
    "meta",
    "link",
}

# Common class/id patterns for navigation/footer (case-insensitive)
NAV_FOOTER_PATTERNS = [
    r"nav",
    r"footer",
    r"header",
    r"menu",
    r"sidebar",
    r"breadcrumb",
    r"cookie",
    r"popup",
    r"modal",
    r"newsletter",
    r"subscribe",
    r"social",
    r"share",
    r"cart",
    r"wishlist",
]


class ContentExtractor:
    """
    Extracts structured content from product page HTML.

    Performs deterministic extraction without LLM assistance.
    The output is then passed to the LLM for structured mapping.
    """

    def __init__(self, selectors: SelectorsConfig | None = None) -> None:
        """
        Initialize the extractor.

        Args:
            selectors: Optional CSS selectors for vendor-specific extraction.
        """
        self.selectors = selectors or SelectorsConfig()

    def extract_source_text(self, html: str, base_url: str) -> SourceText:
        """
        Extract visible text and structured elements from HTML.

        This is the first pass of extraction - deterministic parsing only.

        Args:
            html: The HTML content.
            base_url: Base URL for resolving relative links.

        Returns:
            SourceText with extracted content.
        """
        soup = BeautifulSoup(html, "lxml")

        return SourceText(
            visible_text_blocks=self._extract_text_blocks(soup),
            bullet_lists=self._extract_bullet_lists(soup),
            spec_tables=self._extract_spec_tables(soup),
            json_ld_products=self._extract_json_ld(soup),
            meta_description=self._extract_meta_description(soup),
            page_title=self._extract_title(soup),
        )

    def extract_facts(
        self,
        html: str,
        base_url: str,
        source_text: SourceText | None = None,
    ) -> ExtractedFacts:
        """
        Extract structured facts from HTML.

        Combines deterministic HTML parsing with source text extraction.

        Args:
            html: The HTML content.
            base_url: Base URL for resolving relative links.
            source_text: Optional pre-extracted source text.

        Returns:
            ExtractedFacts with all extracted data.
        """
        soup = BeautifulSoup(html, "lxml")

        if source_text is None:
            source_text = self.extract_source_text(html, base_url)

        # Start with JSON-LD data if available
        facts = self._facts_from_json_ld(source_text.json_ld_products, base_url)

        # Extract additional data from HTML
        if not facts.product_name:
            facts.product_name = self._extract_product_name(soup)

        # Merge description blocks
        if source_text.visible_text_blocks:
            facts.description_blocks = source_text.visible_text_blocks[:10]

        # Extract feature bullets
        facts.feature_bullets = self._flatten_bullets(source_text.bullet_lists)

        # Extract specs from tables
        facts.specs = self._merge_spec_tables(source_text.spec_tables)

        # Extract images (merge with any JSON-LD images already collected)
        html_images = self._extract_images(soup, base_url)
        seen_urls = {img.url for img in facts.images}
        for img in html_images:
            if img.url not in seen_urls:
                facts.images.append(img)
                seen_urls.add(img.url)

        # Extract variant/color information from HTML, merge with JSON-LD
        html_variants, html_variant_images = self._extract_variants(soup, base_url)

        # Merge variants: keep JSON-LD variants, add any new from HTML
        existing_option_names = {v.option_name.lower() for v in facts.variants}
        for v in html_variants:
            if v.option_name.lower() not in existing_option_names:
                facts.variants.append(v)

        # Merge variant image candidates: HTML overrides JSON-LD for same color
        for color, urls in html_variant_images.items():
            if color not in facts.variant_image_candidates:
                facts.variant_image_candidates[color] = urls
            else:
                # Add new URLs not already present
                existing = set(facts.variant_image_candidates[color])
                for url in urls:
                    if url not in existing:
                        facts.variant_image_candidates[color].append(url)

        # Set canonical URL
        facts.canonical_url = self._extract_canonical_url(soup, base_url)

        return facts

    def _extract_text_blocks(self, soup: BeautifulSoup) -> list[str]:
        """Extract visible text blocks, filtering nav/footer."""
        blocks: list[str] = []

        # Try to find main content area
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find("div", class_=re.compile(r"product|pdp|content", re.I))
        )
        search_area = main or soup.body or soup

        if not search_area:
            return blocks

        for elem in search_area.find_all(["p", "div", "span", "h1", "h2", "h3", "h4"]):
            if self._should_skip_element(elem):
                continue

            text = elem.get_text(strip=True)
            if text and len(text) > 20 and len(text) < 2000:
                # Avoid duplicates
                if text not in blocks:
                    blocks.append(text)

        return blocks[:20]  # Limit to top 20 blocks

    def _extract_bullet_lists(self, soup: BeautifulSoup) -> list[list[str]]:
        """Extract bullet lists from the page."""
        lists: list[list[str]] = []

        for ul in soup.find_all(["ul", "ol"]):
            if self._should_skip_element(ul):
                continue

            items = []
            for li in ul.find_all("li", recursive=False):
                text = li.get_text(strip=True)
                if text and len(text) > 5 and len(text) < 500:
                    items.append(text)

            if items and len(items) >= 2:
                lists.append(items)

        return lists[:10]  # Limit to 10 lists

    def _extract_spec_tables(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        """Extract specification tables and definition lists."""
        specs_list: list[dict[str, str]] = []

        # Extract from tables
        for table in soup.find_all("table"):
            if self._should_skip_element(table):
                continue

            specs: dict[str, str] = {}
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True)
                    value = cells[1].get_text(strip=True)
                    if key and value:
                        specs[key] = value

            if specs:
                specs_list.append(specs)

        # Extract from definition lists
        for dl in soup.find_all("dl"):
            if self._should_skip_element(dl):
                continue

            specs: dict[str, str] = {}
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")

            for dt, dd in zip(dts, dds):
                key = dt.get_text(strip=True)
                value = dd.get_text(strip=True)
                if key and value:
                    specs[key] = value

            if specs:
                specs_list.append(specs)

        return specs_list[:5]

    def _extract_json_ld(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """Extract JSON-LD Product schema data."""
        products: list[dict[str, Any]] = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")

                # Handle single objects or arrays
                items = data if isinstance(data, list) else [data]

                for item in items:
                    if isinstance(item, dict):
                        item_type = item.get("@type", "")
                        if item_type == "Product" or "Product" in str(item_type):
                            products.append(item)
                        # Check for nested Product in @graph
                        if "@graph" in item:
                            for node in item["@graph"]:
                                if isinstance(node, dict):
                                    node_type = node.get("@type", "")
                                    if node_type == "Product" or "Product" in str(node_type):
                                        products.append(node)

            except (json.JSONDecodeError, TypeError) as e:
                logger.debug(f"Failed to parse JSON-LD: {e}")
                continue

        return products

    def _extract_meta_description(self, soup: BeautifulSoup) -> str:
        """Extract meta description."""
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and isinstance(meta, Tag):
            return meta.get("content", "") or ""
        return ""

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract page title."""
        title = soup.find("title")
        if title:
            return title.get_text(strip=True)
        return ""

    def _extract_product_name(self, soup: BeautifulSoup) -> str:
        """Extract product name from the page."""
        # Try configured selector first
        if self.selectors.product_name:
            elem = soup.select_one(self.selectors.product_name)
            if elem:
                return elem.get_text(strip=True)

        # Try common patterns
        for selector in [
            "h1[itemprop='name']",
            "h1.product-title",
            "h1.product-name",
            "h1[data-testid='product-title']",
            ".pdp-title h1",
            ".product-hero h1",
            "h1",
        ]:
            elem = soup.select_one(selector)
            if elem and not self._should_skip_element(elem):
                text = elem.get_text(strip=True)
                if text and len(text) < 200:
                    return text

        return ""

    def _extract_images(self, soup: BeautifulSoup, base_url: str) -> list[ImageInfo]:
        """Extract product images including lazy-loaded ones."""
        images: list[ImageInfo] = []
        seen_urls: set[str] = set()

        # Try configured selector first
        if self.selectors.images:
            img_elems = soup.select(self.selectors.images)
        else:
            # Find images in product-related areas
            main = soup.find("main") or soup.find(
                "div", class_=re.compile(r"product|pdp|gallery", re.I)
            )
            img_elems = (main or soup).find_all("img") if main or soup else []

        for img in img_elems:
            if not isinstance(img, Tag):
                continue

            # Check various image source attributes
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
            )

            # Check srcset for high-res images
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset and isinstance(srcset, str):
                # Parse srcset and get largest image
                srcset_url = self._parse_srcset(srcset)
                if srcset_url:
                    src = srcset_url

            if not src or not isinstance(src, str):
                continue

            # Make absolute URL
            full_url = urljoin(base_url, src)

            # Skip small icons, placeholders, etc.
            if self._should_skip_image(full_url):
                continue

            # Avoid duplicates
            normalized = full_url.split("?")[0]  # Remove query params for comparison
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)

            # Extract alt text
            alt = img.get("alt", "") or ""
            if isinstance(alt, list):
                alt = " ".join(str(a) for a in alt)

            images.append(
                ImageInfo(
                    url=full_url,
                    alt_text=str(alt),
                    source_hint="img_tag",
                )
            )

        return images[:20]  # Limit to 20 images

    def _extract_variants(
        self,
        soup: BeautifulSoup,
        base_url: str,
    ) -> tuple[list[VariantOption], dict[str, list[str]]]:
        """
        Extract variant options and color-to-image mappings.

        Returns:
            Tuple of (variant options, color->image mappings)
        """
        variants: list[VariantOption] = []
        color_images: dict[str, list[str]] = {}

        # Look for color swatches with associated images
        # Pattern 1: data attributes on swatch elements
        for swatch in soup.select("[data-color], [data-variant-color], .color-swatch"):
            color = (
                swatch.get("data-color")
                or swatch.get("data-variant-color")
                or swatch.get("title")
                or swatch.get_text(strip=True)
            )
            if not color or not isinstance(color, str):
                continue

            # Look for associated image
            img_url = swatch.get("data-image") or swatch.get("data-variant-image")

            if img_url and isinstance(img_url, str):
                full_url = urljoin(base_url, img_url)
                if color not in color_images:
                    color_images[color] = []
                color_images[color].append(full_url)

        # Pattern 2: JSON data in script tags (common pattern)
        for script in soup.find_all("script"):
            if not script.string:
                continue

            # Look for variant/color data in JavaScript
            text = script.string
            try:
                # Try to find JSON objects with variant data
                json_match = re.search(r"variants?\s*[=:]\s*(\[[\s\S]*?\])", text)
                if json_match:
                    try:
                        variant_data = json.loads(json_match.group(1))
                        if isinstance(variant_data, list):
                            for v in variant_data:
                                if isinstance(v, dict):
                                    color = v.get("color") or v.get("option1")
                                    image = v.get("featured_image", {})
                                    if isinstance(image, dict):
                                        img_url = image.get("src")
                                    elif isinstance(image, str):
                                        img_url = image
                                    else:
                                        img_url = v.get("image")

                                    if color and img_url:
                                        full_url = urljoin(base_url, img_url)
                                        if color not in color_images:
                                            color_images[color] = []
                                        if full_url not in color_images[color]:
                                            color_images[color].append(full_url)
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass

        # Build variant options from color_images keys
        if color_images:
            variants.append(
                VariantOption(
                    option_name="Color",
                    values=list(color_images.keys()),
                )
            )

        # Also look for size selectors
        sizes: list[str] = []
        for elem in soup.select("[data-size], .size-option, [data-option-value]"):
            size = (
                elem.get("data-size") or elem.get("data-option-value") or elem.get_text(strip=True)
            )
            if size and isinstance(size, str) and size not in sizes:
                sizes.append(size)

        if sizes:
            variants.append(VariantOption(option_name="Size", values=sizes))

        return variants, color_images

    def _extract_canonical_url(self, soup: BeautifulSoup, base_url: str) -> str:
        """Extract canonical URL from the page."""
        canonical = soup.find("link", rel="canonical")
        if canonical and isinstance(canonical, Tag):
            href = canonical.get("href")
            if href and isinstance(href, str):
                return urljoin(base_url, href)
        return base_url

    def _facts_from_json_ld(
        self,
        json_ld_products: list[dict[str, Any]],
        base_url: str,
    ) -> ExtractedFacts:
        """Create ExtractedFacts from JSON-LD data."""
        facts = ExtractedFacts(canonical_url=base_url)

        if not json_ld_products:
            return facts

        product = json_ld_products[0]

        # Extract basic info
        facts.product_name = product.get("name", "")
        facts.brand = self._get_nested(product, ["brand", "name"]) or ""

        # Description
        desc = product.get("description", "")
        if desc:
            facts.description_blocks = [desc]

        # Images from JSON-LD
        images = product.get("image", [])
        if isinstance(images, str):
            images = [images]
        elif isinstance(images, dict):
            images = [images.get("url", "")]

        for img_url in images:
            if img_url and isinstance(img_url, str):
                facts.images.append(
                    ImageInfo(
                        url=urljoin(base_url, img_url),
                        source_hint="json_ld",
                    )
                )

        # Extract variants from JSON-LD offers
        offers = product.get("offers", [])
        if isinstance(offers, dict):
            offers = [offers]
        if isinstance(offers, list) and len(offers) > 1:
            colors, sizes, color_images = self._parse_jsonld_offers(offers, base_url)
            if colors:
                facts.variants.append(VariantOption(option_name="Color", values=colors))
            if sizes:
                facts.variants.append(VariantOption(option_name="Size", values=sizes))
            if color_images:
                facts.variant_image_candidates = color_images

        # Store raw JSON-LD
        facts.json_ld_data = product

        return facts

    def _parse_jsonld_offers(
        self,
        offers: list[dict[str, Any]],
        base_url: str,
    ) -> tuple[list[str], list[str], dict[str, list[str]]]:
        """Parse variant colors, sizes, and color→image mappings from JSON-LD offers.

        Offer names typically follow patterns like:
        - "Product Name - L / black"
        - "Product Name - black / L"
        - "Product Name - black"

        Returns:
            Tuple of (unique_colors, unique_sizes, color_to_images)
        """
        colors: list[str] = []
        sizes: list[str] = []
        color_images: dict[str, list[str]] = {}
        seen_colors: set[str] = set()
        seen_sizes: set[str] = set()

        # Common size patterns
        size_pattern = re.compile(
            r"^(XXS|XS|S|M|L|XL|XXL|2XL|3XL|4XL|5XL|"
            r"\d{1,3}(\.\d)?|"  # numeric (26, 32.5, 171)
            r"\d+/\d+|"  # waist/inseam (32/32)
            r"One Size)$",
            re.IGNORECASE,
        )

        for offer in offers:
            name = offer.get("name", "")
            if not name:
                continue

            # Extract the variant part after " - " separator
            parts = name.split(" - ", 1)
            if len(parts) < 2:
                continue
            variant_part = parts[1].strip()

            # Split on " / " to get option values
            options = [o.strip() for o in variant_part.split("/")]

            offer_color = None
            for opt in options:
                if not opt:
                    continue
                if size_pattern.match(opt):
                    if opt not in seen_sizes:
                        seen_sizes.add(opt)
                        sizes.append(opt)
                else:
                    # Treat as color
                    if opt not in seen_colors:
                        seen_colors.add(opt)
                        colors.append(opt)
                    offer_color = opt

            # Map color to offer image
            if offer_color:
                offer_image = offer.get("image")
                if isinstance(offer_image, str) and offer_image:
                    img_url = urljoin(base_url, offer_image)
                    if offer_color not in color_images:
                        color_images[offer_color] = []
                    if img_url not in color_images[offer_color]:
                        color_images[offer_color].append(img_url)

        return colors, sizes, color_images

    def _should_skip_element(self, elem: Tag) -> bool:
        """Check if an element should be skipped during extraction."""
        if elem.name in SKIP_TAGS:
            return True

        # Check class and id for nav/footer patterns
        classes = " ".join(elem.get("class", []))
        elem_id = elem.get("id", "") or ""

        for pattern in NAV_FOOTER_PATTERNS:
            if re.search(pattern, classes, re.I) or re.search(pattern, elem_id, re.I):
                return True

        return False

    def _should_skip_image(self, url: str) -> bool:
        """Check if an image URL should be skipped."""
        url_lower = url.lower()

        # Skip common non-product images
        skip_patterns = [
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
        ]

        for pattern in skip_patterns:
            if pattern in url_lower:
                return True

        # Skip very small images (often icons)
        if re.search(r"[/_-](\d{1,2})x(\d{1,2})[/_.]", url_lower):
            return True

        # Skip data URLs
        if url.startswith("data:"):
            return True

        return False

    def _parse_srcset(self, srcset: str) -> str | None:
        """Parse srcset attribute and return the largest image URL."""
        parts = srcset.split(",")
        best_url = None
        best_width = 0

        for part in parts:
            part = part.strip()
            match = re.match(r"(\S+)\s+(\d+)w", part)
            if match:
                url, width = match.groups()
                if int(width) > best_width:
                    best_width = int(width)
                    best_url = url

        return best_url

    def _flatten_bullets(self, bullet_lists: list[list[str]]) -> list[str]:
        """Flatten bullet lists into a single list of features."""
        features: list[str] = []
        seen: set[str] = set()

        for bullet_list in bullet_lists:
            for item in bullet_list:
                # Normalize for deduplication
                normalized = item.lower().strip()
                if normalized not in seen:
                    seen.add(normalized)
                    features.append(item)

        return features[:15]  # Limit total features

    def _merge_spec_tables(self, spec_tables: list[dict[str, str]]) -> dict[str, str]:
        """Merge multiple spec tables into one."""
        merged: dict[str, str] = {}

        for table in spec_tables:
            for key, value in table.items():
                # Don't overwrite existing values
                if key not in merged:
                    merged[key] = value

        return merged

    def _get_nested(self, obj: dict, keys: list[str]) -> Any:
        """Safely get nested dictionary values."""
        current = obj
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None
        return current

    async def save_outputs(
        self,
        source_text: SourceText,
        facts: ExtractedFacts,
        artifacts_dir: Path,
    ) -> None:
        """
        Save extraction outputs to artifacts directory.

        Args:
            source_text: The extracted source text.
            facts: The extracted facts.
            artifacts_dir: Path to the artifacts directory.
        """
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Save source text
        source_path = artifacts_dir / "source_text.json"
        with open(source_path, "w") as f:
            json.dump(source_text.model_dump(), f, indent=2)

        # Save extracted facts
        facts_path = artifacts_dir / "extracted_facts.json"
        with open(facts_path, "w") as f:
            json.dump(facts.model_dump(mode="json"), f, indent=2, default=str)


def extract_content(
    html: str,
    base_url: str,
    selectors: SelectorsConfig | None = None,
) -> tuple[SourceText, ExtractedFacts]:
    """
    Convenience function to extract content from HTML.

    Args:
        html: The HTML content.
        base_url: Base URL for resolving relative links.
        selectors: Optional vendor-specific selectors.

    Returns:
        Tuple of (SourceText, ExtractedFacts)
    """
    extractor = ContentExtractor(selectors)
    source_text = extractor.extract_source_text(html, base_url)
    facts = extractor.extract_facts(html, base_url, source_text)
    return source_text, facts
