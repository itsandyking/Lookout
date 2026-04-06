"""Tests for feedback replay — threshold impact assessment."""

import json
from pathlib import Path

from lookout.feedback.replay import (
    ReplayDiff,
    ThresholdProposal,
    replay_proposal,
    _replay_title_gate,
)


def _write_decisions(tmp_path: Path, decisions: list[dict]) -> Path:
    """Write decision dicts to a JSONL file and return the path."""
    path = tmp_path / "match_decisions.jsonl"
    with open(path, "w") as f:
        for d in decisions:
            f.write(json.dumps(d) + "\n")
    return path


class TestReplayTitleGate:
    """Title gate threshold replay tests."""

    def test_lowering_word_overlap_recovers_rejected(self, tmp_path):
        """A decision rejected at word_overlap=0.28 should pass if threshold drops to 0.20."""
        decisions = [
            {
                "handle": "petzl-nao-rl",
                "vendor": "Petzl",
                "catalog_title": "Petzl NAO RL Headlamp",
                "outcome": "reject_title_gate",
                "final_url": None,
                "candidates_tried": [
                    {
                        "url": "https://petzl.com/nao-rl",
                        "resolver_confidence": 75,
                        "outcome": "reject_title_gate",
                        "reason": "title_similarity_too_low: 0.35, word_overlap: 0.28",
                        "title_extracted": "NAO RL - Rechargeable Headlamp",
                        "title_similarity": 0.35,
                    }
                ],
            }
        ]
        path = _write_decisions(tmp_path, decisions)

        proposal = ThresholdProposal(
            parameter="title_gate.word_overlap",
            current_value=0.3,
            proposed_value=0.20,
            rationale="5 failures cluster at 0.25-0.29",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.recovered) == 1
        assert diff.recovered[0]["handle"] == "petzl-nao-rl"
        assert diff.recovered[0]["new_outcome"] == "pass_title_gate"
        assert len(diff.regressed) == 0

    def test_unchanged_threshold_produces_no_diff(self, tmp_path):
        """Setting proposed == current should change nothing."""
        # These titles genuinely fail the current gate (0% word overlap, ~9% string sim)
        decisions = [
            {
                "handle": "product-a",
                "vendor": "TestVendor",
                "catalog_title": "Widget Pro 3000",
                "outcome": "reject_title_gate",
                "final_url": None,
                "candidates_tried": [
                    {
                        "url": "https://example.com/widget",
                        "resolver_confidence": 70,
                        "outcome": "reject_title_gate",
                        "reason": "title_similarity_too_low: 0.09, word_overlap: 0.00",
                        "title_extracted": "Mountain Camping Stove Deluxe",
                        "title_similarity": 0.09,
                    }
                ],
            }
        ]
        path = _write_decisions(tmp_path, decisions)

        proposal = ThresholdProposal(
            parameter="title_gate.word_overlap",
            current_value=0.3,
            proposed_value=0.3,
            rationale="no change",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.recovered) == 0
        assert len(diff.regressed) == 0
        assert diff.unchanged == 1

    def test_aggressive_threshold_causes_regressions(self, tmp_path):
        """Raising word_overlap threshold causes a previously passing decision to fail.

        This title pair has title_sim=0.30 (below 0.4 threshold) but
        word_overlap=0.75 (above 0.3 threshold), so it currently passes.
        Raising word_overlap threshold to 0.8 causes both conditions to fail.
        """
        decisions = [
            {
                "handle": "petzl-nao-rl-2",
                "vendor": "Petzl",
                "catalog_title": "Petzl NAO RL Headlamp",
                "outcome": "accepted",
                "final_url": "https://petzl.com/nao-rl",
                "candidates_tried": [
                    {
                        "url": "https://petzl.com/nao-rl",
                        "resolver_confidence": 85,
                        "outcome": "accepted",
                        "title_extracted": "NAO RL - Ultra Performance Rechargeable Multi-Beam Running Headlamp 1500 Lumens",
                        "title_similarity": 0.30,
                    }
                ],
            }
        ]
        path = _write_decisions(tmp_path, decisions)

        proposal = ThresholdProposal(
            parameter="title_gate.word_overlap",
            current_value=0.3,
            proposed_value=0.8,
            rationale="testing aggressive threshold",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.regressed) == 1
        assert diff.regressed[0]["handle"] == "petzl-nao-rl-2"
        assert diff.regressed[0]["new_outcome"] == "reject_title_gate"


class TestReplayPipelineSkip:
    """Pipeline candidate skip threshold replay tests."""

    def test_lowering_skip_threshold_recovers_candidate(self, tmp_path):
        decisions = [
            {
                "handle": "low-conf-product",
                "vendor": "SomeVendor",
                "catalog_title": "Some Product",
                "outcome": "skip_low_confidence",
                "final_url": None,
                "candidates_tried": [
                    {
                        "url": "https://example.com/product",
                        "resolver_confidence": 45,
                        "outcome": "skip_low_confidence",
                        "reason": "confidence 45 < threshold 50",
                    }
                ],
            }
        ]
        path = _write_decisions(tmp_path, decisions)

        proposal = ThresholdProposal(
            parameter="pipeline.candidate_skip",
            current_value=50,
            proposed_value=40,
            rationale="recover borderline candidates",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.recovered) == 1
        assert diff.recovered[0]["handle"] == "low-conf-product"

    def test_raising_skip_threshold_causes_regression(self, tmp_path):
        decisions = [
            {
                "handle": "borderline-product",
                "vendor": "SomeVendor",
                "catalog_title": "Borderline Product",
                "outcome": "accepted",
                "final_url": "https://example.com/prod",
                "candidates_tried": [
                    {
                        "url": "https://example.com/prod",
                        "resolver_confidence": 55,
                        "outcome": "accepted",
                    }
                ],
            }
        ]
        path = _write_decisions(tmp_path, decisions)

        proposal = ThresholdProposal(
            parameter="pipeline.candidate_skip",
            current_value=50,
            proposed_value=60,
            rationale="testing aggressive skip threshold",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.regressed) == 1
        assert diff.regressed[0]["handle"] == "borderline-product"


class TestReplayPostExtraction:
    """Post-extraction threshold replay tests."""

    def test_lowering_pass_threshold_recovers_rejected(self, tmp_path):
        decisions = [
            {
                "handle": "low-conf-extraction",
                "vendor": "TestVendor",
                "catalog_title": "Test Product",
                "outcome": "reject_post_extraction",
                "final_url": None,
                "candidates_tried": [
                    {
                        "url": "https://example.com/test",
                        "outcome": "reject_post_extraction",
                        "confidence": 45,
                        "signals": {
                            "title_similarity": 0.6,
                            "price_ratio": 0.5,
                            "color_overlap": 0.5,
                            "content_quality": 1.0,
                        },
                    }
                ],
            }
        ]
        path = _write_decisions(tmp_path, decisions)

        proposal = ThresholdProposal(
            parameter="post_extraction.pass_threshold",
            current_value=50,
            proposed_value=40,
            rationale="recover borderline extractions",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.recovered) == 1
        assert diff.recovered[0]["handle"] == "low-conf-extraction"
        assert diff.recovered[0]["new_confidence"] > 0

    def test_unchanged_pass_threshold_no_diff(self, tmp_path):
        decisions = [
            {
                "handle": "stable-product",
                "vendor": "TestVendor",
                "catalog_title": "Stable Product",
                "outcome": "reject_post_extraction",
                "final_url": None,
                "candidates_tried": [
                    {
                        "url": "https://example.com/stable",
                        "outcome": "reject_post_extraction",
                        "confidence": 35,
                        "signals": {
                            "title_similarity": 0.3,
                            "price_ratio": 0.5,
                            "color_overlap": 0.3,
                            "content_quality": 0.0,
                        },
                    }
                ],
            }
        ]
        path = _write_decisions(tmp_path, decisions)

        proposal = ThresholdProposal(
            parameter="post_extraction.pass_threshold",
            current_value=50,
            proposed_value=50,
            rationale="no change",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.recovered) == 0
        assert len(diff.regressed) == 0


class TestReplayTitleGateInternal:
    """Direct tests for the _replay_title_gate helper."""

    def test_word_overlap_threshold_respected(self):
        # "NAO RL" overlaps with catalog title but string similarity is low
        result = _replay_title_gate(
            "NAO RL - Rechargeable Headlamp",
            "Petzl NAO RL Headlamp",
            "title_gate.word_overlap",
            0.10,  # very permissive
        )
        assert result["pass"] is True

    def test_demographic_mismatch_unaffected_by_threshold(self):
        # Demographic mismatch should always reject regardless of thresholds
        result = _replay_title_gate(
            "Women's NAO RL Headlamp",
            "Youth NAO RL Headlamp",
            "title_gate.word_overlap",
            0.01,
        )
        assert result["pass"] is False
        assert "demographic_mismatch" in result["reason"]


class TestReplayEmptyFile:
    """Edge case: empty decisions file."""

    def test_empty_jsonl_returns_empty_diff(self, tmp_path):
        path = tmp_path / "match_decisions.jsonl"
        path.write_text("")

        proposal = ThresholdProposal(
            parameter="title_gate.word_overlap",
            current_value=0.3,
            proposed_value=0.2,
            rationale="test",
        )
        diff = replay_proposal(proposal, path)

        assert len(diff.recovered) == 0
        assert len(diff.regressed) == 0
        assert diff.unchanged == 0
