"""Tests for resolver regression test runner."""


def test_rescore_candidates_ranks_correctly():
    from lookout.enrich.resolver import rescore_candidates

    candidates = [
        {"url": "https://example.com/product/reverb-youth-boots", "title": "Reverb Youth Ski Boots", "snippet": "Great boots for kids", "confidence": 50},
        {"url": "https://example.com/category/boots", "title": "All Boots", "snippet": "Browse boots", "confidence": 50},
    ]
    result = rescore_candidates(
        candidates=candidates,
        product_title="Youth Reverb Ski Boots 2024",
        vendor="TestVendor",
        domain="example.com",
    )
    # Product page should score higher than category page
    assert result[0]["url"] == "https://example.com/product/reverb-youth-boots"
    assert result[0]["rescored_confidence"] > result[1]["rescored_confidence"]


def test_rescore_empty_candidates():
    from lookout.enrich.resolver import rescore_candidates

    result = rescore_candidates(
        candidates=[],
        product_title="Test",
        vendor="V",
        domain="example.com",
    )
    assert result == []


def test_rescore_demographic_mismatch_penalized():
    from lookout.enrich.resolver import rescore_candidates

    candidates = [
        {"url": "https://example.com/product/reverb-youth", "title": "Reverb Youth Ski Boots", "snippet": "...", "confidence": 50},
        {"url": "https://example.com/product/revolve-womens", "title": "Revolve Women's Ski Boots", "snippet": "...", "confidence": 50},
    ]
    result = rescore_candidates(
        candidates=candidates,
        product_title="Youth Reverb Ski Boots",
        vendor="TestVendor",
        domain="example.com",
    )
    # Youth product should beat Women's product
    youth = next(c for c in result if "youth" in c["url"])
    womens = next(c for c in result if "womens" in c["url"])
    assert youth["rescored_confidence"] > womens["rescored_confidence"]


def test_rescore_price_match_boosts():
    from lookout.enrich.resolver import rescore_candidates

    candidates = [
        {"url": "https://example.com/product/jacket-a", "title": "Alpine Jacket", "snippet": "On sale for $299.99", "confidence": 50},
        {"url": "https://example.com/product/jacket-b", "title": "Alpine Jacket", "snippet": "Premium at $899.99", "confidence": 50},
    ]
    result = rescore_candidates(
        candidates=candidates,
        product_title="Alpine Jacket",
        vendor="TestVendor",
        domain="example.com",
        catalog_price=299.99,
    )
    # Price-matched candidate should score higher
    jacket_a = next(c for c in result if "jacket-a" in c["url"])
    jacket_b = next(c for c in result if "jacket-b" in c["url"])
    assert jacket_a["rescored_confidence"] > jacket_b["rescored_confidence"]


def test_rescore_preserves_original_fields():
    from lookout.enrich.resolver import rescore_candidates

    candidates = [
        {"url": "https://example.com/product/test", "title": "Test Product", "snippet": "A test", "confidence": 75, "extra_field": "keep_me"},
    ]
    result = rescore_candidates(
        candidates=candidates,
        product_title="Test Product",
        vendor="TestVendor",
        domain="example.com",
    )
    assert result[0]["extra_field"] == "keep_me"
    assert "rescored_confidence" in result[0]
    assert "rescore_reasoning" in result[0]
