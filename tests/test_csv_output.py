"""Tests for CSV output generation."""

from pathlib import Path

import pytest

from lookout.enrich.csv_parser import (
    merch_output_to_shopify_rows,
    parse_input_csv,
    SHOPIFY_CSV_COLUMNS,
)
from lookout.enrich.models import MerchOutput, OutputImage


@pytest.fixture
def sample_input_csv():
    """Path to sample input CSV."""
    return Path(__file__).parent / "fixtures" / "sample_input.csv"


class TestInputCSVParsing:
    """Tests for input CSV parsing."""

    def test_parse_valid_csv(self, sample_input_csv: Path):
        """Test parsing a valid CSV file."""
        rows = list(parse_input_csv(sample_input_csv))

        assert len(rows) == 5

        # Check first row
        first_row = rows[0]
        assert first_row.product_handle == "mens-nano-puff-jacket"
        assert first_row.vendor == "Patagonia"
        assert first_row.has_image is False
        assert first_row.has_variant_images is False
        assert first_row.has_description is False
        assert first_row.needs_description is True
        assert first_row.needs_images is True
        assert first_row.needs_variant_images is True

    def test_parse_with_max_rows(self, sample_input_csv: Path):
        """Test parsing with max_rows limit."""
        rows = list(parse_input_csv(sample_input_csv, max_rows=2))

        assert len(rows) == 2

    def test_parse_row_with_no_gaps(self, sample_input_csv: Path):
        """Test parsing a row that has all content."""
        rows = list(parse_input_csv(sample_input_csv))

        # Find the row with no gaps
        no_gap_row = next(
            (r for r in rows if r.product_handle == "mens-better-sweater"),
            None,
        )
        assert no_gap_row is not None
        assert no_gap_row.has_any_gap is False


class TestShopifyCSVGeneration:
    """Tests for Shopify CSV row generation."""

    def test_generate_product_row(self):
        """Test generating a basic product row."""
        merch_output = MerchOutput(
            handle="test-product",
            body_html="<p>Test description</p>",
            confidence=85,
        )

        rows = merch_output_to_shopify_rows(merch_output)

        assert len(rows) == 1
        assert rows[0]["Handle"] == "test-product"
        assert rows[0]["Body (HTML)"] == "<p>Test description</p>"

    def test_generate_image_rows(self):
        """Test generating image rows."""
        merch_output = MerchOutput(
            handle="test-product",
            body_html="<p>Description</p>",
            images=[
                OutputImage(src="https://example.com/1.jpg", position=1, alt="Image 1"),
                OutputImage(src="https://example.com/2.jpg", position=2, alt="Image 2"),
                OutputImage(src="https://example.com/3.jpg", position=3, alt="Image 3"),
            ],
            confidence=80,
        )

        rows = merch_output_to_shopify_rows(merch_output)

        assert len(rows) == 3  # 1 product + 2 additional image rows

        # First row has product data + first image
        assert rows[0]["Handle"] == "test-product"
        assert rows[0]["Body (HTML)"] == "<p>Description</p>"
        assert rows[0]["Image Src"] == "https://example.com/1.jpg"
        assert rows[0]["Image Position"] == "1"
        assert rows[0]["Image Alt Text"] == "Image 1"

        # Additional image rows
        assert rows[1]["Handle"] == "test-product"
        assert rows[1]["Image Src"] == "https://example.com/2.jpg"
        assert rows[1]["Image Position"] == "2"
        assert "Body (HTML)" not in rows[1] or rows[1]["Body (HTML)"] == ""

    def test_generate_without_body(self):
        """Test generating rows without body HTML."""
        merch_output = MerchOutput(
            handle="test-product",
            images=[
                OutputImage(src="https://example.com/1.jpg", position=1, alt="Image 1"),
            ],
            confidence=75,
        )

        rows = merch_output_to_shopify_rows(merch_output, include_body=False)

        assert len(rows) == 1
        assert rows[0]["Handle"] == "test-product"
        assert "Body (HTML)" not in rows[0] or rows[0]["Body (HTML)"] == ""
        assert rows[0]["Image Src"] == "https://example.com/1.jpg"

    def test_generate_without_images(self):
        """Test generating rows without images."""
        merch_output = MerchOutput(
            handle="test-product",
            body_html="<p>Description only</p>",
            confidence=70,
        )

        rows = merch_output_to_shopify_rows(merch_output, include_images=False)

        assert len(rows) == 1
        assert rows[0]["Handle"] == "test-product"
        assert rows[0]["Body (HTML)"] == "<p>Description only</p>"
        assert "Image Src" not in rows[0] or rows[0]["Image Src"] == ""

    def test_shopify_csv_columns(self):
        """Test that required Shopify columns are present."""
        required_columns = [
            "Handle",
            "Title",
            "Body (HTML)",
            "Vendor",
            "Image Src",
            "Image Position",
            "Image Alt Text",
            "Variant Image",
        ]

        for col in required_columns:
            assert col in SHOPIFY_CSV_COLUMNS, f"Missing column: {col}"
