"""Tests for Brave Image Search fallback."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lookout.enrich.brave_images import (
    BraveImageResolver,
    BraveImageResult,
    ImageMatch,
    normalize_color_for_query,
)
from lookout.enrich.models import BraveImagesSettings, GlobalSettings, ImageInfo


def run(coro):
    """Helper to run async tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestNormalizeColorForQuery:
    """Test color name normalization for Brave search queries."""

    # --- Lens / optics descriptions ---

    def test_strip_w_slash_lens(self):
        assert normalize_color_for_query("Black / Silver w/ Mirror Silver Lens") == "black silver"

    def test_strip_with_keyword(self):
        assert normalize_color_for_query("Matte Black with ChromaPop Sun Green Mirror") == "black"

    def test_strip_trailing_lens(self):
        assert normalize_color_for_query("Black Gold Mirror Lens") == "black gold"

    # --- Non-color modifiers ---

    def test_strip_rib(self):
        assert normalize_color_for_query("Black Rib") == "black"

    def test_strip_heather(self):
        assert normalize_color_for_query("Grey Heather") == "gray"

    def test_strip_melange(self):
        assert normalize_color_for_query("Blue Melange") == "blue"

    def test_strip_print(self):
        assert normalize_color_for_query("Floral Print Red") == "red"

    # --- Exotic color mapping ---

    def test_ceramic_to_beige(self):
        assert normalize_color_for_query("Ceramic") == "beige"

    def test_crimson_to_red(self):
        assert normalize_color_for_query("Crimson") == "red"

    def test_slate_to_gray(self):
        assert normalize_color_for_query("Slate") == "gray"

    def test_obsidian_to_black(self):
        assert normalize_color_for_query("Obsidian") == "black"

    def test_ivory_to_white(self):
        assert normalize_color_for_query("Ivory") == "white"

    def test_cobalt_to_blue(self):
        assert normalize_color_for_query("Cobalt") == "blue"

    def test_sage_to_green(self):
        assert normalize_color_for_query("Sage") == "green"

    def test_cognac_to_brown(self):
        assert normalize_color_for_query("Cognac") == "brown"

    # --- Combination rules ---

    def test_exotic_plus_modifier(self):
        assert normalize_color_for_query("Slate Heather") == "gray"

    def test_slash_separator_deduped(self):
        assert normalize_color_for_query("Black/Black") == "black"

    def test_multi_color_slash(self):
        result = normalize_color_for_query("Navy / Crimson")
        assert result == "navy crimson"

    # --- Passthrough ---

    def test_simple_color_passthrough(self):
        assert normalize_color_for_query("Black") == "black"

    def test_two_word_color_passthrough(self):
        assert normalize_color_for_query("Storm Blue") == "storm blue"

    def test_empty_after_stripping_returns_original(self):
        # If everything gets stripped, fall back to lowered original
        assert normalize_color_for_query("Print") == "print"

    # --- The query uses normalized color, not the original ---

    def test_pass2_query_uses_normalized_color(self):
        """Pass 2 targeted search should use the normalized color in its query."""
        resolver = BraveImageResolver(BraveImagesSettings(max_evaluate=1))

        captured_queries = []

        async def fake_search(query, count=None):
            captured_queries.append(query)
            return []

        resolver._search_brave_images = fake_search

        run(resolver._search_and_verify_color(
            vendor="Smith",
            product_title="I/O MAG S",
            color="Black / Silver w/ Mirror Silver Lens",
        ))

        assert len(captured_queries) == 1
        # Should contain normalized "black silver", not the raw lens description
        assert "black silver" in captured_queries[0]
        assert "Lens" not in captured_queries[0]
        assert "w/" not in captured_queries[0]


class TestBraveImagesSettings:
    def test_defaults(self):
        s = BraveImagesSettings()
        assert s.enabled is True
        assert s.ollama_host == "http://localhost:11434"
        assert s.ollama_model == "gemma4:e4b"
        assert s.max_candidates_per_color == 3
        assert s.min_image_dimensions == 400
        assert s.verify_timeout == 30
        assert s.brave_count == 50
        assert s.max_evaluate == 15

    def test_in_global_settings(self):
        gs = GlobalSettings()
        assert isinstance(gs.brave_images, BraveImagesSettings)


class TestImageInfoSource:
    def test_default_source_empty(self):
        img = ImageInfo(url="https://example.com/img.jpg")
        assert img.source == ""

    def test_source_set(self):
        img = ImageInfo(url="https://example.com/img.jpg", source="brave_image_search")
        assert img.source == "brave_image_search"


class TestBraveImageSearch:
    """Test Brave Image Search API integration."""

    def _resolver(self):
        return BraveImageResolver(BraveImagesSettings())

    def test_parse_results(self):
        """Brave API response is parsed into BraveImageResult objects."""
        fake_response = {
            "results": [
                {
                    "title": "Teva Verra Black Sandal",
                    "url": "https://example.com/product",
                    "source": "example.com",
                    "thumbnail": {"src": "https://imgs.brave.com/thumb.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://cdn.example.com/full.jpg", "width": 1200, "height": 1200},
                    "confidence": "high",
                },
                {
                    "title": "Unrelated Image",
                    "url": "https://other.com/page",
                    "source": "other.com",
                    "thumbnail": {"src": "https://imgs.brave.com/small.jpg", "width": 200, "height": 200},
                    "properties": {"url": "https://other.com/tiny.jpg", "width": 100, "height": 100},
                    "confidence": "low",
                },
            ]
        }
        resolver = self._resolver()
        results = resolver._parse_results(fake_response, min_dim=400)
        assert len(results) == 1
        assert results[0].full_url == "https://cdn.example.com/full.jpg"
        assert results[0].thumbnail_url == "https://imgs.brave.com/thumb.jpg"
        assert results[0].source_page == "https://example.com/product"
        assert results[0].title == "Teva Verra Black Sandal"

    def test_deduplicate_by_domain(self):
        """Only one image per source domain is kept."""
        fake_response = {
            "results": [
                {
                    "title": "Image 1", "url": "https://example.com/page1", "source": "example.com",
                    "thumbnail": {"src": "https://imgs.brave.com/t1.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://cdn.example.com/a.jpg", "width": 800, "height": 800},
                    "confidence": "high",
                },
                {
                    "title": "Image 2", "url": "https://example.com/page2", "source": "example.com",
                    "thumbnail": {"src": "https://imgs.brave.com/t2.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://cdn.example.com/b.jpg", "width": 800, "height": 800},
                    "confidence": "high",
                },
                {
                    "title": "Image 3", "url": "https://other.com/page", "source": "other.com",
                    "thumbnail": {"src": "https://imgs.brave.com/t3.jpg", "width": 500, "height": 500},
                    "properties": {"url": "https://other.com/c.jpg", "width": 800, "height": 800},
                    "confidence": "high",
                },
            ]
        }
        resolver = self._resolver()
        results = resolver._parse_results(fake_response, min_dim=400, dedupe_domains=True)
        assert len(results) == 2
        domains = {r.source_page.split("/")[2] for r in results}
        assert domains == {"example.com", "other.com"}


class TestParseVerifyResponse:
    """Test parsing of structured vision responses."""

    def test_all_pass(self):
        raw = "TYPE: DUFFEL BAG\nPRODUCT: YES\nECOMMERCE: YES\nCOLOR: Black"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is True
        assert result["product_match"] is True
        assert result["color_match"] is True
        assert result["ecommerce_suitable"] is True
        assert result["detected_color"] == "BLACK"
        assert result["detected_type"] == "DUFFEL BAG"

    def test_all_pass_without_type(self):
        """Backwards compatible — TYPE line is optional."""
        raw = "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Black"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is True
        assert result["detected_type"] == ""

    def test_product_no(self):
        raw = "TYPE: PANTS\nPRODUCT: NO\nECOMMERCE: YES\nCOLOR: Black"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False
        assert result["product_match"] is False
        assert result["detected_type"] == "PANTS"

    def test_ecommerce_no(self):
        raw = "TYPE: DUFFEL BAG\nPRODUCT: YES\nECOMMERCE: NO\nCOLOR: Black"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False
        assert result["ecommerce_suitable"] is False

    def test_color_mismatch(self):
        raw = "TYPE: SANDAL\nPRODUCT: YES\nECOMMERCE: YES\nCOLOR: Red"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False
        assert result["color_match"] is False

    def test_color_fuzzy_match_slash(self):
        raw = "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Purple"
        result = BraveImageResolver._parse_verify_response(raw, "Purple Ink/Purple Dusk")
        assert result["color_match"] is True
        assert result["accepted"] is True

    def test_color_fuzzy_match_multiword(self):
        raw = "PRODUCT: YES\nECOMMERCE: YES\nCOLOR: Blue"
        result = BraveImageResolver._parse_verify_response(raw, "Storm Blue")
        assert result["color_match"] is True

    def test_empty_response(self):
        result = BraveImageResolver._parse_verify_response("", "Black")
        assert result["accepted"] is False

    def test_garbled_response(self):
        raw = "I see a black sandal on a white background"
        result = BraveImageResolver._parse_verify_response(raw, "Black")
        assert result["accepted"] is False


class TestVerifyImage:
    """Test the full vision verification flow with mocked Ollama."""

    def _resolver(self):
        return BraveImageResolver(BraveImagesSettings())

    def test_accepted(self):
        resolver = self._resolver()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "response": "TYPE: SANDAL\nPRODUCT: YES\nECOMMERCE: YES\nCOLOR: Black"
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = run(resolver._verify_image(b"fake_img", "Verra Sandal", "Teva", "Black"))
            assert result["accepted"] is True

    def test_ollama_timeout(self):
        resolver = self._resolver()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client_cls.return_value = mock_client

            result = run(resolver._verify_image(b"fake_img", "Verra Sandal", "Teva", "Black"))
            assert result["accepted"] is False


class TestFindVariantImages:
    """Test the two-pass search flow."""

    def _resolver(self):
        return BraveImageResolver(BraveImagesSettings(max_evaluate=3))

    def test_pass1_matches_colors(self):
        resolver = self._resolver()

        async def fake_search(query, count=None):
            return [
                BraveImageResult(
                    full_url="https://cdn.ex.com/black.jpg", thumbnail_url="https://thumb.ex.com/black.jpg",
                    source_page="https://ex.com/p1", title="Verra Black", width=800, height=800, confidence="high",
                ),
                BraveImageResult(
                    full_url="https://cdn.ex.com/grey.jpg", thumbnail_url="https://thumb.ex.com/grey.jpg",
                    source_page="https://ex.com/p2", title="Verra Grey", width=800, height=800, confidence="high",
                ),
            ]

        call_count = {"verify": 0}
        async def fake_download(url):
            return b"fake_image_data"

        async def fake_verify(data, title, vendor, color):
            call_count["verify"] += 1
            if color == "Black" and call_count["verify"] <= 2:
                return {"accepted": True, "product_match": True, "color_match": True,
                        "ecommerce_suitable": True, "detected_color": "BLACK"}
            if color == "Grey" and call_count["verify"] > 2:
                return {"accepted": True, "product_match": True, "color_match": True,
                        "ecommerce_suitable": True, "detected_color": "GREY"}
            return {"accepted": False, "product_match": True, "color_match": False,
                    "ecommerce_suitable": True, "detected_color": "OTHER"}

        resolver._search_brave_images = fake_search
        resolver._download_thumbnail = fake_download
        resolver._verify_image = fake_verify

        result = run(resolver.find_variant_images("Verra Sandal", "Teva", ["Black", "Grey"]))
        assert "Black" in result
        assert result["Black"].url == "https://cdn.ex.com/black.jpg"

    def test_pass2_targeted_search(self):
        resolver = self._resolver()

        search_queries = []
        async def fake_search(query, count=None):
            search_queries.append(query)
            # After normalization, "Rare Color" becomes "rare color" (lowercased)
            if "rare color" in query.lower():
                return [
                    BraveImageResult(
                        full_url="https://cdn.ex.com/rare.jpg", thumbnail_url="https://thumb.ex.com/rare.jpg",
                        source_page="https://ex.com/rare", title="Verra Rare Color",
                        width=800, height=800, confidence="high",
                    ),
                ]
            return []

        async def fake_download(url):
            return b"fake_image_data"

        async def fake_verify(data, title, vendor, color):
            return {"accepted": True, "product_match": True, "color_match": True,
                    "ecommerce_suitable": True, "detected_color": color.upper()}

        resolver._search_brave_images = fake_search
        resolver._download_thumbnail = fake_download
        resolver._verify_image = fake_verify

        result = run(resolver.find_variant_images("Verra Sandal", "Teva", ["Rare Color"]))

        assert len(search_queries) == 2
        assert "Teva Verra Sandal" in search_queries[0]
        assert "rare color" in search_queries[1].lower()
        assert "Rare Color" in result

    def test_empty_colors(self):
        resolver = self._resolver()
        result = run(resolver.find_variant_images("Verra", "Teva", []))
        assert result == {}


class TestTier2cIntegration:
    """Test that Tier 2c plugs into _assign_variant_images correctly."""

    def test_tier2c_called_when_2a_2b_fail(self):
        """When vision and LLM tiers fail, Brave fallback is tried."""
        from lookout.enrich.generator import Generator
        from lookout.enrich.models import ExtractedFacts, ImageInfo, VariantOption

        # Build facts with images but no variant_image_candidates
        facts = ExtractedFacts(
            canonical_url="https://example.com/womens-verra",
            product_name="Women's Verra",
            variants=[VariantOption(option_name="Color", values=["Black", "Grey"])],
            images=[ImageInfo(url="https://example.com/hero.jpg")],
        )

        # Create Generator with mocked LLM that returns nothing, and a brave resolver
        gen = Generator.__new__(Generator)
        gen.llm_client = MagicMock()
        gen.llm_client.select_variant_images_vision = AsyncMock(return_value={})
        gen.llm_client.select_variant_images = AsyncMock(return_value={})

        mock_brave = MagicMock()
        mock_brave.find_variant_images = AsyncMock(return_value={
            "Black": ImageMatch(
                url="https://cdn.ex.com/black.jpg",
                thumbnail_url="https://thumb.ex.com/black.jpg",
                source_page="https://ex.com/p",
                color="Black",
                detected_color="BLACK",
                vision_verified=True,
            ),
        })
        gen.brave_resolver = mock_brave

        variant_map, warnings = run(gen._assign_variant_images(facts))

        mock_brave.find_variant_images.assert_called_once()
        assert "Black" in variant_map
        assert variant_map["Black"] == "https://cdn.ex.com/black.jpg"

    def test_tier2c_skipped_when_no_resolver(self):
        """Without brave_resolver, falls through to Tier 0."""
        from lookout.enrich.generator import Generator
        from lookout.enrich.models import ExtractedFacts, ImageInfo, VariantOption

        facts = ExtractedFacts(
            canonical_url="https://example.com/womens-verra",
            product_name="Women's Verra",
            variants=[VariantOption(option_name="Color", values=["Black"])],
            images=[ImageInfo(url="https://example.com/hero.jpg")],
        )

        gen = Generator.__new__(Generator)
        gen.llm_client = MagicMock()
        gen.llm_client.select_variant_images_vision = AsyncMock(return_value={})
        gen.llm_client.select_variant_images = AsyncMock(return_value={})
        gen.brave_resolver = None

        variant_map, warnings = run(gen._assign_variant_images(facts))

        # Should NOT assign hero to color products — better no image than wrong image
        assert "__all__" not in variant_map
        assert "COLOR_MATCHING_FAILED_NO_IMAGES_ASSIGNED" in warnings


class TestProductImageFallback:
    """Test product image fallback when extraction yields few images."""

    def test_brave_fills_missing_images(self):
        from lookout.enrich.generator import Generator
        from lookout.enrich.models import ExtractedFacts

        facts = ExtractedFacts(
            product_name="Tikka Core Headlamp",
            brand="Petzl",
            canonical_url="https://petzl.com/tikka",
            images=[],
        )

        gen = Generator.__new__(Generator)
        gen.brave_resolver = MagicMock()
        gen.brave_resolver.find_product_images = AsyncMock(return_value=[
            BraveImageResult(
                full_url="https://cdn.ex.com/tikka.jpg",
                thumbnail_url="https://thumb.ex.com/tikka.jpg",
                source_page="https://ex.com/tikka",
                title="Petzl Tikka Core",
                width=1000, height=1000, confidence="high",
            ),
        ])

        images, warnings = run(gen._select_images(facts))

        gen.brave_resolver.find_product_images.assert_called()
        assert len(images) >= 1
        assert images[0].src == "https://cdn.ex.com/tikka.jpg"
        assert "NO_IMAGES_FOUND" not in warnings

    def test_no_fallback_when_enough_images(self):
        from lookout.enrich.generator import Generator
        from lookout.enrich.models import ExtractedFacts, ImageInfo

        facts = ExtractedFacts(
            product_name="Some Product",
            brand="Some Brand",
            canonical_url="https://example.com/product",
            images=[
                ImageInfo(url=f"https://ex.com/img{i}.jpg")
                for i in range(5)
            ],
        )

        gen = Generator.__new__(Generator)
        gen.brave_resolver = MagicMock()

        images, warnings = run(gen._select_images(facts))

        # Should not call brave since we have >= 3 images
        gen.brave_resolver.find_product_images.assert_not_called()
        assert len(images) == 5


import json
import tempfile
from pathlib import Path


class TestMatchDecisionLogging:
    """Test that Brave image search results are logged in match_decisions."""

    def test_brave_field_in_decision_record(self):
        from lookout.enrich.match_validator import MatchDecisionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            decision_logger = MatchDecisionLogger(Path(tmpdir) / "match_decisions.jsonl")
            decision_logger.log(
                handle="womens-verra",
                vendor="Teva",
                catalog_title="Women's Verra",
                catalog_price=75.0,
                catalog_colors=["Black", "Grey"],
                candidates_tried=[],
                outcome="no_match",
                final_url=None,
                brave_image_search={
                    "colors_searched": ["Black", "Grey"],
                    "colors_matched": ["Black"],
                    "candidates_evaluated": 6,
                    "images_accepted": 1,
                },
            )

            decisions_file = Path(tmpdir) / "match_decisions.jsonl"
            assert decisions_file.exists()
            record = json.loads(decisions_file.read_text().strip())
            assert record["brave_image_search"]["colors_matched"] == ["Black"]
            assert record["brave_image_search"]["images_accepted"] == 1

    def test_no_brave_field_when_not_provided(self):
        from lookout.enrich.match_validator import MatchDecisionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            decision_logger = MatchDecisionLogger(Path(tmpdir) / "match_decisions.jsonl")
            decision_logger.log(
                handle="some-product",
                vendor="SomeVendor",
                catalog_title="Some Product",
                catalog_price=50.0,
                catalog_colors=["Red"],
                candidates_tried=[],
                outcome="accept",
                final_url="https://example.com",
            )

            decisions_file = Path(tmpdir) / "match_decisions.jsonl"
            record = json.loads(decisions_file.read_text().strip())
            assert "brave_image_search" not in record
