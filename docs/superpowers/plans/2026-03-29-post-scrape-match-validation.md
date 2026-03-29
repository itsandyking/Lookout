# Post-Scrape Match Validation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate product matches after scraping using title and signal checks, retry with next-best candidates on failure, and log all match decisions to a JSONL file for future resolver improvement.

**Architecture:** New `match_validator.py` module with two validation stages (title gate + signal aggregation). Pipeline retry loop tries up to 3 candidates. All decisions logged to `match_decisions.jsonl` per run. Resolver scoring fixes (asymmetric height, year vs model, demographics) included.

**Tech Stack:** Python, difflib.SequenceMatcher, existing season_signals.score_color_overlap

**Spec:** `docs/superpowers/specs/2026-03-29-post-scrape-match-validation-design.md`

---

### Task 1: Resolver Scoring Fixes

Fix three scoring bugs in the resolver before building the validation layer. These reduce the number of bad candidates that reach validation.

**Files:**
- Modify: `lookout/enrich/resolver.py`
- Create: `tests/test_resolver.py`

- [ ] **Step 1: Write tests for asymmetric height, year vs model, and demographics**

```python
# tests/test_resolver.py
"""Tests for resolver scoring edge cases."""

import re as _re
from difflib import SequenceMatcher


def _extract_words(text: str) -> set[str]:
    """Extract lowercase alphanumeric words from text."""
    return set(_re.findall(r"[a-z0-9]+", text.lower()))


# -- Asymmetric height detection --

def test_asymmetric_height_candidate_has_mid_expected_has_none():
    """Candidate 'Alp Trainer 2 Mid' should be penalized when expected is 'Alp Trainer GTX' (no height word)."""
    expected = "Men's Alp Trainer GTX"
    candidate = "Alp Trainer 2 Mid GORE-TEX Men's Shoe"
    height_fit_words = {"low", "mid", "high", "tall", "short", "wide", "narrow"}

    expected_words = _extract_words(expected)
    candidate_words = _extract_words(candidate)
    extra_words = candidate_words - expected_words
    extra_height = extra_words & height_fit_words
    has_height_in_expected = bool(expected_words & height_fit_words)

    # The fix: extra_height exists but expected has no height word
    assert extra_height == {"mid"}
    assert not has_height_in_expected
    # This combination should trigger a penalty


def test_symmetric_height_both_have_words():
    """Both titles have height words and they differ — critical mismatch."""
    expected = "Alp Trainer Low GTX"
    candidate = "Alp Trainer Mid GTX"
    height_fit_words = {"low", "mid", "high", "tall", "short", "wide", "narrow"}

    expected_words = _extract_words(expected)
    candidate_words = _extract_words(candidate)
    missing_height = (expected_words - candidate_words) & height_fit_words
    extra_height = (candidate_words - expected_words) & height_fit_words

    assert missing_height == {"low"}
    assert extra_height == {"mid"}
    # Both non-empty → critical_mismatch = True (existing behavior, should still work)


def test_no_height_words_no_penalty():
    """Neither title has height words — no penalty."""
    expected = "Foamy Sleeping Pad"
    candidate = "Foamy Sleeping Pad"
    height_fit_words = {"low", "mid", "high", "tall", "short", "wide", "narrow"}

    expected_words = _extract_words(expected)
    assert not (expected_words & height_fit_words)


# -- Year vs model number --

def test_year_mismatch_not_critical():
    """Year difference (2024 vs 2026) should NOT be critical_mismatch."""
    expected_title = "Youth Reverb Ski Boots 2024"
    candidate_title = "Reverb Youth Ski Boots 2026"

    expected_numbers = set(_re.findall(r"\d+", expected_title.lower()))
    candidate_numbers = set(_re.findall(r"\d+", candidate_title.lower()))

    # Both have numbers, no overlap
    assert expected_numbers == {"2024"}
    assert candidate_numbers == {"2026"}
    assert not (expected_numbers & candidate_numbers)

    # But these are years, not model numbers — should get minor penalty, not critical
    expected_years = set(_re.findall(r"20[2-3]\d", expected_title))
    candidate_years = set(_re.findall(r"20[2-3]\d", candidate_title))
    assert expected_years == {"2024"}
    assert candidate_years == {"2026"}


def test_model_number_mismatch_is_critical():
    """Model number difference (99Ti vs 95) should be critical."""
    expected_title = "99Ti Skis"
    candidate_title = "95 Skis"

    # Strip out years first, then check remaining numbers
    year_re = _re.compile(r"20[2-3]\d")
    expected_non_year = set(_re.findall(r"\d+", expected_title)) - set(year_re.findall(expected_title))
    candidate_non_year = set(_re.findall(r"\d+", candidate_title)) - set(year_re.findall(candidate_title))

    assert expected_non_year == {"99"}
    assert candidate_non_year == {"95"}
    assert not (expected_non_year & candidate_non_year)
    # These are model numbers, not years — should be critical


# -- Demographic mismatch --

def test_demographic_mismatch_youth_vs_womens():
    """Youth vs Women's should be penalized."""
    demographics = {"youth", "kids", "boys", "girls", "mens", "men", "womens", "women", "unisex"}
    expected_words = _extract_words("Youth Reverb Ski Boots")
    candidate_words = _extract_words("Revolve TBL Women's Ski Boots")

    expected_demos = expected_words & demographics
    candidate_demos = candidate_words & demographics

    assert expected_demos == {"youth"}
    assert candidate_demos == {"women" }  # "women's" → "women"
    assert not (expected_demos & candidate_demos)


def test_demographic_match_no_penalty():
    """Same demographic should not be penalized."""
    demographics = {"youth", "kids", "boys", "girls", "mens", "men", "womens", "women", "unisex"}
    expected_words = _extract_words("Men's Cloudrock Low WP")
    candidate_words = _extract_words("Men's Cloudrock Low Waterproof")

    expected_demos = expected_words & demographics
    candidate_demos = candidate_words & demographics

    assert expected_demos & candidate_demos  # both have "men"
```

- [ ] **Step 2: Run tests to verify they pass (these test the logic, not the resolver)**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_resolver.py -v
```

Expected: all pass (they test the detection logic directly, not the resolver method).

- [ ] **Step 3: Fix asymmetric height detection in resolver.py**

In `lookout/enrich/resolver.py`, find the height_fit_words check (around line 295-301). After the existing `if missing_height and extra_height:` block, add the asymmetric case:

```python
                # Height/fit words — "Low" vs "Mid" is a different product
                height_fit_words = {"low", "mid", "high", "tall", "short",
                                    "wide", "narrow"}
                missing_height = missing_words & height_fit_words
                extra_height = extra_words & height_fit_words
                if missing_height and extra_height:
                    critical_mismatch = True
                elif extra_height and not (title_words & height_fit_words):
                    # Candidate specifies height (e.g. "Mid") but expected doesn't —
                    # likely a different variant of the same product line
                    candidate.confidence = max(0, candidate.confidence - 15)
                    candidate.reasoning += " -asymmetric_height"
```

- [ ] **Step 4: Fix year vs model number in resolver.py**

Replace the model number check (around line 326-331):

```python
                # Number mismatch — separate years from model numbers
                _year_pattern = _re.compile(r"20[2-3]\d")
                expected_years = set(_year_pattern.findall(title_lower))
                candidate_years = set(_year_pattern.findall(candidate_title))
                expected_numbers = set(_re.findall(r"\d+", title_lower)) - expected_years
                candidate_numbers = set(_re.findall(r"\d+", candidate_title)) - candidate_years

                # Model number mismatch is critical (e.g. "99Ti" vs "95")
                if expected_numbers and candidate_numbers:
                    if not expected_numbers & candidate_numbers:
                        critical_mismatch = True

                # Year mismatch is minor (e.g. "2024" vs "2026" — same product, different year)
                if expected_years and candidate_years:
                    if not expected_years & candidate_years:
                        candidate.confidence = max(0, candidate.confidence - 5)
                        candidate.reasoning += " -year_mismatch"
```

- [ ] **Step 5: Add demographic mismatch detection in resolver.py**

Add after the collab detection block (around line 324), before the model number check:

```python
                # Demographic mismatch — "Youth" vs "Women's" etc.
                demographics = {"youth", "kids", "boys", "girls", "mens", "men",
                                "womens", "women", "unisex", "junior", "jr"}
                expected_demos = title_words & demographics
                candidate_demos = candidate_words & demographics
                if expected_demos and candidate_demos:
                    if not expected_demos & candidate_demos:
                        candidate.confidence = max(0, candidate.confidence - 15)
                        candidate.reasoning += " -demographic_mismatch"
```

- [ ] **Step 6: Update generic_words to include demographics**

In the `generic_words` set (around line 336), add demographic words so they don't trigger false positives in the foreign product name check:

```python
                generic_words = type_words | height_fit_words | edition_words | demographics | {
                    "rope", "ropes", "cord", "ski", "skis", "boot", "boots",
                    "new", "sale",
                    "2024", "2025", "2026", "2027",
                }
```

Remove `"mens", "womens", "men", "women", "kids"` from the existing set since they're now in `demographics`.

- [ ] **Step 7: Run full test suite**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/resolver.py tests/test_resolver.py && git commit -m "fix: resolver scoring — asymmetric height, year vs model, demographics"
```

---

### Task 2: Match Validator Module

Create the `match_validator.py` module with two validation stages.

**Files:**
- Create: `lookout/enrich/match_validator.py`
- Create: `tests/test_match_validator.py`

- [ ] **Step 1: Write tests for title extraction and title gate**

```python
# tests/test_match_validator.py
"""Tests for post-scrape match validation."""


def test_extract_page_title_from_heading():
    from lookout.enrich.match_validator import extract_page_title

    md = "# Reverb Youth Ski Boots 2026\n\nSome content here."
    assert extract_page_title(md) == "Reverb Youth Ski Boots 2026"


def test_extract_page_title_from_h2_when_no_h1():
    from lookout.enrich.match_validator import extract_page_title

    md = "## Alp Trainer 2 Low GORE-TEX\n\nSome content."
    assert extract_page_title(md) == "Alp Trainer 2 Low GORE-TEX"


def test_extract_page_title_none_for_empty():
    from lookout.enrich.match_validator import extract_page_title

    assert extract_page_title("") is None
    assert extract_page_title("No headings here, just text.") is None


def test_title_gate_pass():
    from lookout.enrich.match_validator import check_title_gate

    result = check_title_gate("Reverb Youth Ski Boots", "Youth Reverb Ski Boots 2024")
    assert result["pass"] is True
    assert result["title_similarity"] > 0.5


def test_title_gate_reject_low_similarity():
    from lookout.enrich.match_validator import check_title_gate

    result = check_title_gate("Revolve TBL Women's Ski Boots", "Youth Reverb Ski Boots 2024")
    assert result["pass"] is False
    assert result["title_similarity"] < 0.5


def test_title_gate_reject_demographic_mismatch():
    from lookout.enrich.match_validator import check_title_gate

    # Even if title similarity is OK, demographic mismatch should reject
    result = check_title_gate("Reverb Women's Ski Boots", "Youth Reverb Ski Boots 2024")
    assert result["pass"] is False
    assert result["demographic_match"] is False


def test_title_gate_no_demographics():
    from lookout.enrich.match_validator import check_title_gate

    # No demographics in either title — should pass if similarity is OK
    result = check_title_gate("Foamy Sleeping Pad", "Foamy Sleeping Pad")
    assert result["pass"] is True
    assert result["demographic_match"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py -v
```

Expected: FAIL — `match_validator` module doesn't exist yet.

- [ ] **Step 3: Implement extract_page_title and check_title_gate**

```python
# lookout/enrich/match_validator.py
"""Post-scrape match validation.

Two-stage validation for product URL matches:
- Stage 1 (title gate): cheap string comparison after scrape, before LLM
- Stage 2 (signal aggregation): aggregate post-extraction signals

All decisions are logged to match_decisions.jsonl for future resolver tuning.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Demographic words for mismatch detection
DEMOGRAPHICS = frozenset({
    "youth", "kids", "boys", "girls", "junior", "jr",
    "mens", "men", "womens", "women", "unisex",
})

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)


def extract_page_title(markdown: str) -> str | None:
    """Extract the primary title from scraped markdown.

    Looks for the first markdown heading (# or ## or ###).
    Returns None if no heading found.
    """
    if not markdown:
        return None
    match = _HEADING_RE.search(markdown)
    return match.group(1).strip() if match else None


def _extract_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def check_title_gate(
    page_title: str,
    catalog_title: str,
) -> dict:
    """Stage 1: cheap title + demographic check.

    Args:
        page_title: Title extracted from scraped page.
        catalog_title: Product title from Shopify catalog.

    Returns:
        {"pass": bool, "title_similarity": float,
         "demographic_match": bool | None, "reason": str}
    """
    page_lower = page_title.lower()
    catalog_lower = catalog_title.lower()

    # Title similarity via SequenceMatcher
    title_sim = SequenceMatcher(None, page_lower, catalog_lower).ratio()

    # Demographic check
    page_words = _extract_words(page_title)
    catalog_words = _extract_words(catalog_title)
    page_demos = page_words & DEMOGRAPHICS
    catalog_demos = catalog_words & DEMOGRAPHICS

    demographic_match: bool | None = None
    if page_demos and catalog_demos:
        demographic_match = bool(page_demos & catalog_demos)
    elif page_demos or catalog_demos:
        # One has demographics, the other doesn't — ambiguous, allow
        demographic_match = None

    # Decision
    if demographic_match is False:
        return {
            "pass": False,
            "title_similarity": title_sim,
            "demographic_match": False,
            "reason": f"demographic_mismatch: {catalog_demos} vs {page_demos}",
        }

    if title_sim < 0.4:
        return {
            "pass": False,
            "title_similarity": title_sim,
            "demographic_match": demographic_match,
            "reason": f"title_similarity_too_low: {title_sim:.2f}",
        }

    return {
        "pass": True,
        "title_similarity": title_sim,
        "demographic_match": demographic_match,
        "reason": "ok",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py -v
```

Expected: all pass.

- [ ] **Step 5: Write tests for signal aggregation (Stage 2)**

Add to `tests/test_match_validator.py`:

```python
def test_post_extraction_pass_strong_signals():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts, VariantOption

    facts = ExtractedFacts(
        product_name="Reverb Youth Ski Boots",
        images=[],
        variants=[VariantOption(option_name="Color", values=["Black", "Blue"])],
        json_ld_data={"offers": {"price": "199.99"}},
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Youth Reverb Ski Boots 2024",
        catalog_price=199.99,
        catalog_colors=["Black", "White/Blue"],
    )
    assert result["pass"] is True
    assert result["confidence"] >= 50


def test_post_extraction_fail_wrong_product():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts, VariantOption

    facts = ExtractedFacts(
        product_name="Completely Different Product",
        images=[],
        variants=[VariantOption(option_name="Color", values=["Red", "Green"])],
        json_ld_data={"offers": {"price": "49.99"}},
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Youth Reverb Ski Boots 2024",
        catalog_price=199.99,
        catalog_colors=["Black", "White/Blue"],
    )
    assert result["pass"] is False
    assert result["confidence"] < 50


def test_post_extraction_missing_signals_neutral():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Reverb Youth Ski Boots",
        images=[],
        variants=[],
        json_ld_data=None,
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Youth Reverb Ski Boots 2024",
        catalog_price=None,
        catalog_colors=[],
    )
    # Missing price and colors → neutral (0.5 each), title match is strong
    assert result["pass"] is True
    assert result["confidence"] >= 50
```

- [ ] **Step 6: Run tests to verify they fail**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py::test_post_extraction_pass_strong_signals -v
```

Expected: FAIL — `check_post_extraction` not defined.

- [ ] **Step 7: Implement check_post_extraction**

Add to `lookout/enrich/match_validator.py`:

```python
from .models import ExtractedFacts
from .season_signals import score_color_overlap


def check_post_extraction(
    facts: ExtractedFacts,
    catalog_title: str,
    catalog_price: float | None,
    catalog_colors: list[str],
) -> dict:
    """Stage 2: aggregate post-extraction signals into a confidence score.

    Args:
        facts: Extracted facts from LLM.
        catalog_title: Product title from Shopify catalog.
        catalog_price: Known catalog price (None if unavailable).
        catalog_colors: Known color variants from catalog.

    Returns:
        {"pass": bool, "confidence": float,
         "signals": {"title_similarity", "price_ratio", "color_overlap", "content_quality"},
         "reason": str}
    """
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
            # 1.0 if within 20%, linear decay to 0.0 at 60%+
            price_score = max(0.0, min(1.0, 1.0 - (price_diff - 0.2) / 0.4)) if price_diff > 0.2 else 1.0
        except (ValueError, TypeError):
            price_score = 0.5  # Can't parse — neutral
    else:
        price_score = 0.5  # No data — neutral
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
        color_score = 0.5  # No data — neutral
    signals["color_overlap"] = color_score

    # Content quality (10% weight)
    from .pipeline import _assess_extraction_quality
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
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py -v
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/match_validator.py tests/test_match_validator.py && git commit -m "feat: add match_validator module with title gate and signal aggregation"
```

---

### Task 3: Decision Logger

Add JSONL logging for match decisions.

**Files:**
- Modify: `lookout/enrich/match_validator.py`
- Create: `tests/test_match_decisions.py`

- [ ] **Step 1: Write test for decision logging**

```python
# tests/test_match_decisions.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_decisions.py -v
```

Expected: FAIL — `MatchDecisionLogger` not defined.

- [ ] **Step 3: Implement MatchDecisionLogger**

Add to `lookout/enrich/match_validator.py`:

```python
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_decisions.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/match_validator.py tests/test_match_decisions.py && git commit -m "feat: add MatchDecisionLogger for JSONL match decision tracking"
```

---

### Task 4: Pipeline Retry Loop

Replace the linear resolve → scrape → extract flow with a candidate loop that validates at each stage.

**Files:**
- Modify: `lookout/enrich/pipeline.py`

- [ ] **Step 1: Add match_validator imports to pipeline.py**

At the top of `pipeline.py`, add to the imports:

```python
from .match_validator import (
    MatchDecisionLogger,
    check_post_extraction,
    check_title_gate,
    extract_page_title,
)
```

- [ ] **Step 2: Initialize MatchDecisionLogger in PipelineConfig**

In the `PipelineConfig` class (around line 195), the `output_dir` is already stored. The logger will be initialized when the pipeline runs. No changes needed to the config — the logger is created per-run in the pipeline runner.

Find where `run_pipeline()` is defined and add the logger initialization. Search for `async def run_pipeline` in pipeline.py:

```python
    # Inside run_pipeline, after output_dir is established:
    decision_logger = MatchDecisionLogger(config.output_dir / "match_decisions.jsonl")
```

Pass `decision_logger` to `ProductProcessor.__init__()` and store it as `self.decision_logger`.

Add `decision_logger: MatchDecisionLogger | None = None` parameter to `ProductProcessor.__init__()` (around line 230):

```python
    def __init__(
        self,
        vendors_config: VendorsConfig,
        http_client: httpx.AsyncClient,
        llm_client: LLMClient | None,
        artifacts_base: Path,
        force: bool = False,
        store: Any | None = None,
        verify: bool = False,
        only_mode: str | None = None,
        decision_logger: MatchDecisionLogger | None = None,
    ) -> None:
        # ... existing assignments ...
        self.decision_logger = decision_logger
```

- [ ] **Step 3: Replace the linear flow with the retry loop**

In `ProductProcessor.process()`, replace the section from `# Step 1: Resolve URL` through the end of `# Step 3: Extract structured facts` (approximately lines 418-594) with the candidate retry loop.

The key change: instead of using only `resolver_output.selected_url`, iterate through `resolver_output.candidates` sorted by confidence descending, applying validation gates at each stage.

Replace the block starting at `# Step 1: Resolve URL` (line 418) through the markdown save and fact extraction (through line 594). The new code:

```python
                # Step 1: Resolve URL (use all available barcodes/SKUs)
                search_barcode = input_row.barcode
                search_sku = input_row.sku
                all_barcodes = input_row.all_barcodes
                all_skus = input_row.all_skus
                if all_barcodes:
                    search_barcode = all_barcodes[0]
                if all_skus:
                    search_sku = all_skus[0]

                handle_log.entries.append(
                    LogEntry(
                        message="Resolving product URL",
                        data={
                            "barcodes": len(all_barcodes),
                            "skus": len(all_skus),
                            "known_colors": known_colors[:5] if known_colors else [],
                        },
                    )
                )

                _catalog_price = None
                if input_row.variant_data:
                    _prices = [v.price for v in input_row.variant_data if v.price > 0]
                    if _prices:
                        _catalog_price = _prices[0]

                resolver_output = await self.resolver.resolve(
                    handle=handle,
                    vendor=vendor,
                    vendor_config=vendor_config,
                    hints=input_row.gaps or input_row.suggestions or "",
                    title=input_row.title,
                    barcode=search_barcode,
                    sku=search_sku,
                    catalog_price=_catalog_price,
                )

                await self.resolver.save_output(resolver_output, artifacts_dir)
                metadata["confidence"] = resolver_output.selected_confidence
                metadata["warnings"].extend(resolver_output.warnings)

                # Sort candidates by confidence descending, try up to 3
                candidates = sorted(
                    resolver_output.candidates,
                    key=lambda c: c.confidence,
                    reverse=True,
                )
                max_candidates = 3
                confidence_settings = self.vendors_config.settings.confidence

                match_decisions: list[dict] = []
                accepted_facts = None
                accepted_url = None
                accepted_markdown = None

                catalog_title = input_row.title or handle

                for candidate in candidates[:max_candidates]:
                    if candidate.confidence < confidence_settings.reject_threshold:
                        match_decisions.append({
                            "url": candidate.url,
                            "pre_scrape_confidence": candidate.confidence,
                            "stage": "pre_scrape",
                            "action": "skip",
                            "reason": f"below_threshold: {candidate.confidence}",
                        })
                        continue

                    # Scrape
                    handle_log.entries.append(
                        LogEntry(message=f"Scraping via Firecrawl: {candidate.url}")
                    )
                    md, firecrawl_variant_images = await self.firecrawl.scrape_markdown(
                        candidate.url,
                        swatch_selector=vendor_config.swatch_selector,
                        gallery_selector=vendor_config.gallery_selector,
                        wait_after_click=1500 if (vendor_config.swatch_selector or vendor_config.gallery_selector) else None,
                        wait_for=vendor_config.wait_for,
                    )

                    if not md or is_bot_blocked(md):
                        match_decisions.append({
                            "url": candidate.url,
                            "pre_scrape_confidence": candidate.confidence,
                            "stage": "scrape",
                            "action": "reject",
                            "reason": "bot_blocked" if md else "no_content",
                        })
                        continue

                    # Stage 1: Title gate
                    page_title = extract_page_title(md)
                    if page_title:
                        gate = check_title_gate(page_title, catalog_title)
                        if not gate["pass"]:
                            handle_log.entries.append(
                                LogEntry(
                                    level="WARNING",
                                    message=f"Title gate failed: {gate['reason']}",
                                    data={"page_title": page_title, **gate},
                                )
                            )
                            match_decisions.append({
                                "url": candidate.url,
                                "pre_scrape_confidence": candidate.confidence,
                                "stage": "title_gate",
                                "title_extracted": page_title,
                                "title_similarity": gate["title_similarity"],
                                "demographic_match": gate["demographic_match"],
                                "action": "reject",
                                "reason": gate["reason"],
                            })
                            continue

                    # LLM extraction
                    handle_log.entries.append(LogEntry(message="Extracting facts from markdown"))
                    facts_dict = await self.llm_client.extract_facts_from_markdown(
                        md, candidate.url
                    )

                    if not facts_dict or not facts_dict.get("product_name"):
                        match_decisions.append({
                            "url": candidate.url,
                            "pre_scrape_confidence": candidate.confidence,
                            "stage": "extraction",
                            "action": "reject",
                            "reason": "extraction_failed",
                        })
                        continue

                    from .firecrawl_scraper import _firecrawl_json_to_facts
                    candidate_facts = _firecrawl_json_to_facts(facts_dict, candidate.url)

                    # Stage 2: Signal aggregation
                    check = check_post_extraction(
                        facts=candidate_facts,
                        catalog_title=catalog_title,
                        catalog_price=_catalog_price,
                        catalog_colors=known_colors,
                    )

                    if not check["pass"]:
                        handle_log.entries.append(
                            LogEntry(
                                level="WARNING",
                                message=f"Post-extraction check failed: {check['reason']}",
                                data=check["signals"],
                            )
                        )
                        match_decisions.append({
                            "url": candidate.url,
                            "pre_scrape_confidence": candidate.confidence,
                            "stage": "signal_check",
                            "signals": check["signals"],
                            "post_scrape_confidence": check["confidence"],
                            "action": "reject",
                            "reason": check["reason"],
                        })
                        continue

                    # Accepted!
                    match_decisions.append({
                        "url": candidate.url,
                        "pre_scrape_confidence": candidate.confidence,
                        "stage": "signal_check",
                        "signals": check["signals"],
                        "post_scrape_confidence": check["confidence"],
                        "action": "accept",
                    })
                    accepted_facts = candidate_facts
                    accepted_url = candidate.url
                    accepted_markdown = md
                    handle_log.entries.append(
                        LogEntry(
                            message=f"Match accepted: {candidate.url}",
                            data={"post_scrape_confidence": check["confidence"]},
                        )
                    )
                    break

                # Log decisions
                if self.decision_logger:
                    self.decision_logger.log(
                        handle=handle,
                        vendor=vendor,
                        catalog_title=catalog_title,
                        catalog_price=_catalog_price,
                        catalog_colors=known_colors,
                        candidates_tried=match_decisions,
                        outcome="accepted" if accepted_facts else "all_failed",
                        final_url=accepted_url,
                    )

                # All candidates failed
                if not accepted_facts:
                    handle_log.entries.append(
                        LogEntry(
                            level="ERROR",
                            message=f"All {len(match_decisions)} candidates failed validation",
                        )
                    )
                    metadata["error"] = "All candidates failed post-scrape validation"
                    return None, ProcessingStatus.NO_MATCH, metadata

                # Use the accepted result
                facts = accepted_facts
                scrape_url = accepted_url
                markdown = accepted_markdown

                # Save raw markdown
                md_path = artifacts_dir / "source.md"
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(markdown)

                # Save extraction outputs
                facts_path = artifacts_dir / "extracted_facts.json"
                facts_path.write_text(facts.model_dump_json(indent=2))

                metadata["confidence"] = resolver_output.selected_confidence
```

Note: The fallback domain logic (lines 506-564 in the current code) is now handled by the candidate loop — if the primary URL fails scrape, the next candidate is tried. Remove the fallback domain block. If fallback domains still need support, they can be added as additional candidates in the resolver output in a future change.

- [ ] **Step 4: Remove the old linear scrape/extract code**

Delete the old blocks that the retry loop replaces:
- The old `scrape_url = resolver_output.selected_url` assignment
- The old single `scrape_markdown()` call
- The old bot block / fallback domain loop
- The old single `extract_facts_from_markdown()` call
- The old `_firecrawl_json_to_facts()` call

These are all replaced by the candidate loop above.

- [ ] **Step 5: Run full test suite**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/pipeline.py && git commit -m "feat: pipeline retry loop with post-scrape validation and decision logging"
```

---

### Task 5: Validation Run

Test the complete system on the known problem products.

**Files:** none (test-only task)

- [ ] **Step 1: Run validation on the known problem handles**

```bash
cd /Users/andyking/Lookout && uv run lookout enrich run \
  -h kidss-reverb-ski-boots-2024 \
  -h mens-alp-trainer-gtx \
  -h mens-cloudrock-low-wp \
  -o ./output/validation-retry-test \
  --force --verbose 2>&1
```

- [ ] **Step 2: Check match_decisions.jsonl**

```bash
cd /Users/andyking/Lookout && python3 -c "
import json
for line in open('output/validation-retry-test/match_decisions.jsonl'):
    d = json.loads(line)
    print(f'{d[\"handle\"]}: {d[\"outcome\"]} ({len(d[\"candidates_tried\"])} tried)')
    for c in d['candidates_tried']:
        print(f'  {c[\"stage\"]}: {c[\"action\"]} — {c.get(\"reason\", \"ok\")} ({c[\"url\"][:60]})')
"
```

Expected:
- K2 Reverb: candidate #1 (Revolve) rejected at title_gate (demographic mismatch), candidate #2 (Reverb) accepted
- Salewa: candidate #1 (Mid) potentially rejected or accepted with lower confidence
- On Cloudrock: accepted (correct match)

- [ ] **Step 3: Check enrichment outputs**

```bash
for d in output/validation-retry-test/*/; do
  handle=$(basename "$d")
  [ -f "$d/log.json" ] || continue
  python3 -c "
import json
log = json.load(open('$d/log.json'))
for e in log['entries']:
    if 'Match accepted' in e.get('message','') or 'gate failed' in e.get('message','') or 'check failed' in e.get('message',''):
        print(f'  $handle: {e[\"message\"]}')
" 2>/dev/null
done
```

- [ ] **Step 4: Commit validation results (if desired)**

```bash
cd /Users/andyking/Lookout && git add -A output/validation-retry-test/ && git commit -m "test: validation run with post-scrape match validation"
```

Or clean up if not committing test output.
