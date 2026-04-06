"""
Deterministic quality scorer for extracted facts.

Measures how useful the raw extracted facts are *before* any LLM generation.
Four axes:
  1. Content signal (0-30): Real content vs. boilerplate ratio
  2. Field completeness (0-25): Which fields are populated
  3. Specificity (0-25): Technical depth and substantive values
  4. Deduplication (0-20): Penalizes repeated content across fields

Total: 0-100
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from lookout.enrich.models import ExtractedFacts
from lookout.enrich.scorer import AxisScore, QualityScore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Boilerplate detection
# ---------------------------------------------------------------------------

_BOILERPLATE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^Open media \d+ in modal$", re.I),
    re.compile(r"Skip to (product|content|search|navigation|footer|region)", re.I),
    re.compile(r"\$[\d,]+\.?\d*"),  # pricing
    re.compile(r"Liquid error", re.I),
    re.compile(r"javascript required|enable javascript|update your browser", re.I),
    re.compile(
        r"^(New Arrivals|Best Sellers|Gift Cards|Shop All|My Account|Track My Order|Help Center|Find a Store|Top Rated|Follow Along|Watch Now)\s*$",
        re.I,
    ),
    re.compile(
        r"We.ve got our hands full|This page will automatically refresh|please call.*\d{3}.*\d{4}",
        re.I,
    ),
    re.compile(r"(regular price|sale price|starting at)", re.I),
]


def _is_boilerplate(s: str) -> bool:
    """Return True if the string is boilerplate or too short to be useful."""
    for pat in _BOILERPLATE_PATTERNS:
        if pat.search(s):
            return True
    stripped = s.strip()
    if (
        len(stripped) < 15
        and not re.search(r"\d", stripped)
        and not re.search(r"[A-Z]{2,}", stripped)
    ):
        return True
    return False


def _real_blocks(blocks: list[str]) -> list[str]:
    """Filter to non-boilerplate blocks."""
    return [b for b in blocks if not _is_boilerplate(b)]


def _real_bullets(bullets: list[str]) -> list[str]:
    """Filter to non-boilerplate bullets."""
    return [b for b in bullets if not _is_boilerplate(b)]


# ---------------------------------------------------------------------------
# Axis 1: Content signal (0-30)
# ---------------------------------------------------------------------------


def score_content_signal(facts: ExtractedFacts) -> AxisScore:
    """How much real content vs. boilerplate is present."""
    axis = AxisScore(name="content_signal", score=0, max_score=30)

    total_blocks = len(facts.description_blocks)
    total_bullets = len(facts.feature_bullets)
    total = total_blocks + total_bullets

    real_b = _real_blocks(facts.description_blocks)
    real_f = _real_bullets(facts.feature_bullets)
    real = len(real_b) + len(real_f)

    if total == 0:
        ratio = 0.0
    else:
        ratio = real / total

    if ratio >= 0.80:
        axis.score = 28
    elif ratio >= 0.60:
        axis.score = 22
    elif ratio >= 0.40:
        axis.score = 15
    elif ratio >= 0.20:
        axis.score = 8
    elif ratio >= 0.01:
        axis.score = 3
    else:
        axis.score = 0

    axis.details.append(f"signal ratio {real}/{total} = {ratio:.2f}")

    # JSON-LD rescue bonus
    if facts.json_ld_data:
        desc = facts.json_ld_data.get("description", "")
        if isinstance(desc, str) and len(desc.strip()) > 50:
            bonus = min(5, 30 - axis.score)
            if bonus > 0:
                axis.score += bonus
                axis.details.append(f"JSON-LD rescue +{bonus}")

    return axis


# ---------------------------------------------------------------------------
# Axis 2: Field completeness (0-25)
# ---------------------------------------------------------------------------


def score_field_completeness(facts: ExtractedFacts) -> AxisScore:
    """How many useful fields are populated."""
    axis = AxisScore(name="field_completeness", score=0, max_score=25)

    if facts.product_name:
        axis.score += 4
        axis.details.append("product_name present")

    if facts.brand:
        axis.score += 3
        axis.details.append("brand present")

    real_b = _real_blocks(facts.description_blocks)
    if len(real_b) >= 1:
        axis.score += 5
        axis.details.append(f"{len(real_b)} real description blocks")
    else:
        axis.details.append("no real description blocks")

    real_f = _real_bullets(facts.feature_bullets)
    if len(real_f) >= 3:
        axis.score += 5
        axis.details.append(f"{len(real_f)} real feature bullets")
    elif len(real_f) == 2:
        axis.score += 3
        axis.details.append(f"{len(real_f)} real feature bullets (partial)")
    elif len(real_f) == 1:
        axis.score += 1
        axis.details.append(f"{len(real_f)} real feature bullet (partial)")
    else:
        axis.details.append("no real feature bullets")

    if len(facts.specs) >= 2:
        axis.score += 4
        axis.details.append(f"{len(facts.specs)} spec entries")
    elif len(facts.specs) == 1:
        axis.score += 2
        axis.details.append("1 spec entry (partial)")
    else:
        axis.details.append("no specs")

    if facts.materials:
        axis.score += 2
        axis.details.append("materials present")

    if facts.care:
        axis.score += 2
        axis.details.append("care present")

    return axis


# ---------------------------------------------------------------------------
# Axis 3: Specificity (0-25)
# ---------------------------------------------------------------------------

_MATERIAL_TERMS = re.compile(
    r"(polyester|nylon|cotton|wool|merino|spandex|elastane|lycra|gore-tex|"
    r"primaloft|thinsulate|pertex|cordura|ripstop|fleece|down|silk|linen|"
    r"rayon|modal|tencel|hemp|kevlar|dyneema|polartec)",
    re.I,
)
_MEASUREMENT_UNITS = re.compile(
    r"\b\d+[\s-]*(mm|cm|m|g|kg|oz|lb|in|ft|denier|D|L|ml|mL)\b",
    re.I,
)
_UPPERCASE_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")
_FIBER_COMPOSITION = re.compile(r"\d+\s*%", re.I)


def score_specificity(facts: ExtractedFacts) -> AxisScore:
    """How technically specific and substantive the extracted content is."""
    axis = AxisScore(name="specificity", score=0, max_score=25)

    real_b = _real_blocks(facts.description_blocks)
    real_f = _real_bullets(facts.feature_bullets)
    all_content = " ".join(real_b + real_f)

    # 1. Product name tokens found in real content (+7)
    if facts.product_name and all_content:
        name_tokens = [t.lower() for t in facts.product_name.split() if len(t) > 2]
        if name_tokens:
            content_lower = all_content.lower()
            hits = sum(1 for t in name_tokens if t in content_lower)
            if hits > 0:
                axis.score += 7
                axis.details.append(f"product name tokens in content: {hits}/{len(name_tokens)}")
            else:
                axis.details.append("product name not found in content")
        else:
            axis.details.append("product name too short to check")
    else:
        axis.details.append("no product name or content to check")

    # 2. Technical terminology (+6)
    tech_hits = 0
    tech_hits += len(_MEASUREMENT_UNITS.findall(all_content))
    tech_hits += len(_MATERIAL_TERMS.findall(all_content))
    tech_hits += len(_UPPERCASE_ACRONYM.findall(all_content))

    if tech_hits >= 3:
        axis.score += 6
        axis.details.append(f"technical terms: {tech_hits}")
    elif tech_hits >= 1:
        axis.score += 4
        axis.details.append(f"technical terms: {tech_hits} (partial)")
    else:
        axis.details.append("no technical terminology")

    # 3. Specs have substantive values (+6)
    substantive = 0
    for v in facts.specs.values():
        if re.search(r"\d", v) or len(v.strip()) > 3:
            substantive += 1
    spec_score = min(6, substantive * 2)
    axis.score += spec_score
    if substantive:
        axis.details.append(f"substantive specs: {substantive}")
    else:
        axis.details.append("no substantive specs")

    # 4. Materials composition (+6)
    if facts.materials:
        has_pct = bool(_FIBER_COMPOSITION.search(facts.materials))
        has_fiber = bool(_MATERIAL_TERMS.search(facts.materials))
        if has_pct and has_fiber:
            axis.score += 6
            axis.details.append("materials: composition with %")
        elif has_pct or has_fiber:
            axis.score += 3
            axis.details.append("materials: partial composition")
        else:
            axis.score += 3
            axis.details.append("materials: non-empty but no composition")
    else:
        axis.details.append("materials empty")

    return axis


# ---------------------------------------------------------------------------
# Axis 4: Deduplication (0-20)
# ---------------------------------------------------------------------------


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity on word sets."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _ngrams(text: str, n: int = 6) -> set[tuple[str, ...]]:
    """Extract word n-grams from text."""
    words = text.lower().split()
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def score_deduplication(facts: ExtractedFacts) -> AxisScore:
    """Penalize duplicated content across and within fields."""
    axis = AxisScore(name="deduplication", score=0, max_score=20)

    real_b = _real_blocks(facts.description_blocks)
    real_f = _real_bullets(facts.feature_bullets)

    # 1. Duplicate description blocks (+8, -2 per dupe)
    seen = set()
    dupes = 0
    for block in real_b:
        normalized = block.strip().lower()
        if normalized in seen:
            dupes += 1
        else:
            seen.add(normalized)
    block_score = max(0, 8 - dupes * 2)
    axis.score += block_score
    if dupes:
        axis.details.append(f"duplicate blocks: {dupes}")
    else:
        axis.details.append("no duplicate blocks")

    # 2. Near-duplicate feature bullets — Jaccard >0.7 (+6, -2 per pair)
    near_dupes = 0
    for i in range(len(real_f)):
        for j in range(i + 1, len(real_f)):
            if _jaccard(real_f[i], real_f[j]) > 0.7:
                near_dupes += 1
    bullet_score = max(0, 6 - near_dupes * 2)
    axis.score += bullet_score
    if near_dupes:
        axis.details.append(f"near-duplicate bullet pairs: {near_dupes}")
    else:
        axis.details.append("no near-duplicate bullets")

    # 3. Cross-field repetition — 6-gram overlap between blocks and bullets (+6, -3 per collision)
    block_ngrams: set[tuple[str, ...]] = set()
    for block in real_b:
        block_ngrams |= _ngrams(block)

    bullet_ngrams: set[tuple[str, ...]] = set()
    for bullet in real_f:
        bullet_ngrams |= _ngrams(bullet)

    collisions = len(block_ngrams & bullet_ngrams)
    cross_score = max(0, 6 - collisions * 3)
    axis.score += cross_score
    if collisions:
        axis.details.append(f"cross-field 6-gram collisions: {collisions}")
    else:
        axis.details.append("no cross-field repetition")

    return axis


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def score_facts(facts: ExtractedFacts) -> QualityScore:
    """Compute composite quality score for extracted facts."""
    qs = QualityScore(handle=facts.canonical_url or "unknown")
    qs.axes["content_signal"] = score_content_signal(facts)
    qs.axes["field_completeness"] = score_field_completeness(facts)
    qs.axes["specificity"] = score_specificity(facts)
    qs.axes["deduplication"] = score_deduplication(facts)
    return qs


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def score_facts_dir(
    test_dir: Path,
    handles: list[str] | None = None,
) -> list[QualityScore]:
    """Score extracted facts from each handle subdirectory.

    Loads ``extracted_facts.json`` from each handle subdirectory in *test_dir*.
    """
    if handles is None:
        handles = [
            d.name
            for d in sorted(test_dir.iterdir())
            if d.is_dir() and (d / "extracted_facts.json").exists()
        ]

    scores = []
    for handle in handles:
        facts_path = test_dir / handle / "extracted_facts.json"
        if not facts_path.exists():
            logger.info("Skipping %s: no extracted_facts.json", handle)
            continue

        try:
            data = json.loads(facts_path.read_text())
            facts = ExtractedFacts(**data)
        except Exception as e:
            logger.warning("Failed to load %s: %s", facts_path, e)
            continue

        qs = score_facts(facts)
        qs.handle = handle
        scores.append(qs)

    return scores
