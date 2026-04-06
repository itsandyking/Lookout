"""Tests for resolver scoring edge cases."""

import re as _re
from difflib import SequenceMatcher
from unittest.mock import MagicMock

from lookout.enrich.resolver import URLCandidate


def _extract_words(text: str) -> set[str]:
    """Extract lowercase alphanumeric words from text."""
    return set(_re.findall(r"[a-z0-9]+", text.lower()))


def test_asymmetric_height_candidate_has_mid_expected_has_none():
    expected = "Men's Alp Trainer GTX"
    candidate = "Alp Trainer 2 Mid GORE-TEX Men's Shoe"
    height_fit_words = {"low", "mid", "high", "tall", "short", "wide", "narrow"}
    expected_words = _extract_words(expected)
    candidate_words = _extract_words(candidate)
    extra_words = candidate_words - expected_words
    extra_height = extra_words & height_fit_words
    has_height_in_expected = bool(expected_words & height_fit_words)
    assert extra_height == {"mid"}
    assert not has_height_in_expected


def test_symmetric_height_both_have_words():
    expected = "Alp Trainer Low GTX"
    candidate = "Alp Trainer Mid GTX"
    height_fit_words = {"low", "mid", "high", "tall", "short", "wide", "narrow"}
    expected_words = _extract_words(expected)
    candidate_words = _extract_words(candidate)
    missing_height = (expected_words - candidate_words) & height_fit_words
    extra_height = (candidate_words - expected_words) & height_fit_words
    assert missing_height == {"low"}
    assert extra_height == {"mid"}


def test_no_height_words_no_penalty():
    expected = "Foamy Sleeping Pad"
    height_fit_words = {"low", "mid", "high", "tall", "short", "wide", "narrow"}
    expected_words = _extract_words(expected)
    assert not (expected_words & height_fit_words)


def test_year_mismatch_not_critical():
    expected_title = "Youth Reverb Ski Boots 2024"
    candidate_title = "Reverb Youth Ski Boots 2026"
    expected_years = set(_re.findall(r"20[2-3]\d", expected_title))
    candidate_years = set(_re.findall(r"20[2-3]\d", candidate_title))
    assert expected_years == {"2024"}
    assert candidate_years == {"2026"}


def test_model_number_mismatch_is_critical():
    expected_title = "99Ti Skis"
    candidate_title = "95 Skis"
    year_re = _re.compile(r"20[2-3]\d")
    expected_non_year = set(_re.findall(r"\d+", expected_title)) - set(year_re.findall(expected_title))
    candidate_non_year = set(_re.findall(r"\d+", candidate_title)) - set(year_re.findall(candidate_title))
    assert expected_non_year == {"99"}
    assert candidate_non_year == {"95"}
    assert not (expected_non_year & candidate_non_year)


def test_demographic_mismatch_youth_vs_womens():
    demographics = {"youth", "kids", "boys", "girls", "mens", "men", "womens", "women", "unisex"}
    expected_words = _extract_words("Youth Reverb Ski Boots")
    candidate_words = _extract_words("Revolve TBL Women's Ski Boots")
    expected_demos = expected_words & demographics
    candidate_demos = candidate_words & demographics
    assert expected_demos == {"youth"}
    assert candidate_demos == {"women"}
    assert not (expected_demos & candidate_demos)


def test_demographic_match_no_penalty():
    demographics = {"youth", "kids", "boys", "girls", "mens", "men", "womens", "women", "unisex"}
    expected_words = _extract_words("Men's Cloudrock Low WP")
    candidate_words = _extract_words("Men's Cloudrock Low Waterproof")
    expected_demos = expected_words & demographics
    candidate_demos = candidate_words & demographics
    assert expected_demos & candidate_demos


# --- Near-homonym model name detection ---

def test_near_homonym_reverb_vs_revolve():
    """Reverb and Revolve are near-homonyms (0.67 similarity) — different products."""
    sim = SequenceMatcher(None, "reverb", "revolve").ratio()
    assert sim >= 0.55, f"Expected >=0.55, got {sim}"
    assert "reverb" != "revolve"


def test_near_homonym_recon_vs_react():
    """Recon and React — another near-homonym pair in ski boots."""
    sim = SequenceMatcher(None, "recon", "react").ratio()
    assert sim >= 0.55, f"Expected >=0.55, got {sim}"


def test_not_near_homonym_reverb_vs_protac():
    """Reverb and Protac are not near-homonyms — low similarity."""
    sim = SequenceMatcher(None, "reverb", "protac").ratio()
    assert sim < 0.55, f"Expected <0.55, got {sim}"


def _score_candidate(expected_title: str, candidate_title: str,
                     vendor: str = "K2", initial_confidence: int = 100) -> URLCandidate:
    """Run the resolver's title-match scoring logic on a single candidate.

    Extracts the scoring section from ProductResolver.resolve() so we can
    test it in isolation.
    """
    filler = {"the", "a", "an", "by", "for", "in", "of", "and", "with"}
    title_lower = expected_title.lower()
    title_words = set(_re.findall(r'[a-z0-9]+', title_lower)) - filler

    candidate = URLCandidate(
        url="https://example.com/product",
        confidence=initial_confidence,
        title=candidate_title,
    )
    candidate_lower = candidate_title.lower()
    candidate_words = set(_re.findall(r'[a-z0-9]+', candidate_lower)) - filler

    overlap = title_words & candidate_words
    overlap_ratio = len(overlap) / len(title_words) if title_words else 0
    seq_ratio = SequenceMatcher(None, title_lower, candidate_lower).ratio()

    missing_words = title_words - candidate_words
    extra_words = candidate_words - title_words

    # Critical mismatch checks
    critical_mismatch = False
    type_words = {"ski", "skis", "boot", "boots", "shoe", "shoes",
                  "jacket", "pants", "helmet", "goggles", "sunglasses",
                  "gloves", "pole", "poles", "binding", "bindings",
                  "board", "snowboard",
                  "pad", "bundle", "pack", "kit", "set", "system", "combo"}
    height_fit_words = {"low", "mid", "high", "tall", "short", "wide", "narrow"}
    edition_words = {"standard", "pro", "plus", "lite", "max",
                     "mini", "ultra", "evo", "comp"}
    demographics = {"youth", "kids", "boys", "girls", "mens", "men",
                    "womens", "women", "unisex", "junior", "jr"}

    # Demographic penalty
    expected_demos = title_words & demographics
    candidate_demos = candidate_words & demographics
    if expected_demos and candidate_demos:
        if not expected_demos & candidate_demos:
            candidate.confidence = max(0, candidate.confidence - 15)
            candidate.reasoning += " -demographic_mismatch"

    # Foreign product / near-homonym detection
    generic_words = type_words | height_fit_words | edition_words | demographics | {
        "rope", "ropes", "cord", "ski", "skis", "boot", "boots",
        "new", "sale", "2024", "2025", "2026", "2027",
    }
    vendor_words = set(_re.findall(r'[a-z0-9]+', vendor.lower())) if vendor else set()
    generic_words |= vendor_words

    foreign_names = extra_words - generic_words - set(_re.findall(r'\d+', candidate_lower))
    missing_names = missing_words - generic_words - set(_re.findall(r'\d+', title_lower))

    has_foreign_product = False
    if foreign_names and missing_names:
        has_foreign_product = True
        near_homonym = False
        for fn in foreign_names:
            for mn in missing_names:
                pair_sim = SequenceMatcher(None, fn, mn).ratio()
                if pair_sim >= 0.55 and fn != mn:
                    near_homonym = True
                    break
            if near_homonym:
                break

        if near_homonym:
            candidate.confidence = max(0, candidate.confidence - 40)
            candidate.reasoning += f" -near_homonym"
        else:
            candidate.confidence = max(0, candidate.confidence - 35)
            candidate.reasoning += f" -foreign_product"

    if critical_mismatch:
        candidate.confidence = max(0, candidate.confidence - 30)
        candidate.reasoning += " -critical_mismatch"
    elif has_foreign_product:
        pass  # No boost for foreign products
    elif overlap_ratio >= 0.6 or seq_ratio >= 0.5:
        boost = int(20 * overlap_ratio)
        candidate.confidence = min(100, candidate.confidence + boost)
        candidate.reasoning += f" +title_match({overlap_ratio:.0%})"

    return candidate


def test_reverb_vs_revolve_killed():
    """Youth Reverb should NOT match Revolve TBL Women's — near-homonym + demographic."""
    result = _score_candidate(
        "Youth Reverb Ski Boots 2024",
        "K2 Revolve TBL Women's Ski Boots 2024",
        vendor="K2",
    )
    # Should be heavily penalized: -15 (demo) -40 (near-homonym) = 45
    assert result.confidence <= 50, f"Expected <=50, got {result.confidence}"
    assert "-near_homonym" in result.reasoning
    assert "-demographic_mismatch" in result.reasoning


def test_reverb_vs_reverb_different_year_still_ok():
    """Same model name different year should still score well."""
    result = _score_candidate(
        "Youth Reverb Ski Boots 2024",
        "K2 Youth Reverb Ski Boots 2026",
        vendor="K2",
    )
    # Same model — should get a boost, not a penalty
    assert result.confidence >= 80, f"Expected >=80, got {result.confidence}"
    assert "-near_homonym" not in result.reasoning
    assert "-foreign_product" not in result.reasoning


def test_foreign_product_no_boost():
    """A candidate with a different product name should NOT get a title_match boost
    even if seq_ratio is high."""
    result = _score_candidate(
        "Youth Reverb Ski Boots 2024",
        "K2 Revolve TBL Women's Ski Boots 2024",
        vendor="K2",
    )
    assert "+title_match" not in result.reasoning


def test_exact_match_still_boosted():
    """An exact product match should still get a boost."""
    result = _score_candidate(
        "Reverb Youth Ski Boots 2024",
        "K2 Reverb Youth Ski Boots 2024",
        vendor="K2",
    )
    assert "+title_match" in result.reasoning
    assert result.confidence >= 100


def test_completely_different_product_penalized():
    """Completely different product names get foreign_product penalty."""
    result = _score_candidate(
        "BWII Climbing Rope",
        "Protac Dynamic Rope",
        vendor="Edelrid",
    )
    assert result.confidence <= 70, f"Expected <=70, got {result.confidence}"
    assert "-foreign_product" in result.reasoning
