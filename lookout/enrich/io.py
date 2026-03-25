"""
Input CSV parsing utilities for the enrichment pipeline.

Handles:
- Merchandising priority CSV input parsing
- Shopify product export CSV parsing
"""

import csv
import logging
from collections.abc import Generator
from pathlib import Path

from pydantic import ValidationError

from .models import InputRow

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
