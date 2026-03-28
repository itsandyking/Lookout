"""Tests for Shopify JSON API scraper."""

from lookout.enrich.shopify_scraper import _shopify_json_to_facts, _strip_html


class TestStripHtml:
    def test_strips_tags(self):
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_empty_string(self):
        assert _strip_html("") == ""

    def test_none_returns_empty(self):
        assert _strip_html(None) == ""


class TestShopifyJsonToFacts:
    def test_maps_basic_fields(self):
        product = {
            "title": "Dancer 1 Ski",
            "vendor": "Faction",
            "product_type": "Skis",
            "body_html": "<p>A versatile all-mountain ski.</p>",
            "tags": ["Ski", "Freeride"],
            "images": [
                {"src": "https://cdn.shopify.com/img1.jpg", "alt": "Front view", "width": 1200, "height": 800, "variant_ids": []},
                {"src": "https://cdn.shopify.com/img2.jpg", "alt": "Side view", "width": 1200, "height": 800, "variant_ids": []},
            ],
            "variants": [
                {"id": 1, "title": "178", "option1": "178", "price": "599.00", "sku": "FCN-D1-178"},
            ],
        }
        facts = _shopify_json_to_facts(product, "https://factionskis.com/products/dancer-1")

        assert facts.product_name == "Dancer 1 Ski"
        assert facts.brand == "Faction"
        assert len(facts.images) == 2
        assert facts.images[0].url == "https://cdn.shopify.com/img1.jpg"
        assert facts.images[0].alt_text == "Front view"
        assert facts.specs.get("Product Type") == "Skis"
        assert len(facts.description_blocks) > 0

    def test_extracts_colors_from_variants(self):
        product = {
            "title": "Test Product",
            "vendor": "Test",
            "body_html": "",
            "images": [
                {"src": "https://cdn.shopify.com/img1.jpg", "variant_ids": [1]},
                {"src": "https://cdn.shopify.com/img2.jpg", "variant_ids": [2]},
            ],
            "variants": [
                {"id": 1, "option1": "Blue", "option2": "Large"},
                {"id": 2, "option1": "Red", "option2": "Medium"},
            ],
        }
        facts = _shopify_json_to_facts(product, "https://example.com/products/test")

        assert "Blue" in facts.variant_image_candidates
        assert "Red" in facts.variant_image_candidates

    def test_handles_empty_product(self):
        facts = _shopify_json_to_facts({}, "https://example.com/products/test")
        assert facts.product_name == ""
        assert facts.images == []
        assert facts.description_blocks == []

    def test_skips_default_title_variants(self):
        product = {
            "title": "Simple Product",
            "vendor": "Test",
            "body_html": "",
            "images": [],
            "variants": [
                {"id": 1, "option1": "Default Title"},
            ],
        }
        facts = _shopify_json_to_facts(product, "https://example.com/products/test")
        assert facts.variant_image_candidates == {}
