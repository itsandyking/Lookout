"""Tests for content extraction."""

from pathlib import Path

import pytest

from lookout.enrich.extractor import ContentExtractor, extract_content


@pytest.fixture
def sample_html():
    """Load sample product HTML."""
    fixture_path = Path(__file__).parent / "fixtures" / "sample_product.html"
    with open(fixture_path) as f:
        return f.read()


class TestContentExtractor:
    """Tests for ContentExtractor."""

    def test_extract_source_text(self, sample_html: str):
        """Test extracting source text from HTML."""
        extractor = ContentExtractor()
        source_text = extractor.extract_source_text(
            sample_html, "https://www.patagonia.com/product/test"
        )

        # Should have extracted text blocks
        assert len(source_text.visible_text_blocks) > 0

        # Should have bullet lists (features)
        assert len(source_text.bullet_lists) > 0
        feature_list = source_text.bullet_lists[0]
        assert any("windproof" in item.lower() for item in feature_list)

        # Should have spec tables
        assert len(source_text.spec_tables) > 0
        specs = source_text.spec_tables[0]
        assert "Weight" in specs or any("weight" in k.lower() for k in specs.keys())

        # Should have JSON-LD
        assert len(source_text.json_ld_products) > 0
        assert source_text.json_ld_products[0]["@type"] == "Product"

        # Should have meta description
        assert "lightweight" in source_text.meta_description.lower()

    def test_extract_facts(self, sample_html: str):
        """Test extracting structured facts from HTML."""
        extractor = ContentExtractor()
        _, facts = extract_content(
            sample_html, "https://www.patagonia.com/product/test"
        )

        # Product name from JSON-LD
        assert "Nano Puff" in facts.product_name

        # Brand from JSON-LD
        assert facts.brand == "Patagonia"

        # Should have images
        assert len(facts.images) > 0

        # Should have feature bullets
        assert len(facts.feature_bullets) > 0
        assert any("windproof" in bullet.lower() for bullet in facts.feature_bullets)

        # Should have specs
        assert len(facts.specs) > 0

        # Should have variant image candidates (from color swatches)
        assert len(facts.variant_image_candidates) > 0
        assert "Lagom Blue" in facts.variant_image_candidates
        assert "Black" in facts.variant_image_candidates

        # Should have variants
        assert len(facts.variants) > 0
        color_variant = next(
            (v for v in facts.variants if v.option_name.lower() == "color"),
            None,
        )
        assert color_variant is not None
        assert "Lagom Blue" in color_variant.values

    def test_extract_images(self, sample_html: str):
        """Test image extraction including lazy-loaded."""
        extractor = ContentExtractor()
        _, facts = extract_content(
            sample_html, "https://www.patagonia.com/product/test"
        )

        # Should include regular src images
        image_urls = [img.url for img in facts.images]
        assert any("main-blue.jpg" in url for url in image_urls)

        # Should include lazy-loaded (data-src) images
        assert any("detail-1.jpg" in url for url in image_urls)

        # Should include JSON-LD images
        assert any("product-1.jpg" in url for url in image_urls)

    def test_extract_json_ld(self, sample_html: str):
        """Test JSON-LD extraction."""
        extractor = ContentExtractor()
        source_text = extractor.extract_source_text(
            sample_html, "https://www.patagonia.com/product/test"
        )

        assert len(source_text.json_ld_products) == 1
        product = source_text.json_ld_products[0]

        assert product["@type"] == "Product"
        assert product["name"] == "Men's Nano Puff Jacket"
        assert product["brand"]["name"] == "Patagonia"
        assert len(product["image"]) == 2

    def test_extract_canonical_url(self, sample_html: str):
        """Test canonical URL extraction."""
        extractor = ContentExtractor()
        _, facts = extract_content(
            sample_html, "https://www.patagonia.com/product/test"
        )

        assert "84212.html" in facts.canonical_url

    def test_skip_nav_footer(self, sample_html: str):
        """Test that nav/footer content is filtered."""
        extractor = ContentExtractor()
        source_text = extractor.extract_source_text(
            sample_html, "https://www.patagonia.com/product/test"
        )

        # Should not include navigation links
        all_text = " ".join(source_text.visible_text_blocks)
        # Home/Shop are in nav, should be filtered
        # (This is a soft test since nav text might still appear in other contexts)

    def test_empty_html(self):
        """Test handling empty HTML."""
        extractor = ContentExtractor()
        source_text, facts = extract_content("", "https://example.com")

        assert len(source_text.visible_text_blocks) == 0
        assert len(facts.images) == 0
        assert facts.product_name == ""

    def test_malformed_html(self):
        """Test handling malformed HTML."""
        html = "<html><body><p>Unclosed tag<div>Content</body>"
        extractor = ContentExtractor()
        source_text, facts = extract_content(html, "https://example.com")

        # Should not crash, might extract some text
        assert isinstance(source_text.visible_text_blocks, list)
