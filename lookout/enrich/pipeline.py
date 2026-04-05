"""
Main pipeline orchestration for the merchfill system.

This module coordinates:
1. Reading input CSV
2. Processing each product (resolve -> scrape -> extract -> generate)
3. Managing concurrency and rate limiting
4. Caching and artifact management
5. Generating output files
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .extractor import ContentExtractor, extract_content
from .firecrawl_scraper import FirecrawlScraper
from .gmc_rules import check_prohibited_terms, validate_title
from .generator import Generator
from .io import parse_input_csv
from .llm import LLMClient, get_llm_client
from .models import (
    HandleLog,
    InputRow,
    LogEntry,
    MerchOutput,
    ProcessingStatus,
    VendorConfig,
    VendorsConfig,
)
from .resolver import URLResolver
from .scraper import WebScraper
from .shopify_output import ShopifyOutputBuilder
from .match_validator import (
    MatchDecisionLogger,
    check_post_extraction,
    check_title_gate,
    extract_page_title,
)
from .utils import ensure_dir, load_vendors_config, sanitize_filename

logger = logging.getLogger(__name__)

# Type alias for event callback function
EventCallback = Callable[[dict[str, Any]], None]

# Markers of bot-protection / waiting room pages
_BAD_CONTENT_MARKERS = [
    "sit tight",
    "hands full at the moment",
    "please verify you are a human",
    "checking your browser",
    "access denied",
    "enable javascript",
    "ray id",  # Cloudflare
]


def _assess_extraction_quality(facts: Any) -> dict[str, Any]:
    """Check if extracted content looks like a real product page.

    Returns dict with 'usable' bool, 'score' 0-100, and 'reason' string.
    """
    score = 0
    reasons: list[str] = []

    if facts.product_name:
        score += 30
    else:
        reasons.append("no product name")

    if facts.json_ld_data:
        score += 25
    if facts.images:
        score += 20
    elif not facts.json_ld_data:
        reasons.append("no images")

    if facts.feature_bullets:
        score += 10
    if facts.description_blocks:
        score += 10
        # Check for bot-protection markers in description
        all_text = " ".join(facts.description_blocks).lower()
        for marker in _BAD_CONTENT_MARKERS:
            if marker in all_text:
                score = max(0, score - 40)
                reasons.append(f"bot-protection detected: '{marker}'")
                break
    else:
        reasons.append("no description")

    if facts.specs:
        score += 5

    usable = score >= 30
    reason = "; ".join(reasons) if reasons else "good"

    return {"usable": usable, "score": score, "reason": reason}


def _cross_reference_catalog(store: Any, input_row: Any, facts: Any) -> dict[str, Any]:
    """Cross-reference scraped data against vendor catalog in TVR.

    Checks:
    - Product name similarity (scraped vs. catalog)
    - Price plausibility (scraped price vs. known cost/MSRP)
    - Catalog image availability (may be higher quality than scraped)
    - Catalog description availability

    Returns dict with 'warnings' list and optional 'catalog_description'.
    """
    from difflib import SequenceMatcher

    warnings: list[str] = []
    result: dict[str, Any] = {"warnings": warnings}

    barcode = (input_row.barcode or "").strip()
    if not barcode:
        return result

    # Look up variant by barcode
    variant = store.get_variant_by_barcode(barcode)
    if not variant:
        return result

    product = store.get_product(input_row.product_handle)

    # Check product name similarity
    if facts.product_name and product:
        catalog_title = product.get("title", "")
        if catalog_title:
            ratio = SequenceMatcher(
                None,
                facts.product_name.lower(),
                catalog_title.lower(),
            ).ratio()
            if ratio < 0.3:
                warnings.append(
                    f"PRODUCT_NAME_MISMATCH: scraped='{facts.product_name}' "
                    f"vs catalog='{catalog_title}' (similarity={ratio:.0%})"
                )
            elif ratio < 0.6:
                warnings.append(
                    f"PRODUCT_NAME_LOW_SIMILARITY: scraped='{facts.product_name}' "
                    f"vs catalog='{catalog_title}' (similarity={ratio:.0%})"
                )

    # Check price plausibility
    if variant.get("price") and facts.json_ld_data:
        scraped_price = None
        offers = facts.json_ld_data.get("offers", {})
        if isinstance(offers, dict):
            scraped_price = offers.get("price")
        elif isinstance(offers, list) and offers:
            scraped_price = offers[0].get("price")

        if scraped_price:
            try:
                scraped_f = float(scraped_price)
                known_price = float(variant["price"])
                # Flag if scraped price differs by more than 50% from known price
                if known_price > 0 and abs(scraped_f - known_price) / known_price > 0.5:
                    warnings.append(
                        f"PRICE_MISMATCH: scraped=${scraped_f:.2f} "
                        f"vs catalog=${known_price:.2f}"
                    )
            except (ValueError, TypeError):
                pass

    # Check for catalog image (may be higher quality)
    catalog_img = store.find_catalog_image(barcode)
    if catalog_img:
        result["catalog_image"] = catalog_img

    # Check for catalog description
    if product:
        catalog_desc = store.find_catalog_description(product["id"])
        if catalog_desc:
            result["catalog_description"] = catalog_desc

    return result


class PipelineConfig:
    """Configuration for the pipeline."""

    def __init__(
        self,
        input_path: Path | None = None,
        output_dir: Path = Path("./output"),
        vendors_path: Path = Path("./vendors.yaml"),
        shopify_export_path: Path | None = None,
        concurrency: int = 5,
        max_rows: int | None = None,
        force: bool = False,
        dry_run: bool = False,
        input_rows: list | None = None,
        verify: bool = False,
        only_mode: str | None = None,
        llm_provider: str | None = None,
    ) -> None:
        self.input_path = input_path
        self.output_dir = output_dir
        self.vendors_path = vendors_path
        self.shopify_export_path = shopify_export_path
        self.concurrency = concurrency
        self.max_rows = max_rows
        self.force = force
        self.dry_run = dry_run
        # Pre-built InputRow objects (bypass CSV, carry rich variant data)
        self.input_rows = input_rows or []
        self.verify = verify
        self.only_mode = only_mode
        self.llm_provider = llm_provider


class ProductProcessor:
    """
    Processes a single product through the pipeline.

    Handles:
    - URL resolution
    - Web scraping
    - Content extraction
    - Merchandising generation
    - Artifact saving
    """

    def __init__(
        self,
        vendors_config: VendorsConfig,
        http_client: httpx.AsyncClient,
        llm_client: LLMClient | None,
        artifacts_base: Path,
        force: bool = False,
        store: Any | None = None,
        verify: bool = False,
        only_mode: str | None = None,
        decision_logger: MatchDecisionLogger | None = None,
    ) -> None:
        self.vendors_config = vendors_config
        self.http_client = http_client
        self.llm_client = llm_client
        self.artifacts_base = artifacts_base
        self.force = force
        self.store = store  # Optional LookoutStore for catalog cross-referencing
        self.verify = verify
        self.only_mode = only_mode
        self.decision_logger = decision_logger

        self.resolver = URLResolver(http_client=http_client)
        self.firecrawl = FirecrawlScraper()
        self.generator = Generator(llm_client=llm_client)

    async def process(
        self,
        input_row: InputRow,
    ) -> tuple[MerchOutput | None, ProcessingStatus, dict[str, Any]]:
        """
        Process a single product.

        Args:
            input_row: The input row to process.

        Returns:
            Tuple of (merch_output, status, metadata)
        """
        start_time = time.time()
        handle = input_row.product_handle
        vendor = input_row.vendor

        # Initialize log
        handle_log = HandleLog(handle=handle)
        metadata: dict[str, Any] = {
            "confidence": 0,
            "warnings": [],
            "error": "",
        }

        # Setup artifacts directory
        artifacts_dir = self.artifacts_base / sanitize_filename(handle)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Apply --only filter
            if self.only_mode:
                if self.only_mode == "images":
                    input_row = input_row.model_copy(update={"has_description": True, "has_variant_images": True})
                elif self.only_mode == "description":
                    input_row = input_row.model_copy(update={"has_image": True, "has_variant_images": True})
                elif self.only_mode == "variant-images":
                    input_row = input_row.model_copy(update={"has_image": True, "has_description": True})

            # Check if product has any gaps (force mode skips this check)
            if not self.force and not input_row.has_any_gap:
                handle_log.entries.append(LogEntry(level="INFO", message="No gaps to fill"))
                return None, ProcessingStatus.SKIPPED_NO_GAPS, metadata

            # Check vendor configuration
            vendor_config = self.vendors_config.vendors.get(vendor)
            if not vendor_config:
                handle_log.entries.append(
                    LogEntry(
                        level="WARNING",
                        message=f"Vendor not configured: {vendor}",
                    )
                )
                metadata["warnings"].append(f"VENDOR_NOT_CONFIGURED: {vendor}")
                return None, ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED, metadata

            if vendor_config.blocked:
                handle_log.entries.append(
                    LogEntry(level="INFO", message=f"Vendor blocked (bot protection): {vendor}")
                )
                return None, ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED, metadata

            # Check cache
            if not self.force and self._is_cached(artifacts_dir):
                handle_log.entries.append(LogEntry(level="INFO", message="Using cached artifacts"))
                return self._load_cached_output(artifacts_dir, input_row, metadata)

            # Step 0: Check for catalog images (skip scraping if we have them all)
            catalog_images = input_row.catalog_images_by_color
            known_colors = input_row.known_colors
            if catalog_images and known_colors:
                handle_log.entries.append(
                    LogEntry(
                        message=f"Catalog images available for {len(catalog_images)}/{len(known_colors)} colors",
                        data={"colors_with_images": list(catalog_images.keys())},
                    )
                )

            # Early exit: catalog images cover all variants, no description needed
            from .colors import colors_match

            all_colors_covered = (
                known_colors
                and catalog_images
                and all(
                    any(colors_match(kc, cc) for cc in catalog_images)
                    for kc in known_colors
                )
            )

            if all_colors_covered and not input_row.needs_description:
                handle_log.entries.append(
                    LogEntry(
                        message=f"Catalog images cover all {len(known_colors)} colors — skipping scraping",
                    )
                )
                # Build output directly from catalog data
                from .colors import find_matching_color
                variant_map = {}
                for color in known_colors:
                    match = find_matching_color(color, catalog_images)
                    if match:
                        variant_map[color] = catalog_images[match]

                images = []
                if input_row.needs_images:
                    from .models import OutputImage
                    for i, (color, url) in enumerate(variant_map.items(), 1):
                        images.append(OutputImage(src=url, position=i, alt=f"{input_row.title} - {color}"))

                merch_output = MerchOutput(
                    handle=handle,
                    body_html=None,
                    images=images,
                    variant_image_map=variant_map,
                    confidence=90,  # High confidence — using vendor's own catalog images
                )
                merch_output.warnings.append("CATALOG_IMAGES_USED_DIRECTLY")
                await self.generator.save_output(merch_output, artifacts_dir)
                return merch_output, ProcessingStatus.UPDATED, metadata

            # Step 0: Try Shopify JSON API (fastest path, no browser needed)
            if vendor_config.is_shopify:
                from .shopify_scraper import scrape_shopify_product

                handle_log.entries.append(
                    LogEntry(message=f"Trying Shopify JSON API on {vendor_config.domain}")
                )

                shopify_facts = await scrape_shopify_product(
                    domain=vendor_config.domain,
                    handle=handle,
                    http_client=self.http_client,
                    title=input_row.title,
                )

                if shopify_facts and shopify_facts.product_name:
                    handle_log.entries.append(
                        LogEntry(
                            message=f"Shopify JSON succeeded: {shopify_facts.product_name}",
                            data={"images": len(shopify_facts.images)},
                        )
                    )
                    facts = shopify_facts
                    scrape_url = f"https://{vendor_config.domain}/products/{handle}"
                    metadata["confidence"] = 100  # Direct API match
                    metadata["source"] = "shopify_json"

                    # Save extraction outputs
                    facts_path = artifacts_dir / "extracted_facts.json"
                    facts_path.parent.mkdir(parents=True, exist_ok=True)
                    facts_path.write_text(facts.model_dump_json(indent=2))

                    # Skip directly to Step 3b (quality check)
                    # We need to jump past the resolve + scrape + extract steps
                    # Use a flag to skip those steps
                    shopify_succeeded = True
                else:
                    handle_log.entries.append(
                        LogEntry(
                            level="WARNING",
                            message="Shopify JSON failed, falling through to resolver",
                        )
                    )
                    shopify_succeeded = False
            else:
                shopify_succeeded = False

            firecrawl_variant_images = None  # Set by Firecrawl scrape if swatch params present

            if not shopify_succeeded:
                # Step 1: Resolve URL (use all available barcodes/SKUs)
                search_barcode = input_row.barcode
                search_sku = input_row.sku
                all_barcodes = input_row.all_barcodes
                all_skus = input_row.all_skus
                if all_barcodes:
                    search_barcode = all_barcodes[0]
                if all_skus:
                    search_sku = all_skus[0]

                handle_log.entries.append(
                    LogEntry(
                        message="Resolving product URL",
                        data={
                            "barcodes": len(all_barcodes),
                            "skus": len(all_skus),
                            "known_colors": known_colors[:5] if known_colors else [],
                        },
                    )
                )
                # Get catalog price from variant data for resolver scoring
                _catalog_price = None
                if input_row.variant_data:
                    _prices = [v.price for v in input_row.variant_data if v.price > 0]
                    if _prices:
                        _catalog_price = _prices[0]

                resolver_output = await self.resolver.resolve(
                    handle=handle,
                    vendor=vendor,
                    vendor_config=vendor_config,
                    hints=input_row.gaps or input_row.suggestions or "",
                    title=input_row.title,
                    barcode=search_barcode,
                    sku=search_sku,
                    catalog_price=_catalog_price,
                )

                # Save resolver output
                await self.resolver.save_output(resolver_output, artifacts_dir)

                metadata["confidence"] = resolver_output.selected_confidence
                metadata["warnings"].extend(resolver_output.warnings)

                # Step 2: Candidate retry loop — try top candidates with validation
                from .firecrawl_scraper import is_bot_blocked, _firecrawl_json_to_facts

                catalog_title = input_row.title or handle
                confidence_settings = self.vendors_config.settings.confidence
                candidates = sorted(
                    resolver_output.candidates,
                    key=lambda c: c.confidence,
                    reverse=True,
                )

                match_decisions: list[dict] = []
                accepted_facts = None
                accepted_url = None
                accepted_markdown = None
                accepted_variant_images = None

                for candidate in candidates[:3]:
                    if candidate.confidence < 50:
                        match_decisions.append({
                            "url": candidate.url,
                            "resolver_confidence": candidate.confidence,
                            "outcome": "skip_low_confidence",
                            "reason": f"confidence {candidate.confidence} < threshold 50",
                        })
                        continue

                    # Scrape the candidate
                    handle_log.entries.append(
                        LogEntry(
                            message=f"Scraping candidate: {candidate.url} (confidence={candidate.confidence})",
                        )
                    )

                    cand_markdown, cand_variant_images = await self.firecrawl.scrape_markdown(
                        candidate.url,
                        swatch_selector=vendor_config.swatch_selector,
                        gallery_selector=vendor_config.gallery_selector,
                        wait_after_click=1500 if (vendor_config.swatch_selector or vendor_config.gallery_selector) else None,
                        wait_for=vendor_config.wait_for,
                    )

                    # Bot block check
                    if not cand_markdown or is_bot_blocked(cand_markdown):
                        block_reason = "bot_blocked" if cand_markdown else "no_content"
                        handle_log.entries.append(
                            LogEntry(
                                level="WARNING",
                                message=f"Candidate {block_reason}: {candidate.url}",
                            )
                        )
                        match_decisions.append({
                            "url": candidate.url,
                            "resolver_confidence": candidate.confidence,
                            "outcome": "reject_bot_blocked",
                            "reason": block_reason,
                        })
                        continue

                    # Title gate check
                    page_title = extract_page_title(cand_markdown, catalog_title=catalog_title)
                    if page_title:
                        gate = check_title_gate(page_title, catalog_title)
                        if not gate["pass"]:
                            handle_log.entries.append(
                                LogEntry(
                                    level="WARNING",
                                    message=f"Title gate failed: {gate['reason']} (page='{page_title}')",
                                )
                            )
                            match_decisions.append({
                                "url": candidate.url,
                                "resolver_confidence": candidate.confidence,
                                "outcome": "reject_title_gate",
                                "reason": gate["reason"],
                                "title_extracted": page_title,
                                "title_similarity": gate["title_similarity"],
                            })
                            continue

                    # Extract facts
                    handle_log.entries.append(LogEntry(message="Extracting facts from markdown"))
                    facts_dict = await self.llm_client.extract_facts_from_markdown(
                        cand_markdown, candidate.url
                    )

                    if not facts_dict or not facts_dict.get("product_name"):
                        handle_log.entries.append(
                            LogEntry(
                                level="WARNING",
                                message=f"Extraction failed for candidate: {candidate.url}",
                            )
                        )
                        match_decisions.append({
                            "url": candidate.url,
                            "resolver_confidence": candidate.confidence,
                            "outcome": "reject_extraction_failed",
                            "reason": "no product_name in extraction",
                        })
                        continue

                    cand_facts = _firecrawl_json_to_facts(facts_dict, candidate.url)

                    # Post-extraction validation
                    swatch_colors = list(cand_variant_images.keys()) if cand_variant_images else None
                    post_check = check_post_extraction(
                        cand_facts, catalog_title, _catalog_price, known_colors or [],
                        vendor_colors=swatch_colors,
                    )
                    if not post_check["pass"]:
                        handle_log.entries.append(
                            LogEntry(
                                level="WARNING",
                                message=f"Post-extraction check failed: {post_check['reason']}",
                                data=post_check["signals"],
                            )
                        )
                        match_decisions.append({
                            "url": candidate.url,
                            "resolver_confidence": candidate.confidence,
                            "outcome": "reject_post_extraction",
                            "reason": post_check["reason"],
                            "confidence": post_check["confidence"],
                            "signals": post_check["signals"],
                        })
                        continue

                    # Accepted!
                    handle_log.entries.append(
                        LogEntry(
                            message=f"Candidate accepted: {candidate.url} (post-scrape confidence={post_check['confidence']:.0f})",
                        )
                    )
                    match_decisions.append({
                        "url": candidate.url,
                        "resolver_confidence": candidate.confidence,
                        "outcome": "accept",
                        "reason": "ok",
                        "confidence": post_check["confidence"],
                        "signals": post_check["signals"],
                    })
                    accepted_facts = cand_facts
                    accepted_url = candidate.url
                    accepted_markdown = cand_markdown
                    accepted_variant_images = cand_variant_images
                    break

                # Log all decisions
                if self.decision_logger:
                    outcome = "accept" if accepted_facts else "no_match"
                    self.decision_logger.log(
                        handle=handle,
                        vendor=vendor,
                        catalog_title=catalog_title,
                        candidates_tried=match_decisions,
                        outcome=outcome,
                        final_url=accepted_url,
                        catalog_price=_catalog_price,
                        catalog_colors=known_colors,
                        resolver_candidates=[
                            {"url": c.url, "confidence": c.confidence,
                             "title": c.title, "snippet": c.snippet,
                             "reasoning": c.reasoning}
                            for c in candidates
                        ],
                    )

                if not accepted_facts:
                    handle_log.entries.append(
                        LogEntry(
                            level="WARNING",
                            message=f"No candidate accepted ({len(match_decisions)} tried)",
                        )
                    )
                    return None, ProcessingStatus.NO_MATCH, metadata

                # Use accepted candidate values
                facts = accepted_facts
                scrape_url = accepted_url
                markdown = accepted_markdown
                firecrawl_variant_images = accepted_variant_images

                # Save raw markdown and extracted facts for accepted candidate
                md_path = artifacts_dir / "source.md"
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(markdown)

                facts_path = artifacts_dir / "extracted_facts.json"
                facts_path.write_text(facts.model_dump_json(indent=2))

            # Step 3b: Content quality check
            quality = _assess_extraction_quality(facts)
            if not quality["usable"]:
                    metadata["warnings"].append(
                        f"LOW_EXTRACTION_QUALITY: {quality['reason']}"
                    )

            # Step 3b1: Season signal detection
            from .season_signals import check_season_match as _check_season

            # Gather vendor colors from extracted facts
            _vendor_colors: list[str] = []
            for v in facts.variants:
                if v.option_name.lower() in ("color", "colour", "style"):
                    _vendor_colors = v.values
                    break

            _season_input = {
                "colors": input_row.known_colors,
                "tags": [],  # tags populated from store if available
                "title": input_row.title or "",
            }
            if self.store:
                _prod = self.store.get_product(handle)
                if _prod:
                    _season_input["tags"] = _prod.get("tags", "").split(", ") if isinstance(_prod.get("tags"), str) else _prod.get("tags", [])

            _vendor_input = {
                "colors": _vendor_colors,
                "product_name": facts.product_name,
            }
            season_signals = _check_season(_season_input, _vendor_input, scrape_url)

            if season_signals["flags"]:
                for flag in season_signals["flags"]:
                    metadata["warnings"].append(
                        f"SEASON_{flag}: {season_signals['confidence_note']}"
                    )
                handle_log.entries.append(
                    LogEntry(
                        level="WARNING",
                        message=f"Season signals: {season_signals['flags']}",
                        data=season_signals,
                    )
                )
            else:
                handle_log.entries.append(
                    LogEntry(
                        message="Season check OK — no mismatch signals",
                        data={"color_overlap": season_signals["color_overlap"]},
                    )
                )

            # Step 3b2: Inject variant images from Firecrawl swatch extraction
            if firecrawl_variant_images and not facts.variant_image_candidates:
                facts.variant_image_candidates = {
                    color: urls if isinstance(urls, list) else [urls]
                    for color, urls in firecrawl_variant_images.items()
                }
                handle_log.entries.append(
                    LogEntry(
                        message=f"Firecrawl swatch extraction found images for {len(firecrawl_variant_images)} colors",
                        data={"colors": list(firecrawl_variant_images.keys())},
                    )
                )

            # Step 3c: Inject catalog images from TVR variant data
            from .colors import deduplicate_color_images, find_matching_color

            catalog_imgs = input_row.catalog_images_by_color
            if catalog_imgs:
                for color, img_url in catalog_imgs.items():
                    # Check if this color already exists under a different name
                    existing_key = find_matching_color(color, facts.variant_image_candidates)
                    if existing_key:
                        if img_url not in facts.variant_image_candidates[existing_key]:
                            facts.variant_image_candidates[existing_key].append(img_url)
                    else:
                        facts.variant_image_candidates[color] = [img_url]
                handle_log.entries.append(
                    LogEntry(
                        message=f"Injected {len(catalog_imgs)} catalog images from TVR",
                        data={"colors": list(catalog_imgs.keys())},
                    )
                )

            # Deduplicate color entries (e.g., "Black / Gray" and "Black | Gray")
            if facts.variant_image_candidates:
                before_count = len(facts.variant_image_candidates)
                facts.variant_image_candidates = deduplicate_color_images(
                    facts.variant_image_candidates
                )
                after_count = len(facts.variant_image_candidates)
                if before_count != after_count:
                    handle_log.entries.append(
                        LogEntry(
                            message=f"Deduplicated colors: {before_count} → {after_count}",
                        )
                    )

            # Also inject known colors from TVR if extractor didn't find them
            if input_row.known_colors and not any(
                v.option_name.lower() in ("color", "colour") for v in facts.variants
            ):
                from .models import VariantOption
                facts.variants.append(
                    VariantOption(option_name="Color", values=input_row.known_colors)
                )
                handle_log.entries.append(
                    LogEntry(
                        message=f"Injected {len(input_row.known_colors)} known colors from TVR",
                    )
                )

            # Step 3d: Standalone swatch scrape fallback
            # Only if Firecrawl's integrated extraction didn't find anything
            # and vendor has explicit selectors configured
            if (
                not vendor_config.is_shopify
                and not facts.variant_image_candidates
                and (vendor_config.swatch_selector or vendor_config.gallery_selector)
            ):
                handle_log.entries.append(
                    LogEntry(message="Attempting swatch scrape for variant images")
                )
                swatch_images = await self.firecrawl.scrape_variant_images(
                    url=scrape_url,
                    swatch_selector=vendor_config.swatch_selector,
                    gallery_selector=vendor_config.gallery_selector,
                )
                if swatch_images:
                    facts.variant_image_candidates = swatch_images
                    handle_log.entries.append(
                        LogEntry(
                            message=f"Swatch scrape found images for {len(swatch_images)} colors",
                            data={"colors": list(swatch_images.keys())},
                        )
                    )

            # Step 3e: Color-specific image search fallback (if swatch scrape didn't find anything)
            if facts.variants and not facts.variant_image_candidates:
                color_variant = next(
                    (v for v in facts.variants if v.option_name.lower() in ("color", "colour")),
                    None,
                )
                if color_variant and color_variant.values:
                    handle_log.entries.append(
                        LogEntry(message=f"Searching for {len(color_variant.values)} color-specific images")
                    )
                    color_imgs = await self.resolver.search_color_images(
                        vendor_config=vendor_config,
                        product_name=facts.product_name,
                        colors=color_variant.values,
                    )
                    if color_imgs:
                        facts.variant_image_candidates = color_imgs
                        handle_log.entries.append(
                            LogEntry(
                                message=f"Found color images for {len(color_imgs)} colors",
                                data={"colors": list(color_imgs.keys())},
                            )
                        )

            # Step 4: Generate merchandising output
            handle_log.entries.append(LogEntry(message="Generating merchandising output"))

            merch_output = await self.generator.generate_output(input_row, facts)
            metadata["warnings"].extend(merch_output.warnings)

            # Step 4a: GMC compliance check
            gmc_flags = []
            if merch_output.body_html:
                gmc_flags.extend(check_prohibited_terms(merch_output.body_html))
            if input_row.title:
                gmc_flags.extend(validate_title(input_row.title))
            if gmc_flags:
                merch_output.gmc_flags = gmc_flags
                handle_log.entries.append(
                    LogEntry(
                        level="WARNING",
                        message=f"GMC flags: {', '.join(gmc_flags[:3])}",
                        data={"gmc_flags": gmc_flags},
                    )
                )

            # Step 4b: Validate image URLs (HEAD request)
            if merch_output.images:
                from .generator import validate_image_urls
                from .models import OutputImage

                img_dicts = [{"src": img.src, "alt": img.alt, "position": img.position}
                             for img in merch_output.images]
                validated = await validate_image_urls(img_dicts, self.http_client)

                valid_images = []
                for img_data in validated:
                    if img_data.get("valid", True):
                        valid_images.append(
                            OutputImage(src=img_data["src"], position=img_data["position"], alt=img_data["alt"])
                        )
                    else:
                        reason = img_data.get("validation_error", "unknown")
                        handle_log.entries.append(
                            LogEntry(
                                level="WARNING",
                                message=f"Image failed validation: {reason}",
                                data={"url": img_data["src"][:80]},
                            )
                        )
                        metadata["warnings"].append(f"IMAGE_INVALID: {reason} — {img_data['src'][:60]}")

                # Re-number positions
                for i, img in enumerate(valid_images, 1):
                    img.position = i
                merch_output.images = valid_images

            # Step 4c: Catalog cross-reference (if store available)
            if self.store and input_row.barcode:
                xref = _cross_reference_catalog(
                    self.store, input_row, facts
                )
                if xref["warnings"]:
                    metadata["warnings"].extend(xref["warnings"])
                    for w in xref["warnings"]:
                        handle_log.entries.append(
                            LogEntry(level="WARNING", message=f"Catalog xref: {w}")
                        )
                if xref.get("catalog_description"):
                    handle_log.entries.append(
                        LogEntry(
                            message="Catalog description available for comparison",
                            data={"catalog_desc_length": len(xref["catalog_description"])},
                        )
                    )

            # Step 4d: Fact-check generated description (opt-in via --verify)
            if self.verify and self.llm_client and merch_output.body_html:
                try:
                    facts_dict = {
                        "product_name": facts.product_name,
                        "description_blocks": facts.description_blocks[:5],
                        "feature_bullets": facts.feature_bullets[:10],
                        "specs": dict(list(facts.specs.items())[:10]),
                        "materials": facts.materials,
                    }
                    verification = await self.llm_client.verify_description(
                        facts=facts_dict,
                        description=merch_output.body_html,
                    )
                    verdict = verification.get("verdict", "UNKNOWN")
                    unsupported = verification.get("unsupported", [])
                    embellished = verification.get("embellished", [])

                    if verdict == "FAIL":
                        metadata["warnings"].append(
                            f"FACT_CHECK_FAILED: {len(unsupported)} unsupported, "
                            f"{len(embellished)} embellished claims"
                        )
                        handle_log.entries.append(
                            LogEntry(
                                level="WARNING",
                                message=f"Fact-check FAILED: {unsupported[:2]}",
                                data=verification,
                            )
                        )
                    else:
                        handle_log.entries.append(
                            LogEntry(
                                message=f"Fact-check PASSED: {len(verification.get('supported', []))} claims verified",
                            )
                        )

                    # Save verification result
                    import json as _json
                    verify_path = artifacts_dir / "fact_check.json"
                    with open(verify_path, "w") as f:
                        _json.dump(verification, f, indent=2)

                except Exception as e:
                    logger.warning(f"Fact-check failed: {e}")

            # Save merchandising output
            await self.generator.save_output(merch_output, artifacts_dir)

            handle_log.entries.append(
                LogEntry(
                    message="Processing complete",
                    data={
                        "has_body": bool(merch_output.body_html),
                        "image_count": len(merch_output.images),
                        "variant_mappings": len(merch_output.variant_image_map),
                    },
                )
            )

            handle_log.status = ProcessingStatus.UPDATED
            return merch_output, ProcessingStatus.UPDATED, metadata

        except Exception as e:
            logger.exception(f"Error processing {handle}")
            handle_log.entries.append(LogEntry(level="ERROR", message=str(e)))
            metadata["error"] = str(e)
            return None, ProcessingStatus.FAILED, metadata

        finally:
            # Calculate processing time
            elapsed_ms = int((time.time() - start_time) * 1000)
            metadata["processing_time_ms"] = elapsed_ms

            # Save log
            handle_log.completed_at = datetime.now(UTC)
            self._save_log(handle_log, artifacts_dir)

    def _is_cached(self, artifacts_dir: Path) -> bool:
        """Check if valid cached artifacts exist."""
        required_files = ["resolver.json", "merch_output.json"]
        for filename in required_files:
            if not (artifacts_dir / filename).exists():
                return False
        return True

    def _load_cached_output(
        self,
        artifacts_dir: Path,
        input_row: InputRow,
        metadata: dict[str, Any],
    ) -> tuple[MerchOutput | None, ProcessingStatus, dict[str, Any]]:
        """Load cached merchandising output."""
        try:
            with open(artifacts_dir / "merch_output.json") as f:
                data = json.load(f)
            merch_output = MerchOutput.model_validate(data)

            # Load resolver for confidence
            with open(artifacts_dir / "resolver.json") as f:
                resolver_data = json.load(f)
            metadata["confidence"] = resolver_data.get("selected_confidence", 0)
            metadata["warnings"] = merch_output.warnings

            return merch_output, ProcessingStatus.UPDATED, metadata

        except Exception as e:
            logger.warning(f"Failed to load cached output: {e}")
            return None, ProcessingStatus.FAILED, metadata

    def _save_log(self, handle_log: HandleLog, artifacts_dir: Path) -> None:
        """Save the processing log."""
        log_path = artifacts_dir / "log.json"
        with open(log_path, "w") as f:
            json.dump(handle_log.model_dump(mode="json"), f, indent=2, default=str)


class Pipeline:
    """
    Main pipeline orchestrator.

    Manages:
    - Concurrent processing with rate limiting
    - Per-domain concurrency limits
    - Output aggregation
    - Event callbacks for progress tracking
    - Cancellation support
    """

    def __init__(
        self,
        config: PipelineConfig,
        event_cb: EventCallback | None = None,
        cancel_flag_path: Path | None = None,
    ) -> None:
        self.config = config
        self.event_cb = event_cb
        self.cancel_flag_path = cancel_flag_path
        self.vendors_config: VendorsConfig | None = None
        self.llm_client: LLMClient | None = None
        self._domain_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(2)
        )
        self._cancelled = False

    def _emit_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Emit an event via the callback if configured."""
        if self.event_cb:
            event = {
                "type": event_type,
                "timestamp": datetime.now(UTC).isoformat(),
                **(data or {}),
            }
            try:
                self.event_cb(event)
            except Exception as e:
                logger.warning(f"Event callback failed: {e}")

    def _check_cancelled(self) -> bool:
        """Check if the pipeline has been cancelled."""
        if self._cancelled:
            return True
        if self.cancel_flag_path and self.cancel_flag_path.exists():
            self._cancelled = True
            return True
        return False

    async def run(self) -> dict[str, Path]:
        """
        Run the pipeline.

        Returns:
            Dictionary mapping output type to file path.
        """
        # Load configuration
        self.vendors_config = load_vendors_config(self.config.vendors_path)

        # Initialize LLM client (optional - may fail if no API key)
        try:
            self.llm_client = get_llm_client(provider_name=self.config.llm_provider)
            logger.info("LLM client initialized (provider: %s)", self.config.llm_provider or "auto")
        except ValueError as e:
            logger.warning(f"LLM client not available: {e}")
            self.llm_client = None

        # Setup directories — output_dir is used directly as the artifacts root
        # (each handle gets its own subdirectory under output_dir)
        artifacts_dir = ensure_dir(self.config.output_dir)

        # Initialize output builder
        output_builder = ShopifyOutputBuilder(self.config.shopify_export_path)

        # Load input rows: pre-built (with variant data) or from CSV
        if self.config.input_rows:
            input_rows = self.config.input_rows
            if self.config.max_rows:
                input_rows = input_rows[: self.config.max_rows]
            logger.info(f"Using {len(input_rows)} pre-built input rows (with variant data)")
        elif self.config.input_path:
            input_rows = list(
                parse_input_csv(
                    self.config.input_path,
                    max_rows=self.config.max_rows,
                )
            )
        else:
            logger.error("No input rows or input path provided")
            return {}
        total_rows = len(input_rows)

        # Emit RUN_STARTED event
        self._emit_event("RUN_STARTED", {"total": total_rows})

        # Process products
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
        ) as http_client:
            # Try to initialize store for catalog cross-referencing (optional)
            store = None
            try:
                from lookout.store import LookoutStore
                store = LookoutStore()
                logger.info("Store connected — catalog cross-referencing enabled")
            except Exception:
                logger.debug("Store not available — catalog cross-referencing disabled")

            decision_logger = MatchDecisionLogger(self.config.output_dir / "match_decisions.jsonl")

            processor = ProductProcessor(
                vendors_config=self.vendors_config,
                http_client=http_client,
                llm_client=self.llm_client,
                artifacts_base=artifacts_dir,
                force=self.config.force,
                store=store,
                verify=self.config.verify,
                only_mode=self.config.only_mode,
                decision_logger=decision_logger,
            )

            # Create semaphore for global concurrency
            semaphore = asyncio.Semaphore(self.config.concurrency)

            # Process all rows concurrently
            tasks = []
            for input_row in input_rows:
                # Check for cancellation before scheduling
                if self._check_cancelled():
                    logger.info("Pipeline cancelled, stopping new tasks")
                    break

                task = self._process_with_semaphore(
                    semaphore,
                    processor,
                    input_row,
                    output_builder,
                )
                tasks.append(task)

            await asyncio.gather(*tasks)

        # Write outputs (always, even if cancelled - for partial results)
        outputs = output_builder.write_outputs(
            self.config.output_dir,
            dry_run=self.config.dry_run,
        )

        # Log summary
        summary = output_builder.get_summary()
        logger.info(
            f"Pipeline complete: {summary['updated']} updated, "
            f"{summary['skipped']} skipped, {summary['no_match']} no match, "
            f"{summary['failed']} failed"
        )

        # Emit RUN_DONE event
        self._emit_event(
            "RUN_DONE",
            {
                "total": summary["total"],
                "updated": summary["updated"],
                "skipped": summary["skipped"],
                "no_match": summary["no_match"],
                "failed": summary["failed"],
                "cancelled": self._cancelled,
            },
        )

        return outputs

    async def _process_with_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        processor: ProductProcessor,
        input_row: InputRow,
        output_builder: ShopifyOutputBuilder,
    ) -> None:
        """Process a row with semaphore for concurrency control."""
        # Check for cancellation
        if self._check_cancelled():
            return

        async with semaphore:
            # Check again after acquiring semaphore
            if self._check_cancelled():
                return

            handle = input_row.product_handle
            vendor = input_row.vendor

            # Emit ITEM_STARTED event
            self._emit_event("ITEM_STARTED", {"handle": handle, "vendor": vendor})

            # Also apply per-domain rate limiting
            vendor_config = self.vendors_config.vendors.get(input_row.vendor)
            if vendor_config:
                domain_sem = self._domain_semaphores[vendor_config.domain]
                async with domain_sem:
                    result = await processor.process(input_row)
            else:
                result = await processor.process(input_row)

            merch_output, status, metadata = result

            output_rows = output_builder.add_result(
                input_row=input_row,
                merch_output=merch_output,
                status=status,
                match_confidence=metadata.get("confidence", 0),
                warnings=metadata.get("warnings", []),
                error_message=metadata.get("error", ""),
                processing_time_ms=metadata.get("processing_time_ms", 0),
            )

            # Log progress
            logger.info(
                f"Processed {input_row.product_handle}: {status.value} "
                f"(confidence: {metadata.get('confidence', 0)})"
            )

            # Emit appropriate event based on status
            warnings = metadata.get("warnings", [])
            if status == ProcessingStatus.FAILED:
                self._emit_event(
                    "ITEM_FAILED",
                    {
                        "handle": handle,
                        "error": metadata.get("error", "Unknown error"),
                    },
                )
            else:
                self._emit_event(
                    "ITEM_DONE",
                    {
                        "handle": handle,
                        "status": status.value,
                        "match_confidence": metadata.get("confidence", 0),
                        "warnings_count": len(warnings),
                        "output_rows_count": output_rows,
                    },
                )


async def run_pipeline(
    config: PipelineConfig,
    event_cb: EventCallback | None = None,
    cancel_flag_path: Path | None = None,
) -> dict[str, Path]:
    """
    Run the merchandising pipeline.

    Args:
        config: Pipeline configuration.
        event_cb: Optional callback function for progress events.
                  Called with dict containing event type and data.
        cancel_flag_path: Optional path to a cancel flag file.
                          If file exists, pipeline will stop gracefully.

    Returns:
        Dictionary mapping output type to file path.
    """
    pipeline = Pipeline(config, event_cb=event_cb, cancel_flag_path=cancel_flag_path)
    return await pipeline.run()
