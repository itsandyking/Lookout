"""Tests for OllamaVisionClient color matching logic."""

import pytest

from lookout.enrich.llm import OllamaVisionClient, _color_tokens_overlap


class TestColorTokensOverlap:
    def test_exact_match(self):
        assert _color_tokens_overlap("blue", "blue")

    def test_partial_token_match(self):
        assert _color_tokens_overlap("navy blue", "blue")

    def test_no_match(self):
        assert not _color_tokens_overlap("red", "blue")

    def test_ignores_filler(self):
        # "dark" is filler, "blue" is the meaningful token
        assert _color_tokens_overlap("dark blue", "blue")

    def test_filler_only_no_match(self):
        # Both sides only have filler words after filtering
        assert not _color_tokens_overlap("dark", "light")


class TestMatchColors:
    def test_exact_color_match(self):
        detected = [
            ("img1.jpg", "Red"),
            ("img2.jpg", "Blue"),
        ]
        variants = ["Red", "Blue"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {"Red": "img1.jpg", "Blue": "img2.jpg"}

    def test_substring_match(self):
        """Detected 'Navy Blue' should match variant 'Blue'."""
        detected = [("img1.jpg", "Navy Blue")]
        variants = ["Blue"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {"Blue": "img1.jpg"}

    def test_variant_contains_detected(self):
        """Variant 'Navy Blue' should match detected 'Blue'."""
        detected = [("img1.jpg", "Blue")]
        variants = ["Navy Blue"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {"Navy Blue": "img1.jpg"}

    def test_case_insensitive(self):
        detected = [("img1.jpg", "RED")]
        variants = ["red"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {"red": "img1.jpg"}

    def test_no_match_returns_empty(self):
        detected = [("img1.jpg", "Purple")]
        variants = ["Red", "Blue"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {}

    def test_skips_none_detections(self):
        detected = [("img1.jpg", None), ("img2.jpg", "Red")]
        variants = ["Red"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {"Red": "img2.jpg"}

    def test_no_url_reuse(self):
        """Same URL shouldn't be assigned to two colors."""
        detected = [("img1.jpg", "Blue")]
        variants = ["Blue", "Navy Blue"]
        result = OllamaVisionClient.match_colors(detected, variants)
        # Only one should match — first match wins
        assert len(result) == 1
        assert "Blue" in result

    def test_token_overlap_match(self):
        """'Forest Green' detected should match 'Green' variant via token overlap."""
        detected = [("img1.jpg", "Forest Green")]
        variants = ["Green"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {"Green": "img1.jpg"}

    def test_multiple_colors_partial_match(self):
        """Only matching colors get assigned."""
        detected = [
            ("img1.jpg", "Black"),
            ("img2.jpg", "Red"),
            ("img3.jpg", "Teal"),
        ]
        variants = ["Black", "Red", "White"]
        result = OllamaVisionClient.match_colors(detected, variants)
        assert result == {"Black": "img1.jpg", "Red": "img2.jpg"}
        assert "White" not in result

    def test_empty_inputs(self):
        assert OllamaVisionClient.match_colors([], []) == {}
        assert OllamaVisionClient.match_colors([], ["Red"]) == {}
        assert OllamaVisionClient.match_colors([("img.jpg", "Red")], []) == {}
