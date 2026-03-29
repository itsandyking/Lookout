# Post-Scrape Match Validation — Design Spec

**Date:** 2026-03-29
**Goal:** Catch wrong product matches after scraping by validating post-scrape signals, retrying with alternate candidates, and logging all match decisions for future resolver improvement.

**Problem:** The resolver scores candidates pre-scrape using URL structure, title overlap, and search snippets. But some mismatches are only detectable after scraping — the page title says "Mid" when we wanted "Low", or the colors don't overlap. Currently these fire as warnings but don't trigger a retry. Wrong matches waste LLM calls and produce bad output.

---

## Architecture

### New module: `lookout/enrich/match_validator.py`

Two validation stages, each progressively more expensive:

**Stage 1 — Title gate (post-scrape, pre-LLM)**

Cheap string comparison on data already available from the scrape.

- Extract page title from scraped markdown: first `# heading`, or `<title>` if no heading
- Compare against catalog title using `SequenceMatcher`
- Check for demographic mismatch (Youth/Men's/Women's/Kids vs each other)
- Check for asymmetric height mismatch (candidate says "Mid" but catalog doesn't specify height)

Decision logic:
- `title_similarity < 0.4` → reject
- Demographic mismatch (expected "Youth", got "Women's") → reject
- Otherwise → pass to extraction

Cost: zero (string comparison on existing data).

**Stage 2 — Signal aggregation (post-extraction)**

Runs after LLM fact extraction. Aggregates multiple signals into a post-scrape confidence score.

| Signal | Source | Weight | Threshold |
|--------|--------|--------|-----------|
| Title similarity | `facts.product_name` vs catalog title | 40% | < 0.4 = fail |
| Price plausibility | JSON-LD price vs catalog price | 25% | > 50% diff = fail |
| Color overlap | Extracted variant colors vs known colors | 25% | < 0.2 Jaccard = fail |
| Content quality | `_assess_extraction_quality()` result | 10% | not usable = fail |

Scoring formula:
```
post_scrape_confidence = (
    title_sim_score * 40 +
    price_score * 25 +
    color_score * 25 +
    quality_score * 10
)
```

Where each component score is 0.0-1.0:
- `title_sim_score`: SequenceMatcher ratio (clamped 0-1)
- `price_score`: 1.0 if within 20%, linear decay to 0.0 at 60%+ difference, 0.5 if no price available
- `color_score`: Jaccard overlap excluding neutrals (from `season_signals.score_color_overlap`), 0.5 if no colors to compare
- `quality_score`: 1.0 if usable, 0.0 if not

Decision: `post_scrape_confidence < 50` → reject, try next candidate.

### Functions

```python
def extract_page_title(markdown: str) -> str | None:
    """Extract the primary title from scraped markdown."""

def check_title_gate(
    page_title: str,
    catalog_title: str,
) -> dict:
    """Stage 1: cheap title + demographic check.

    Returns:
        {"pass": bool, "title_similarity": float,
         "demographic_match": bool | None, "reason": str}
    """

def check_post_extraction(
    facts: ExtractedFacts,
    catalog_title: str,
    catalog_price: float | None,
    catalog_colors: list[str],
) -> dict:
    """Stage 2: aggregate post-extraction signals.

    Returns:
        {"pass": bool, "confidence": float, "signals": {
            "title_similarity": float, "price_ratio": float | None,
            "color_overlap": float | None, "content_quality": bool,
        }, "reason": str}
    """
```

---

## Pipeline Integration

### Retry loop in `pipeline.py`

Replace the current linear resolve → scrape → extract flow with a candidate loop.

Current flow (simplified):
```
resolver_output = await self.resolver.resolve(...)
scrape_url = resolver_output.selected_url
markdown = await self.firecrawl.scrape_markdown(scrape_url, ...)
facts = await self.llm_client.extract_facts_from_markdown(markdown, ...)
# ... generate output
```

New flow:
```
resolver_output = await self.resolver.resolve(...)
candidates = sorted(resolver_output.candidates, key=lambda c: c.confidence, reverse=True)
max_retries = 3

match_decisions = []

for candidate in candidates[:max_retries]:
    # Scrape
    markdown = await self.firecrawl.scrape_markdown(candidate.url, ...)

    # Stage 1: Title gate
    page_title = extract_page_title(markdown)
    gate = check_title_gate(page_title, catalog_title)

    if not gate["pass"]:
        match_decisions.append({...candidate info, stage: "title_gate", action: "reject", ...gate})
        continue

    # LLM extraction
    facts = await self.llm_client.extract_facts_from_markdown(markdown, ...)

    # Stage 2: Signal aggregation
    check = check_post_extraction(facts, catalog_title, catalog_price, known_colors)

    if not check["pass"]:
        match_decisions.append({...candidate info, stage: "signal_check", action: "reject", ...check})
        continue

    # Accepted
    match_decisions.append({...candidate info, stage: "signal_check", action: "accept", ...check})
    break

else:
    # All candidates failed
    match_decisions.append({"outcome": "all_failed"})
    # Write decisions, return NO_MATCH

# Write match decisions to JSONL
```

### Constraints

- **Max 3 candidates tried** — caps LLM cost. If top 3 fail, it's a search problem.
- **Stage 1 is free** — only string comparison, no API calls. Can reject bad matches before the expensive LLM step.
- **Stage 2 costs one LLM call per candidate** — but only reached if Stage 1 passes.
- **Shopify JSON path unchanged** — validation only applies to the resolver → Firecrawl path. Shopify JSON matches are inherently high-confidence (direct handle match).

---

## Decision Log

### Format

One JSONL file per run: `{output_dir}/match_decisions.jsonl`

Each line is a complete match decision record:

```json
{
  "handle": "kidss-reverb-ski-boots-2024",
  "vendor": "K2 Sports",
  "catalog_title": "Youth Reverb Ski Boots 2024",
  "catalog_price": 199.99,
  "catalog_colors": ["Black", "White/Blue"],
  "timestamp": "2026-03-29T12:00:00Z",
  "candidates_tried": [
    {
      "url": "https://k2snow.com/.../revolve-tbl-womens",
      "pre_scrape_confidence": 87,
      "stage": "title_gate",
      "title_extracted": "Revolve TBL Women's Ski Boots",
      "title_similarity": 0.42,
      "demographic_match": false,
      "action": "reject",
      "reason": "demographic_mismatch: youth vs womens"
    },
    {
      "url": "https://k2snow.com/.../reverb-youth-ski-boots",
      "pre_scrape_confidence": 60,
      "stage": "signal_check",
      "signals": {
        "title_similarity": 0.95,
        "price_ratio": 1.0,
        "color_overlap": 0.8,
        "content_quality": true
      },
      "post_scrape_confidence": 88,
      "action": "accept"
    }
  ],
  "outcome": "accepted",
  "final_url": "https://k2snow.com/.../reverb-youth-ski-boots",
  "retries": 1
}
```

### Purpose

Each entry is an automatic test case for resolver improvement:
- **Rejected candidates** show what the resolver scored highly but was actually wrong
- **Accepted after retry** shows the resolver's ranking was off — the right answer was #2 or #3
- **All-failed entries** show products that need manual URL pinning or search strategy changes
- Over time, this corpus can tune resolver weights and build a regression test suite

---

## Resolver Scoring Fixes (included)

While modifying the resolver for this work, fix the three bugs found during validation:

### 1. Asymmetric height detection
Current: only penalizes when BOTH titles have height words.
Fix: also penalize (-15) when candidate has a height word (Low/Mid/High) but expected doesn't.

### 2. Year vs model number separation
Current: all number mismatches trigger `critical_mismatch` (-30).
Fix: separate year patterns (`/202\d/`) from model numbers (`/\b\d{2,3}\b/` not matching years). Years get -5 (minor), model numbers keep -30 (critical).

### 3. Demographic mismatch detection
New: detect demographic words (youth, kids, boys, girls, mens, men, womens, women, unisex) in both titles. Mismatch gets -15.

---

## Not in scope

- `lookout enrich test-resolver` command — future, once decision logs accumulate
- Auto-tuning resolver weights from decision data — future
- Match correction UI in review — future
- URL pinning for known-difficult products — future
