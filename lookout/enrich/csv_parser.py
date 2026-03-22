"""
CSV parsing utilities for input and output files.
"""

import csv
import logging
from pathlib import Path
from typing import Generator

from pydantic import ValidationError

from .models import (
    InputRow,
    MerchOutput,
    OutputImage,
    RunReportRow,
    ShopifyProductRow,
    VariantImageAssignment,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Input CSV Parsing
# -----------------------------------------------------------------------------


def parse_input_csv(
    csv_path: str | Path,
    max_rows: int | None = None,
) -> Generator[InputRow, None, None]:
    """
    Parse the merchandising priority CSV input file.

    Args:
        csv_path: Path to the input CSV file.
        max_rows: Optional maximum number of rows to process.

    Yields:
        InputRow objects for each valid row.

    Raises:
        FileNotFoundError: If the CSV file doesn't exist.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {csv_path}")

    rows_yielded = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
            if max_rows is not None and rows_yielded >= max_rows:
                logger.info(f"Reached max_rows limit ({max_rows}), stopping")
                return

            try:
                input_row = InputRow.model_validate(row)
                rows_yielded += 1
                yield input_row
            except ValidationError as e:
                logger.warning(f"Row {row_num}: Validation error - {e}")
                continue


def count_input_rows(csv_path: str | Path) -> int:
    """
    Count the number of data rows in an input CSV.

    Args:
        csv_path: Path to the CSV file.

    Returns:
        Number of data rows (excluding header).
    """
    csv_path = Path(csv_path)
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # Skip header
        return sum(1 for _ in reader)


# -----------------------------------------------------------------------------
# Shopify Export CSV Parsing (Optional Input)
# -----------------------------------------------------------------------------


class ShopifyExportRow:
    """Represents a row from a Shopify product export CSV."""

    def __init__(self, row: dict[str, str]) -> None:
        self.handle = row.get("Handle", "")
        self.title = row.get("Title", "")
        self.body_html = row.get("Body (HTML)", "")
        self.vendor = row.get("Vendor", "")
        self.product_type = row.get("Type", "")
        self.tags = row.get("Tags", "")
        self.option1_name = row.get("Option1 Name", "")
        self.option1_value = row.get("Option1 Value", "")
        self.option2_name = row.get("Option2 Name", "")
        self.option2_value = row.get("Option2 Value", "")
        self.option3_name = row.get("Option3 Name", "")
        self.option3_value = row.get("Option3 Value", "")
        self.variant_sku = row.get("Variant SKU", "")
        self.variant_image = row.get("Variant Image", "")
        self.image_src = row.get("Image Src", "")
        self.image_position = row.get("Image Position", "")


def parse_shopify_export(
    csv_path: str | Path,
) -> dict[str, list[ShopifyExportRow]]:
    """
    Parse a Shopify product export CSV.

    Groups rows by handle since Shopify exports have multiple rows
    per product (for variants and images).

    Args:
        csv_path: Path to the Shopify export CSV.

    Returns:
        Dictionary mapping handles to lists of rows.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Shopify export CSV not found: {csv_path}")

    products: dict[str, list[ShopifyExportRow]] = {}

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        current_handle = ""
        for row in reader:
            # Shopify CSV uses empty Handle for continuation rows
            handle = row.get("Handle", "").strip()
            if handle:
                current_handle = handle

            if current_handle:
                if current_handle not in products:
                    products[current_handle] = []
                products[current_handle].append(ShopifyExportRow(row))

    return products


def get_variant_options_from_export(
    export_rows: list[ShopifyExportRow],
) -> list[dict[str, str]]:
    """
    Extract variant options from Shopify export rows.

    Args:
        export_rows: List of ShopifyExportRow for a single product.

    Returns:
        List of dicts with option names and values for each variant.
    """
    variants = []

    for row in export_rows:
        options: dict[str, str] = {}

        if row.option1_name and row.option1_value:
            options[row.option1_name] = row.option1_value
        if row.option2_name and row.option2_value:
            options[row.option2_name] = row.option2_value
        if row.option3_name and row.option3_value:
            options[row.option3_name] = row.option3_value

        if options:
            variants.append(options)

    return variants


# -----------------------------------------------------------------------------
# Output CSV Writing
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
    "Handle",
    "Option Name",
    "Option Value",
    "Image Src",
    "Confidence",
    "Warning",
]


def write_variant_image_assignments(
    output_path: str | Path,
    assignments: list[VariantImageAssignment],
) -> None:
    """
    Write the variant image assignments CSV.

    Args:
        output_path: Path for the output CSV.
        assignments: List of variant image assignments.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=VARIANT_IMAGE_COLUMNS)
        writer.writeheader()

        for assignment in assignments:
            writer.writerow(
                {
                    "Handle": assignment.Handle,
                    "Option Name": assignment.Option_Name,
                    "Option Value": assignment.Option_Value,
                    "Image Src": assignment.Image_Src,
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
