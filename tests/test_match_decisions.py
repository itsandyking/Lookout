"""Tests for match decision JSONL logging."""

import json
from pathlib import Path


def test_log_match_decision(tmp_path):
    from lookout.enrich.match_validator import MatchDecisionLogger

    logger = MatchDecisionLogger(tmp_path / "match_decisions.jsonl")
    logger.log(
        handle="test-product",
        vendor="TestVendor",
        catalog_title="Test Product",
        candidates_tried=[
            {
                "url": "https://example.com/wrong",
                "pre_scrape_confidence": 80,
                "stage": "title_gate",
                "action": "reject",
                "reason": "title_similarity_too_low",
            },
            {
                "url": "https://example.com/right",
                "pre_scrape_confidence": 70,
                "stage": "signal_check",
                "action": "accept",
                "post_scrape_confidence": 75,
            },
        ],
        outcome="accepted",
        final_url="https://example.com/right",
    )

    lines = (tmp_path / "match_decisions.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["handle"] == "test-product"
    assert record["outcome"] == "accepted"
    assert len(record["candidates_tried"]) == 2
    assert record["final_url"] == "https://example.com/right"


def test_log_multiple_decisions(tmp_path):
    from lookout.enrich.match_validator import MatchDecisionLogger

    logger = MatchDecisionLogger(tmp_path / "match_decisions.jsonl")
    logger.log(handle="product-1", vendor="V", catalog_title="P1",
               candidates_tried=[], outcome="all_failed", final_url=None)
    logger.log(handle="product-2", vendor="V", catalog_title="P2",
               candidates_tried=[{"url": "u", "action": "accept"}],
               outcome="accepted", final_url="u")

    lines = (tmp_path / "match_decisions.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
