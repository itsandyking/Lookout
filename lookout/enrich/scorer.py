"""
Quality scorer for enrichment output.

Implements a 5-axis composite quality score for generated product descriptions,
designed for use in a Karpathy Loop optimization cycle.

Axes:
  1. Factual fidelity (0-30): Only contains claims present in ExtractedFacts
  2. Structural compliance (0-25): Correct HTML structure (intro, features, specs)
  3. Length targets (0-15): Body 100-400 words, features ≤12 words each
  4. Anti-hype compliance (0-15): No banned marketing words
  5. Coverage (0-15): Used available facts or left them on the table

Total: 0-100
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from lookout.enrich.models import ExtractedFacts, MerchOutput

logger = logging.getLogger(__name__)

BANNED_WORDS = [
    "amazing",
    "incredible",
    "must-have",
    "must have",
    "game-changer",
    "game changer",
    "revolutionary",
    "unbelievable",
    "stunning",
    "breathtaking",
    "perfect",
    "flawless",
    "best-selling",
    "best selling",
    "world-class",
    "world class",
    "cutting-edge",
    "cutting edge",
    "unmatched",
    "unparalleled",
    "extraordinary",
    "phenomenal",
    "superb",
    "ultimate",
    "fantastic",
]


@dataclass
class AxisScore:
    """Score for a single quality axis."""

    name: str
    score: int
    max_score: int
    details: list[str] = field(default_factory=list)

    @property
    def pct(self) -> float:
        return (self.score / self.max_score * 100) if self.max_score else 0.0


@dataclass
class QualityScore:
    """Composite quality score across all axes."""

    handle: str
    axes: dict[str, AxisScore] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(a.score for a in self.axes.values())

    @property
    def max_total(self) -> int:
        return sum(a.max_score for a in self.axes.values())

    @property
    def pct(self) -> float:
        return (self.total / self.max_total * 100) if self.max_total else 0.0

    def summary_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "total": self.total,
            "max": self.max_total,
            "pct": round(self.pct, 1),
            "axes": {
                name: {"score": a.score, "max": a.max_score, "details": a.details}
                for name, a in self.axes.items()
            },
        }


class _HTMLStructureParser(HTMLParser):
    """Extract structural elements from generated HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.has_p = False
        self.has_ul = False
        self.has_table = False
        self.has_h3 = False
        self.li_texts: list[str] = []
        self._current_tag: str = ""
        self._current_data: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self._current_tag = tag
        self._current_data = ""
        if tag == "p":
            self.has_p = True
        elif tag == "ul":
            self.has_ul = True
        elif tag == "table":
            self.has_table = True
        elif tag == "h3":
            self.has_h3 = True

    def handle_data(self, data: str) -> None:
        self._current_data += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self.li_texts.append(self._current_data.strip())
        self._current_tag = ""
        self._current_data = ""


def _word_count(html: str) -> int:
    """Count words in HTML by stripping tags."""
    text = re.sub(r"<[^>]+>", " ", html)
    return len(text.split())


def _extract_text(html: str) -> str:
    """Strip HTML tags and return plain text."""
    return re.sub(r"<[^>]+>", " ", html).strip()


def score_structural_compliance(body_html: str, facts: ExtractedFacts) -> AxisScore:
    """Axis 2: Structural compliance (0-25).

    - Has intro paragraph: +8
    - Has feature list (when features available): +7
    - Has spec table (when specs available): +5
    - Uses semantic HTML (h3, ul, table): +5
    """
    axis = AxisScore(name="structural_compliance", score=0, max_score=25)

    parser = _HTMLStructureParser()
    try:
        parser.feed(body_html)
    except Exception:
        axis.details.append("HTML parse error")
        return axis

    # Intro paragraph
    if parser.has_p:
        axis.score += 8
        axis.details.append("has intro paragraph")
    else:
        axis.details.append("missing intro paragraph")

    # Feature list (only penalize if facts had features)
    if facts.feature_bullets:
        if parser.has_ul:
            axis.score += 7
            axis.details.append("has feature list")
        else:
            axis.details.append("missing feature list (facts had features)")
    else:
        axis.score += 7  # no features to list = full marks
        axis.details.append("no features in facts (ok)")

    # Spec table (only penalize if facts had specs)
    if facts.specs:
        if parser.has_table:
            axis.score += 5
            axis.details.append("has spec table")
        else:
            axis.details.append("missing spec table (facts had specs)")
    else:
        axis.score += 5  # no specs = full marks
        axis.details.append("no specs in facts (ok)")

    # Semantic HTML usage
    semantic_count = sum([
        parser.has_p,
        parser.has_h3,
        parser.has_ul or not facts.feature_bullets,
        parser.has_table or not facts.specs,
    ])
    semantic_score = min(5, int(semantic_count / 4 * 5))
    axis.score += semantic_score
    axis.details.append(f"semantic HTML: {semantic_count}/4 elements")

    return axis


def score_length_targets(body_html: str) -> AxisScore:
    """Axis 3: Length targets (0-15).

    - Body 100-400 words: +10
    - Feature bullets ≤12 words each: +5
    """
    axis = AxisScore(name="length_targets", score=0, max_score=15)

    words = _word_count(body_html)
    if 100 <= words <= 400:
        axis.score += 10
        axis.details.append(f"body length {words}w (target 100-400)")
    elif 50 <= words < 100:
        axis.score += 5
        axis.details.append(f"body short: {words}w (target 100-400)")
    elif 400 < words <= 600:
        axis.score += 5
        axis.details.append(f"body long: {words}w (target 100-400)")
    else:
        axis.details.append(f"body out of range: {words}w (target 100-400)")

    # Check feature bullet lengths
    parser = _HTMLStructureParser()
    try:
        parser.feed(body_html)
    except Exception:
        axis.details.append("HTML parse error for bullet check")
        return axis

    if parser.li_texts:
        long_bullets = [li for li in parser.li_texts if len(li.split()) > 12]
        if not long_bullets:
            axis.score += 5
            axis.details.append(f"all {len(parser.li_texts)} bullets ≤12 words")
        else:
            # Partial credit
            ok_ratio = 1 - len(long_bullets) / len(parser.li_texts)
            axis.score += int(5 * ok_ratio)
            axis.details.append(
                f"{len(long_bullets)}/{len(parser.li_texts)} bullets >12 words"
            )
    else:
        axis.score += 5  # no bullets = no penalty
        axis.details.append("no bullets to check")

    return axis


def score_anti_hype(body_html: str) -> AxisScore:
    """Axis 4: Anti-hype compliance (0-15).

    Deducts points for each banned marketing word found.
    """
    axis = AxisScore(name="anti_hype", score=15, max_score=15)

    text_lower = _extract_text(body_html).lower()
    found = []
    for word in BANNED_WORDS:
        if word in text_lower:
            found.append(word)

    if found:
        penalty = min(15, len(found) * 5)
        axis.score = max(0, 15 - penalty)
        axis.details.append(f"banned words found: {', '.join(found)}")
    else:
        axis.details.append("no banned words")

    return axis


def score_coverage(body_html: str, facts: ExtractedFacts) -> AxisScore:
    """Axis 5: Coverage (0-15).

    Checks whether available facts were used or left on the table.
    - Description blocks used: +5
    - Feature bullets used: +5
    - Specs/materials/care used: +5
    """
    axis = AxisScore(name="coverage", score=0, max_score=15)
    text_lower = _extract_text(body_html).lower()

    # Description coverage: check if key phrases from description blocks appear
    if facts.description_blocks:
        blocks_used = 0
        for block in facts.description_blocks:
            # Check if significant words from the block appear in output
            words = [w for w in block.lower().split() if len(w) > 4]
            if words:
                matches = sum(1 for w in words[:10] if w in text_lower)
                if matches >= min(3, len(words[:10])):
                    blocks_used += 1
        if blocks_used > 0:
            axis.score += 5
            axis.details.append(f"description: {blocks_used}/{len(facts.description_blocks)} blocks used")
        else:
            axis.details.append("description blocks not reflected in output")
    else:
        axis.score += 5
        axis.details.append("no description blocks in facts (ok)")

    # Feature coverage
    if facts.feature_bullets:
        features_used = 0
        for bullet in facts.feature_bullets:
            key_words = [w for w in bullet.lower().split() if len(w) > 4]
            if key_words:
                matches = sum(1 for w in key_words[:5] if w in text_lower)
                if matches >= min(2, len(key_words[:5])):
                    features_used += 1
        ratio = features_used / len(facts.feature_bullets)
        axis.score += int(5 * ratio)
        axis.details.append(
            f"features: {features_used}/{len(facts.feature_bullets)} used"
        )
    else:
        axis.score += 5
        axis.details.append("no feature bullets in facts (ok)")

    # Specs/materials/care coverage
    rich_fields = []
    if facts.specs:
        rich_fields.append("specs")
    if facts.materials:
        rich_fields.append("materials")
    if facts.care:
        rich_fields.append("care")

    if rich_fields:
        used = 0
        if facts.specs:
            # Check if any spec values appear in output
            spec_hits = sum(
                1 for v in facts.specs.values() if v.lower() in text_lower
            )
            if spec_hits > 0:
                used += 1
        if facts.materials and facts.materials.lower()[:20] in text_lower:
            used += 1
        if facts.care and facts.care.lower()[:20] in text_lower:
            used += 1
        ratio = used / len(rich_fields)
        axis.score += int(5 * ratio)
        axis.details.append(f"rich fields: {used}/{len(rich_fields)} used ({', '.join(rich_fields)})")
    else:
        axis.score += 5
        axis.details.append("no rich fields in facts (ok)")

    return axis


def score_factual_fidelity_from_verification(
    verification: dict[str, Any],
) -> AxisScore:
    """Axis 1: Factual fidelity (0-30) from an existing verification result.

    Uses the output of LLMClient.verify_description() to score fidelity.
    """
    axis = AxisScore(name="factual_fidelity", score=0, max_score=30)

    verdict = verification.get("verdict", "ERROR")
    supported = verification.get("supported", [])
    unsupported = verification.get("unsupported", [])
    embellished = verification.get("embellished", [])

    if verdict == "ERROR":
        axis.details.append("verification failed")
        return axis

    total_claims = len(supported) + len(unsupported) + len(embellished)
    if total_claims == 0:
        axis.score = 30
        axis.details.append("no claims to verify")
        return axis

    # Score based on ratio of supported claims
    supported_ratio = len(supported) / total_claims
    axis.score = int(30 * supported_ratio)

    if unsupported:
        axis.details.append(f"{len(unsupported)} unsupported claims")
    if embellished:
        axis.details.append(f"{len(embellished)} embellished claims")
    if supported:
        axis.details.append(f"{len(supported)}/{total_claims} claims supported")

    if verdict == "PASS":
        axis.details.append("verdict: PASS")
    else:
        axis.details.append("verdict: FAIL")

    return axis


def score_quality(
    body_html: str,
    facts: ExtractedFacts,
    verification: dict[str, Any] | None = None,
) -> QualityScore:
    """Compute composite quality score for a generated description.

    Args:
        body_html: The generated Shopify HTML.
        facts: The extracted facts used as input.
        verification: Optional output from LLMClient.verify_description().
            If None, factual fidelity axis is skipped (scored as 0).

    Returns:
        QualityScore with per-axis breakdown.
    """
    qs = QualityScore(handle=facts.canonical_url or "unknown")

    # Axis 1: Factual fidelity (requires LLM verification)
    if verification:
        qs.axes["factual_fidelity"] = score_factual_fidelity_from_verification(
            verification
        )
    else:
        qs.axes["factual_fidelity"] = AxisScore(
            name="factual_fidelity",
            score=0,
            max_score=30,
            details=["skipped (no verification data)"],
        )

    # Axes 2-5: deterministic
    qs.axes["structural_compliance"] = score_structural_compliance(body_html, facts)
    qs.axes["length_targets"] = score_length_targets(body_html)
    qs.axes["anti_hype"] = score_anti_hype(body_html)
    qs.axes["coverage"] = score_coverage(body_html, facts)

    return qs


def load_artifacts(output_dir: Path, handle: str) -> tuple[MerchOutput | None, ExtractedFacts | None]:
    """Load merch_output.json and facts.json for a product handle."""
    handle_dir = output_dir / handle

    merch_path = handle_dir / "merch_output.json"
    facts_path = handle_dir / "facts.json"

    merch_output = None
    facts = None

    if merch_path.exists():
        try:
            data = json.loads(merch_path.read_text())
            merch_output = MerchOutput(**data)
        except Exception as e:
            logger.warning("Failed to load %s: %s", merch_path, e)

    if facts_path.exists():
        try:
            data = json.loads(facts_path.read_text())
            facts = ExtractedFacts(**data)
        except Exception as e:
            logger.warning("Failed to load %s: %s", facts_path, e)

    return merch_output, facts


def score_output_dir(
    output_dir: Path,
    handles: list[str] | None = None,
    verifications: dict[str, dict[str, Any]] | None = None,
) -> list[QualityScore]:
    """Score all products in an output directory.

    Args:
        output_dir: Path to the enrichment output directory.
        handles: Optional list of handles to score. If None, scores all.
        verifications: Optional dict of handle→verification result.

    Returns:
        List of QualityScore objects.
    """
    if handles is None:
        handles = [
            d.name
            for d in sorted(output_dir.iterdir())
            if d.is_dir() and (d / "merch_output.json").exists()
        ]

    scores = []
    for handle in handles:
        merch, facts = load_artifacts(output_dir, handle)
        if not merch or not merch.body_html or not facts:
            logger.info("Skipping %s: missing artifacts", handle)
            continue

        verification = (verifications or {}).get(handle)
        qs = score_quality(merch.body_html, facts, verification)
        qs.handle = handle
        scores.append(qs)

    return scores
