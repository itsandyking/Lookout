"""
Content generator for enrichment pipeline.

This module handles:
1. Generating Body HTML from extracted facts
2. Selecting and ordering product images
3. Assigning variant images (Tier 0 and Tier 1)
4. Producing the final MerchOutput
"""

import json
import logging
import re
from pathlib import Path

from .llm import LLMClient
from .models import ExtractedFacts, ImageInfo, InputRow, MerchOutput, OutputImage

logger = logging.getLogger(__name__)

# Extensions Shopify won't import
_NON_IMPORTABLE_EXTENSIONS = {".svg", ".webp", ".avif", ".gif", ".bmp", ".ico"}

# Query param patterns that suggest expiring/signed URLs
_EXPIRING_URL_PARAMS = {"token", "expires", "signature", "sig", "x-amz-credential"}


def _check_image_importable(url: str) -> str | None:
    """Check if an image URL is likely importable into Shopify.

    Returns None if OK, or a reason string if not importable.
    """
    url_lower = url.lower()

    # Check extension
    # Strip query params for extension check
    path = url_lower.split("?")[0]
    for ext in _NON_IMPORTABLE_EXTENSIONS:
        if path.endswith(ext):
            return f"unsupported format: {ext}"

    # Check for tiny/icon images in URL (but not aspect ratio suffixes like "-1x1.")
    icon_markers = ["icon", "logo", "badge", "favicon", "pixel"]
    if any(marker in url_lower for marker in icon_markers):
        return "likely icon/badge"

    # Check for data URIs
    if url.startswith("data:"):
        return "data URI"

    # Warn about potentially expiring signed URLs (don't block, just note)
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    params = parse_qs(parsed.query.lower())
    for param in _EXPIRING_URL_PARAMS:
        if param in params:
            logger.debug(f"Image URL may expire (has {param} param): {url[:80]}")

    return None


async def validate_image_urls(
    images: list[dict], http_client: object | None = None
) -> list[dict]:
    """HEAD-request image URLs and annotate with validation results.

    Args:
        images: List of dicts with at least 'src' key.
        http_client: Optional shared HTTP client.

    Returns:
        Same list with added 'valid', 'status_code', 'content_type', 'size' keys.
    """
    import httpx

    close_client = False
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        close_client = True

    try:
        for img in images:
            try:
                resp = await http_client.head(
                    img["src"],
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                content_type = resp.headers.get("content-type", "")
                content_length = int(resp.headers.get("content-length", 0))

                img["status_code"] = resp.status_code
                img["content_type"] = content_type
                img["size_bytes"] = content_length

                if resp.status_code != 200:
                    img["valid"] = False
                    img["validation_error"] = f"HTTP {resp.status_code}"
                elif not content_type.startswith("image/"):
                    img["valid"] = False
                    img["validation_error"] = f"not an image: {content_type}"
                elif content_length > 0 and content_length < 1024:
                    img["valid"] = False
                    img["validation_error"] = f"too small: {content_length} bytes"
                elif content_length > 20 * 1024 * 1024:
                    img["valid"] = False
                    img["validation_error"] = f"too large: {content_length} bytes"
                else:
                    img["valid"] = True

            except Exception as e:
                img["valid"] = False
                img["validation_error"] = str(e)
    finally:
        if close_client:
            await http_client.aclose()

    return images


class Generator:
    """
    Generates content output from extracted facts.

    Handles:
    - Body HTML generation (LLM-assisted)
    - Product image selection and ordering
    - Variant image assignment (Tier 0 and Tier 1)
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        """
        Initialize the generator.

        Args:
            llm_client: LLM client for text generation. If not provided,
                       body HTML generation will be skipped.
        """
        self.llm_client = llm_client

    async def generate_output(
        self,
        input_row: InputRow,
        facts: ExtractedFacts,
    ) -> MerchOutput:
        """
        Generate merchandising output for a product.

        Only generates content for missing fields as indicated by input_row.

        Args:
            input_row: The input row with gap flags.
            facts: Extracted product facts.

        Returns:
            MerchOutput with generated content.
        """
        output = MerchOutput(
            handle=input_row.product_handle,
            confidence=0,
        )

        warnings: list[str] = []

        # Generate body HTML if needed
        if input_row.needs_description:
            body_html, body_warnings = await self._generate_body_html(input_row, facts)
            output.body_html = body_html
            warnings.extend(body_warnings)

        # Generate images if needed
        if input_row.needs_images:
            images, image_warnings = self._select_images(facts)
            output.images = images
            warnings.extend(image_warnings)

        # Generate variant images if needed
        if input_row.needs_variant_images:
            variant_map, variant_warnings = await self._assign_variant_images(
                facts, selected_images=output.images or None
            )
            output.variant_image_map = variant_map
            warnings.extend(variant_warnings)

        output.warnings = warnings

        # Calculate overall confidence
        output.confidence = self._calculate_confidence(output, facts)

        return output

    async def _generate_body_html(
        self,
        input_row: InputRow,
        facts: ExtractedFacts,
    ) -> tuple[str | None, list[str]]:
        """
        Generate Body HTML from extracted facts.

        Args:
            input_row: Input row with product info.
            facts: Extracted product facts.

        Returns:
            Tuple of (body_html, warnings)
        """
        warnings: list[str] = []

        if not self.llm_client:
            warnings.append("LLM_CLIENT_NOT_AVAILABLE")
            return self._generate_fallback_html(facts), warnings

        # Check if we have enough content to generate
        has_content = facts.description_blocks or facts.feature_bullets or facts.specs

        if not has_content:
            warnings.append("INSUFFICIENT_CONTENT_FOR_DESCRIPTION")
            return None, warnings

        try:
            # Prepare facts for LLM
            facts_dict = {
                "product_name": facts.product_name,
                "brand": facts.brand,
                "description_blocks": facts.description_blocks[:3],
                "feature_bullets": facts.feature_bullets[:6],
                "specs": dict(list(facts.specs.items())[:10]),
                "materials": facts.materials,
                "care": facts.care,
                "fit_dimensions": facts.fit_dimensions,
            }

            body_html = await self.llm_client.generate_body_html(
                facts=facts_dict,
                handle=input_row.product_handle,
                vendor=input_row.vendor,
            )

            # Validate HTML
            body_html = self._clean_html(body_html)

            if not body_html or len(body_html) < 50:
                warnings.append("GENERATED_HTML_TOO_SHORT")
                return self._generate_fallback_html(facts), warnings

            return body_html, warnings

        except Exception as e:
            logger.error(f"Error generating body HTML: {e}")
            warnings.append(f"HTML_GENERATION_ERROR: {e!s}")
            return self._generate_fallback_html(facts), warnings

    def _generate_fallback_html(self, facts: ExtractedFacts) -> str | None:
        """
        Generate simple fallback HTML without LLM.

        Args:
            facts: Extracted product facts.

        Returns:
            Simple HTML body or None.
        """
        parts: list[str] = []

        # Add intro from description blocks
        if facts.description_blocks:
            first_desc = facts.description_blocks[0]
            # Truncate if too long
            if len(first_desc) > 500:
                first_desc = first_desc[:497] + "..."
            parts.append(f"<p>{self._escape_html(first_desc)}</p>")

        # Add features if available
        if facts.feature_bullets:
            parts.append("<h3>Features</h3>")
            parts.append("<ul>")
            for bullet in facts.feature_bullets[:6]:
                # Truncate long bullets
                if len(bullet) > 100:
                    bullet = bullet[:97] + "..."
                parts.append(f"<li>{self._escape_html(bullet)}</li>")
            parts.append("</ul>")

        # Add specs if available
        if facts.specs:
            parts.append("<h3>Specifications</h3>")
            parts.append("<table>")
            for key, value in list(facts.specs.items())[:8]:
                parts.append(
                    f"<tr><td>{self._escape_html(key)}</td><td>{self._escape_html(value)}</td></tr>"
                )
            parts.append("</table>")

        if not parts:
            return None

        return "\n".join(parts)

    def _select_images(
        self,
        facts: ExtractedFacts,
    ) -> tuple[list[OutputImage], list[str]]:
        """
        Select and order product images.

        Args:
            facts: Extracted product facts.

        Returns:
            Tuple of (images, warnings)
        """
        warnings: list[str] = []
        images: list[OutputImage] = []

        if not facts.images:
            warnings.append("NO_IMAGES_FOUND")
            return images, warnings

        # Deduplicate images by URL (without query params)
        seen_urls: set[str] = set()
        unique_images: list[ImageInfo] = []

        for img in facts.images:
            normalized = img.url.split("?")[0]
            if normalized not in seen_urls:
                seen_urls.add(normalized)
                unique_images.append(img)

        # Filter out non-importable images
        importable: list[ImageInfo] = []
        for img in unique_images:
            issue = _check_image_importable(img.url)
            if issue:
                warnings.append(f"IMAGE_SKIPPED: {issue} — {img.url[:80]}")
            else:
                importable.append(img)

        # Sort by source hint (prefer JSON-LD, then img_tag)
        def sort_key(img: ImageInfo) -> int:
            if img.source_hint == "json_ld":
                return 0
            if img.source_hint == "gallery":
                return 1
            return 2

        importable.sort(key=sort_key)

        # Create output images with positions
        for position, img in enumerate(importable[:10], start=1):
            alt_text = img.alt_text or self._generate_alt_text(facts.product_name, position)

            images.append(
                OutputImage(
                    src=img.url,
                    position=position,
                    alt=alt_text,
                )
            )

        if len(importable) > 10:
            warnings.append(f"TRUNCATED_IMAGES: {len(importable)} importable, limited to 10")
        if not importable and unique_images:
            warnings.append("ALL_IMAGES_FILTERED: none passed import validation")

        return images, warnings

    async def _assign_variant_images(
        self,
        facts: ExtractedFacts,
        selected_images: list[OutputImage] | None = None,
    ) -> tuple[dict[str, str | list[str]], list[str]]:
        """
        Assign images to variants using tiered approach.

        Tier 0: Assign hero image to ALL variants (size-only, single-color)
        Tier 1: Use explicit color->image mappings from HTML/JS extraction
        Tier 2: LLM-assisted color->image matching

        Args:
            facts: Extracted product facts.
            selected_images: Already-selected output images (for Tier 0).

        Returns:
            Tuple of (variant_image_map, warnings)
        """
        warnings: list[str] = []
        variant_map: dict[str, str | list[str]] = {}

        # Check if we have variant image candidates from extraction
        if facts.variant_image_candidates:
            # Tier 1: Use explicit color->image mappings from HTML
            for color, image_urls in facts.variant_image_candidates.items():
                if image_urls:
                    variant_map[color] = image_urls[0]

            if variant_map:
                logger.info(f"Tier 1 variant images assigned: {len(variant_map)} colors")
                return variant_map, warnings

        # Check if we have color variants defined
        color_variant = None
        for variant in facts.variants:
            if variant.option_name.lower() in ("color", "colour"):
                color_variant = variant
                break

        # Tier 2: LLM-assisted color matching (only if color variants exist)
        if color_variant and self.llm_client and facts.images:
            try:
                images_for_llm = [
                    {"url": img.url, "alt_text": img.alt_text} for img in facts.images[:20]
                ]

                llm_mapping = await self.llm_client.select_variant_images(
                    facts=facts.model_dump(mode="json"),
                    available_images=images_for_llm,
                )

                if llm_mapping:
                    variant_map = llm_mapping
                    logger.info(f"Tier 2 (LLM) variant images assigned: {len(variant_map)} colors")
                    return variant_map, warnings

            except Exception as e:
                logger.warning(f"LLM variant image selection failed: {e}")
                warnings.append(f"LLM_VARIANT_SELECTION_ERROR: {e!s}")

        # Tier 0: Assign hero image to all variants
        # Works for: single-color products, size-only variants, or when
        # color matching fails. Every variant gets the first product image.
        hero_url = None
        if selected_images:
            hero_url = selected_images[0].src
        elif facts.images:
            hero_url = facts.images[0].url

        if hero_url:
            # Use "__all__" as a special key meaning "apply to every variant"
            variant_map["__all__"] = hero_url
            if not color_variant:
                logger.info("Tier 0: Hero image assigned to all variants (no color options)")
            else:
                logger.info("Tier 0: Hero image assigned to all variants (color matching failed)")
                warnings.append("COLOR_MATCHING_FAILED_USING_HERO")
        else:
            warnings.append("NO_IMAGES_FOR_VARIANT_ASSIGNMENT")

        return variant_map, warnings

    def _calculate_confidence(
        self,
        output: MerchOutput,
        facts: ExtractedFacts,
    ) -> int:
        """
        Calculate overall confidence score for the output.

        Args:
            output: The generated output.
            facts: The extracted facts.

        Returns:
            Confidence score 0-100.
        """
        score = 50  # Base score

        # Boost for having body HTML
        if output.body_html:
            if len(output.body_html) > 200:
                score += 15
            elif len(output.body_html) > 100:
                score += 10
            else:
                score += 5

        # Boost for having images
        if output.images:
            score += min(len(output.images) * 3, 15)

        # Boost for variant images
        if output.variant_image_map:
            score += min(len(output.variant_image_map) * 2, 10)

        # Penalty for warnings
        critical_warnings = [
            "NO_IMAGES_FOUND",
            "INSUFFICIENT_CONTENT",
            "HTML_GENERATION_ERROR",
        ]
        for warning in output.warnings:
            if any(cw in warning for cw in critical_warnings):
                score -= 10
            else:
                score -= 2

        # Boost for rich source data
        if facts.json_ld_data:
            score += 5
        if facts.feature_bullets:
            score += 5
        if facts.specs:
            score += 5

        return max(0, min(100, score))

    def _generate_alt_text(self, product_name: str, position: int) -> str:
        """Generate alt text for an image."""
        if position == 1:
            return product_name
        return f"{product_name} - Image {position}"

    def _clean_html(self, html: str) -> str:
        """Clean and validate HTML output."""
        # Remove markdown code blocks if present
        html = re.sub(r"^```(?:html)?\s*", "", html.strip())
        html = re.sub(r"\s*```$", "", html)

        # Basic cleanup
        html = html.strip()

        return html

    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    async def save_output(
        self,
        output: MerchOutput,
        artifacts_dir: Path,
    ) -> None:
        """
        Save merchandising output to artifacts directory.

        Args:
            output: The merchandising output.
            artifacts_dir: Path to the artifacts directory.
        """
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        output_path = artifacts_dir / "merch_output.json"
        with open(output_path, "w") as f:
            json.dump(output.model_dump(mode="json"), f, indent=2, default=str)


async def generate_output(
    input_row: InputRow,
    facts: ExtractedFacts,
    llm_client: LLMClient | None = None,
) -> MerchOutput:
    """
    Convenience function to generate merchandising output.

    Args:
        input_row: Input row with gap flags.
        facts: Extracted product facts.
        llm_client: Optional LLM client.

    Returns:
        MerchOutput with generated content.
    """
    generator = Generator(llm_client)
    return await generator.generate_output(input_row, facts)
