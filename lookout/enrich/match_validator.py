"""Post-scrape match validation.

Two-stage validation for product URL matches:
- Stage 1 (title gate): cheap string comparison after scrape, before LLM
- Stage 2 (signal aggregation): aggregate post-extraction signals

All decisions are logged to match_decisions.jsonl for future resolver tuning.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

DEMOGRAPHICS = frozenset({
    "youth", "kids", "boys", "girls", "junior", "jr",
    "mens", "men", "womens", "women", "unisex",
})

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)

# Section headings that are NOT product titles — skip the title gate for these
_GENERIC_HEADINGS = frozenset({
    "features", "materials", "specifications", "specs", "details",
    "description", "overview", "reviews", "shipping", "returns",
    "related products", "you may also like", "similar products",
    "materiales", "características",  # Spanish variants
})

# Patterns in headings that indicate non-product content
_NON_PRODUCT_PATTERNS = re.compile(
    r"(?i)"
    r"(?:^color:|^colour:|^size:|^rating|^question|^review|^faq|^help"
    r"|experts?\s+break|we\s+got\s+you|you\s+may\s+also"
    r"|^share\b|^shop\s+(?:all|the)|^free\s+shipping"
    r"|^sign\s+up|^subscribe|^newsletter"
    r"|^your\s+cart|^shopping\s+cart|^cart\b|^checkout"
    r"|^\$\d|^\d+[\.,]\d{2}\s*$"  # Prices like "$359.99"
    r"|^save\s+\d|^sold\s+out|^out\s+of\s+stock)",
)


def extract_page_title(markdown: str) -> str | None:
    """Extract the primary product title from scraped markdown.

    Returns None if no heading found or if the first heading is a generic
    section name rather than a product title. Uses both a static list and
    pattern matching to filter non-product headings.
    """
    if not markdown:
        return None
    match = _HEADING_RE.search(markdown)
    if not match:
        return None
    title = match.group(1).strip()
    # Skip generic section headings
    if title.lower() in _GENERIC_HEADINGS:
        return None
    # Skip headings matching non-product patterns
    if _NON_PRODUCT_PATTERNS.search(title):
        return None
    return title


def _extract_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def check_title_gate(
    page_title: str,
    catalog_title: str,
) -> dict:
    """Stage 1: cheap title + demographic check.

    Returns:
        {"pass": bool, "title_similarity": float,
         "demographic_match": bool | None, "reason": str}
    """
    page_lower = page_title.lower()
    catalog_lower = catalog_title.lower()

    title_sim = SequenceMatcher(None, page_lower, catalog_lower).ratio()

    page_words = _extract_words(page_title)
    catalog_words = _extract_words(catalog_title)
    page_demos = page_words & DEMOGRAPHICS
    catalog_demos = catalog_words & DEMOGRAPHICS

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

    # Word overlap — if key product words appear in page title, it's likely correct
    # even if overall string similarity is low (e.g. SPA pages with short titles)
    filler = {"the", "a", "an", "by", "for", "in", "of", "and", "with", "s"}
    catalog_meaningful = catalog_words - filler - DEMOGRAPHICS
    page_meaningful = page_words - filler - DEMOGRAPHICS
    word_overlap = len(catalog_meaningful & page_meaningful) / max(len(catalog_meaningful), 1)

    if title_sim < 0.4 and word_overlap < 0.3:
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


def check_post_extraction(
    facts,  # ExtractedFacts
    catalog_title: str,
    catalog_price: float | None,
    catalog_colors: list[str],
) -> dict:
    """Stage 2: aggregate post-extraction signals into a confidence score.

    Returns:
        {"pass": bool, "confidence": float,
         "signals": {"title_similarity", "price_ratio", "color_overlap", "content_quality"},
         "reason": str}
    """
    from .season_signals import score_color_overlap
    from .pipeline import _assess_extraction_quality

    signals: dict = {}

    # Title similarity (40% weight)
    if facts.product_name:
        title_sim = SequenceMatcher(
            None, facts.product_name.lower(), catalog_title.lower()
        ).ratio()
    else:
        title_sim = 0.0
    signals["title_similarity"] = title_sim

    # Price plausibility (25% weight)
    scraped_price = None
    if facts.json_ld_data:
        offers = facts.json_ld_data.get("offers", {})
        if isinstance(offers, dict):
            scraped_price = offers.get("price")
        elif isinstance(offers, list) and offers:
            scraped_price = offers[0].get("price")

    if scraped_price is not None and catalog_price and catalog_price > 0:
        try:
            price_diff = abs(float(scraped_price) - catalog_price) / catalog_price
            price_score = max(0.0, min(1.0, 1.0 - (price_diff - 0.2) / 0.4)) if price_diff > 0.2 else 1.0
        except (ValueError, TypeError):
            price_score = 0.5
    else:
        price_score = 0.5
    signals["price_ratio"] = price_score

    # Color overlap (25% weight)
    vendor_colors: list[str] = []
    for v in facts.variants:
        if v.option_name.lower() in ("color", "colour", "style"):
            vendor_colors = v.values
            break

    if vendor_colors and catalog_colors:
        overlap_result = score_color_overlap(catalog_colors, vendor_colors)
        color_score = overlap_result["overlap"]
    else:
        color_score = 0.5
    signals["color_overlap"] = color_score

    # Content quality (10% weight)
    quality = _assess_extraction_quality(facts)
    quality_score = 1.0 if quality["usable"] else 0.0
    signals["content_quality"] = quality_score

    # Weighted confidence
    confidence = (
        title_sim * 40
        + price_score * 25
        + color_score * 25
        + quality_score * 10
    )

    passed = confidence >= 50
    reason = "ok" if passed else f"low_post_scrape_confidence: {confidence:.0f}"

    return {
        "pass": passed,
        "confidence": confidence,
        "signals": signals,
        "reason": reason,
    }


class MatchDecisionLogger:
    """Appends match decisions to a JSONL file for future resolver tuning."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def log(
        self,
        handle: str,
        vendor: str,
        catalog_title: str,
        candidates_tried: list[dict],
        outcome: str,
        final_url: str | None,
        catalog_price: float | None = None,
        catalog_colors: list[str] | None = None,
    ) -> None:
        record = {
            "handle": handle,
            "vendor": vendor,
            "catalog_title": catalog_title,
            "catalog_price": catalog_price,
            "catalog_colors": catalog_colors or [],
            "timestamp": datetime.now(UTC).isoformat(),
            "candidates_tried": candidates_tried,
            "outcome": outcome,
            "final_url": final_url,
            "retries": len(candidates_tried) - 1 if candidates_tried else 0,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")
        logger.info(
            "Match decision for %s: %s (%d candidates tried)",
            handle, outcome, len(candidates_tried),
        )
