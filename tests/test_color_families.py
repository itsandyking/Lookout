"""Tests for color family text fallback classification."""

import pytest

from lookout.enrich.color_families import (
    COLOR_FAMILIES,
    infer_color_family,
)


class TestInferColorFamily:
    """Test text-based color name → family mapping."""

    def test_exact_family_name(self):
        assert infer_color_family("Black") == "Black"

    def test_case_insensitive(self):
        assert infer_color_family("black") == "Black"

    def test_family_as_substring(self):
        assert infer_color_family("Basin Green") == "Green"

    def test_creative_name_lookup(self):
        assert infer_color_family("Obsidian") == "Black"

    def test_creative_name_brine(self):
        assert infer_color_family("Brine") == "Green"

    def test_multi_word_creative(self):
        assert infer_color_family("Nouveau Green") == "Green"

    def test_navy_is_navy_not_blue(self):
        assert infer_color_family("Navy") == "Navy"

    def test_slash_color_multi(self):
        assert infer_color_family("Black/Poppy") == "Multi"

    def test_unknown_returns_none(self):
        assert infer_color_family("Zephyr") is None

    def test_empty_string(self):
        assert infer_color_family("") is None

    def test_default_title(self):
        assert infer_color_family("Default Title") is None

    def test_gold(self):
        assert infer_color_family("Antique Gold") == "Gold"

    def test_silver(self):
        assert infer_color_family("Brushed Silver") == "Silver"

    def test_cream_is_beige(self):
        assert infer_color_family("Cream") == "Beige"

    def test_charcoal_is_gray(self):
        assert infer_color_family("Charcoal") == "Gray"

    def test_coral_is_pink(self):
        assert infer_color_family("Coral") == "Pink"

    def test_burgundy_is_red(self):
        assert infer_color_family("Burgundy") == "Red"

    def test_tan_is_brown(self):
        assert infer_color_family("Tan") == "Brown"

    def test_word_creative_fallback(self):
        # "Deep" is unknown, "Obsidian" hits creative lookup word-by-word
        assert infer_color_family("Deep Obsidian") == "Black"
