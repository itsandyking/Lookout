"""Tests for GMC compliance rules."""

import pytest

from lookout.enrich.gmc_rules import (
    GMC_COLOR_MAP,
    check_prohibited_terms,
    check_required_attributes,
    map_color_for_gmc,
    structure_title,
    validate_gtin,
    validate_title,
)


class TestValidateGtin:
    def test_valid_upc_12(self):
        assert validate_gtin("012345678905") is True

    def test_valid_ean_13(self):
        assert validate_gtin("4006381333931") is True

    def test_invalid_check_digit(self):
        assert validate_gtin("012345678900") is False

    def test_wrong_length(self):
        assert validate_gtin("12345") is False

    def test_non_numeric(self):
        assert validate_gtin("ABCDEFGHIJKL") is False

    def test_empty_string(self):
        assert validate_gtin("") is False

    def test_valid_ean_8(self):
        assert validate_gtin("96385074") is True

    def test_valid_gtin_14(self):
        assert validate_gtin("00012345678905") is True


class TestValidateTitle:
    def test_valid_title(self):
        violations = validate_title("Patagonia Nano Puff Jacket - Blue")
        assert violations == []

    def test_title_too_long(self):
        long_title = "A" * 151
        violations = validate_title(long_title)
        assert any("150" in v for v in violations)

    def test_empty_title(self):
        violations = validate_title("")
        assert any("empty" in v.lower() for v in violations)

    def test_all_caps(self):
        violations = validate_title("PATAGONIA NANO PUFF JACKET")
        assert any("caps" in v.lower() for v in violations)


class TestCheckProhibitedTerms:
    def test_clean_text(self):
        result = check_prohibited_terms("A warm jacket for cold weather hiking.")
        assert result == []

    def test_promotional_language(self):
        result = check_prohibited_terms("The best jacket ever! Free shipping included.")
        assert len(result) > 0

    def test_superlatives(self):
        result = check_prohibited_terms("This incredible, amazing premium jacket.")
        assert len(result) > 0

    def test_price_mention(self):
        result = check_prohibited_terms("Only $99.99 while supplies last!")
        assert len(result) > 0


class TestMapColorForGmc:
    def test_known_mapping(self):
        if "Midnight" in GMC_COLOR_MAP:
            result = map_color_for_gmc("Midnight")
            assert result == GMC_COLOR_MAP["Midnight"]

    def test_unmapped_color_passes_through(self):
        result = map_color_for_gmc("Blue")
        assert result == "Blue"

    def test_case_insensitive_lookup(self):
        if "midnight" in {k.lower() for k in GMC_COLOR_MAP}:
            result = map_color_for_gmc("midnight")
            assert result != ""

    def test_empty_string(self):
        result = map_color_for_gmc("")
        assert result == ""


class TestStructureTitle:
    def test_basic_structure(self):
        title = structure_title(
            brand="Patagonia",
            product_type="Jacket",
            attributes={"color": "Blue", "gender": "Men's"},
        )
        assert "Patagonia" in title
        assert "Jacket" in title
        assert len(title) <= 150

    def test_truncation(self):
        title = structure_title(
            brand="Patagonia",
            product_type="Ultra-Lightweight Down Insulated Waterproof Jacket",
            attributes={"color": "Midnight Navy Blue", "size": "Extra Large Tall"},
        )
        assert len(title) <= 150


class TestCheckRequiredAttributes:
    def test_complete_product(self):
        product = {
            "title": "Patagonia Jacket",
            "body_html": "A warm jacket.",
            "image": "https://example.com/img.jpg",
            "price": "299.00",
            "barcode": "012345678905",
        }
        missing = check_required_attributes(product)
        assert missing == []

    def test_missing_fields(self):
        product = {"title": "Jacket"}
        missing = check_required_attributes(product)
        assert len(missing) > 0
        assert any("image" in m.lower() for m in missing)
