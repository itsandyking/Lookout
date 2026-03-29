"""Tests for post-scrape match validation."""


def test_extract_page_title_from_heading():
    from lookout.enrich.match_validator import extract_page_title
    md = "# Reverb Youth Ski Boots 2026\n\nSome content here."
    assert extract_page_title(md) == "Reverb Youth Ski Boots 2026"


def test_extract_page_title_from_h2_when_no_h1():
    from lookout.enrich.match_validator import extract_page_title
    md = "## Alp Trainer 2 Low GORE-TEX\n\nSome content."
    assert extract_page_title(md) == "Alp Trainer 2 Low GORE-TEX"


def test_extract_page_title_none_for_empty():
    from lookout.enrich.match_validator import extract_page_title
    assert extract_page_title("") is None
    assert extract_page_title("No headings here, just text.") is None


def test_extract_page_title_picks_best_heading():
    """When catalog title is given, picks the heading with highest word overlap."""
    from lookout.enrich.match_validator import extract_page_title
    md = (
        "# Product Description\n\n"
        "## YOUR CART\n\n"
        "## Men's Cloudrock Low WP Hiking Boot\n\n"
        "### Features\n\n"
    )
    result = extract_page_title(md, catalog_title="Men's Cloudrock Low WP")
    assert result == "Men's Cloudrock Low WP Hiking Boot"


def test_extract_page_title_returns_none_when_no_overlap():
    """No heading overlaps with catalog title → return None."""
    from lookout.enrich.match_validator import extract_page_title
    md = "# YOUR CART\n\n## Frequently Bought Together\n\n## Best Sellers\n"
    result = extract_page_title(md, catalog_title="Men's Cloudrock Low WP")
    assert result is None


def test_title_gate_pass():
    from lookout.enrich.match_validator import check_title_gate
    result = check_title_gate("Reverb Youth Ski Boots", "Youth Reverb Ski Boots 2024")
    assert result["pass"] is True
    assert result["title_similarity"] > 0.5


def test_title_gate_reject_low_similarity():
    from lookout.enrich.match_validator import check_title_gate
    result = check_title_gate("Mountain Camping Stove Deluxe", "Youth Reverb Ski Boots 2024")
    assert result["pass"] is False
    assert result["title_similarity"] < 0.4


def test_title_gate_reject_demographic_mismatch():
    from lookout.enrich.match_validator import check_title_gate
    result = check_title_gate("Reverb Women's Ski Boots", "Youth Reverb Ski Boots 2024")
    assert result["pass"] is False
    assert result["demographic_match"] is False


def test_title_gate_no_demographics():
    from lookout.enrich.match_validator import check_title_gate
    result = check_title_gate("Foamy Sleeping Pad", "Foamy Sleeping Pad")
    assert result["pass"] is True
    assert result["demographic_match"] is None


def test_post_extraction_pass_strong_signals():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts, VariantOption

    facts = ExtractedFacts(
        canonical_url="https://example.com/reverb-youth",
        product_name="Reverb Youth Ski Boots",
        images=[],
        variants=[VariantOption(option_name="Color", values=["Black", "Blue"])],
        json_ld_data={"offers": {"price": "199.99"}},
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Youth Reverb Ski Boots 2024",
        catalog_price=199.99,
        catalog_colors=["Black", "White/Blue"],
    )
    assert result["pass"] is True
    assert result["confidence"] >= 50


def test_post_extraction_fail_wrong_product():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts, VariantOption

    facts = ExtractedFacts(
        canonical_url="https://example.com/different",
        product_name="Completely Different Product",
        images=[],
        variants=[VariantOption(option_name="Color", values=["Red", "Green"])],
        json_ld_data={"offers": {"price": "49.99"}},
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Youth Reverb Ski Boots 2024",
        catalog_price=199.99,
        catalog_colors=["Black", "White/Blue"],
    )
    assert result["pass"] is False
    assert result["confidence"] < 50


def test_post_extraction_missing_signals_neutral():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        canonical_url="https://example.com/reverb-youth",
        product_name="Reverb Youth Ski Boots",
        images=[],
        variants=[],
        json_ld_data=None,
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Youth Reverb Ski Boots 2024",
        catalog_price=None,
        catalog_colors=[],
    )
    assert result["pass"] is True
    assert result["confidence"] >= 50
