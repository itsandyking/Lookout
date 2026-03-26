"""
Shopify CSV output generation.

This module handles:
1. Converting MerchOutput to Shopify CSV rows
2. Generating the shopify_update.csv
3. Generating variant_image_assignments.csv
4. Supporting optional Shopify export for variant rows
"""

import logging
from pathlib import Path

from ..output.enrich_export import (
    merch_output_to_shopify_rows,
    write_run_report,
    write_shopify_csv,
    write_variant_image_assignments,
)
from .io import ShopifyExportRow, parse_shopify_export
from .models import (
    InputRow,
    MerchOutput,
    ProcessingStatus,
    RunReportRow,
    VariantImageAssignment,
)

logger = logging.getLogger(__name__)


class ShopifyOutputBuilder:
    """
    Builds Shopify-compatible CSV output from merchandising results.

    Handles:
    - Product rows (handle, body HTML)
    - Image rows (same handle, image fields only)
    - Variant image assignments (separate CSV)
    - Full variant rows (when Shopify export provided)
    """

    def __init__(
        self,
        shopify_export_path: Path | None = None,
    ) -> None:
        """
        Initialize the output builder.

        Args:
            shopify_export_path: Optional path to Shopify product export CSV.
                               If provided, enables full variant row output.
        """
        self.shopify_export: dict[str, list[ShopifyExportRow]] = {}

        if shopify_export_path and shopify_export_path.exists():
            logger.info(f"Loading Shopify export from {shopify_export_path}")
            self.shopify_export = parse_shopify_export(shopify_export_path)
            logger.info(f"Loaded {len(self.shopify_export)} products from export")

        self._rows: list[dict[str, str]] = []
        self._variant_assignments: list[VariantImageAssignment] = []
        self._report_rows: list[RunReportRow] = []

    def add_result(
        self,
        input_row: InputRow,
        merch_output: MerchOutput | None,
        status: ProcessingStatus,
        match_confidence: int = 0,
        warnings: list[str] | None = None,
        error_message: str = "",
        processing_time_ms: int = 0,
    ) -> int:
        """
        Add a processing result to the output.

        Args:
            input_row: The original input row.
            merch_output: The generated merchandising output (if successful).
            status: Processing status.
            match_confidence: URL match confidence score.
            warnings: List of warnings.
            error_message: Error message if failed.
            processing_time_ms: Processing time in milliseconds.

        Returns:
            Number of CSV rows generated for this product.
        """
        warnings = warnings or []
        row_count = 0

        # Generate Shopify rows if we have output
        if merch_output and status == ProcessingStatus.UPDATED:
            # Determine what to include based on input row gaps
            include_body = input_row.needs_description and merch_output.body_html
            include_images = input_row.needs_images and merch_output.images

            if include_body or include_images:
                # Generate basic product/image rows
                rows = merch_output_to_shopify_rows(
                    merch_output,
                    include_body=include_body,
                    include_images=include_images,
                )
                self._rows.extend(rows)
                row_count = len(rows)

            # Handle variant images
            if input_row.needs_variant_images:
                self._add_variant_assignments(
                    input_row,
                    merch_output,
                )

        # Add report row
        self._report_rows.append(
            RunReportRow(
                handle=input_row.product_handle,
                vendor=input_row.vendor,
                status=status,
                match_confidence=match_confidence,
                warnings="; ".join(warnings),
                output_rows_count=row_count,
                error_message=error_message,
                processing_time_ms=processing_time_ms,
            )
        )

        return row_count

    def _add_variant_assignments(
        self,
        input_row: InputRow,
        merch_output: MerchOutput,
    ) -> None:
        """
        Add variant image assignments.

        If Shopify export is available, generates full variant rows.
        Otherwise, generates variant_image_assignments.csv entries.

        Args:
            input_row: The input row.
            merch_output: The merchandising output.
        """
        handle = input_row.product_handle

        # Check if we have variant image mappings
        if not merch_output.variant_image_map:
            # No mappings - add a warning entry
            self._variant_assignments.append(
                VariantImageAssignment(
                    Handle=handle,
                    Option_Name="Color",
                    Option_Value="",
                    Image_Src="",
                    Confidence=0,
                    Warning="VARIANT_IMAGE_NOT_ASSIGNED",
                )
            )
            return

        # Handle __all__ first — expand to actual variants if export data available
        if "__all__" in merch_output.variant_image_map:
            hero_url = merch_output.variant_image_map["__all__"]
            if isinstance(hero_url, list):
                hero_url = hero_url[0] if hero_url else ""

            if input_row.variant_data:
                # Expand to per-variant rows using variant data (with SKU)
                for variant in input_row.variant_data:
                    option_value = variant.color or variant.size or ""
                    if not option_value:
                        continue
                    self._variant_assignments.append(
                        VariantImageAssignment(
                            Handle=handle,
                            Variant_SKU=variant.sku,
                            Variant_ID=str(variant.variant_id) if variant.variant_id else "",
                            Option_Name="Color" if variant.color else "Size",
                            Option_Value=option_value,
                            Variant_Image=hero_url,
                            Confidence=merch_output.confidence,
                            Warning="EXPANDED_FROM_ALL",
                        )
                    )
            elif handle in self.shopify_export:
                # Expand from Shopify export (no SKU data available)
                for row in self.shopify_export[handle]:
                    if row.option1_value and row.option1_value.lower() != "default title":
                        self._variant_assignments.append(
                            VariantImageAssignment(
                                Handle=handle,
                                Option_Name=row.option1_name or "Title",
                                Option_Value=row.option1_value,
                                Variant_Image=hero_url,
                                Confidence=merch_output.confidence,
                                Warning="EXPANDED_FROM_ALL",
                            )
                        )
            else:
                # No variant data at all — keep __all__ marker
                self._variant_assignments.append(
                    VariantImageAssignment(
                        Handle=handle,
                        Option_Name="__all__",
                        Option_Value="",
                        Variant_Image=hero_url,
                        Confidence=merch_output.confidence,
                        Warning="ASSIGN_TO_ALL_VARIANTS",
                    )
                )

        # Process remaining non-__all__ entries
        non_all_entries = {
            k: v for k, v in merch_output.variant_image_map.items() if k != "__all__"
        }

        if non_all_entries:
            if handle in self.shopify_export:
                self._add_variant_rows_from_export(handle, non_all_entries)
            else:
                # Expand color→image to per-variant rows using variant data
                self._expand_color_to_variants(
                    input_row, non_all_entries, merch_output.confidence
                )

    def _expand_color_to_variants(
        self,
        input_row: InputRow,
        color_image_map: dict[str, str | list[str]],
        confidence: int,
    ) -> None:
        """Expand color→image mappings to per-variant rows with SKU/ID.

        Uses input_row.variant_data to match each variant to its color's
        image. This produces rows that Ablestar can match by SKU.
        """
        from lookout.enrich.colors import colors_match

        handle = input_row.product_handle

        # Normalize image sources
        resolved_map: dict[str, str] = {}
        for color, image_src in color_image_map.items():
            if isinstance(image_src, list):
                image_src = image_src[0] if image_src else ""
            elif isinstance(image_src, dict):
                image_src = image_src.get("url", image_src.get("src", ""))
            elif not isinstance(image_src, str):
                image_src = str(image_src) if image_src else ""
            if image_src:
                resolved_map[color] = image_src

        if not resolved_map:
            return

        # If we have variant data, create one row per variant
        if input_row.variant_data:
            for variant in input_row.variant_data:
                if not variant.color:
                    continue
                # Find the matching image for this variant's color
                matched_image = None
                for map_color, image_url in resolved_map.items():
                    if colors_match(variant.color, map_color):
                        matched_image = image_url
                        break
                if matched_image:
                    self._variant_assignments.append(
                        VariantImageAssignment(
                            Handle=handle,
                            Variant_SKU=variant.sku,
                            Variant_ID=str(variant.variant_id) if variant.variant_id else "",
                            Option_Name="Color",
                            Option_Value=variant.color,
                            Variant_Image=matched_image,
                            Confidence=confidence,
                            Warning="",
                        )
                    )
        else:
            # No variant data — fall back to color-level rows (no SKU)
            for color, image_src in resolved_map.items():
                self._variant_assignments.append(
                    VariantImageAssignment(
                        Handle=handle,
                        Option_Name="Color",
                        Option_Value=color,
                        Variant_Image=image_src,
                        Confidence=confidence,
                        Warning="NO_VARIANT_DATA",
                    )
                )

    def _add_variant_rows_from_export(
        self,
        handle: str,
        variant_image_map: dict[str, str | list[str]],
    ) -> None:
        """
        Add full variant rows using Shopify export data.

        Args:
            handle: Product handle.
            variant_image_map: Color to image URL mapping.
        """
        export_rows = self.shopify_export[handle]

        for row in export_rows:
            # Check each option for a match
            matched_image = None

            for option_name, option_value in [
                (row.option1_name, row.option1_value),
                (row.option2_name, row.option2_value),
                (row.option3_name, row.option3_value),
            ]:
                if option_name.lower() in ("color", "colour"):
                    # Try to match the option value
                    image_src = variant_image_map.get(option_value)
                    if image_src:
                        if isinstance(image_src, list):
                            matched_image = image_src[0]
                        else:
                            matched_image = image_src
                        break

            if matched_image:
                # Add a variant row with the image
                self._rows.append(
                    {
                        "Handle": handle,
                        "Option1 Name": row.option1_name,
                        "Option1 Value": row.option1_value,
                        "Option2 Name": row.option2_name,
                        "Option2 Value": row.option2_value,
                        "Option3 Name": row.option3_name,
                        "Option3 Value": row.option3_value,
                        "Variant Image": matched_image,
                    }
                )

                # Also add to assignments for tracking
                self._variant_assignments.append(
                    VariantImageAssignment(
                        Handle=handle,
                        Option_Name="Color",
                        Option_Value=option_value,
                        Image_Src=matched_image,
                        Confidence=85,  # Higher confidence with export match
                        Warning="",
                    )
                )

    def write_outputs(
        self,
        output_dir: Path,
        dry_run: bool = False,
    ) -> dict[str, Path]:
        """
        Write all output files.

        Args:
            output_dir: Directory for output files.
            dry_run: If True, skip writing shopify_update.csv.

        Returns:
            Dictionary mapping output type to file path.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}

        # Write shopify_update.csv
        if not dry_run and self._rows:
            shopify_path = output_dir / "shopify_update.csv"
            write_shopify_csv(shopify_path, self._rows)
            outputs["shopify_csv"] = shopify_path
            logger.info(f"Wrote {len(self._rows)} rows to {shopify_path}")

        # Write variant_image_assignments.csv
        if self._variant_assignments:
            assignments_path = output_dir / "variant_image_assignments.csv"
            write_variant_image_assignments(assignments_path, self._variant_assignments)
            outputs["variant_assignments"] = assignments_path

        # Write run_report.csv
        if self._report_rows:
            report_path = output_dir / "run_report.csv"
            write_run_report(report_path, self._report_rows)
            outputs["run_report"] = report_path

        return outputs

    def get_summary(self) -> dict[str, int]:
        """
        Get a summary of processing results.

        Returns:
            Dictionary with counts by status.
        """
        summary: dict[str, int] = {
            "total": len(self._report_rows),
            "updated": 0,
            "skipped": 0,
            "no_match": 0,
            "failed": 0,
            "shopify_rows": len(self._rows),
            "variant_assignments": len(self._variant_assignments),
        }

        for row in self._report_rows:
            if row.status == ProcessingStatus.UPDATED:
                summary["updated"] += 1
            elif row.status in (
                ProcessingStatus.SKIPPED,
                ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED,
                ProcessingStatus.SKIPPED_NO_GAPS,
            ):
                summary["skipped"] += 1
            elif row.status == ProcessingStatus.NO_MATCH:
                summary["no_match"] += 1
            elif row.status == ProcessingStatus.FAILED:
                summary["failed"] += 1

        return summary


def generate_shopify_output(
    results: list[tuple[InputRow, MerchOutput | None, ProcessingStatus, dict]],
    output_dir: Path,
    shopify_export_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Path]:
    """
    Generate all Shopify output files from processing results.

    Args:
        results: List of (input_row, merch_output, status, metadata) tuples.
        output_dir: Directory for output files.
        shopify_export_path: Optional path to Shopify product export.
        dry_run: If True, skip writing shopify_update.csv.

    Returns:
        Dictionary mapping output type to file path.
    """
    builder = ShopifyOutputBuilder(shopify_export_path)

    for input_row, merch_output, status, metadata in results:
        builder.add_result(
            input_row=input_row,
            merch_output=merch_output,
            status=status,
            match_confidence=metadata.get("confidence", 0),
            warnings=metadata.get("warnings", []),
            error_message=metadata.get("error", ""),
            processing_time_ms=metadata.get("processing_time_ms", 0),
        )

    return builder.write_outputs(output_dir, dry_run)
