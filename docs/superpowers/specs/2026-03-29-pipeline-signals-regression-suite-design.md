# Pipeline Signals, Regression Suite & Confidence Handling — Design Spec

**Date:** 2026-03-29
**Goal:** Wire real price and color data into post-scrape validation, build a resolver regression test suite from decision logs, lower the retry loop confidence threshold, and document bot-blocked vendors.

---

## 1. Real Price Signal

**Problem:** `check_post_extraction` looks for price in `facts.json_ld_data.offers.price` which is always null. Prices ARE extracted by the LLM into `facts.specs` (e.g. `{"Price": "$175.00"}`).

**Fix:** Add `_extract_price_from_facts(facts) -> float | None` helper in `match_validator.py`:
- Check `facts.specs` for keys matching (case-insensitive): "price", "msrp", "regular price", "retail price", "base price"
- Parse first dollar/euro/pound amount with regex: `r"[\$€£]([\d,]+\.?\d*)"`
- Strip commas, convert to float
- Fall back to existing JSON-LD path if specs has no price
- Return None if no price found anywhere

Replace the JSON-LD-only price extraction in `check_post_extraction` with a call to this helper. Scoring formula unchanged (1.0 within 20%, linear decay to 0.0 at 60%+, 0.5 if no price).

**Files:** `lookout/enrich/match_validator.py`

---

## 2. Real Color Signal

**Problem:** `check_post_extraction` looks for colors in `facts.variants` which is almost always empty. Two richer sources exist but aren't used.

**Fix:** Add `vendor_colors: list[str] | None = None` parameter to `check_post_extraction`:

**Source 1 — Swatch extraction:** The pipeline's `firecrawl_variant_images` dict has color names as keys (e.g. `{"Black": [...], "Blue": [...]}`). Pass `list(firecrawl_variant_images.keys())` as `vendor_colors` when available.

**Source 2 — Specs fallback:** If no `vendor_colors` passed, check `facts.specs` for "Color"/"Colour"/"Colors"/"Colours" key. Split on `/`, `,`, `|` to handle multi-color values.

**Source 3 — Existing facts.variants:** Keep as final fallback (current behavior).

Feed whichever colors we find into `score_color_overlap(catalog_colors, vendor_colors)`. Scoring unchanged.

**Files:** `lookout/enrich/match_validator.py`, `lookout/enrich/pipeline.py` (pass swatch colors through)

---

## 3. Lower Retry Loop Threshold

**Problem:** Products with resolver confidence 50-69 are skipped entirely in the retry loop, but the post-scrape validation (title gate + signal check) can catch bad matches that slip through at lower confidence.

**Fix:** In the pipeline retry loop's skip condition, change from `candidate.confidence < confidence_settings.reject_threshold` (70) to `candidate.confidence < 50`. The global `reject_threshold` config stays at 70 for warnings and display — only the retry loop cutoff is lowered, since it has validation gates behind it.

One-line change in `pipeline.py`.

---

## 4. Regression Suite

### 4a. Cache resolver candidates in decision logs

Extend `MatchDecisionLogger.log()` to accept an optional `resolver_candidates` parameter — the raw candidate list from the resolver output. Each candidate has `url`, `title`, `snippet`, `confidence`, `reasoning`.

Written to `match_decisions.jsonl` alongside existing fields. This means future pipeline runs automatically build the test corpus.

### 4b. `lookout enrich test-resolver` command

New CLI command that:
1. Reads all `match_decisions.jsonl` files from a directory (default: `output/`)
2. For each decision that has `resolver_candidates` cached:
   - Re-runs the resolver's scoring logic against the cached candidates
   - Compares the winning URL against `final_url` in the decision
   - Reports matches (pass) and regressions (different winner)
3. Outputs summary: X pass, Y regressed, Z skipped (no cached candidates)

**Scoring replay:** The resolver's `_score_candidate` and title comparison logic need to be callable independently from the search/HTTP layer. Extract a `rescore_candidates(candidates, title, vendor, catalog_price)` function that applies all scoring rules to a pre-built candidate list. The `test-resolver` command calls this.

**No network calls.** Runs in seconds. Tests scoring logic only.

**Files:**
- Modify: `lookout/enrich/match_validator.py` (extend logger)
- Modify: `lookout/enrich/resolver.py` (extract `rescore_candidates`)
- Modify: `lookout/enrich/pipeline.py` (pass resolver_candidates to logger)
- Modify: `lookout/cli.py` (add `test-resolver` command)

---

## 5. Bot-Blocked Vendor Documentation

Add `blocked: bool = False` field to `VendorConfig` in `models.py`. When true:
- Pipeline logs "Vendor blocked — skipping" and returns `SKIPPED_VENDOR_BLOCKED`
- Dashboard shows blocked vendors distinctly
- `batch_validate.py` skips them automatically (currently hardcoded)

Mark as blocked in `vendors.yaml`:
- Teva (17 blocks in batch)
- Smartwool (2 blocks)
- Patagonia (known Akamai)
- Altra (1 block)

Do NOT mark as blocked (partial — some URLs work):
- Arc'teryx, Burton, Rossignol, Helly Hansen, Prana, La Sportiva

**Files:** `lookout/enrich/models.py`, `lookout/enrich/pipeline.py`, `vendors.yaml`

---

## Not in scope

- Full pipeline replay (`test-pipeline`) — future, if regression suite proves valuable
- Residential proxy integration — separate infrastructure project
- Auto-tuning scoring weights from decision data — overfitting risk
