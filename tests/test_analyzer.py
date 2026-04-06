"""Tests for the feedback pattern analyzer."""

import json
from pathlib import Path

from lookout.feedback.analyzer import (
    analyze,
    PatternCluster,
    ThresholdProposal,
    THRESHOLD_REGISTRY,
    _detect_penalty_stacking,
)


def _write_decisions(path: Path, decisions: list[dict]) -> Path:
    """Write decision dicts as JSONL and return the path."""
    fp = path / "match_decisions.jsonl"
    fp.write_text("\n".join(json.dumps(d) for d in decisions))
    return fp


class TestClusterDetection:
    """Verify that failure reasons are grouped correctly."""

    def test_title_gate_failures_cluster_by_vendor(self, tmp_path):
        """5 title gate failures from one vendor should create one cluster."""
        decisions = [
            {
                "handle": f"petzl-product-{i}",
                "vendor": "Petzl",
                "candidates_tried": [
                    {
                        "outcome": "reject_title_gate",
                        "word_overlap": 0.25 + i * 0.01,
                    }
                ],
            }
            for i in range(5)
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 1
        c = clusters[0]
        assert c.failure_reason == "reject_title_gate"
        assert c.count == 5
        assert c.common_vendor == "Petzl"
        assert len(c.affected_handles) == 5
        assert c.actionable is True

    def test_clusters_below_three_not_surfaced(self, tmp_path):
        """Only 2 failures for a reason should not produce a cluster."""
        decisions = [
            {
                "handle": f"product-{i}",
                "vendor": "SomeVendor",
                "candidates_tried": [
                    {"outcome": "reject_title_gate", "word_overlap": 0.28}
                ],
            }
            for i in range(2)
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 0

    def test_mixed_vendors_no_common_vendor(self, tmp_path):
        """Failures from different vendors should have common_vendor=None."""
        decisions = [
            {
                "handle": f"product-{i}",
                "vendor": f"Vendor-{i}",
                "candidates_tried": [
                    {"outcome": "skip_low_confidence"}
                ],
            }
            for i in range(4)
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 1
        assert clusters[0].common_vendor is None


class TestThresholdBoundary:
    """Verify threshold boundary detection and proposals."""

    def test_word_overlap_boundary_proposes_correct_value(self, tmp_path):
        """Values at 0.25-0.29 (within 80% of 0.30 cutoff) should trigger a proposal."""
        decisions = [
            {
                "handle": f"product-{i}",
                "vendor": "TestVendor",
                "candidates_tried": [
                    {
                        "outcome": "reject_title_gate",
                        "word_overlap": val,
                    }
                ],
            }
            for i, val in enumerate([0.25, 0.26, 0.27, 0.28, 0.29])
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 1
        c = clusters[0]
        assert c.threshold_boundary is not None
        assert c.threshold_boundary["param"] == "title_gate.word_overlap"

        assert c.proposal is not None
        assert c.proposal.parameter == "title_gate.word_overlap"
        assert c.proposal.current_value == 0.3
        # Proposed = min(0.25) * 0.9 = 0.225
        assert c.proposal.proposed_value == 0.225

    def test_values_outside_boundary_no_proposal(self, tmp_path):
        """Values far below threshold should not trigger boundary detection."""
        decisions = [
            {
                "handle": f"product-{i}",
                "vendor": "TestVendor",
                "candidates_tried": [
                    {
                        "outcome": "reject_title_gate",
                        "word_overlap": val,
                    }
                ],
            }
            for i, val in enumerate([0.05, 0.08, 0.10])
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 1
        c = clusters[0]
        # 0.05-0.10 is well below 80% of 0.30 (= 0.24), so no boundary
        assert c.proposal is None


class TestBotBlocked:
    """Verify bot-blocked clusters are flagged non-actionable."""

    def test_bot_blocked_not_actionable(self, tmp_path):
        decisions = [
            {
                "handle": f"arcteryx-{i}",
                "vendor": "Arc'teryx",
                "candidates_tried": [
                    {"outcome": "reject_bot_blocked"}
                ],
            }
            for i in range(4)
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 1
        c = clusters[0]
        assert c.failure_reason == "reject_bot_blocked"
        assert c.actionable is False
        assert c.proposal is None


class TestPenaltyStacking:
    """Verify penalty stacking detection."""

    def test_stacking_flagged_when_no_dominant_penalty(self, tmp_path):
        """Total penalty >50, no single >60% of total => stacking."""
        decisions = [
            {
                "handle": f"product-{i}",
                "vendor": "MixVendor",
                "candidates_tried": [
                    {
                        "outcome": "skip_low_confidence",
                        "penalties": {
                            "foreign_product": -20,
                            "demographic": -15,
                            "near_homonym": -20,
                        },
                    }
                ],
            }
            for i in range(3)
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 1
        c = clusters[0]
        assert c.threshold_boundary is not None
        assert c.threshold_boundary.get("penalty_stacking") is True

    def test_no_stacking_when_one_penalty_dominates(self, tmp_path):
        """One penalty >60% of total should not flag stacking."""
        decisions = [
            {
                "handle": f"product-{i}",
                "vendor": "TestVendor",
                "candidates_tried": [
                    {
                        "outcome": "skip_low_confidence",
                        "penalties": {
                            "foreign_product": -45,
                            "demographic": -10,
                        },
                    }
                ],
            }
            for i in range(3)
        ]
        fp = _write_decisions(tmp_path, decisions)
        clusters = analyze(fp)

        assert len(clusters) == 1
        c = clusters[0]
        # 45/55 = 0.818 > 0.6 so no stacking
        assert c.threshold_boundary is None or not c.threshold_boundary.get(
            "penalty_stacking"
        )


class TestEdgeCases:
    """Edge cases and robustness."""

    def test_empty_decisions_file(self, tmp_path):
        fp = tmp_path / "match_decisions.jsonl"
        fp.write_text("")
        assert analyze(fp) == []

    def test_missing_decisions_file(self, tmp_path):
        fp = tmp_path / "nonexistent.jsonl"
        assert analyze(fp) == []

    def test_with_feedback_dir(self, tmp_path):
        """Analyzer should work when feedback dir is provided."""
        decisions = [
            {
                "handle": f"product-{i}",
                "vendor": "V",
                "candidates_tried": [{"outcome": "reject_title_gate"}],
            }
            for i in range(3)
        ]
        fp = _write_decisions(tmp_path, decisions)
        fb_dir = tmp_path / "feedback"
        fb_dir.mkdir()
        # Write a feedback file
        (fb_dir / "product-0_run1_rejected.json").write_text(json.dumps({
            "handle": "product-0",
            "run_id": "run1",
            "disposition": "rejected",
            "reason": "wrong_match",
        }))

        clusters = analyze(fp, feedback_dir=fb_dir)
        assert len(clusters) == 1
