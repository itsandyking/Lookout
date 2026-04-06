"""Brave Image Search fallback for variant and product images.

Uses Brave's Image Search API to find product images when vendor sites
are blocked or scraping fails. Images are verified via local Ollama vision
before being accepted.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass

import httpx

from lookout.enrich.models import BraveImagesSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Color-name normalization for search queries
# ---------------------------------------------------------------------------
# These rules clean up creative/complex color names so Brave image search
# returns better results.  They only affect the *query string* — the original
# color name is preserved in all stored output.

# Exotic → standard color mapping (lowercase key → replacement)
_EXOTIC_COLOR_MAP: dict[str, str] = {
    "ceramic": "beige",
    "crimson": "red",
    "slate": "gray",
    "charcoal": "gray",
    "obsidian": "black",
    "onyx": "black",
    "ivory": "white",
    "bone": "white",
    "pearl": "white",
    "cobalt": "blue",
    "navy": "dark blue",
    "teal": "blue green",
    "olive": "green",
    "sage": "green",
    "moss": "green",
    "wine": "red",
    "burgundy": "dark red",
    "maroon": "dark red",
    "mauve": "pink",
    "coral": "orange pink",
    "rust": "orange",
    "cognac": "brown",
    "espresso": "dark brown",
    "mocha": "brown",
    "taupe": "brown gray",
    "khaki": "tan",
    "sand": "tan",
    "dune": "tan",
    "stone": "gray",
    "pewter": "gray",
    "graphite": "gray",
    "ash": "gray",
    "fog": "gray",
    "storm": "gray",
    "ink": "dark blue",
    "indigo": "dark blue",
    "midnight": "dark blue",
    "azure": "blue",
    "sky": "light blue",
    "arctic": "light blue",
    "lagoon": "blue green",
    "fuchsia": "pink",
    "magenta": "pink",
    "blush": "light pink",
    "rose": "pink",
    "flamingo": "pink",
    "citron": "yellow",
    "mustard": "yellow",
    "grey": "gray",
    "gold": "yellow",
    "amber": "orange",
    "tangerine": "orange",
    "paprika": "red orange",
    "cayenne": "red",
    "scarlet": "red",
    "cherry": "red",
    "cardinal": "red",
    "lava": "red",
    "sunset": "orange",
}

# Non-color modifiers to strip (case-insensitive, whole word)
_NON_COLOR_MODIFIERS: set[str] = {
    "rib", "heather", "melange", "marled", "marl",
    "print", "printed", "stripe", "striped", "plaid",
    "camo", "floral", "check", "checked",
    "matte", "satin", "gloss", "glossy", "metallic",
    "washed", "faded", "distressed", "acid",
    "solid", "classic", "premium", "pro",
}

# Regex: strip everything after "w/" or "with" (lens/optics descriptions)
_RE_LENS_SUFFIX = re.compile(r"\s+w/\s.*$|\s+with\s.*$", re.IGNORECASE)
# Regex: strip trailing "Lens" (with optional preceding word)
_RE_LENS_WORD = re.compile(r"\s+\S*\s*lens$", re.IGNORECASE)


def normalize_color_for_query(color: str) -> str:
    """Normalize a creative color name into a search-friendly query term.

    Rules applied in order:
    1. Strip lens/optics descriptions (after "w/" or "with", trailing "Lens")
    2. Drop non-color modifiers (Rib, Heather, Melange, etc.)
    3. Map exotic color names to standard ones

    >>> normalize_color_for_query("Black / Silver w/ Mirror Silver Lens")
    'black silver'
    >>> normalize_color_for_query("Black Rib")
    'black'
    >>> normalize_color_for_query("Ceramic")
    'beige'
    """
    text = color.strip()

    # 1a. Strip lens/optics descriptions after "w/" or "with"
    text = _RE_LENS_SUFFIX.sub("", text)
    # 1b. Strip trailing "Lens" phrase
    text = _RE_LENS_WORD.sub("", text)

    # Normalize separators: "/" → space
    text = text.replace("/", " ")

    # Collapse whitespace
    tokens = text.split()

    # 2. Drop non-color modifiers
    tokens = [t for t in tokens if t.lower() not in _NON_COLOR_MODIFIERS]

    # 3. Map exotic names to standard colors (per-token)
    mapped: list[str] = []
    for t in tokens:
        replacement = _EXOTIC_COLOR_MAP.get(t.lower())
        if replacement:
            mapped.append(replacement)
        else:
            mapped.append(t.lower())

    result = " ".join(mapped)

    # Deduplicate adjacent identical words (e.g. "black black" → "black")
    deduped = []
    for word in result.split():
        if not deduped or word != deduped[-1]:
            deduped.append(word)

    return " ".join(deduped) if deduped else color.lower().strip()

BRAVE_IMAGE_SEARCH_URL = "https://api.search.brave.com/res/v1/images/search"


@dataclass
class BraveImageResult:
    """A single image result from Brave Image Search."""

    full_url: str
    thumbnail_url: str
    source_page: str
    title: str
    width: int
    height: int
    confidence: str


@dataclass
class ImageMatch:
    """An accepted image match for a variant color."""

    url: str
    thumbnail_url: str
    source_page: str
    color: str
    detected_color: str
    vision_verified: bool
    source: str = "brave_image_search"


class BraveImageResolver:
    """Finds product images via Brave Image Search with vision verification."""

    def __init__(self, settings: BraveImagesSettings) -> None:
        self.settings = settings
        self._brave_semaphore = asyncio.Semaphore(1)

    def _parse_results(
        self,
        data: dict,
        min_dim: int | None = None,
        dedupe_domains: bool = True,
    ) -> list[BraveImageResult]:
        """Parse Brave API response into filtered BraveImageResult list."""
        if min_dim is None:
            min_dim = self.settings.min_image_dimensions

        results: list[BraveImageResult] = []
        seen_domains: set[str] = set()

        for item in data.get("results", []):
            props = item.get("properties", {})
            thumb = item.get("thumbnail", {})
            confidence = item.get("confidence", "")

            # Skip low confidence
            if confidence not in ("high", "medium"):
                continue

            # Check full-size dimensions
            w = props.get("width") or 0
            h = props.get("height") or 0
            if w < min_dim or h < min_dim:
                continue

            full_url = props.get("url", "")
            thumbnail_url = thumb.get("src", "")
            source_page = item.get("url", "")

            if not full_url or not thumbnail_url:
                continue

            # Deduplicate by source domain
            if dedupe_domains:
                domain = item.get("source", "")
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)

            results.append(
                BraveImageResult(
                    full_url=full_url,
                    thumbnail_url=thumbnail_url,
                    source_page=source_page,
                    title=item.get("title", ""),
                    width=w,
                    height=h,
                    confidence=confidence,
                )
            )

        return results

    async def _search_brave_images(
        self,
        query: str,
        count: int | None = None,
    ) -> list[BraveImageResult]:
        """Query Brave Image Search API and return parsed results."""
        api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not api_key:
            logger.warning("BRAVE_SEARCH_API_KEY not set, skipping image search")
            return []

        if count is None:
            count = self.settings.brave_count

        async with self._brave_semaphore:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        BRAVE_IMAGE_SEARCH_URL,
                        params={"q": query, "count": count},
                        headers={
                            "Accept": "application/json",
                            "Accept-Encoding": "gzip",
                            "X-Subscription-Token": api_key,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as e:
                logger.warning("Brave image search failed for '%s': %s", query, e)
                return []
            finally:
                # Ensure 1 query/sec rate limit for Brave free tier
                await asyncio.sleep(1.0)

        results = self._parse_results(data)
        logger.info("Brave image search '%s': %d results after filtering", query, len(results))
        return results

    async def _download_thumbnail(self, url: str) -> bytes | None:
        """Download a thumbnail image. Returns bytes or None on failure."""
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "image" not in content_type:
                    return None
                return resp.content
        except Exception as e:
            logger.debug("Thumbnail download failed %s: %s", url, e)
            return None

    async def _verify_image(
        self,
        image_data: bytes,
        product_title: str,
        vendor: str,
        expected_color: str,
    ) -> dict:
        """Ask Ollama vision to verify an image matches product + color.

        Returns dict with keys: accepted, product_match, color_match,
        ecommerce_suitable, detected_color.
        """
        b64 = base64.b64encode(image_data).decode()

        prompt = (
            f"This image should be a product photo of: {vendor} {product_title}\n"
            f"Expected color: {expected_color}\n\n"
            f"Answer these 3 questions with YES or NO, then the color:\n"
            f"1. Is this a product photo of a {product_title} (or very similar product)?\n"
            f"2. Is it suitable for e-commerce (clean background, good quality, shows the product clearly)?\n"
            f"3. What is the main color of the product? (just the color name)\n\n"
            f"Format your answer exactly as:\n"
            f"PRODUCT: YES or NO\n"
            f"ECOMMERCE: YES or NO\n"
            f"COLOR: <color name>"
        )

        payload = {
            "model": self.settings.ollama_model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "think": False,
            "options": {"num_predict": 50, "temperature": 0.1},
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.verify_timeout) as client:
                resp = await client.post(
                    f"{self.settings.ollama_host}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "").strip()
        except Exception as e:
            logger.warning("Vision verification failed: %s", e)
            return {
                "accepted": False,
                "product_match": False,
                "color_match": False,
                "ecommerce_suitable": False,
                "detected_color": "",
            }

        return self._parse_verify_response(raw, expected_color)

    @staticmethod
    def _parse_verify_response(raw: str, expected_color: str) -> dict:
        """Parse the structured vision response."""
        lines = raw.upper().split("\n")
        product_match = False
        ecommerce = False
        detected_color = ""

        for line in lines:
            line = line.strip()
            if line.startswith("PRODUCT:"):
                product_match = "YES" in line
            elif line.startswith("ECOMMERCE:"):
                ecommerce = "YES" in line
            elif line.startswith("COLOR:"):
                detected_color = line.split(":", 1)[1].strip()

        # Color match: check if detected color overlaps with expected
        color_match = False
        if detected_color:
            expected_tokens = {t.lower() for t in expected_color.replace("/", " ").split()}
            detected_tokens = {t.lower() for t in detected_color.replace("/", " ").split()}
            noise = {"dark", "light", "bright", "deep", "pale", "matte"}
            expected_tokens -= noise
            detected_tokens -= noise
            color_match = bool(expected_tokens & detected_tokens)

        accepted = product_match and ecommerce and color_match

        return {
            "accepted": accepted,
            "product_match": product_match,
            "color_match": color_match,
            "ecommerce_suitable": ecommerce,
            "detected_color": detected_color,
        }

    async def _search_and_verify_color(
        self,
        vendor: str,
        product_title: str,
        color: str,
        candidates: list[BraveImageResult] | None = None,
    ) -> ImageMatch | None:
        """Search for and verify an image for a specific color.

        If candidates are provided, uses those instead of querying Brave.
        """
        if candidates is None:
            search_color = normalize_color_for_query(color)
            query = f"{vendor} {product_title} {search_color}"
            candidates = await self._search_brave_images(query, count=10)

        if not candidates:
            return None

        limit = self.settings.max_candidates_per_color
        for candidate in candidates[:limit]:
            thumb_data = await self._download_thumbnail(candidate.thumbnail_url)
            if not thumb_data:
                continue

            result = await self._verify_image(thumb_data, product_title, vendor, color)

            if result["accepted"]:
                logger.info(
                    "Brave image accepted for '%s' color '%s': %s",
                    product_title,
                    color,
                    candidate.full_url[:80],
                )
                return ImageMatch(
                    url=candidate.full_url,
                    thumbnail_url=candidate.thumbnail_url,
                    source_page=candidate.source_page,
                    color=color,
                    detected_color=result["detected_color"],
                    vision_verified=True,
                )
            else:
                logger.debug(
                    "Brave image rejected for '%s' color '%s': product=%s color=%s ecom=%s detected=%s",
                    product_title,
                    color,
                    result["product_match"],
                    result["color_match"],
                    result["ecommerce_suitable"],
                    result["detected_color"],
                )

        return None

    async def find_variant_images(
        self,
        product_title: str,
        vendor: str,
        colors: list[str],
    ) -> dict[str, ImageMatch]:
        """Find variant images for a list of colors using two-pass search.

        Pass 1: Broad query "{vendor} {product_title}", sort results by color
                using vision, match against needed colors.
        Pass 2: For unmatched colors, targeted query per color.

        Returns dict mapping color name -> ImageMatch.
        """
        if not colors:
            return {}

        mapping: dict[str, ImageMatch] = {}
        remaining_colors = list(colors)

        # Pass 1: Broad search
        broad_query = f"{vendor} {product_title}"
        all_candidates = await self._search_brave_images(broad_query)

        if all_candidates:
            evaluate_candidates = all_candidates[: self.settings.max_evaluate]

            for candidate in evaluate_candidates:
                if not remaining_colors:
                    break

                thumb_data = await self._download_thumbnail(candidate.thumbnail_url)
                if not thumb_data:
                    continue

                # Try each remaining color against this image
                for color in list(remaining_colors):
                    result = await self._verify_image(
                        thumb_data,
                        product_title,
                        vendor,
                        color,
                    )
                    if result["accepted"]:
                        mapping[color] = ImageMatch(
                            url=candidate.full_url,
                            thumbnail_url=candidate.thumbnail_url,
                            source_page=candidate.source_page,
                            color=color,
                            detected_color=result["detected_color"],
                            vision_verified=True,
                        )
                        remaining_colors.remove(color)
                        logger.info(
                            "Pass 1: matched '%s' for color '%s'",
                            candidate.full_url[:60],
                            color,
                        )
                        break

            logger.info(
                "Brave pass 1: %d/%d colors matched",
                len(colors) - len(remaining_colors),
                len(colors),
            )

        # Pass 2: Targeted per-color search for stragglers
        for color in list(remaining_colors):
            match = await self._search_and_verify_color(vendor, product_title, color)
            if match:
                mapping[color] = match
                remaining_colors.remove(color)
                logger.info("Pass 2: matched color '%s'", color)

        if remaining_colors:
            logger.info(
                "Brave image search: %d colors unmatched: %s",
                len(remaining_colors),
                remaining_colors,
            )

        return mapping

    async def find_product_images(
        self,
        product_title: str,
        vendor: str,
        max_images: int = 5,
    ) -> list[BraveImageResult]:
        """Find general product images (no color matching).

        Returns validated BraveImageResult list for product image fallback.
        """
        query = f"{vendor} {product_title}"
        candidates = await self._search_brave_images(query)
        return candidates[:max_images]
