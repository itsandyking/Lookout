"""Tests for FirecrawlScraper."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from lookout.enrich.firecrawl_scraper import (
    EXTRACTION_SCHEMA,
    FirecrawlScraper,
    _clean_image_url,
    _firecrawl_json_to_facts,
)
from lookout.enrich.models import ExtractedFacts, ImageInfo


class TestCleanImageUrl:
    def test_strips_resize_params(self):
        url = "https://cdn.example.com/img.jpg?impolicy=bglt&imwidth=246"
        assert _clean_image_url(url) == "https://cdn.example.com/img.jpg"

    def test_strips_common_params(self):
        url = "https://cdn.example.com/img.jpg?w=300&h=400&fit=crop&q=80"
        assert _clean_image_url(url) == "https://cdn.example.com/img.jpg"

    def test_preserves_non_resize_params(self):
        url = "https://cdn.example.com/img.jpg?v=1234&id=abc"
        assert _clean_image_url(url) == "https://cdn.example.com/img.jpg?v=1234&id=abc"

    def test_no_params_unchanged(self):
        url = "https://cdn.example.com/img.jpg"
        assert _clean_image_url(url) == url

    def test_mixed_params(self):
        url = "https://cdn.example.com/img.jpg?v=1&imwidth=246&format=webp"
        result = _clean_image_url(url)
        assert "imwidth" not in result
        assert "format" not in result
        assert "v=1" in result


class TestExtractionSchema:
    def test_schema_has_required_fields(self):
        props = EXTRACTION_SCHEMA["properties"]
        assert "product_name" in props
        assert "description_blocks" in props
        assert "feature_bullets" in props
        assert "specs" in props
        assert "images" in props
        assert "brand" in props

    def test_schema_is_valid_json_schema(self):
        assert EXTRACTION_SCHEMA["type"] == "object"
        for prop in EXTRACTION_SCHEMA["properties"].values():
            assert "type" in prop


class TestFirecrawlJsonToFacts:
    def test_maps_basic_fields(self):
        data = {
            "product_name": "Alpine Jacket",
            "brand": "Patagonia",
            "description_blocks": ["A warm jacket for cold days."],
            "feature_bullets": ["Waterproof", "Breathable"],
            "specs": {"Weight": "400g", "Material": "Gore-Tex"},
            "images": ["https://example.com/img1.jpg", "https://example.com/img2.jpg"],
            "colors": ["Blue", "Red"],
            "materials": "Gore-Tex Pro",
            "price": "$299",
        }
        facts = _firecrawl_json_to_facts(data, "https://patagonia.com/product/alpine-jacket")

        assert isinstance(facts, ExtractedFacts)
        assert facts.product_name == "Alpine Jacket"
        assert facts.brand == "Patagonia"
        assert facts.description_blocks == ["A warm jacket for cold days."]
        assert facts.feature_bullets == ["Waterproof", "Breathable"]
        assert facts.specs == {"Weight": "400g", "Material": "Gore-Tex"}
        assert len(facts.images) == 2
        assert facts.images[0].url == "https://example.com/img1.jpg"
        assert facts.materials == "Gore-Tex Pro"
        assert facts.canonical_url == "https://patagonia.com/product/alpine-jacket"

    def test_handles_empty_data(self):
        facts = _firecrawl_json_to_facts({}, "https://example.com")
        assert facts.product_name == ""
        assert facts.images == []
        assert facts.specs == {}

    def test_handles_missing_fields(self):
        data = {"product_name": "Test Product"}
        facts = _firecrawl_json_to_facts(data, "https://example.com")
        assert facts.product_name == "Test Product"
        assert facts.description_blocks == []
        assert facts.feature_bullets == []


class TestFirecrawlScraper:
    def test_extract_calls_firecrawl_with_schema(self):
        async def _run():
            mock_client = AsyncMock()
            mock_doc = MagicMock()
            mock_doc.json = {
                "product_name": "Test Product",
                "brand": "TestBrand",
                "description_blocks": ["A test product."],
                "feature_bullets": [],
                "specs": {},
                "images": [],
                "colors": [],
                "materials": "",
                "price": "",
            }
            mock_doc.metadata = {"sourceURL": "https://example.com/product"}
            mock_client.scrape.return_value = mock_doc

            scraper = FirecrawlScraper(client=mock_client, min_delay_ms=0, max_delay_ms=0)
            facts = await scraper.extract("https://example.com/product")

            assert isinstance(facts, ExtractedFacts)
            assert facts.product_name == "Test Product"
            mock_client.scrape.assert_called_once()

        asyncio.run(_run())

    def test_extract_returns_none_on_failure(self):
        async def _run():
            mock_client = AsyncMock()
            mock_client.scrape.side_effect = Exception("Connection refused")

            scraper = FirecrawlScraper(client=mock_client, min_delay_ms=0, max_delay_ms=0)
            result = await scraper.extract("https://example.com/product")

            assert result is None

        asyncio.run(_run())

    def test_scrape_html_returns_scraped_page(self):
        async def _run():
            mock_client = AsyncMock()
            mock_doc = MagicMock()
            mock_doc.html = "<html><body><h1>Product</h1></body></html>"
            mock_doc.metadata = {"sourceURL": "https://example.com/product", "title": "Product"}
            mock_client.scrape.return_value = mock_doc

            scraper = FirecrawlScraper(client=mock_client, min_delay_ms=0, max_delay_ms=0)
            page = await scraper.scrape_html("https://example.com/product")

            assert page.html == "<html><body><h1>Product</h1></body></html>"
            assert page.success

        asyncio.run(_run())


class TestPipelineIntegration:
    def test_extract_produces_valid_extracted_facts(self):
        async def _run():
            mock_client = AsyncMock()
            mock_doc = MagicMock()
            mock_doc.json = {
                "product_name": "Alpine Pro Jacket",
                "brand": "Patagonia",
                "description_blocks": ["A versatile alpine jacket."],
                "feature_bullets": ["Waterproof", "Breathable", "Packable"],
                "specs": {"Weight": "340g", "Material": "Gore-Tex"},
                "images": ["https://cdn.example.com/jacket-front.jpg"],
                "colors": ["Blue", "Black"],
                "materials": "Gore-Tex Pro 3L",
                "price": "$399",
            }
            mock_doc.metadata = {"sourceURL": "https://patagonia.com/product/alpine-pro"}
            mock_client.scrape.return_value = mock_doc

            scraper = FirecrawlScraper(client=mock_client, min_delay_ms=0, max_delay_ms=0)
            facts = await scraper.extract("https://patagonia.com/product/alpine-pro")

            assert isinstance(facts, ExtractedFacts)
            assert facts.canonical_url == "https://patagonia.com/product/alpine-pro"
            assert facts.product_name == "Alpine Pro Jacket"
            assert len(facts.description_blocks) == 1
            assert len(facts.feature_bullets) == 3
            assert len(facts.specs) == 2
            assert len(facts.images) == 1
            assert facts.images[0].url == "https://cdn.example.com/jacket-front.jpg"
            assert facts.materials == "Gore-Tex Pro 3L"

        asyncio.run(_run())
