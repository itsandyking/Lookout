"""Replay past decisions with modified thresholds to show impact.

Takes a ThresholdProposal and replays match_decisions.jsonl entries,
comparing old vs new outcomes. Pure computation — no network calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from lookout.enrich.match_validator import check_title_gate
from lookout.feedback.analyzer import ThresholdProposal

logger = logging.getLogger(__name__)


@dataclass
class ReplayDiff:
    """Result of replaying decisions with a modified threshold."""

    proposal: ThresholdProposal
    recovered: list[dict] = field(default_factory=list)  # was rejected, now passes
    regressed: list[dict] = field(default_factory=list)  # was accepted, now rejected
    unchanged: int = 0


def _load_decisions(decisions_path: Path) -> list[dict]:
    """Load all decision records from a JSONL file."""
    decisions = []
    with open(decisions_path) as f:
        for line in f:
            line = line.strip()
            if line:
                decisions.append(json.loads(line))
    return decisions


# ---------------------------------------------------------------------------
# Title gate replay helpers
# ---------------------------------------------------------------------------


def _replay_title_gate(
    page_title: str,
    catalog_title: str,
    param: str,
    proposed_value: float,
) -> dict:
    """Re-run check_title_gate with a single threshold overridden.

    The thresholds in check_title_gate are hardcoded, so we replicate the
    logic with the proposed value substituted for the target parameter.
    """
    import re
    from difflib import SequenceMatcher

    page_lower = page_title.lower()
    catalog_lower = catalog_title.lower()
    title_sim = SequenceMatcher(None, page_lower, catalog_lower).ratio()

    page_words = set(re.findall(r"[a-z0-9]+", page_lower))
    catalog_words = set(re.findall(r"[a-z0-9]+", catalog_lower))

    demographics = frozenset(
        {
            "youth",
            "kids",
            "boys",
            "girls",
            "junior",
            "jr",
            "mens",
            "men",
            "womens",
            "women",
            "unisex",
        }
    )
    demo_normalize = {"men": "mens", "women": "womens"}
    page_demos = {demo_normalize.get(w, w) for w in page_words & demographics}
    catalog_demos = {demo_normalize.get(w, w) for w in catalog_words & demographics}

    demographic_match: bool | None = None
    if page_demos and catalog_demos:
        demographic_match = bool(page_demos & catalog_demos)
    elif page_demos or catalog_demos:
        demographic_match = None

    if demographic_match is False:
        return {
            "pass": False,
            "title_similarity": title_sim,
            "demographic_match": False,
            "reason": f"demographic_mismatch: {catalog_demos} vs {page_demos}",
        }

    filler = {"the", "a", "an", "by", "for", "in", "of", "and", "with", "s"}
    catalog_meaningful = catalog_words - filler - demographics
    page_meaningful = page_words - filler - demographics
    word_overlap = len(catalog_meaningful & page_meaningful) / max(len(catalog_meaningful), 1)

    # Apply the proposed threshold for the target parameter
    title_sim_threshold = 0.4
    word_overlap_threshold = 0.3
    if param == "title_gate.title_similarity":
        title_sim_threshold = proposed_value
    elif param == "title_gate.word_overlap":
        word_overlap_threshold = proposed_value

    if title_sim < title_sim_threshold and word_overlap < word_overlap_threshold:
        return {
            "pass": False,
            "title_similarity": title_sim,
            "demographic_match": demographic_match,
            "reason": f"title_similarity_too_low: {title_sim:.2f}, word_overlap: {word_overlap:.2f}",
        }

    return {
        "pass": True,
        "title_similarity": title_sim,
        "demographic_match": demographic_match,
        "reason": "ok",
    }


# ---------------------------------------------------------------------------
# Resolver replay helpers
# ---------------------------------------------------------------------------

_RESOLVER_SKIP_THRESHOLD = 50  # pipeline.candidate_skip default


def _replay_resolver_decision(
    decision: dict,
    param: str,
    proposed_value: float,
) -> str | None:
    """Re-evaluate a resolver-stage decision with a modified threshold.

    Returns the new outcome string, or None if the decision is not relevant
    to the given parameter.
    """
    candidates_tried = decision.get("candidates_tried", [])
    if not candidates_tried:
        return None

    # For pipeline.candidate_skip, check if any candidate's confidence
    # was between the proposed and current thresholds
    if param == "pipeline.candidate_skip":
        for entry in candidates_tried:
            if entry.get("outcome") == "skip_low_confidence":
                conf = entry.get("resolver_confidence", 0)
                if conf >= proposed_value:
                    return "recovered_from_skip"
            elif entry.get("outcome") in ("accepted", "accept"):
                conf = entry.get("resolver_confidence", 0)
                if conf < proposed_value:
                    return "regressed_to_skip"
        return None

    # For resolver.* thresholds, re-run rescore_candidates if we have the
    # raw resolver_candidates data
    resolver_candidates = decision.get("resolver_candidates")
    if not resolver_candidates:
        return None

    decision.get("catalog_title", "")
    decision.get("vendor", "")
    decision.get("catalog_price")

    # Rescore with current logic (thresholds are hardcoded in resolver)
    # The rescore itself doesn't accept threshold overrides, so we compare
    # the stored confidence against the proposed threshold boundary.
    # For penalty-type params, check if the penalty was applied.
    for entry in candidates_tried:
        reasoning = entry.get("rescore_reasoning", "")
        conf = entry.get("resolver_confidence", 0)

        if param == "resolver.foreign_product_penalty" and "foreign_product" in reasoning:
            # Penalty was applied. Check if reducing it would push above skip threshold
            current_penalty = -20  # as applied in resolver code
            new_penalty = proposed_value
            diff = new_penalty - current_penalty
            new_conf = conf + diff
            if (
                entry.get("outcome") == "skip_low_confidence"
                and new_conf >= _RESOLVER_SKIP_THRESHOLD
            ):
                return "recovered_from_penalty"
            if (
                entry.get("outcome") not in ("skip_low_confidence",)
                and new_conf < _RESOLVER_SKIP_THRESHOLD
            ):
                return "regressed_from_penalty"

    return None


# ---------------------------------------------------------------------------
# Main replay function
# ---------------------------------------------------------------------------


def replay_proposal(
    proposal: ThresholdProposal,
    decisions_path: Path,
) -> ReplayDiff:
    """Replay all decisions with a modified threshold and return the diff.

    Loads match_decisions.jsonl, re-evaluates each relevant decision with the
    proposed threshold value, and compares against the original outcome.
    """
    decisions = _load_decisions(decisions_path)
    diff = ReplayDiff(proposal=proposal)

    param = proposal.parameter

    for decision in decisions:
        candidates_tried = decision.get("candidates_tried", [])
        handle = decision.get("handle", "unknown")
        decision.get("outcome", "")

        if param.startswith("title_gate."):
            _replay_title_gate_decisions(
                decision,
                candidates_tried,
                handle,
                param,
                proposal,
                diff,
            )
        elif param.startswith("resolver.") or param == "pipeline.candidate_skip":
            _replay_resolver_decisions(
                decision,
                candidates_tried,
                handle,
                param,
                proposal,
                diff,
            )
        elif param.startswith("post_extraction."):
            _replay_post_extraction_decisions(
                decision,
                candidates_tried,
                handle,
                param,
                proposal,
                diff,
            )
        else:
            diff.unchanged += 1

    return diff


def _replay_title_gate_decisions(
    decision: dict,
    candidates_tried: list[dict],
    handle: str,
    param: str,
    proposal: ThresholdProposal,
    diff: ReplayDiff,
) -> None:
    """Check title gate decisions for changes under the proposed threshold."""
    changed = False
    for entry in candidates_tried:
        outcome = entry.get("outcome", "")
        page_title = entry.get("title_extracted")
        catalog_title = decision.get("catalog_title", "")

        if outcome == "reject_title_gate" and page_title and catalog_title:
            # Re-run with proposed threshold
            new_gate = _replay_title_gate(
                page_title,
                catalog_title,
                param,
                proposal.proposed_value,
            )
            if new_gate["pass"]:
                diff.recovered.append(
                    {
                        "handle": handle,
                        "url": entry.get("url", ""),
                        "old_outcome": outcome,
                        "new_outcome": "pass_title_gate",
                        "title_similarity": new_gate["title_similarity"],
                    }
                )
                changed = True

        elif outcome not in ("reject_title_gate", "reject_bot_blocked", "skip_low_confidence"):
            # Was passing — check if it would now fail under a tighter threshold
            if page_title and catalog_title:
                # Re-run with proposed value
                old_gate = check_title_gate(page_title, catalog_title)
                new_gate = _replay_title_gate(
                    page_title,
                    catalog_title,
                    param,
                    proposal.proposed_value,
                )
                if old_gate["pass"] and not new_gate["pass"]:
                    diff.regressed.append(
                        {
                            "handle": handle,
                            "url": entry.get("url", ""),
                            "old_outcome": "pass_title_gate",
                            "new_outcome": "reject_title_gate",
                            "title_similarity": new_gate["title_similarity"],
                        }
                    )
                    changed = True

    if not changed:
        diff.unchanged += 1


def _replay_resolver_decisions(
    decision: dict,
    candidates_tried: list[dict],
    handle: str,
    param: str,
    proposal: ThresholdProposal,
    diff: ReplayDiff,
) -> None:
    """Check resolver/pipeline decisions for changes under the proposed threshold."""
    new_outcome = _replay_resolver_decision(decision, param, proposal.proposed_value)
    if new_outcome and "recovered" in new_outcome:
        diff.recovered.append(
            {
                "handle": handle,
                "old_outcome": decision.get("outcome", ""),
                "new_outcome": new_outcome,
            }
        )
    elif new_outcome and "regressed" in new_outcome:
        diff.regressed.append(
            {
                "handle": handle,
                "old_outcome": decision.get("outcome", ""),
                "new_outcome": new_outcome,
            }
        )
    else:
        diff.unchanged += 1


def _replay_post_extraction_decisions(
    decision: dict,
    candidates_tried: list[dict],
    handle: str,
    param: str,
    proposal: ThresholdProposal,
    diff: ReplayDiff,
) -> None:
    """Check post-extraction decisions for changes under the proposed threshold.

    Re-evaluates confidence against the proposed pass_threshold or
    recalculates with the proposed title_weight.
    """
    changed = False
    for entry in candidates_tried:
        signals = entry.get("signals")
        if not signals:
            continue

        outcome = entry.get("outcome", "")
        old_confidence = entry.get("confidence", entry.get("post_confidence"))
        if old_confidence is None:
            continue

        # Recalculate confidence with modified weights/thresholds
        title_weight = 40
        pass_threshold = 50

        if param == "post_extraction.title_weight":
            title_weight = proposal.proposed_value
        elif param == "post_extraction.pass_threshold":
            pass_threshold = proposal.proposed_value

        title_sim = signals.get("title_similarity", 0)
        price_ratio = signals.get("price_ratio", 0.5)
        color_overlap = signals.get("color_overlap", 0.5)
        content_quality = signals.get("content_quality", 0)

        # Remaining weight redistributed proportionally among other signals
        other_weight = 100 - title_weight
        # Original: price=25, color=25, quality=10 out of 60 non-title
        new_confidence = (
            title_sim * title_weight
            + price_ratio * (other_weight * 25 / 60)
            + color_overlap * (other_weight * 25 / 60)
            + content_quality * (other_weight * 10 / 60)
        )

        new_pass = new_confidence >= pass_threshold
        old_pass = outcome not in (
            "reject_post_extraction",
            "low_post_scrape_confidence",
        )

        if not old_pass and new_pass:
            diff.recovered.append(
                {
                    "handle": handle,
                    "url": entry.get("url", ""),
                    "old_outcome": outcome,
                    "new_outcome": "pass_post_extraction",
                    "old_confidence": old_confidence,
                    "new_confidence": round(new_confidence, 1),
                }
            )
            changed = True
        elif old_pass and not new_pass:
            diff.regressed.append(
                {
                    "handle": handle,
                    "url": entry.get("url", ""),
                    "old_outcome": outcome,
                    "new_outcome": "reject_post_extraction",
                    "old_confidence": old_confidence,
                    "new_confidence": round(new_confidence, 1),
                }
            )
            changed = True

    if not changed:
        diff.unchanged += 1
