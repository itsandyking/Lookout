"""Tests for the audit weight optimization system."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from lookout.audit.models import ProductScore
from lookout.audit.priority_fn import compute_priority, rank_scores
from lookout.audit.weight_config import BOUNDS, PriorityWeights, _CONTINUOUS_PARAMS
from lookout.audit.weight_optimizer import (
    _spearman,
    compute_spearman,
    load_expert_ranking,
    load_snapshot,
    save_snapshot,
)


# ---------------------------------------------------------------------------
# PriorityWeights
# ---------------------------------------------------------------------------


class TestPriorityWeights:
    def test_to_dict_from_dict_roundtrip(self):
        w = PriorityWeights(gap_image=2.0, session_scale=500.0, inventory_transform="log")
        d = w.to_dict()
        w2 = PriorityWeights.from_dict(d)
        assert w == w2

    def test_from_dict_ignores_unknown_keys(self):
        d = PriorityWeights().to_dict()
        d["unknown_key"] = 999
        w = PriorityWeights.from_dict(d)
        assert not hasattr(w, "unknown_key") or getattr(w, "unknown_key", None) is None

    def test_to_array_length(self):
        w = PriorityWeights()
        arr = w.to_array()
        assert len(arr) == 8

    def test_to_array_from_array_roundtrip(self):
        w = PriorityWeights(
            gap_image=1.5, gap_variant_images=2.0, gap_description=0.8,
            gap_type=1.0, gap_tags=0.3, session_scale=200.0,
            impression_scale=5000.0, disapproval_boost=1.2,
        )
        arr = w.to_array()
        w2 = PriorityWeights.from_array(arr)
        assert w2.gap_image == pytest.approx(1.5)
        assert w2.gap_variant_images == pytest.approx(2.0)
        assert w2.session_scale == pytest.approx(200.0)

    def test_from_array_clamps_to_bounds(self):
        # Values outside bounds should be clamped
        arr = [-10.0, 50.0, 1.0, 1.0, 1.0, 5.0, 50.0, 10.0]
        w = PriorityWeights.from_array(arr)
        assert w.gap_image == 0.0  # clamped to lower bound
        assert w.gap_variant_images == 3.0  # clamped to upper bound
        assert w.session_scale == 10.0  # clamped to lower bound
        assert w.impression_scale == 100.0  # clamped to lower bound
        assert w.disapproval_boost == 2.0  # clamped to upper bound

    def test_from_array_wrong_length_raises(self):
        with pytest.raises(ValueError, match="Expected 8"):
            PriorityWeights.from_array([1.0, 2.0])

    def test_default_weights_match_model_constants(self):
        """Default PriorityWeights should match the hardcoded values in models.py."""
        w = PriorityWeights()
        assert w.gap_image == 1.0
        assert w.gap_variant_images == 1.0
        assert w.gap_description == 1.0
        assert w.gap_type == 0.5
        assert w.gap_tags == 0.5
        assert w.session_scale == 100.0
        assert w.impression_scale == 1000.0
        assert w.disapproval_boost == 0.5


# ---------------------------------------------------------------------------
# compute_priority — matches calculate_gaps()
# ---------------------------------------------------------------------------


def _make_score(**kwargs) -> ProductScore:
    """Helper to build a ProductScore with sensible defaults."""
    defaults = dict(
        product_id=1, handle="test-product", title="Test", vendor="TestVendor",
        product_type="Jacket",
    )
    defaults.update(kwargs)
    return ProductScore(**defaults)


class TestComputePriority:
    def test_no_gaps_returns_zero(self):
        s = _make_score(
            has_product_image=True, has_all_variant_images=True,
            has_description=True, has_product_type=True, has_tags=True,
            inventory_value=1000.0,
        )
        assert compute_priority(s, PriorityWeights()) == 0.0

    def test_matches_calculate_gaps_simple(self):
        """Priority with default weights must match calculate_gaps()."""
        s = _make_score(
            has_product_image=False, has_description=False,
            has_product_type=False, has_tags=False,
            has_all_variant_images=True,
            inventory_value=500.0,
            full_price_inventory_value=250.0,
        )
        s.calculate_gaps()

        p = compute_priority(s, PriorityWeights())
        assert p == pytest.approx(s.priority_score, rel=1e-9)

    def test_matches_calculate_gaps_with_variant_images(self):
        s = _make_score(
            has_product_image=True, has_description=True,
            has_product_type=True, has_tags=True,
            has_all_variant_images=False,
            variant_count=4, variants_missing_images=2,
            inventory_value=1000.0,
            full_price_inventory_value=1000.0,
        )
        s.calculate_gaps()

        p = compute_priority(s, PriorityWeights())
        assert p == pytest.approx(s.priority_score, rel=1e-9)

    def test_matches_calculate_gaps_with_online_signals(self):
        s = _make_score(
            has_product_image=False, has_description=True,
            has_product_type=True, has_tags=True,
            has_all_variant_images=True,
            inventory_value=800.0,
            online_sessions=50, opportunity_gap=0.7,
        )
        s.calculate_gaps()

        p = compute_priority(s, PriorityWeights())
        assert p == pytest.approx(s.priority_score, rel=1e-9)

    def test_matches_calculate_gaps_with_gmc_signals(self):
        s = _make_score(
            has_product_image=False, has_description=True,
            has_product_type=True, has_tags=True,
            has_all_variant_images=True,
            inventory_value=600.0,
            gmc_impressions=500, discovery_gap=0.4,
            gmc_disapproved=True,
        )
        s.calculate_gaps()

        p = compute_priority(s, PriorityWeights())
        assert p == pytest.approx(s.priority_score, rel=1e-9)

    def test_matches_calculate_gaps_all_signals(self):
        """Full model with all signal types enabled."""
        s = _make_score(
            has_product_image=False, has_description=False,
            has_product_type=True, has_tags=False,
            has_all_variant_images=False,
            variant_count=6, variants_missing_images=3,
            inventory_value=2000.0,
            full_price_inventory_value=1500.0,
            online_sessions=120, opportunity_gap=0.85,
            gmc_impressions=3000, discovery_gap=0.6,
            gmc_disapproved=True,
        )
        s.calculate_gaps()

        p = compute_priority(s, PriorityWeights())
        assert p == pytest.approx(s.priority_score, rel=1e-9)

    def test_custom_weights_differ_from_default(self):
        s = _make_score(
            has_product_image=False, has_description=False,
            inventory_value=1000.0,
        )
        default_p = compute_priority(s, PriorityWeights())
        custom = PriorityWeights(gap_image=2.5, gap_description=0.1)
        custom_p = compute_priority(s, custom)
        assert custom_p != default_p


# ---------------------------------------------------------------------------
# rank_scores
# ---------------------------------------------------------------------------


class TestRankScores:
    def test_returns_handles_in_priority_order(self):
        s1 = _make_score(handle="low", has_product_image=False, inventory_value=100.0)
        s2 = _make_score(handle="high", has_product_image=False, inventory_value=1000.0)
        s3 = _make_score(handle="complete", inventory_value=5000.0)  # no gaps

        result = rank_scores([s1, s2, s3], PriorityWeights())
        assert result == ["high", "low"]

    def test_excludes_zero_gap_products(self):
        s = _make_score(handle="perfect", inventory_value=9999.0)
        result = rank_scores([s], PriorityWeights())
        assert result == []


# ---------------------------------------------------------------------------
# Spearman correlation
# ---------------------------------------------------------------------------


class TestSpearman:
    def test_perfect_correlation(self):
        assert _spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self):
        assert _spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)

    def test_zero_correlation(self):
        # [1,2,3,4,5] vs [3,5,1,4,2] has known Spearman
        r = _spearman([1, 2, 3, 4, 5], [3, 5, 1, 4, 2])
        assert -1.0 <= r <= 1.0

    def test_single_element_returns_zero(self):
        assert _spearman([1], [1]) == 0.0

    def test_empty_returns_zero(self):
        assert _spearman([], []) == 0.0


class TestComputeSpearman:
    def test_perfect_match(self):
        """If formula ranking matches expert ranking, correlation = 1.0."""
        # Products with decreasing inventory value -> decreasing priority
        scores = [
            _make_score(handle="a", has_product_image=False, inventory_value=300.0),
            _make_score(handle="b", has_product_image=False, inventory_value=200.0),
            _make_score(handle="c", has_product_image=False, inventory_value=100.0),
        ]
        expert = ["a", "b", "c"]
        corr = compute_spearman(scores, PriorityWeights(), expert)
        assert corr == pytest.approx(1.0)

    def test_reverse_ranking(self):
        scores = [
            _make_score(handle="a", has_product_image=False, inventory_value=300.0),
            _make_score(handle="b", has_product_image=False, inventory_value=200.0),
            _make_score(handle="c", has_product_image=False, inventory_value=100.0),
        ]
        expert = ["c", "b", "a"]
        corr = compute_spearman(scores, PriorityWeights(), expert)
        assert corr == pytest.approx(-1.0)

    def test_no_overlap_returns_zero(self):
        scores = [
            _make_score(handle="a", has_product_image=False, inventory_value=100.0),
        ]
        expert = ["x", "y", "z"]
        corr = compute_spearman(scores, PriorityWeights(), expert)
        assert corr == 0.0


# ---------------------------------------------------------------------------
# load_expert_ranking
# ---------------------------------------------------------------------------


class TestLoadExpertRanking:
    def test_handles_comments_and_blanks(self, tmp_path):
        ranking_file = tmp_path / "ranking.txt"
        ranking_file.write_text(
            "# This is a comment\n"
            "product-a\n"
            "\n"
            "# Another comment\n"
            "product-b\n"
            "product-c\n"
            "\n"
        )
        result = load_expert_ranking(ranking_file)
        assert result == ["product-a", "product-b", "product-c"]

    def test_empty_file(self, tmp_path):
        ranking_file = tmp_path / "empty.txt"
        ranking_file.write_text("")
        assert load_expert_ranking(ranking_file) == []


# ---------------------------------------------------------------------------
# save_snapshot / load_snapshot roundtrip
# ---------------------------------------------------------------------------


class TestSnapshotRoundtrip:
    def test_roundtrip(self, tmp_path):
        scores = [
            _make_score(
                handle="product-a", has_product_image=False,
                inventory_value=500.0, total_inventory=10,
                online_sessions=42, gmc_impressions=100,
            ),
            _make_score(
                handle="product-b", has_description=False,
                inventory_value=1200.0, variant_count=3,
                variants_missing_images=1,
            ),
        ]

        path = tmp_path / "snapshot.json"
        save_snapshot(scores, path)
        assert path.exists()

        loaded = load_snapshot(path)
        assert len(loaded) == 2
        assert loaded[0].handle == "product-a"
        assert loaded[0].inventory_value == 500.0
        assert loaded[0].online_sessions == 42
        assert loaded[1].handle == "product-b"
        assert loaded[1].has_description is False

    def test_priority_preserved_after_roundtrip(self, tmp_path):
        """Compute priority before and after snapshot — must match."""
        s = _make_score(
            handle="x", has_product_image=False,
            inventory_value=800.0, full_price_inventory_value=400.0,
        )
        before = compute_priority(s, PriorityWeights())

        path = tmp_path / "snap.json"
        save_snapshot([s], path)
        loaded = load_snapshot(path)
        after = compute_priority(loaded[0], PriorityWeights())

        assert after == pytest.approx(before, rel=1e-9)
