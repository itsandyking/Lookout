"""Detect patterns in enrichment failures and propose threshold changes.

Loads decision logs from match_decisions.jsonl and feedback dispositions,
clusters failures by root cause, identifies threshold boundaries causing
false rejections, and proposes specific threshold changes.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from lookout.feedback.collector import FeedbackEntry, load_all_feedback

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold registry -- static map of tunable parameters
# ---------------------------------------------------------------------------

THRESHOLD_REGISTRY: dict[str, dict] = {
    "title_gate.word_overlap": {
        "current": 0.3,
        "file": "match_validator.py",
        "line_context": "word_overlap < 0.3",
        "direction": "lower_to_recover",
    },
    "title_gate.title_similarity": {
        "current": 0.4,
        "file": "match_validator.py",
        "line_context": "title_sim < 0.4",
        "direction": "lower_to_recover",
    },
    "resolver.strong_overlap": {
        "current": 0.6,
        "file": "resolver.py",
        "line_context": "overlap_ratio >= 0.6",
        "direction": "lower_to_recover",
    },
    "resolver.strong_seq": {
        "current": 0.5,
        "file": "resolver.py",
        "line_context": "seq_ratio >= 0.5",
        "direction": "lower_to_recover",
    },
    "resolver.weak_overlap": {
        "current": 0.2,
        "file": "resolver.py",
        "line_context": "overlap_ratio < 0.2",
        "direction": "raise_to_recover",
    },
    "resolver.weak_seq": {
        "current": 0.3,
        "file": "resolver.py",
        "line_context": "seq_ratio < 0.3",
        "direction": "raise_to_recover",
    },
    "resolver.moderate_overlap": {
        "current": 0.3,
        "file": "resolver.py",
        "line_context": "overlap_ratio < 0.3",
        "direction": "raise_to_recover",
    },
    "resolver.foreign_product_penalty": {
        "current": -35,
        "file": "resolver.py",
        "direction": "raise_to_recover",
    },
    "resolver.near_homonym_penalty": {
        "current": -40,
        "file": "resolver.py",
        "direction": "raise_to_recover",
    },
    "resolver.demographic_penalty": {
        "current": -15,
        "file": "resolver.py",
        "direction": "raise_to_recover",
    },
    "resolver.critical_mismatch_penalty": {
        "current": -30,
        "file": "resolver.py",
        "direction": "raise_to_recover",
    },
    "post_extraction.pass_threshold": {
        "current": 50,
        "file": "match_validator.py",
        "direction": "lower_to_recover",
    },
    "post_extraction.title_weight": {
        "current": 40,
        "file": "match_validator.py",
        "direction": "lower_to_recover",
    },
    "pipeline.candidate_skip": {
        "current": 50,
        "file": "pipeline.py",
        "direction": "lower_to_recover",
    },
}

# Failure reasons considered non-actionable (no threshold fix possible)
NON_ACTIONABLE_REASONS = frozenset({
    "reject_bot_blocked",
    "reject_no_candidates",
    "reject_timeout",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ThresholdProposal:
    """A proposed change to a scoring threshold."""

    parameter: str          # e.g. "title_gate.word_overlap"
    current_value: float    # e.g. 0.30
    proposed_value: float   # e.g. 0.22
    rationale: str          # e.g. "5 failures cluster at 0.25-0.29"


@dataclass
class PatternCluster:
    """A group of decisions sharing a failure pattern."""

    failure_reason: str
    count: int
    common_vendor: str | None
    common_type: str | None
    affected_handles: list[str]
    threshold_boundary: dict | None  # {"param": ..., "min": ..., "max": ...}
    proposal: ThresholdProposal | None
    actionable: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_decisions(path: Path) -> list[dict]:
    """Load decision objects from a JSONL file."""
    decisions = []
    if not path.exists():
        logger.warning("Decision log not found: %s", path)
        return decisions
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            decisions.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed decision line: %s", exc)
    return decisions


def _extract_failed_candidates(decisions: list[dict]) -> list[dict]:
    """Pull out individual candidate outcomes that are failures.

    Each returned dict has the candidate entry merged with top-level
    decision fields (handle, vendor) for context.
    """
    failed = []
    for dec in decisions:
        handle = dec.get("handle", "")
        vendor = dec.get("vendor", "")
        for cand in dec.get("candidates_tried", []):
            outcome = cand.get("outcome", "")
            if outcome.startswith(("reject_", "skip_")):
                entry = {**cand, "handle": handle, "vendor": vendor}
                # Carry type_words from the decision level if present
                if "type_words" in dec:
                    entry.setdefault("type_words", dec["type_words"])
                failed.append(entry)
    return failed


def _common_value(items: list[str]) -> str | None:
    """Return the value if all items are identical, else None."""
    unique = set(items)
    if len(unique) == 1:
        return unique.pop()
    return None


def _common_type(entries: list[dict]) -> str | None:
    """Detect a shared product type from type_words across entries."""
    types = []
    for e in entries:
        tw = e.get("type_words")
        if isinstance(tw, list) and tw:
            types.append(" ".join(tw))
        elif isinstance(tw, str) and tw:
            types.append(tw)
    if not types:
        return None
    return _common_value(types)


def _detect_threshold_boundary(
    entries: list[dict],
) -> tuple[dict | None, ThresholdProposal | None]:
    """Check if a cluster's numeric values fall near a known threshold.

    Returns (boundary_info, proposal) or (None, None).
    """
    for param, info in THRESHOLD_REGISTRY.items():
        # Derive a short key to look for in candidate dicts
        short_key = param.rsplit(".", 1)[-1]  # e.g. "word_overlap"
        values = []
        for e in entries:
            v = e.get(short_key)
            if v is not None:
                try:
                    values.append(float(v))
                except (TypeError, ValueError):
                    pass
        if not values:
            continue

        cutoff = float(info["current"])
        direction = info["direction"]

        # "within 80% of the cutoff" means the value is between
        # 80% * cutoff and cutoff (for lower_to_recover) or
        # cutoff and cutoff / 0.8 (for raise_to_recover).
        if direction == "lower_to_recover":
            # Failures happen when value < cutoff. We want to see if
            # values are close to (but below) the cutoff.
            lower_bound = cutoff * 0.8
            in_boundary = [v for v in values if lower_bound <= v <= cutoff]
        else:
            # raise_to_recover: failures happen when value < cutoff
            # (for penalties, cutoff is negative so "raise" means less negative)
            if cutoff < 0:
                # Penalty values: failures cluster near the penalty.
                # "within 80%" means between cutoff and 80% of cutoff
                upper_bound = cutoff * 0.8  # less negative
                in_boundary = [v for v in values if cutoff <= v <= upper_bound]
            else:
                upper_bound = cutoff * 1.2
                in_boundary = [v for v in values if cutoff <= v <= upper_bound]

        if not in_boundary:
            continue

        val_min = min(in_boundary)
        val_max = max(in_boundary)
        boundary = {"param": param, "min": val_min, "max": val_max}

        # Propose moving threshold to 10% below the cluster minimum
        if direction == "lower_to_recover":
            proposed = round(val_min * 0.9, 4)
        else:
            if cutoff < 0:
                # For negative penalties, "10% below min" means more negative
                proposed = round(val_min * 1.1, 4)
            else:
                proposed = round(val_min * 1.1, 4)

        proposal = ThresholdProposal(
            parameter=param,
            current_value=cutoff,
            proposed_value=proposed,
            rationale=(
                f"{len(in_boundary)} failures cluster at "
                f"{val_min:.2f}-{val_max:.2f}"
            ),
        )
        return boundary, proposal

    return None, None


def _detect_penalty_stacking(entries: list[dict]) -> bool:
    """Check if penalties stack to >50 without any single dominant penalty."""
    for e in entries:
        penalties = e.get("penalties", {})
        if not penalties:
            continue
        values = [abs(v) for v in penalties.values() if isinstance(v, (int, float))]
        total = sum(values)
        if total <= 50:
            continue
        # No single penalty is >60% of total
        if all(v / total <= 0.6 for v in values):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(
    decisions_path: Path,
    feedback_dir: Path | None = None,
) -> list[PatternCluster]:
    """Analyze decision logs for failure patterns.

    Parameters
    ----------
    decisions_path:
        Path to ``match_decisions.jsonl``.
    feedback_dir:
        Optional directory of feedback JSON files (from collector.py).
        Currently used to cross-reference dispositions but not required.

    Returns
    -------
    List of PatternCluster objects, sorted by count descending.
    Only clusters with 3+ occurrences are included.
    """
    decisions = _load_decisions(decisions_path)
    if not decisions:
        return []

    # Optionally load feedback for future cross-referencing
    feedback: list[FeedbackEntry] = []
    if feedback_dir and feedback_dir.exists():
        feedback = load_all_feedback(feedback_dir)
    feedback_by_handle = {f.handle: f for f in feedback}

    # --- Pass 1: Group failed candidates by outcome reason ---
    failed = _extract_failed_candidates(decisions)
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for entry in failed:
        by_reason[entry.get("outcome", "unknown")].append(entry)

    clusters: list[PatternCluster] = []

    for reason, entries in by_reason.items():
        if len(entries) < 3:
            continue

        handles = list(dict.fromkeys(e["handle"] for e in entries))
        vendors = [e.get("vendor", "") for e in entries if e.get("vendor")]

        # --- Pass 2: Find commonalities ---
        common_vendor = _common_value(vendors) if vendors else None
        common_type = _common_type(entries)

        actionable = reason not in NON_ACTIONABLE_REASONS

        # Threshold boundary detection
        boundary, proposal = (None, None)
        if actionable:
            boundary, proposal = _detect_threshold_boundary(entries)

        # Penalty stacking detection
        if actionable and _detect_penalty_stacking(entries):
            if proposal is None:
                # No threshold proposal yet -- note stacking in boundary info
                boundary = boundary or {}
                boundary["penalty_stacking"] = True

        clusters.append(PatternCluster(
            failure_reason=reason,
            count=len(entries),
            common_vendor=common_vendor,
            common_type=common_type,
            affected_handles=handles,
            threshold_boundary=boundary,
            proposal=proposal,
            actionable=actionable,
        ))

    clusters.sort(key=lambda c: c.count, reverse=True)
    return clusters
