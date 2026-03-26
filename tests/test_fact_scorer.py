"""Tests for the fact quality scorer."""

import json
from pathlib import Path

import pytest

from lookout.enrich.models import ExtractedFacts
from lookout.enrich.fact_scorer import (
    AxisScore,
    QualityScore,
    _is_boilerplate,
    score_content_signal,
    score_deduplication,
    score_facts,
    score_facts_dir,
    score_field_completeness,
    score_specificity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_facts(**overrides) -> ExtractedFacts:
    """Build an ExtractedFacts with sensible defaults."""
    defaults = {
        "canonical_url": "https://vendor.example.com/product",
        "product_name": "Men's Nano Puff Jacket",
        "brand": "Patagonia",
        "description_blocks": [
            "Windproof and water-resistant, the Nano Puff Jacket uses 60-g PrimaLoft "
            "Gold Insulation Eco with 55% postconsumer recycled content."
        ],
        "feature_bullets": [
            "60-g PrimaLoft Gold Insulation Eco",
            "Windproof and water-resistant shell",
            "Zippered handwarmer pockets",
            "Internal zippered chest pocket",
            "Stuff-sack style internal chest pocket",
        ],
        "specs": {
            "Weight": "337 g (11.9 oz)",
            "Fit": "Regular",
            "Insulation": "60-g PrimaLoft Gold Insulation Eco",
        },
        "materials": "Shell: 1.4-oz 22-denier ripstop recycled polyester",
        "care": "Machine wash cold, tumble dry low",
    }
    defaults.update(overrides)
    return ExtractedFacts(**defaults)


def _make_boilerplate_facts() -> ExtractedFacts:
    """Facts dominated by boilerplate (like the I/O MAG extraction)."""
    return _make_facts(
        product_name="I/O MAG",
        brand="Smith Optics",
        description_blocks=[
            "Open media 1 in modal",
            "Open media 2 in modal",
            "Open media 3 in modal",
            "Skip to product information",
        ],
        feature_bullets=[
            "New Arrivals",
            "Best Sellers",
            "Gift Cards",
            "Shop All",
            "My Account",
            "Track My Order",
        ],
        specs={},
        materials="",
        care="",
    )


def _make_empty_facts() -> ExtractedFacts:
    """Completely empty facts."""
    return _make_facts(
        product_name="",
        brand="",
        description_blocks=[],
        feature_bullets=[],
        specs={},
        materials="",
        care="",
    )


# ---------------------------------------------------------------------------
# Boilerplate detection
# ---------------------------------------------------------------------------

class TestBoilerplateDetection:
    def test_open_media_modal(self):
        assert _is_boilerplate("Open media 1 in modal") is True

    def test_open_media_modal_large_number(self):
        assert _is_boilerplate("Open media 42 in modal") is True

    def test_new_arrivals(self):
        assert _is_boilerplate("New Arrivals") is True

    def test_best_sellers(self):
        assert _is_boilerplate("Best Sellers") is True

    def test_shop_all(self):
        assert _is_boilerplate("Shop All") is True

    def test_skip_to_content(self):
        assert _is_boilerplate("Skip to content") is True

    def test_pricing(self):
        assert _is_boilerplate("Regular price$59.95") is True

    def test_liquid_error(self):
        assert _is_boilerplate("Liquid error (snippets/price line 172)") is True

    def test_real_insulation_spec(self):
        assert _is_boilerplate("60-g PrimaLoft Gold Insulation") is False

    def test_real_dwr_finish(self):
        # Short but has uppercase acronym — not boilerplate
        assert _is_boilerplate("DWR finish") is False

    def test_short_with_digits(self):
        # Short but has digits — not boilerplate
        assert _is_boilerplate("60-g fill") is False

    def test_very_short_no_signal(self):
        assert _is_boilerplate("ok") is True

    def test_weve_got_our_hands_full(self):
        assert _is_boilerplate("We've got our hands full at the moment") is True

    def test_real_description_block(self):
        assert _is_boilerplate(
            "Windproof and water-resistant, the Nano Puff Jacket uses 60-g PrimaLoft"
        ) is False

    def test_follow_along(self):
        assert _is_boilerplate("Follow Along") is True

    def test_watch_now(self):
        assert _is_boilerplate("Watch Now") is True

    def test_javascript_required(self):
        assert _is_boilerplate("JavaScript required to view this page") is True

    def test_sale_price(self):
        assert _is_boilerplate("sale price $49.99") is True

    def test_starting_at(self):
        assert _is_boilerplate("Starting at $199") is True


# ---------------------------------------------------------------------------
# Axis 1: Content signal (0-30)
# ---------------------------------------------------------------------------

class TestContentSignal:
    def test_all_boilerplate(self):
        facts = _make_boilerplate_facts()
        axis = score_content_signal(facts)
        assert axis.score <= 3
        assert axis.max_score == 30

    def test_all_real(self):
        facts = _make_facts()
        axis = score_content_signal(facts)
        # 6 real items out of 6 total = 1.0 ratio -> score 28
        assert axis.score >= 28
        assert axis.max_score == 30

    def test_mixed_content(self):
        facts = _make_facts(
            description_blocks=[
                "Real description with useful content about the product features.",
                "Open media 1 in modal",
                "Skip to product information",
            ],
            feature_bullets=[
                "Waterproof 2L GORE-TEX membrane",
                "New Arrivals",
                "Best Sellers",
            ],
        )
        axis = score_content_signal(facts)
        # 2 real out of 6 = 0.33 -> score 15
        assert 8 <= axis.score <= 22

    def test_empty_content(self):
        facts = _make_empty_facts()
        axis = score_content_signal(facts)
        assert axis.score == 0

    def test_json_ld_rescue(self):
        facts = _make_boilerplate_facts()
        facts.json_ld_data = {
            "description": "The I/O MAG goggle features ChromaPop lens technology for enhanced clarity and contrast in all conditions."
        }
        axis = score_content_signal(facts)
        # Boilerplate score (low) + JSON-LD rescue bonus
        assert axis.score > 0
        assert "JSON-LD rescue" in str(axis.details)

    def test_json_ld_short_no_rescue(self):
        facts = _make_boilerplate_facts()
        facts.json_ld_data = {"description": "Short"}
        axis = score_content_signal(facts)
        assert "JSON-LD rescue" not in str(axis.details)


# ---------------------------------------------------------------------------
# Axis 2: Field completeness (0-25)
# ---------------------------------------------------------------------------

class TestFieldCompleteness:
    def test_fully_populated(self):
        facts = _make_facts()
        axis = score_field_completeness(facts)
        assert axis.score == 25
        assert axis.max_score == 25

    def test_empty_facts(self):
        facts = _make_empty_facts()
        axis = score_field_completeness(facts)
        assert axis.score == 0

    def test_partial_bullets(self):
        facts = _make_facts(feature_bullets=[
            "Waterproof shell",
            "Zippered pockets",
        ])
        axis = score_field_completeness(facts)
        # 4 (name) + 3 (brand) + 5 (desc) + 3 (2 bullets) + 4 (specs) + 2 (mat) + 2 (care) = 23
        assert axis.score == 23

    def test_single_bullet(self):
        facts = _make_facts(feature_bullets=["Waterproof shell"])
        axis = score_field_completeness(facts)
        # 4 + 3 + 5 + 1 + 4 + 2 + 2 = 21
        assert axis.score == 21

    def test_no_specs(self):
        facts = _make_facts(specs={})
        axis = score_field_completeness(facts)
        # 4 + 3 + 5 + 5 + 0 + 2 + 2 = 21
        assert axis.score == 21

    def test_boilerplate_blocks_not_counted(self):
        facts = _make_facts(
            description_blocks=["Open media 1 in modal", "Open media 2 in modal"],
        )
        axis = score_field_completeness(facts)
        # No real description blocks -> 0 for that axis
        assert "no real description blocks" in str(axis.details)


# ---------------------------------------------------------------------------
# Axis 3: Specificity (0-25)
# ---------------------------------------------------------------------------

class TestSpecificity:
    def test_name_in_content(self):
        facts = _make_facts()
        axis = score_specificity(facts)
        # Product name tokens (Nano, Puff, Jacket) should appear in content
        assert axis.score >= 7
        assert "product name tokens in content" in str(axis.details)

    def test_technical_terms(self):
        facts = _make_facts()
        axis = score_specificity(facts)
        # Has measurement units (60-g, 337 g), material terms (polyester),
        # acronyms (PrimaLoft, etc.)
        assert axis.score >= 13

    def test_substantive_specs(self):
        facts = _make_facts()
        axis = score_specificity(facts)
        # 3 specs with digits/values -> 6 pts
        assert "substantive specs" in str(axis.details)

    def test_materials_with_composition(self):
        facts = _make_facts(materials="55% recycled polyester, 45% nylon")
        axis = score_specificity(facts)
        assert "composition with %" in str(axis.details)

    def test_empty_materials(self):
        facts = _make_facts(materials="")
        axis = score_specificity(facts)
        assert "materials empty" in str(axis.details)

    def test_no_content(self):
        facts = _make_empty_facts()
        axis = score_specificity(facts)
        assert axis.score == 0

    def test_boilerplate_only(self):
        facts = _make_boilerplate_facts()
        axis = score_specificity(facts)
        # No real content -> low specificity
        assert axis.score < 10


# ---------------------------------------------------------------------------
# Axis 4: Deduplication (0-20)
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_no_dupes(self):
        facts = _make_facts()
        axis = score_deduplication(facts)
        assert axis.score == 20
        assert axis.max_score == 20

    def test_duplicate_blocks(self):
        facts = _make_facts(
            description_blocks=[
                "Same block of text here",
                "Same block of text here",
                "Different block of text here",
            ]
        )
        axis = score_deduplication(facts)
        # 1 dupe -> 8-2=6 for blocks, +6 bullets, +6 cross = 18
        assert axis.score == 18
        assert "duplicate blocks: 1" in str(axis.details)

    def test_near_duplicate_bullets(self):
        facts = _make_facts(
            feature_bullets=[
                "Waterproof GORE-TEX membrane shell with sealed seams",
                "Waterproof GORE-TEX membrane shell with taped seams",
                "Zippered handwarmer pockets with fleece lining",
            ]
        )
        axis = score_deduplication(facts)
        assert "near-duplicate bullet pairs" in str(axis.details)
        assert axis.score < 20

    def test_cross_field_repetition(self):
        shared = "Windproof and water-resistant shell with 60-g PrimaLoft Gold Insulation"
        facts = _make_facts(
            description_blocks=[shared + " Eco for maximum warmth"],
            feature_bullets=[shared + " for cold weather use"],
        )
        axis = score_deduplication(facts)
        assert "cross-field 6-gram collisions" in str(axis.details)
        assert axis.score < 20

    def test_empty_no_penalty(self):
        facts = _make_empty_facts()
        axis = score_deduplication(facts)
        # Empty = 8 + 6 + 6 = 20 (no duplicates possible)
        assert axis.score == 20


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

class TestScoreFacts:
    def test_good_facts(self):
        facts = _make_facts()
        qs = score_facts(facts)
        assert qs.total > 80
        assert qs.max_total == 100
        assert "content_signal" in qs.axes
        assert "field_completeness" in qs.axes
        assert "specificity" in qs.axes
        assert "deduplication" in qs.axes

    def test_bad_facts(self):
        facts = _make_boilerplate_facts()
        qs = score_facts(facts)
        assert qs.total < 40

    def test_empty_facts(self):
        facts = _make_empty_facts()
        qs = score_facts(facts)
        # Only deduplication gives full marks (nothing to dupe)
        assert qs.total <= 25

    def test_handle_from_url(self):
        facts = _make_facts()
        qs = score_facts(facts)
        assert qs.handle == "https://vendor.example.com/product"


# ---------------------------------------------------------------------------
# Batch: score_facts_dir
# ---------------------------------------------------------------------------

class TestScoreFactsDir:
    def test_scores_from_dir(self, tmp_path):
        handle = "test-product"
        handle_dir = tmp_path / handle
        handle_dir.mkdir()

        facts = _make_facts()
        (handle_dir / "extracted_facts.json").write_text(facts.model_dump_json())

        scores = score_facts_dir(tmp_path)
        assert len(scores) == 1
        assert scores[0].handle == handle
        assert scores[0].total > 0

    def test_filter_by_handles(self, tmp_path):
        for name in ["product-a", "product-b"]:
            d = tmp_path / name
            d.mkdir()
            facts = _make_facts()
            (d / "extracted_facts.json").write_text(facts.model_dump_json())

        scores = score_facts_dir(tmp_path, handles=["product-a"])
        assert len(scores) == 1
        assert scores[0].handle == "product-a"

    def test_skips_missing_facts(self, tmp_path):
        d = tmp_path / "no-facts"
        d.mkdir()
        # No extracted_facts.json
        scores = score_facts_dir(tmp_path, handles=["no-facts"])
        assert len(scores) == 0

    def test_multiple_products(self, tmp_path):
        for name in ["good-product", "bad-product"]:
            d = tmp_path / name
            d.mkdir()
            if name == "good-product":
                facts = _make_facts()
            else:
                facts = _make_boilerplate_facts()
            (d / "extracted_facts.json").write_text(facts.model_dump_json())

        scores = score_facts_dir(tmp_path)
        assert len(scores) == 2
        good = next(s for s in scores if s.handle == "good-product")
        bad = next(s for s in scores if s.handle == "bad-product")
        assert good.total > bad.total
