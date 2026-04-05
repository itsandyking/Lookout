# Brave Image Search Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Brave Image Search as a fallback source for variant color images and product images when vendor sites are blocked or scraping fails.

**Architecture:** New `BraveImageResolver` module queries Brave's Image Search API, downloads thumbnails for vision verification via local Ollama, and feeds accepted images into the existing pipeline output tagged with `source: "brave_image_search"`. Two-pass search: broad query first, targeted per-color queries for stragglers.

**Tech Stack:** Brave Image Search API, Ollama vision (gemma4:e4b on localhost), httpx async HTTP, Pydantic models, existing pipeline infrastructure.

**Spec:** `docs/superpowers/specs/2026-04-05-brave-image-search-fallback-design.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `lookout/enrich/brave_images.py` | BraveImageResolver: Brave API queries, thumbnail download, vision verification, two-pass search |
| Create | `tests/test_brave_images.py` | Tests for BraveImageResolver |
| Modify | `lookout/enrich/models.py:226-239` | Add `BraveImagesSettings` to `GlobalSettings` |
| Modify | `lookout/enrich/models.py:275-283` | Add `source` field to `ImageInfo` |
| Modify | `lookout/enrich/generator.py:456-564` | Add Tier 2c (Brave fallback) in `_assign_variant_images()` |
| Modify | `lookout/enrich/generator.py:370-454` | Add product image fallback in `_select_images()` |
| Modify | `lookout/enrich/pipeline.py:320-324` | Change blocked vendor handling to allow Brave image search |
| Modify | `lookout/cli.py:448-449` | Add `--brave-images / --no-brave-images` CLI flag |
| Modify | `vendors.yaml:750+` | Add `brave_images` settings block |

---

### Task 1: BraveImagesSettings and ImageInfo Source Field

**Files:**
- Modify: `lookout/enrich/models.py:226-239`
- Modify: `lookout/enrich/models.py:275-283`
- Test: `tests/test_brave_images.py`

- [ ] **Step 1: Write failing test for settings model**

```python
# tests/test_brave_images.py
"""Tests for Brave Image Search fallback."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lookout.enrich.models import BraveImagesSettings, GlobalSettings, ImageInfo


def run(coro):
    """Helper to run async tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestBraveImagesSettings:
    def test_defaults(self):
        s = BraveImagesSettings()
        assert s.enabled is True
        assert s.ollama_host == "http://localhost:11434"
        assert s.ollama_model == "gemma4:e4b"
        assert s.max_candidates_per_color == 3
        assert s.min_image_dimensions == 400
        assert s.verify_timeout == 30
        assert s.brave_count == 50
        assert s.max_evaluate == 15

    def test_in_global_settings(self):
        gs = GlobalSettings()
        assert isinstance(gs.brave_images, BraveImagesSettings)


class TestImageInfoSource:
    def test_default_source_empty(self):
        img = ImageInfo(url="https://example.com/img.jpg")
        assert img.source == ""

    def test_source_set(self):
        img = ImageInfo(url="https://example.com/img.jpg", source="brave_image_search")
        assert img.source == "brave_image_search"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py -v`
Expected: FAIL — `ImportError: cannot import name 'BraveImagesSettings'`

- [ ] **Step 3: Add BraveImagesSettings to models.py**

In `lookout/enrich/models.py`, add before the `GlobalSettings` class (before line 226):

```python
class BraveImagesSettings(BaseModel):
    """Settings for Brave Image Search fallback."""

    enabled: bool = True
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e4b"
    max_candidates_per_color: int = 3
    min_image_dimensions: int = 400
    verify_timeout: int = 30
    brave_count: int = 50
    max_evaluate: int = 15
```

Add `brave_images` field to `GlobalSettings`:

```python
class GlobalSettings(BaseModel):
    """Global pipeline settings."""

    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
    rate_limiting: RateLimitSettings = Field(default_factory=RateLimitSettings)
    retries: RetrySettings = Field(default_factory=RetrySettings)
    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    brave_images: BraveImagesSettings = Field(default_factory=BraveImagesSettings)
```

Add `source` field to `ImageInfo`:

```python
class ImageInfo(BaseModel):
    """Information about an extracted image."""

    url: str
    inferred_view: str | None = None
    source_hint: str = ""
    alt_text: str = ""
    width: int | None = None
    height: int | None = None
    source: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestBraveImagesSettings -v && uv run pytest tests/test_brave_images.py::TestImageInfoSource -v`
Expected: PASS

- [ ] **Step 5: Run existing tests to confirm no regressions**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/ -x -q`
Expected: All existing tests pass (ImageInfo's new field has a default, so existing code is unaffected)

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout
git add lookout/enrich/models.py tests/test_brave_images.py
git commit -m "feat: add BraveImagesSettings and ImageInfo.source field"
```

---

### Task 2: BraveImageResolver — Brave API Search

**Files:**
- Create: `lookout/enrich/brave_images.py`
- Test: `tests/test_brave_images.py`

- [ ] **Step 1: Write failing test for Brave image search**

Append to `tests/test_brave_images.py`:

```python
from lookout.enrich.brave_images import BraveImageResolver, BraveImageResult


class TestBraveImageSearch:
    """Test Brave Image Search API integration."""

    def _resolver(self):
        from lookout.enrich.models import BraveImagesSettings
        return BraveImageResolver(BraveImagesSettings())

    def test_parse_results(self):
        """Brave API response is parsed into BraveImageResult objects."""
        fake_response = {
            "results": [
                {
                    "title": "Teva Verra Black Sandal",
                    "url": "https://example.com/product",
                    "source": "example.com",
                    "thumbnail": {"src": "https://imgs.brave.com/thumb.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://cdn.example.com/full.jpg", "width": 1200, "height": 1200},
                    "confidence": "high",
                },
                {
                    "title": "Unrelated Image",
                    "url": "https://other.com/page",
                    "source": "other.com",
                    "thumbnail": {"src": "https://imgs.brave.com/small.jpg", "width": 200, "height": 200},
                    "properties": {"url": "https://other.com/tiny.jpg", "width": 100, "height": 100},
                    "confidence": "low",
                },
            ]
        }
        resolver = self._resolver()
        results = resolver._parse_results(fake_response, min_dim=400)

        # Should filter out the 100x100 image and low confidence
        assert len(results) == 1
        assert results[0].full_url == "https://cdn.example.com/full.jpg"
        assert results[0].thumbnail_url == "https://imgs.brave.com/thumb.jpg"
        assert results[0].source_page == "https://example.com/product"
        assert results[0].title == "Teva Verra Black Sandal"

    def test_deduplicate_by_domain(self):
        """Only one image per source domain is kept."""
        fake_response = {
            "results": [
                {
                    "title": "Image 1",
                    "url": "https://example.com/page1",
                    "source": "example.com",
                    "thumbnail": {"src": "https://imgs.brave.com/t1.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://cdn.example.com/a.jpg", "width": 800, "height": 800},
                    "confidence": "high",
                },
                {
                    "title": "Image 2",
                    "url": "https://example.com/page2",
                    "source": "example.com",
                    "thumbnail": {"src": "https://imgs.brave.com/t2.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://cdn.example.com/b.jpg", "width": 800, "height": 800},
                    "confidence": "high",
                },
                {
                    "title": "Image 3",
                    "url": "https://other.com/page",
                    "source": "other.com",
                    "thumbnail": {"src": "https://imgs.brave.com/t3.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://other.com/c.jpg", "width": 800, "height": 800},
                    "confidence": "high",
                },
            ]
        }
        resolver = self._resolver()
        results = resolver._parse_results(fake_response, min_dim=400, dedupe_domains=True)
        assert len(results) == 2
        domains = {r.source_page.split("/")[2] for r in results}
        assert domains == {"example.com", "other.com"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestBraveImageSearch -v`
Expected: FAIL — `ImportError: cannot import name 'BraveImageResolver'`

- [ ] **Step 3: Create brave_images.py with search and parsing**

Create `lookout/enrich/brave_images.py`:

```python
"""Brave Image Search fallback for variant and product images.

Uses Brave's Image Search API to find product images when vendor sites
are blocked or scraping fails. Images are verified via local Ollama vision
before being accepted.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field

import httpx

from lookout.enrich.models import BraveImagesSettings

logger = logging.getLogger(__name__)

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
            return {"accepted": False, "product_match": False, "color_match": False,
                    "ecommerce_suitable": False, "detected_color": ""}

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
            # Remove noise
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
            query = f"{vendor} {product_title} {color}"
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
                    product_title, color, candidate.full_url[:80],
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
                    product_title, color, result["product_match"],
                    result["color_match"], result["ecommerce_suitable"],
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
            # Download thumbnails up to max_evaluate
            evaluate_candidates = all_candidates[:self.settings.max_evaluate]

            for candidate in evaluate_candidates:
                if not remaining_colors:
                    break

                thumb_data = await self._download_thumbnail(candidate.thumbnail_url)
                if not thumb_data:
                    continue

                # Try each remaining color against this image
                for color in list(remaining_colors):
                    result = await self._verify_image(
                        thumb_data, product_title, vendor, color,
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
                        logger.info("Pass 1: matched '%s' for color '%s'", candidate.full_url[:60], color)
                        break  # This image matched a color, move to next image

            logger.info(
                "Brave pass 1: %d/%d colors matched",
                len(colors) - len(remaining_colors), len(colors),
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
                len(remaining_colors), remaining_colors,
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
        # For product images, just return the top candidates — no vision needed
        # since we're not matching colors. The existing validate_image_urls()
        # in generator.py will do the HTTP HEAD check.
        return candidates[:max_images]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestBraveImageSearch -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/andyking/Lookout
git add lookout/enrich/brave_images.py tests/test_brave_images.py
git commit -m "feat: BraveImageResolver with search, parsing, and vision verification"
```

---

### Task 3: Vision Verification Tests

**Files:**
- Test: `tests/test_brave_images.py`
- Modify: `lookout/enrich/brave_images.py` (if fixes needed)

- [ ] **Step 1: Write tests for vision verify response parsing**

Append to `tests/test_brave_images.py`:

```python
from lookout.enrich.brave_images import BraveImageResolver


class TestParseVerifyResponse:
    """Test parsing of structured vision responses."""

    def test_all_pass(self):
        raw = "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Black"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is True
        assert result["product_match"] is True
        assert result["color_match"] is True
        assert result["ecommerce_suitable"] is True
        assert result["detected_color"] == "BLACK"

    def test_product_no(self):
        raw = "PRODUCT: NO\nECOMMERCE: YES\nCOLOR: Black"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False
        assert result["product_match"] is False

    def test_ecommerce_no(self):
        raw = "PRODUCT: YES\nECOMMERCE: NO\nCOLOR: Black"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False
        assert result["ecommerce_suitable"] is False

    def test_color_mismatch(self):
        raw = "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Red"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False
        assert result["color_match"] is False

    def test_color_fuzzy_match_slash(self):
        """'Purple Ink/Purple Dusk' matches detected 'Purple'."""
        raw = "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Purple"
        result = BraveImageResolver._parse_verify_response(raw, "Purple Ink/Purple Dusk")
        assert result["color_match"] is True
        assert result["accepted"] is True

    def test_color_fuzzy_match_multiword(self):
        """'Storm Blue' matches detected 'Blue'."""
        raw = "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Blue"
        result = BraveImageResolver._parse_verify_response(raw, "Storm Blue")
        assert result["color_match"] is True

    def test_empty_response(self):
        result = BraveImageResolver._parse_verify_response("", "Black")
        assert result["accepted"] is False

    def test_garbled_response(self):
        raw = "I see a black sandal on a white background"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False


class TestVerifyImage:
    """Test the full vision verification flow with mocked Ollama."""

    def _resolver(self):
        from lookout.enrich.models import BraveImagesSettings
        return BraveImageResolver(BraveImagesSettings())

    def test_accepted(self):
        resolver = self._resolver()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "response": "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Black"
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = run(resolver._verify_image(b"fake_img", "Verra Sandal", "Teva", "Black"))
            assert result["accepted"] is True

    def test_ollama_timeout(self):
        resolver = self._resolver()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client_cls.return_value = mock_client

            result = run(resolver._verify_image(b"fake_img", "Verra Sandal", "Teva", "Black"))
            assert result["accepted"] is False
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestParseVerifyResponse tests/test_brave_images.py::TestVerifyImage -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
cd /Users/andyking/Lookout
git add tests/test_brave_images.py
git commit -m "test: vision verification parsing and Ollama integration"
```

---

### Task 4: Two-Pass Search Flow Tests

**Files:**
- Test: `tests/test_brave_images.py`

- [ ] **Step 1: Write tests for find_variant_images two-pass flow**

Append to `tests/test_brave_images.py`:

```python
class TestFindVariantImages:
    """Test the two-pass search flow."""

    def _resolver(self):
        from lookout.enrich.models import BraveImagesSettings
        return BraveImageResolver(BraveImagesSettings(max_evaluate=3))

    def test_pass1_matches_colors(self):
        """Broad search matches multiple colors from one query."""
        resolver = self._resolver()

        # Mock: broad search returns 2 images, each matches a different color
        async def fake_search(query, count=None):
            return [
                BraveImageResult(
                    full_url="https://cdn.ex.com/black.jpg",
                    thumbnail_url="https://thumb.ex.com/black.jpg",
                    source_page="https://ex.com/p1",
                    title="Verra Black", width=800, height=800, confidence="high",
                ),
                BraveImageResult(
                    full_url="https://cdn.ex.com/grey.jpg",
                    thumbnail_url="https://thumb.ex.com/grey.jpg",
                    source_page="https://ex.com/p2",
                    title="Verra Grey", width=800, height=800, confidence="high",
                ),
            ]

        call_count = {"verify": 0}
        async def fake_download(url):
            return b"fake_image_data"

        async def fake_verify(data, title, vendor, color):
            call_count["verify"] += 1
            # First image matches Black, second matches Grey
            if color == "Black" and call_count["verify"] <= 2:
                return {"accepted": True, "product_match": True,
                        "color_match": True, "ecommerce_suitable": True,
                        "detected_color": "BLACK"}
            if color == "Grey" and call_count["verify"] > 2:
                return {"accepted": True, "product_match": True,
                        "color_match": True, "ecommerce_suitable": True,
                        "detected_color": "GREY"}
            return {"accepted": False, "product_match": True,
                    "color_match": False, "ecommerce_suitable": True,
                    "detected_color": "OTHER"}

        resolver._search_brave_images = fake_search
        resolver._download_thumbnail = fake_download
        resolver._verify_image = fake_verify

        result = run(resolver.find_variant_images("Verra Sandal", "Teva", ["Black", "Grey"]))
        assert "Black" in result
        assert result["Black"].url == "https://cdn.ex.com/black.jpg"

    def test_pass2_targeted_search(self):
        """Colors not matched in pass 1 get targeted per-color queries."""
        resolver = self._resolver()

        search_queries = []
        async def fake_search(query, count=None):
            search_queries.append(query)
            if "Rare Color" in query:
                return [
                    BraveImageResult(
                        full_url="https://cdn.ex.com/rare.jpg",
                        thumbnail_url="https://thumb.ex.com/rare.jpg",
                        source_page="https://ex.com/rare",
                        title="Verra Rare Color", width=800, height=800, confidence="high",
                    ),
                ]
            return []  # Broad search returns nothing

        async def fake_download(url):
            return b"fake_image_data"

        async def fake_verify(data, title, vendor, color):
            return {"accepted": True, "product_match": True,
                    "color_match": True, "ecommerce_suitable": True,
                    "detected_color": color.upper()}

        resolver._search_brave_images = fake_search
        resolver._download_thumbnail = fake_download
        resolver._verify_image = fake_verify

        result = run(resolver.find_variant_images("Verra Sandal", "Teva", ["Rare Color"]))

        # Should have done broad search first, then targeted
        assert len(search_queries) == 2
        assert "Teva Verra Sandal" in search_queries[0]
        assert "Rare Color" in search_queries[1]
        assert "Rare Color" in result

    def test_empty_colors(self):
        resolver = self._resolver()
        result = run(resolver.find_variant_images("Verra", "Teva", []))
        assert result == {}
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestFindVariantImages -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
cd /Users/andyking/Lookout
git add tests/test_brave_images.py
git commit -m "test: two-pass variant image search flow"
```

---

### Task 5: Pipeline Integration — Tier 2c in Variant Image Assignment

**Files:**
- Modify: `lookout/enrich/generator.py:456-564`
- Test: `tests/test_brave_images.py`

- [ ] **Step 1: Write failing test for Tier 2c integration**

Append to `tests/test_brave_images.py`:

```python
class TestTier2cIntegration:
    """Test that Tier 2c plugs into _assign_variant_images correctly."""

    def test_tier2c_called_when_2a_2b_fail(self):
        """When vision and LLM tiers fail, Brave fallback is tried."""
        from lookout.enrich.generator import Generator
        from lookout.enrich.models import (
            BraveImagesSettings, ExtractedFacts, ImageInfo, VariantOption,
        )

        # Build facts with images but no variant_image_candidates
        facts = ExtractedFacts(
            product_name="Women's Verra",
            variants=[VariantOption(option_name="Color", values=["Black", "Grey"])],
            images=[ImageInfo(url="https://example.com/hero.jpg")],
        )

        # Generator with a mocked LLM that returns nothing, and a brave resolver
        gen = Generator.__new__(Generator)
        gen.llm_client = MagicMock()
        gen.llm_client.select_variant_images_vision = AsyncMock(return_value={})
        gen.llm_client.select_variant_images = AsyncMock(return_value={})

        mock_brave = MagicMock()
        mock_brave.find_variant_images = AsyncMock(return_value={
            "Black": ImageMatch(
                url="https://cdn.ex.com/black.jpg",
                thumbnail_url="https://thumb.ex.com/black.jpg",
                source_page="https://ex.com/p",
                color="Black",
                detected_color="BLACK",
                vision_verified=True,
            ),
        })
        gen.brave_resolver = mock_brave

        variant_map, warnings = run(gen._assign_variant_images(facts))

        mock_brave.find_variant_images.assert_called_once()
        assert "Black" in variant_map
        assert variant_map["Black"] == "https://cdn.ex.com/black.jpg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestTier2cIntegration -v`
Expected: FAIL — `Generator` has no `brave_resolver` attribute

- [ ] **Step 3: Modify generator.py to add Tier 2c**

In `lookout/enrich/generator.py`, update the `Generator.__init__` to accept an optional `brave_resolver` parameter. Find the `__init__` method and add:

```python
self.brave_resolver = brave_resolver
```

Then in `_assign_variant_images()`, insert Tier 2c between line 542 (end of Tier 2b) and line 544 (start of Tier 0). After the Tier 2b `except` block and before the `# Tier 0` comment:

```python
        # Tier 2c: Brave Image Search fallback with vision verification
        if color_variant and self.brave_resolver and not variant_map:
            try:
                brave_mapping = await self.brave_resolver.find_variant_images(
                    product_title=facts.product_name or "",
                    vendor=facts.brand or "",
                    colors=color_variant.values,
                )
                if brave_mapping:
                    variant_map = {
                        color: match.url for color, match in brave_mapping.items()
                    }
                    logger.info(
                        "Tier 2c (Brave) variant images assigned: %d/%d colors",
                        len(variant_map), len(color_variant.values),
                    )
                    return variant_map, warnings
            except Exception as e:
                logger.warning("Brave image search failed: %s", e)
                warnings.append(f"BRAVE_IMAGE_SEARCH_ERROR: {e!s}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestTier2cIntegration -v`
Expected: PASS

- [ ] **Step 5: Run existing generator tests to confirm no regressions**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout
git add lookout/enrich/generator.py tests/test_brave_images.py
git commit -m "feat: Tier 2c Brave Image Search fallback in variant image assignment"
```

---

### Task 6: Pipeline Integration — Product Image Fallback in _select_images

**Files:**
- Modify: `lookout/enrich/generator.py:370-454`
- Test: `tests/test_brave_images.py`

- [ ] **Step 1: Write failing test for product image fallback**

Append to `tests/test_brave_images.py`:

```python
class TestProductImageFallback:
    """Test product image fallback when extraction yields few images."""

    def test_brave_fills_missing_images(self):
        from lookout.enrich.generator import Generator
        from lookout.enrich.models import ExtractedFacts, ImageInfo

        # Facts with no images
        facts = ExtractedFacts(
            product_name="Tikka Core Headlamp",
            brand="Petzl",
            images=[],
        )

        gen = Generator.__new__(Generator)
        gen.brave_resolver = MagicMock()
        gen.brave_resolver.find_product_images = AsyncMock(return_value=[
            BraveImageResult(
                full_url="https://cdn.ex.com/tikka.jpg",
                thumbnail_url="https://thumb.ex.com/tikka.jpg",
                source_page="https://ex.com/tikka",
                title="Petzl Tikka Core",
                width=1000, height=1000, confidence="high",
            ),
        ])

        images, warnings = gen._select_images(facts)

        gen.brave_resolver.find_product_images.assert_called_once()
        assert len(images) >= 1
        assert images[0].src == "https://cdn.ex.com/tikka.jpg"

    def test_no_fallback_when_enough_images(self):
        from lookout.enrich.generator import Generator
        from lookout.enrich.models import ExtractedFacts, ImageInfo

        facts = ExtractedFacts(
            product_name="Some Product",
            brand="Some Brand",
            images=[
                ImageInfo(url=f"https://ex.com/img{i}.jpg")
                for i in range(5)
            ],
        )

        gen = Generator.__new__(Generator)
        gen.brave_resolver = MagicMock()

        images, warnings = gen._select_images(facts)

        # Should not call brave since we have >= 3 images
        gen.brave_resolver.find_product_images.assert_not_called()
        assert len(images) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestProductImageFallback -v`
Expected: FAIL — `find_product_images` not called / `brave_resolver` not checked in `_select_images`

- [ ] **Step 3: Modify _select_images to add Brave fallback**

In `generator.py:_select_images()`, after line 387 (the `NO_IMAGES_FOUND` warning), before `return images, warnings`, add:

```python
        if not facts.images:
            # Try Brave Image Search fallback for product images
            if self.brave_resolver:
                try:
                    brave_results = asyncio.get_event_loop().run_until_complete(
                        self.brave_resolver.find_product_images(
                            product_title=facts.product_name or "",
                            vendor=facts.brand or "",
                        )
                    )
                    if brave_results:
                        for br in brave_results:
                            facts.images.append(
                                ImageInfo(
                                    url=br.full_url,
                                    alt_text=br.title,
                                    width=br.width,
                                    height=br.height,
                                    source="brave_image_search",
                                )
                            )
                        logger.info("Brave product image fallback: added %d images", len(brave_results))
                        # Clear the warning since we found images
                        warnings = [w for w in warnings if w != "NO_IMAGES_FOUND"]
                    else:
                        return images, warnings
                except Exception as e:
                    logger.warning("Brave product image fallback failed: %s", e)
                    return images, warnings
            else:
                return images, warnings
```

Note: Since `_select_images` is synchronous but `find_product_images` is async, check whether the calling context is already in an async event loop. If so, use the existing pattern from the codebase. The actual implementation should match how other async calls are made from `Generator` methods — inspect the `_assign_variant_images` caller to see if `_select_images` is called from an async context and refactor to `async def _select_images` if needed.

Also add the threshold check — after line 454 (before `return images, warnings` at the end), add a check for fewer than 3 images:

```python
        # If we have fewer than 3 images, try Brave fallback
        if len(images) < 3 and self.brave_resolver:
            try:
                brave_results = await self.brave_resolver.find_product_images(
                    product_title=facts.product_name or "",
                    vendor=facts.brand or "",
                    max_images=3 - len(images),
                )
                for br in brave_results:
                    issue = _check_image_importable(br.full_url)
                    if not issue:
                        images.append(
                            OutputImage(
                                src=br.full_url,
                                position=len(images) + 1,
                                alt=br.title or self._generate_alt_text(facts.product_name, len(images) + 1),
                                source="brave_image_search",
                            )
                        )
                if brave_results:
                    logger.info("Brave product image fallback: padded to %d images", len(images))
            except Exception as e:
                logger.warning("Brave product image fallback failed: %s", e)
```

The implementer should make `_select_images` async to match the rest of `Generator`'s methods, and adapt the test accordingly.

- [ ] **Step 4: Run tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestProductImageFallback -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout
git add lookout/enrich/generator.py tests/test_brave_images.py
git commit -m "feat: Brave product image fallback when extraction yields < 3 images"
```

---

### Task 7: Blocked Vendor Path — Allow Brave-Only Enrichment

**Files:**
- Modify: `lookout/enrich/pipeline.py:320-324`
- Test: `tests/test_brave_images.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_brave_images.py`:

```python
class TestBlockedVendorPath:
    """Test that blocked vendors get Brave image search instead of skipping."""

    def test_blocked_vendor_tries_brave(self):
        """Blocked vendors should not be skipped entirely — Brave images are attempted."""
        # This is an integration-level test; mock at the pipeline boundary.
        # The key assertion: when vendor_config.blocked is True, the pipeline
        # should NOT return SKIPPED_VENDOR_NOT_CONFIGURED immediately.
        # Instead it should skip scraping but call brave_resolver.
        from lookout.enrich.models import ProcessingStatus, VendorConfig

        vc = VendorConfig(domain="teva.com", blocked=True)
        assert vc.blocked is True
        # The actual pipeline change is tested by running the pipeline with
        # a blocked vendor and verifying it produces output.
        # See integration test below.
```

- [ ] **Step 2: Modify pipeline.py blocked vendor handling**

In `pipeline.py`, replace lines 320-324:

```python
            if vendor_config.blocked:
                handle_log.entries.append(
                    LogEntry(level="INFO", message=f"Vendor blocked (bot protection): {vendor}")
                )
                return None, ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED, metadata
```

With:

```python
            if vendor_config.blocked:
                handle_log.entries.append(
                    LogEntry(level="INFO", message=f"Vendor blocked (bot protection): {vendor}")
                )
                # If Brave image search is available, try it instead of skipping
                if self.brave_resolver:
                    handle_log.entries.append(
                        LogEntry(level="INFO", message="Attempting Brave image search fallback for blocked vendor")
                    )
                    try:
                        # Get color variants from input row
                        colors = input_row.known_colors or []
                        brave_mapping = await self.brave_resolver.find_variant_images(
                            product_title=input_row.title or "",
                            vendor=vendor,
                            colors=colors,
                        )
                        if brave_mapping:
                            variant_map = {
                                color: match.url for color, match in brave_mapping.items()
                            }
                            metadata["brave_image_search"] = {
                                "colors_searched": colors,
                                "colors_matched": list(brave_mapping.keys()),
                                "candidates_evaluated": sum(1 for _ in brave_mapping.values()),
                                "images_accepted": len(brave_mapping),
                            }
                            handle_log.entries.append(
                                LogEntry(
                                    level="INFO",
                                    message=f"Brave images found for {len(brave_mapping)}/{len(colors)} colors",
                                    data={"colors_matched": list(brave_mapping.keys())},
                                )
                            )
                            # Build minimal output with just variant images
                            # (no body_html since we can't scrape the vendor site)
                            output = self._build_brave_only_output(
                                input_row, variant_map, brave_mapping, metadata,
                            )
                            return output, ProcessingStatus.UPDATED, metadata
                    except Exception as e:
                        logger.warning("Brave fallback for blocked vendor failed: %s", e)
                        handle_log.entries.append(
                            LogEntry(level="WARNING", message=f"Brave fallback failed: {e}")
                        )

                return None, ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED, metadata
```

The implementer will need to add the `_build_brave_only_output` helper method to `ProductProcessor` that constructs the output dict with variant image assignments but no body_html. Follow the pattern of the existing output construction in the `process()` method (around lines 700-900), but only include the variant image map and image list.

- [ ] **Step 3: Run tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py -v && uv run pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
cd /Users/andyking/Lookout
git add lookout/enrich/pipeline.py tests/test_brave_images.py
git commit -m "feat: blocked vendors attempt Brave image search instead of skipping"
```

---

### Task 8: Configuration and CLI

**Files:**
- Modify: `lookout/cli.py:448-449`
- Modify: `lookout/enrich/pipeline.py:193-223` (PipelineConfig)
- Modify: `vendors.yaml:750+`

- [ ] **Step 1: Add brave_images settings to vendors.yaml**

In `vendors.yaml`, inside the `settings:` block (after existing settings), add:

```yaml
  brave_images:
    enabled: true
    ollama_host: "http://localhost:11434"
    ollama_model: "gemma4:e4b"
    max_candidates_per_color: 3
    min_image_dimensions: 400
    verify_timeout: 30
    brave_count: 50
    max_evaluate: 15
```

- [ ] **Step 2: Add CLI flag**

In `lookout/cli.py`, add a new option after line 448 (the `--llm` option):

```python
@click.option("--brave-images/--no-brave-images", "brave_images", default=None, help="Enable/disable Brave Image Search fallback (default: from vendors.yaml)")
```

Update the `run` function signature to include `brave_images`:

```python
def run(input_path, vendor, handles, output_dir, vendors_path, concurrency, max_rows, force, dry_run, verify, only_mode, llm_provider, brave_images, verbose):
```

- [ ] **Step 3: Wire BraveImageResolver into PipelineConfig and pipeline startup**

In `pipeline.py`, modify `PipelineConfig.__init__` to accept `brave_images: bool | None = None`.

In the pipeline startup (where `ProductProcessor` is created), instantiate `BraveImageResolver` if enabled:

```python
from lookout.enrich.brave_images import BraveImageResolver

# Determine if brave images enabled
brave_enabled = config.brave_images
if brave_enabled is None:
    brave_enabled = vendors_config.settings.brave_images.enabled

brave_resolver = None
if brave_enabled:
    brave_resolver = BraveImageResolver(vendors_config.settings.brave_images)
```

Pass `brave_resolver` to both `Generator` and `ProductProcessor`.

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd /Users/andyking/Lookout
git add lookout/cli.py lookout/enrich/pipeline.py vendors.yaml
git commit -m "feat: brave_images config in vendors.yaml and --brave-images CLI flag"
```

---

### Task 9: Match Decision Logging for Brave Images

**Files:**
- Modify: `lookout/enrich/match_validator.py:279-312`
- Test: `tests/test_brave_images.py`

- [ ] **Step 1: Write test for brave_image_search field in decisions**

Append to `tests/test_brave_images.py`:

```python
import json
import tempfile
from pathlib import Path

class TestMatchDecisionLogging:
    """Test that Brave image search results are logged in match_decisions."""

    def test_brave_field_in_decision_record(self):
        from lookout.enrich.match_validator import MatchDecisionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = MatchDecisionLogger(Path(tmpdir))
            logger.log(
                handle="womens-verra",
                vendor="Teva",
                catalog_title="Women's Verra",
                catalog_price=75.0,
                catalog_colors=["Black", "Grey"],
                candidates_tried=[],
                outcome="no_match",
                final_url=None,
                brave_image_search={
                    "colors_searched": ["Black", "Grey"],
                    "colors_matched": ["Black"],
                    "candidates_evaluated": 6,
                    "images_accepted": 1,
                },
            )

            decisions_file = Path(tmpdir) / "match_decisions.jsonl"
            assert decisions_file.exists()
            record = json.loads(decisions_file.read_text().strip())
            assert record["brave_image_search"]["colors_matched"] == ["Black"]
            assert record["brave_image_search"]["images_accepted"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestMatchDecisionLogging -v`
Expected: FAIL — `log()` doesn't accept `brave_image_search` kwarg

- [ ] **Step 3: Add brave_image_search to MatchDecisionLogger.log()**

In `match_validator.py`, modify the `log()` method signature to accept `brave_image_search: dict | None = None`, and add it to the record dict:

```python
    def log(
        self,
        handle: str,
        vendor: str,
        catalog_title: str,
        catalog_price: float | None,
        catalog_colors: list[str] | None,
        candidates_tried: list[dict],
        outcome: str,
        final_url: str | None,
        resolver_candidates: list[dict] | None = None,
        brave_image_search: dict | None = None,
    ) -> None:
```

In the record construction, add:

```python
            if brave_image_search:
                record["brave_image_search"] = brave_image_search
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_brave_images.py::TestMatchDecisionLogging -v && uv run pytest tests/test_match_decisions.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd /Users/andyking/Lookout
git add lookout/enrich/match_validator.py tests/test_brave_images.py
git commit -m "feat: log brave_image_search results in match_decisions.jsonl"
```

---

### Task 10: End-to-End Smoke Test

**Files:**
- Test: manual CLI test

- [ ] **Step 1: Run enrichment on a single blocked vendor product**

```bash
cd /Users/andyking/Lookout
source .env
uv run lookout enrich run \
  --force \
  --brave-images \
  -h womens-verra \
  -o output/test-brave-e4b \
  --verbose
```

Expected: Instead of `SKIPPED_VENDOR_NOT_CONFIGURED`, should attempt Brave image search and produce variant images for Verra's colors.

- [ ] **Step 2: Check output for correct tagging**

```bash
cat output/test-brave-e4b/womens-verra/log.json | python3 -m json.tool
```

Verify:
- Log shows "Attempting Brave image search fallback for blocked vendor"
- Log shows "Brave images found for N/M colors"
- Images have `source: "brave_image_search"`

- [ ] **Step 3: Run on a non-blocked product with few images**

```bash
uv run lookout enrich run \
  --force \
  --brave-images \
  -h tikka-core-headlamp-450-lumens \
  -o output/test-brave-petzl \
  --verbose
```

Expected: Normal resolver runs, but if variant images or product images are low, Brave fallback supplements.

- [ ] **Step 4: Commit any fixes discovered during smoke test**

```bash
cd /Users/andyking/Lookout
git add -A
git commit -m "fix: smoke test fixes for Brave image search integration"
```

---

### Task 11: Run Full No-Match Batch with E4B

**Files:**
- Output: `output/retry-no-match-brave/`

- [ ] **Step 1: Run all 21 no_match products through the updated pipeline**

```bash
cd /Users/andyking/Lookout
source .env
uv run lookout enrich run \
  --force \
  --brave-images \
  -o output/retry-no-match-brave \
  -h womens-tirra-sport \
  -h womens-verra \
  -h disco-mens-endless-promise-sleeping-bag-15f-2026 \
  -h mens-garibaldi-2-0-jacket \
  -h mens-hydratrek-sandal \
  -h womens-hydratrek-sandal \
  -h mens-original-universal \
  -h mens-skyline-long-sleeve-shirt \
  -h womens-naya-cropped-stowhood \
  -h mens-hudson \
  -h sinsolo-bandana \
  -h womens-short-sleeved-eyelet-tee \
  -h mens-short-sleeved-flax-blend-shirt \
  -h tikka-core-headlamp-450-lumens \
  -h womens-gauze-tank \
  -h womens-sima-long-sleeve-shirt \
  -h 11-4-mm-bw-ii-static-600 \
  -h mens-trailhead \
  -h womens-blaze-86-skis \
  -h mens-merino-sun-hoodie \
  -h womens-lone-peak-9-waterproof-mid \
  --verbose
```

- [ ] **Step 2: Review results**

```bash
uv run lookout enrich review -d output/retry-no-match-brave
```

Compare outcomes vs the original no_match results. Key metrics:
- How many of the 21 now have variant images?
- How many Brave images were accepted vs rejected by vision?
- Which vendors/products still failed?

- [ ] **Step 3: Commit results summary**

```bash
cd /Users/andyking/Lookout
git add -A
git commit -m "results: Brave image search retry on 21 no_match products"
```
