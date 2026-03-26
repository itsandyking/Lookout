"""
Output CSV writing for the enrichment pipeline.

Handles:
- Shopify-compatible product import CSV
- Variant image assignments CSV
- Run report CSV
"""

import csv
import logging
from pathlib import Path

from lookout.enrich.models import (
    MerchOutput,
    RunReportRow,
    VariantImageAssignment,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Shopify CSV Output
# -----------------------------------------------------------------------------

# Standard Shopify CSV columns (in order)
SHOPIFY_CSV_COLUMNS = [
    "Handle",
    "Title",
    "Body (HTML)",
    "Vendor",
    "Type",
    "Tags",
    "Published",
    "Option1 Name",
    "Option1 Value",
    "Option2 Name",
    "Option2 Value",
    "Option3 Name",
    "Option3 Value",
    "Variant SKU",
    "Variant Grams",
    "Variant Inventory Tracker",
    "Variant Inventory Qty",
    "Variant Inventory Policy",
    "Variant Fulfillment Service",
    "Variant Price",
    "Variant Compare At Price",
    "Variant Requires Shipping",
    "Variant Taxable",
    "Variant Barcode",
    "Image Src",
    "Image Position",
    "Image Alt Text",
    "Gift Card",
    "SEO Title",
    "SEO Description",
    "Variant Image",
    "Variant Weight Unit",
]


def write_shopify_csv(
    output_path: str | Path,
    rows: list[dict[str, str]],
) -> None:
    """
    Write a Shopify-compatible product import CSV.

    Args:
        output_path: Path for the output CSV.
        rows: List of row dictionaries.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHOPIFY_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Wrote {len(rows)} rows to {output_path}")


def merch_output_to_shopify_rows(
    merch_output: MerchOutput,
    include_body: bool = True,
    include_images: bool = True,
) -> list[dict[str, str]]:
    """
    Convert a MerchOutput to Shopify CSV rows.

    Args:
        merch_output: The merchandising output to convert.
        include_body: Whether to include body HTML.
        include_images: Whether to include image rows.

    Returns:
        List of row dictionaries for the Shopify CSV.
    """
    rows: list[dict[str, str]] = []

    # Primary product row
    primary_row: dict[str, str] = {"Handle": merch_output.handle}

    if include_body and merch_output.body_html:
        primary_row["Body (HTML)"] = merch_output.body_html

    # If we have images, include the first one in the primary row
    if include_images and merch_output.images:
        first_image = merch_output.images[0]
        primary_row["Image Src"] = first_image.src
        primary_row["Image Position"] = str(first_image.position)
        if first_image.alt:
            primary_row["Image Alt Text"] = first_image.alt

    rows.append(primary_row)

    # Additional image rows
    if include_images and len(merch_output.images) > 1:
        for image in merch_output.images[1:]:
            image_row: dict[str, str] = {
                "Handle": merch_output.handle,
                "Image Src": image.src,
                "Image Position": str(image.position),
            }
            if image.alt:
                image_row["Image Alt Text"] = image.alt
            rows.append(image_row)

    return rows


# -----------------------------------------------------------------------------
# Variant Image Assignments CSV
# -----------------------------------------------------------------------------

VARIANT_IMAGE_COLUMNS = [
    "Variant SKU",
    "Variant ID",
    "Handle",
    "Option Name",
    "Option Value",
    "Variant Image",
    "Confidence",
    "Warning",
]


def write_variant_image_assignments(
    output_path: str | Path,
    assignments: list[VariantImageAssignment],
) -> None:
    """
    Write the variant image assignments CSV.

    Format matches Ablestar/Matrixify per-variant import:
    SKU as first column for variant-level matching.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=VARIANT_IMAGE_COLUMNS)
        writer.writeheader()

        for assignment in assignments:
            writer.writerow(
                {
                    "Variant SKU": assignment.Variant_SKU,
                    "Variant ID": assignment.Variant_ID,
                    "Handle": assignment.Handle,
                    "Option Name": assignment.Option_Name,
                    "Option Value": assignment.Option_Value,
                    "Variant Image": assignment.Variant_Image,
                    "Confidence": assignment.Confidence,
                    "Warning": assignment.Warning,
                }
            )

    logger.info(f"Wrote {len(assignments)} variant image assignments to {output_path}")


# -----------------------------------------------------------------------------
# Run Report CSV
# -----------------------------------------------------------------------------

RUN_REPORT_COLUMNS = [
    "handle",
    "vendor",
    "status",
    "match_confidence",
    "warnings",
    "output_rows_count",
    "error_message",
    "processing_time_ms",
]


def write_run_report(
    output_path: str | Path,
    rows: list[RunReportRow],
) -> None:
    """
    Write the run report CSV.

    Args:
        output_path: Path for the output CSV.
        rows: List of run report rows.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RUN_REPORT_COLUMNS)
        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    "handle": row.handle,
                    "vendor": row.vendor,
                    "status": row.status.value,
                    "match_confidence": row.match_confidence,
                    "warnings": row.warnings,
                    "output_rows_count": row.output_rows_count,
                    "error_message": row.error_message,
                    "processing_time_ms": row.processing_time_ms,
                }
            )

    logger.info(f"Wrote run report with {len(rows)} entries to {output_path}")
