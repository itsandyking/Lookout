"""Tests for catalog health text checks."""

import pytest

from lookout.audit.health_checks import (
    check_description_quality,
    check_title_description_coherence,
)


class TestDescriptionQuality:
    """Test description quality flagging."""

    def test_empty_body(self):
        result = check_description_quality("")
        assert result["quality"] == "empty"

    def test_none_body(self):
        result = check_description_quality(None)
        assert result["quality"] == "empty"

    def test_short_body(self):
        result = check_description_quality("<p>Buy now</p>")
        assert result["quality"] == "weak"

    def test_boilerplate_buy_locally(self):
        result = check_description_quality(
            "<p>This is a great product. Buy locally at your nearest dealer for the best price.</p>"
        )
        assert result["quality"] == "weak"
        assert "buy locally" in result["reason"].lower()

    def test_boilerplate_contact_dealer(self):
        result = check_description_quality(
            "<p>For more information, contact dealer for pricing and availability.</p>"
        )
        assert result["quality"] == "weak"

    def test_boilerplate_description_coming(self):
        result = check_description_quality(
            "<p>Description coming soon.</p>"
        )
        assert result["quality"] == "weak"

    def test_title_repeated(self):
        result = check_description_quality(
            "<p>Men's Down Jacket</p>",
            product_title="Men's Down Jacket",
        )
        assert result["quality"] == "weak"
        assert "title repeated" in result["reason"].lower()

    def test_good_description(self):
        result = check_description_quality(
            "<p>The Alpine Down Jacket features 800-fill goose down insulation "
            "with a water-resistant shell. Zippered hand pockets and an "
            "adjustable hood keep you warm in cold conditions.</p>"
        )
        assert result["quality"] == "ok"

    def test_html_tags_stripped(self):
        """Quality check should work on text content, not raw HTML."""
        result = check_description_quality(
            "<div><ul><li>Feature 1</li><li>Feature 2</li><li>Feature 3</li>"
            "<li>Feature 4</li><li>Feature 5</li></ul></div>"
        )
        assert result["quality"] == "ok"


class TestTitleDescriptionCoherence:
    """Test title-description coherence checking."""

    def test_coherent(self):
        result = check_title_description_coherence(
            title="Men's Down Jacket",
            product_type="Jackets",
            body_html="<p>This insulated down jacket keeps you warm.</p>",
        )
        assert result["coherence"] == "ok"

    def test_mismatch(self):
        result = check_title_description_coherence(
            title="Women's Down Jacket",
            product_type="Jackets",
            body_html="<p>These hiking boots feature Vibram soles and waterproof leather.</p>",
        )
        assert result["coherence"] == "mismatch"

    def test_empty_body_is_ok(self):
        """Empty body is caught by description quality, not coherence."""
        result = check_title_description_coherence(
            title="Men's Down Jacket",
            product_type="Jackets",
            body_html="",
        )
        assert result["coherence"] == "ok"

    def test_type_word_match(self):
        """Product type word in description is sufficient."""
        result = check_title_description_coherence(
            title="Patagonia Nano Puff",
            product_type="Jackets",
            body_html="<p>Lightweight synthetic jacket with PrimaLoft insulation.</p>",
        )
        assert result["coherence"] == "ok"

    def test_ignores_common_words(self):
        """Words like 'men's', 'women's', brand names shouldn't drive mismatch."""
        result = check_title_description_coherence(
            title="Patagonia Men's Better Sweater",
            product_type="Fleece",
            body_html="<p>Classic fleece pullover with a sweater-knit face.</p>",
        )
        assert result["coherence"] == "ok"
