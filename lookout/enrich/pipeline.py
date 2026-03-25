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
from .generator import Generator
from .io import parse_input_csv
from .llm import LLMClient, get_llm_client
from .models import (
    HandleLog,
    InputRow,
    LogEntry,
    MerchOutput,
    ProcessingStatus,
    VendorsConfig,
)
from .resolver import URLResolver
from .scraper import WebScraper
from .shopify_output import ShopifyOutputBuilder
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


class PipelineConfig:
    """Configuration for the pipeline."""

    def __init__(
        self,
        input_path: Path,
        output_dir: Path,
        vendors_path: Path,
        shopify_export_path: Path | None = None,
        concurrency: int = 5,
        max_rows: int | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.input_path = input_path
        self.output_dir = output_dir
        self.vendors_path = vendors_path
        self.shopify_export_path = shopify_export_path
        self.concurrency = concurrency
        self.max_rows = max_rows
        self.force = force
        self.dry_run = dry_run


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
    ) -> None:
        self.vendors_config = vendors_config
        self.http_client = http_client
        self.llm_client = llm_client
        self.artifacts_base = artifacts_base
        self.force = force

        self.resolver = URLResolver(http_client=http_client)
        self.scraper = WebScraper(http_client=http_client)
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
            # Check if product has any gaps
            if not input_row.has_any_gap:
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

            # Check cache
            if not self.force and self._is_cached(artifacts_dir):
                handle_log.entries.append(LogEntry(level="INFO", message="Using cached artifacts"))
                return self._load_cached_output(artifacts_dir, input_row, metadata)

            # Step 1: Resolve URL
            handle_log.entries.append(LogEntry(message="Resolving product URL"))
            resolver_output = await self.resolver.resolve(
                handle=handle,
                vendor=vendor,
                vendor_config=vendor_config,
                hints=input_row.gaps or input_row.suggestions or "",
                title=input_row.title,
                barcode=input_row.barcode,
            )

            # Save resolver output
            await self.resolver.save_output(resolver_output, artifacts_dir)

            metadata["confidence"] = resolver_output.selected_confidence
            metadata["warnings"].extend(resolver_output.warnings)

            # Check confidence threshold
            confidence_settings = self.vendors_config.settings.confidence
            if resolver_output.selected_confidence < confidence_settings.reject_threshold:
                handle_log.entries.append(
                    LogEntry(
                        level="WARNING",
                        message=f"URL confidence too low: {resolver_output.selected_confidence}",
                    )
                )
                return None, ProcessingStatus.NO_MATCH, metadata

            if not resolver_output.selected_url:
                handle_log.entries.append(LogEntry(level="WARNING", message="No URL found"))
                return None, ProcessingStatus.NO_MATCH, metadata

            # Step 2: Scrape page
            handle_log.entries.append(
                LogEntry(
                    message=f"Scraping {resolver_output.selected_url}",
                    data={"use_playwright": vendor_config.use_playwright},
                )
            )

            scraped_page = await self.scraper.scrape(
                resolver_output.selected_url,
                vendor_config,
            )

            if not scraped_page.success:
                handle_log.entries.append(
                    LogEntry(
                        level="ERROR",
                        message=f"Scrape failed: {scraped_page.error}",
                    )
                )
                metadata["error"] = scraped_page.error or "Scrape failed"
                return None, ProcessingStatus.FAILED, metadata

            # Save HTML
            await self.scraper.save_html(scraped_page, artifacts_dir)

            # Step 3: Extract content
            handle_log.entries.append(LogEntry(message="Extracting content"))

            extractor = ContentExtractor(vendor_config.selectors)
            source_text, facts = extract_content(
                scraped_page.html,
                scraped_page.final_url,
                vendor_config.selectors,
            )

            # Save extraction outputs
            await extractor.save_outputs(source_text, facts, artifacts_dir)

            # Step 3b: Content quality check
            quality = _assess_extraction_quality(facts)
            if not quality["usable"]:
                handle_log.entries.append(
                    LogEntry(
                        level="WARNING",
                        message=f"Low quality extraction: {quality['reason']}",
                        data=quality,
                    )
                )
                # If we used static scraping, retry with Playwright
                if not vendor_config.use_playwright:
                    handle_log.entries.append(
                        LogEntry(message="Retrying with Playwright")
                    )
                    playwright_page = await self.scraper._scrape_dynamic(
                        resolver_output.selected_url,
                        vendor_config.playwright_config,
                    )
                    if playwright_page.success:
                        source_text, facts = extract_content(
                            playwright_page.html,
                            playwright_page.final_url,
                            vendor_config.selectors,
                        )
                        await extractor.save_outputs(source_text, facts, artifacts_dir)
                        quality = _assess_extraction_quality(facts)
                        handle_log.entries.append(
                            LogEntry(
                                message=f"Playwright retry quality: {quality['reason']}",
                                data=quality,
                            )
                        )

                if not quality["usable"]:
                    metadata["warnings"].append(
                        f"LOW_EXTRACTION_QUALITY: {quality['reason']}"
                    )

            # Step 4: Generate merchandising output
            handle_log.entries.append(LogEntry(message="Generating merchandising output"))

            merch_output = await self.generator.generate_output(input_row, facts)
            metadata["warnings"].extend(merch_output.warnings)

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
            self.llm_client = get_llm_client()
            logger.info("LLM client initialized")
        except ValueError as e:
            logger.warning(f"LLM client not available: {e}")
            self.llm_client = None

        # Setup directories
        artifacts_dir = ensure_dir(self.config.output_dir / "artifacts")

        # Initialize output builder
        output_builder = ShopifyOutputBuilder(self.config.shopify_export_path)

        # Count total rows for progress tracking
        input_rows = list(
            parse_input_csv(
                self.config.input_path,
                max_rows=self.config.max_rows,
            )
        )
        total_rows = len(input_rows)

        # Emit RUN_STARTED event
        self._emit_event("RUN_STARTED", {"total": total_rows})

        # Process products
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
        ) as http_client:
            processor = ProductProcessor(
                vendors_config=self.vendors_config,
                http_client=http_client,
                llm_client=self.llm_client,
                artifacts_base=artifacts_dir,
                force=self.config.force,
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
