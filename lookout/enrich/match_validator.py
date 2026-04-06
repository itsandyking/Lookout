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

DEMOGRAPHICS = frozenset(
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

# Normalize "men" → "mens", "women" → "womens" so they match
_DEMO_NORMALIZE = {"men": "mens", "women": "womens"}

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)

_PRICE_KEYS = {
    "price",
    "msrp",
    "regular price",
    "retail price",
    "base price",
    "price range",
    "our price",
}
_PRICE_RE = re.compile(r"[\$€£]([\d,]+\.?\d*)")


def extract_page_title(markdown: str, catalog_title: str | None = None) -> str | None:
    """Extract the best product title heading from scraped markdown.

    Strategy: find ALL headings, then pick the one most similar to the
    catalog title. This avoids false matches on navigation, marketing,
    or section headings that happen to appear first.

    If no catalog_title is provided, returns the first heading (legacy).
    Returns None if no heading found or no heading has meaningful overlap.
    """
    if not markdown:
        return None

    headings = _HEADING_RE.findall(markdown)
    if not headings:
        return None

    headings = [h.strip() for h in headings if h.strip()]
    if not headings:
        return None

    # Without catalog title, fall back to first heading (legacy behavior)
    if not catalog_title:
        return headings[0]

    # Score each heading by word overlap with catalog title
    catalog_words = _extract_words(catalog_title)
    filler = {"the", "a", "an", "by", "for", "in", "of", "and", "with", "s"}
    catalog_meaningful = catalog_words - filler - DEMOGRAPHICS
    if not catalog_meaningful:
        return headings[0]

    best_heading = None
    best_overlap = 0.0

    for heading in headings:
        heading_words = _extract_words(heading)
        heading_meaningful = heading_words - filler - DEMOGRAPHICS
        if not heading_meaningful:
            continue
        overlap = len(catalog_meaningful & heading_meaningful) / len(catalog_meaningful)
        if overlap > best_overlap:
            best_overlap = overlap
            best_heading = heading

    # Need at least 30% word overlap to consider it a product title
    if best_overlap >= 0.3:
        return best_heading

    return None


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
    # Normalize "men"→"mens", "women"→"womens" so "Men's" matches "Mens"
    page_demos = {_DEMO_NORMALIZE.get(w, w) for w in page_words & DEMOGRAPHICS}
    catalog_demos = {_DEMO_NORMALIZE.get(w, w) for w in catalog_words & DEMOGRAPHICS}

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


def _extract_price_from_facts(facts) -> float | None:
    """Extract a numeric price from facts.specs or JSON-LD offers.

    Checks specs keys (case-insensitive) for common price labels, parses
    the first dollar/euro/pound amount found, then falls back to JSON-LD.
    """
    # 1. Check facts.specs
    if facts.specs:
        for key, value in facts.specs.items():
            if key.strip().lower() in _PRICE_KEYS:
                m = _PRICE_RE.search(str(value))
                if m:
                    try:
                        return float(m.group(1).replace(",", ""))
                    except ValueError:
                        pass

    # 2. Fallback to JSON-LD offers.price
    if facts.json_ld_data:
        offers = facts.json_ld_data.get("offers", {})
        raw_price = None
        if isinstance(offers, dict):
            raw_price = offers.get("price")
        elif isinstance(offers, list) and offers:
            raw_price = offers[0].get("price")
        if raw_price is not None:
            try:
                return float(raw_price)
            except (ValueError, TypeError):
                pass

    return None


def check_post_extraction(
    facts,  # ExtractedFacts
    catalog_title: str,
    catalog_price: float | None,
    catalog_colors: list[str],
    vendor_colors: list[str] | None = None,
) -> dict:
    """Stage 2: aggregate post-extraction signals into a confidence score.

    Returns:
        {"pass": bool, "confidence": float,
         "signals": {"title_similarity", "price_ratio", "color_overlap", "content_quality"},
         "reason": str}
    """
    from .pipeline import _assess_extraction_quality
    from .season_signals import score_color_overlap

    signals: dict = {}

    # Title similarity (40% weight)
    if facts.product_name:
        title_sim = SequenceMatcher(None, facts.product_name.lower(), catalog_title.lower()).ratio()
    else:
        title_sim = 0.0
    signals["title_similarity"] = title_sim

    # Price plausibility (25% weight)
    scraped_price = _extract_price_from_facts(facts)

    if scraped_price is not None and catalog_price and catalog_price > 0:
        try:
            price_diff = abs(scraped_price - catalog_price) / catalog_price
            price_score = (
                max(0.0, min(1.0, 1.0 - (price_diff - 0.2) / 0.4)) if price_diff > 0.2 else 1.0
            )
        except (ValueError, TypeError):
            price_score = 0.5
    else:
        price_score = 0.5
    signals["price_ratio"] = price_score

    # Color overlap (25% weight)
    # Priority: 1) vendor_colors param (swatch extraction), 2) specs, 3) variants
    resolved_colors: list[str] = []
    if vendor_colors:
        resolved_colors = vendor_colors
    elif facts.specs:
        for key, value in facts.specs.items():
            if key.strip().lower() in ("color", "colour", "colors", "colours"):
                resolved_colors = [c.strip() for c in re.split(r"[/,|]", str(value)) if c.strip()]
                break
    if not resolved_colors:
        for v in facts.variants:
            if v.option_name.lower() in ("color", "colour", "style"):
                resolved_colors = v.values
                break

    if resolved_colors and catalog_colors:
        overlap_result = score_color_overlap(catalog_colors, resolved_colors)
        color_score = overlap_result["overlap"]
    else:
        color_score = 0.5
    signals["color_overlap"] = color_score

    # Content quality (10% weight)
    quality = _assess_extraction_quality(facts)
    quality_score = 1.0 if quality["usable"] else 0.0
    signals["content_quality"] = quality_score

    # Weighted confidence
    confidence = title_sim * 40 + price_score * 25 + color_score * 25 + quality_score * 10

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
        resolver_candidates: list[dict] | None = None,
        brave_image_search: dict | None = None,
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
        if resolver_candidates is not None:
            record["resolver_candidates"] = resolver_candidates
        if brave_image_search:
            record["brave_image_search"] = brave_image_search
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")
        logger.info(
            "Match decision for %s: %s (%d candidates tried)",
            handle,
            outcome,
            len(candidates_tried),
        )
