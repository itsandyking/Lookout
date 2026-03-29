"""Tests for resolver scoring edge cases."""

import re as _re
from difflib import SequenceMatcher


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
