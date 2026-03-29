# Pipeline Signals, Regression Suite & Config — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire real price and color data into post-scrape validation, build a resolver regression test suite from decision logs, lower the retry threshold, and mark bot-blocked vendors.

**Architecture:** Extend `match_validator.py` to extract price from `facts.specs` and accept vendor colors from swatch extraction. Cache resolver candidates in decision JSONL. New `test-resolver` CLI command replays scoring and detects regressions. Config changes for threshold and blocked vendors.

**Tech Stack:** Python, regex for price parsing, existing `score_color_overlap`, Click CLI

**Spec:** `docs/superpowers/specs/2026-03-29-pipeline-signals-regression-suite-design.md`

---

### Task 1: Real Price Signal

Extract price from `facts.specs` instead of only checking JSON-LD.

**Files:**
- Modify: `lookout/enrich/match_validator.py`
- Modify: `tests/test_match_validator.py`

- [ ] **Step 1: Write tests for price extraction**

Add to `tests/test_match_validator.py`:

```python
def test_extract_price_from_specs_dollar():
    from lookout.enrich.match_validator import _extract_price_from_facts
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Test",
        canonical_url="https://example.com",
        images=[],
        variants=[],
        specs={"Price": "$175.00"},
    )
    assert _extract_price_from_facts(facts) == 175.00


def test_extract_price_from_specs_euro():
    from lookout.enrich.match_validator import _extract_price_from_facts
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Test",
        canonical_url="https://example.com",
        images=[],
        variants=[],
        specs={"Regular Price": "€1,299.99"},
    )
    assert _extract_price_from_facts(facts) == 1299.99


def test_extract_price_from_specs_range():
    from lookout.enrich.match_validator import _extract_price_from_facts
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Test",
        canonical_url="https://example.com",
        images=[],
        variants=[],
        specs={"Price Range": "$760.00 – $830.00"},
    )
    # Should extract the first price
    assert _extract_price_from_facts(facts) == 760.00


def test_extract_price_falls_back_to_json_ld():
    from lookout.enrich.match_validator import _extract_price_from_facts
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Test",
        canonical_url="https://example.com",
        images=[],
        variants=[],
        specs={},
        json_ld_data={"offers": {"price": "99.99"}},
    )
    assert _extract_price_from_facts(facts) == 99.99


def test_extract_price_none_when_missing():
    from lookout.enrich.match_validator import _extract_price_from_facts
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Test",
        canonical_url="https://example.com",
        images=[],
        variants=[],
        specs={"Material": "Gore-Tex"},
    )
    assert _extract_price_from_facts(facts) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py::test_extract_price_from_specs_dollar -v
```

Expected: FAIL — `_extract_price_from_facts` not defined.

- [ ] **Step 3: Implement `_extract_price_from_facts`**

Add to `lookout/enrich/match_validator.py`, before `check_post_extraction`:

```python
_PRICE_RE = re.compile(r"[\$€£]([\d,]+\.?\d*)")

_PRICE_KEYS = frozenset({
    "price", "msrp", "regular price", "retail price", "base price",
    "price range", "our price",
})


def _extract_price_from_facts(facts) -> float | None:
    """Extract price from facts.specs, falling back to JSON-LD.

    Checks specs keys matching common price labels, parses first
    dollar/euro/pound amount found. Returns None if no price.
    """
    # Try specs first
    if facts.specs:
        for key, value in facts.specs.items():
            if key.lower() in _PRICE_KEYS:
                match = _PRICE_RE.search(str(value))
                if match:
                    try:
                        return float(match.group(1).replace(",", ""))
                    except ValueError:
                        continue

    # Fall back to JSON-LD
    if facts.json_ld_data:
        offers = facts.json_ld_data.get("offers", {})
        if isinstance(offers, dict):
            price = offers.get("price")
        elif isinstance(offers, list) and offers:
            price = offers[0].get("price")
        else:
            price = None
        if price is not None:
            try:
                return float(price)
            except (ValueError, TypeError):
                pass

    return None
```

- [ ] **Step 4: Update `check_post_extraction` to use the helper**

Replace the price extraction block (lines 173-189 of `match_validator.py`) with:

```python
    # Price plausibility (25% weight)
    scraped_price = _extract_price_from_facts(facts)

    if scraped_price is not None and catalog_price and catalog_price > 0:
        price_diff = abs(scraped_price - catalog_price) / catalog_price
        price_score = max(0.0, min(1.0, 1.0 - (price_diff - 0.2) / 0.4)) if price_diff > 0.2 else 1.0
    else:
        price_score = 0.5  # No data — neutral
    signals["price_ratio"] = price_score
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/match_validator.py tests/test_match_validator.py && git commit -m "feat: extract price from facts.specs for post-scrape validation"
```

---

### Task 2: Real Color Signal

Accept vendor colors from swatch extraction and specs fallback.

**Files:**
- Modify: `lookout/enrich/match_validator.py`
- Modify: `lookout/enrich/pipeline.py`
- Modify: `tests/test_match_validator.py`

- [ ] **Step 1: Write tests for color extraction**

Add to `tests/test_match_validator.py`:

```python
def test_post_extraction_with_vendor_colors():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Reverb Youth Ski Boots",
        canonical_url="https://example.com",
        images=[],
        variants=[],
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Youth Reverb Ski Boots 2024",
        catalog_price=None,
        catalog_colors=["Black", "Blue"],
        vendor_colors=["Black", "Red"],
    )
    assert result["pass"] is True
    # Color overlap should be non-neutral since we have real data
    assert result["signals"]["color_overlap"] != 0.5


def test_post_extraction_color_from_specs():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="CloudLite Sleeping Bag",
        canonical_url="https://example.com",
        images=[],
        variants=[],
        specs={"Color": "Blue Sky"},
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="CloudLite Sleeping Bag",
        catalog_price=None,
        catalog_colors=["Blue Sky"],
    )
    assert result["signals"]["color_overlap"] != 0.5


def test_post_extraction_color_specs_multi():
    from lookout.enrich.match_validator import check_post_extraction
    from lookout.enrich.models import ExtractedFacts

    facts = ExtractedFacts(
        product_name="Some Product",
        canonical_url="https://example.com",
        images=[],
        variants=[],
        specs={"Colors": "Red / Blue / Green"},
    )
    result = check_post_extraction(
        facts=facts,
        catalog_title="Some Product",
        catalog_price=None,
        catalog_colors=["Red", "Blue"],
    )
    assert result["signals"]["color_overlap"] != 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py::test_post_extraction_with_vendor_colors -v
```

Expected: FAIL — `vendor_colors` not a parameter of `check_post_extraction`.

- [ ] **Step 3: Add `vendor_colors` parameter and specs fallback**

Update `check_post_extraction` signature and color extraction block in `match_validator.py`:

```python
def check_post_extraction(
    facts,  # ExtractedFacts
    catalog_title: str,
    catalog_price: float | None,
    catalog_colors: list[str],
    vendor_colors: list[str] | None = None,
) -> dict:
```

Replace the color overlap section (lines 192-204) with:

```python
    # Color overlap (25% weight)
    # Source 1: explicit vendor_colors (from swatch extraction)
    # Source 2: facts.specs Color/Colour key
    # Source 3: facts.variants (existing, rarely populated)
    colors_to_compare: list[str] = []
    if vendor_colors:
        colors_to_compare = vendor_colors
    elif facts.specs:
        for key, value in facts.specs.items():
            if key.lower() in ("color", "colour", "colors", "colours"):
                # Split multi-color values: "Red / Blue / Green"
                colors_to_compare = [c.strip() for c in re.split(r"[/,|]", str(value)) if c.strip()]
                break
    if not colors_to_compare:
        for v in facts.variants:
            if v.option_name.lower() in ("color", "colour", "style"):
                colors_to_compare = v.values
                break

    if colors_to_compare and catalog_colors:
        overlap_result = score_color_overlap(catalog_colors, colors_to_compare)
        color_score = overlap_result["overlap"]
    else:
        color_score = 0.5  # No data — neutral
    signals["color_overlap"] = color_score
```

- [ ] **Step 4: Pass swatch colors in pipeline.py**

In `pipeline.py`, find the `check_post_extraction` call (around line 575). The `cand_variant_images` variable is in scope — it's the swatch extraction result from the scrape call. Pass the color names:

```python
                    # Post-extraction validation
                    swatch_colors = list(cand_variant_images.keys()) if cand_variant_images else None
                    post_check = check_post_extraction(
                        cand_facts, catalog_title, _catalog_price, known_colors or [],
                        vendor_colors=swatch_colors,
                    )
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_validator.py -v && uv run pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/match_validator.py lookout/enrich/pipeline.py tests/test_match_validator.py && git commit -m "feat: real color signal from swatch extraction and specs fallback"
```

---

### Task 3: Lower Retry Loop Threshold

**Files:**
- Modify: `lookout/enrich/pipeline.py`

- [ ] **Step 1: Change the skip condition**

In `pipeline.py`, find the retry loop skip condition (around line 488-492):

```python
                for candidate in candidates[:3]:
                    if candidate.confidence < confidence_settings.reject_threshold:
```

Change to:

```python
                for candidate in candidates[:3]:
                    if candidate.confidence < 50:
```

- [ ] **Step 2: Run tests**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/pipeline.py && git commit -m "fix: lower retry loop threshold to 50 (validation gates are the safety net)"
```

---

### Task 4: Cache Resolver Candidates in Decision Logs

**Files:**
- Modify: `lookout/enrich/match_validator.py`
- Modify: `lookout/enrich/pipeline.py`
- Modify: `tests/test_match_decisions.py`

- [ ] **Step 1: Write test for resolver_candidates in log**

Add to `tests/test_match_decisions.py`:

```python
def test_log_includes_resolver_candidates(tmp_path):
    from lookout.enrich.match_validator import MatchDecisionLogger

    logger = MatchDecisionLogger(tmp_path / "decisions.jsonl")
    logger.log(
        handle="test",
        vendor="V",
        catalog_title="Test Product",
        candidates_tried=[{"url": "u", "action": "accept"}],
        outcome="accepted",
        final_url="u",
        resolver_candidates=[
            {"url": "https://example.com/a", "confidence": 85, "title": "Product A", "snippet": "..."},
            {"url": "https://example.com/b", "confidence": 70, "title": "Product B", "snippet": "..."},
        ],
    )

    import json
    record = json.loads((tmp_path / "decisions.jsonl").read_text().strip())
    assert "resolver_candidates" in record
    assert len(record["resolver_candidates"]) == 2
    assert record["resolver_candidates"][0]["confidence"] == 85
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_decisions.py::test_log_includes_resolver_candidates -v
```

Expected: FAIL — `resolver_candidates` not in log output.

- [ ] **Step 3: Add `resolver_candidates` to MatchDecisionLogger.log()**

In `match_validator.py`, update the `log()` method:

```python
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")
        logger.info(
            "Match decision for %s: %s (%d candidates tried)",
            handle, outcome, len(candidates_tried),
        )
```

- [ ] **Step 4: Pass resolver candidates in pipeline.py**

In `pipeline.py`, find the `self.decision_logger.log(...)` call (around line 619). Add the `resolver_candidates` parameter:

```python
                if self.decision_logger:
                    outcome = "accept" if accepted_facts else "no_match"
                    self.decision_logger.log(
                        handle=handle,
                        vendor=vendor,
                        catalog_title=catalog_title,
                        candidates_tried=match_decisions,
                        outcome=outcome,
                        final_url=accepted_url,
                        catalog_price=_catalog_price,
                        catalog_colors=known_colors,
                        resolver_candidates=[
                            {"url": c.url, "confidence": c.confidence,
                             "title": c.title, "snippet": c.snippet,
                             "reasoning": c.reasoning}
                            for c in candidates
                        ],
                    )
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_match_decisions.py -v && uv run pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/match_validator.py lookout/enrich/pipeline.py tests/test_match_decisions.py && git commit -m "feat: cache resolver candidates in match decision JSONL"
```

---

### Task 5: Regression Test Command

**Files:**
- Modify: `lookout/enrich/resolver.py` (extract `rescore_candidates`)
- Modify: `lookout/cli.py` (add `test-resolver` command)
- Create: `tests/test_regression_runner.py`

- [ ] **Step 1: Extract `rescore_candidates` from resolver**

In `resolver.py`, add a module-level function that applies scoring to a pre-built candidate list without any network calls. This wraps the existing `_score_candidate` logic:

```python
def rescore_candidates(
    candidates: list[dict],
    product_title: str,
    vendor: str,
    domain: str,
    catalog_price: float | None = None,
) -> list[dict]:
    """Re-score a list of pre-built candidates using current scoring logic.

    Used by the regression test runner to replay scoring without network.

    Args:
        candidates: List of {"url", "title", "snippet", "confidence"} dicts.
        product_title: Our catalog product title.
        vendor: Vendor name.
        domain: Vendor domain.
        catalog_price: Known catalog price for price signal.

    Returns:
        Candidates with updated confidence scores, sorted descending.
    """
    resolver = URLResolver(http_client=None)
    scored = []
    for c in candidates:
        base_score = resolver._score_candidate(
            url=c["url"],
            title=c.get("title", ""),
            snippet=c.get("snippet", ""),
            domain=domain,
            product_title=product_title,
        )
        scored.append({
            **c,
            "rescored_confidence": base_score,
        })
    scored.sort(key=lambda x: x["rescored_confidence"], reverse=True)
    return scored
```

Note: `_score_candidate` already works without instance state (only uses `self` for method dispatch). Passing `http_client=None` is safe since we don't do any HTTP. However, the title comparison logic that runs in `resolve()` (height/demographic/year checks) applies AFTER `_score_candidate`. For a proper replay, we need to also apply those checks.

Add a `_apply_title_adjustments` class method to URLResolver that takes a candidate and product_title, applies the height/demographic/year/collab adjustments, and returns the adjusted confidence. Then call it from `rescore_candidates`.

Actually, simpler: extract the title adjustment block (lines 250-370ish) into a static method `_adjust_for_title(candidate_title, product_title, vendor, confidence)` that returns the adjusted confidence. Call it from both `resolve()` and `rescore_candidates`.

```python
    @staticmethod
    def _adjust_for_title(
        candidate_title: str,
        product_title: str,
        vendor: str,
        confidence: int,
    ) -> tuple[int, str]:
        """Apply title-based confidence adjustments.

        Returns (adjusted_confidence, reasoning_suffix).
        """
        # [Move the existing title comparison block here — lines 252-370ish]
        # This is the block that starts with:
        #   title_lower = clean_title.lower()
        #   title_words = set(_re.findall(r'[a-z0-9]+', title_lower))
        # And ends before the deduplication step
        ...
```

This is a significant refactor of resolver.py. Read the file carefully, extract the logic, and update `resolve()` to call the new static method. Make sure all variables are passed as parameters (no closure over `clean_title` etc).

- [ ] **Step 2: Write test for rescore_candidates**

```python
# tests/test_regression_runner.py
"""Tests for resolver regression test runner."""


def test_rescore_candidates_ranks_correctly():
    from lookout.enrich.resolver import rescore_candidates

    candidates = [
        {"url": "https://example.com/product/reverb-youth-boots", "title": "Reverb Youth Ski Boots", "snippet": "...", "confidence": 50},
        {"url": "https://example.com/category/boots", "title": "All Boots", "snippet": "...", "confidence": 50},
    ]
    result = rescore_candidates(
        candidates=candidates,
        product_title="Youth Reverb Ski Boots 2024",
        vendor="TestVendor",
        domain="example.com",
    )
    # Product page should score higher than category page
    assert result[0]["url"] == "https://example.com/product/reverb-youth-boots"
    assert result[0]["rescored_confidence"] > result[1]["rescored_confidence"]


def test_rescore_empty_candidates():
    from lookout.enrich.resolver import rescore_candidates

    result = rescore_candidates(
        candidates=[],
        product_title="Test",
        vendor="V",
        domain="example.com",
    )
    assert result == []
```

- [ ] **Step 3: Run tests**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/test_regression_runner.py -v
```

- [ ] **Step 4: Add `test-resolver` CLI command**

In `lookout/cli.py`, add the command under the `enrich` group:

```python
@enrich.command("test-resolver")
@click.option("--output-dir", "-d", type=click.Path(exists=True, path_type=Path),
              default=Path("./output"), help="Directory containing match_decisions.jsonl files")
@click.option("--verbose", is_flag=True)
def test_resolver_cmd(output_dir, verbose):
    """Replay resolver scoring against cached candidates to detect regressions."""
    import json
    from lookout.enrich.resolver import rescore_candidates
    from lookout.enrich.utils import load_vendors_config

    vendors_path = find_vendors_yaml(None)
    if vendors_path:
        vc = load_vendors_config(vendors_path)
    else:
        vc = None

    decisions = []
    for path in output_dir.rglob("match_decisions.jsonl"):
        for line in path.read_text().strip().split("\n"):
            if line.strip():
                decisions.append(json.loads(line))

    if not decisions:
        console.print("[yellow]No match_decisions.jsonl files found[/yellow]")
        return

    # Filter to decisions with cached resolver_candidates
    testable = [d for d in decisions if d.get("resolver_candidates")]
    skipped = len(decisions) - len(testable)

    passed = 0
    regressed = 0
    regressions = []

    for d in testable:
        # Get vendor domain
        domain = ""
        if vc:
            vendor_config = vc.vendors.get(d["vendor"])
            if vendor_config:
                domain = vendor_config.domain

        rescored = rescore_candidates(
            candidates=d["resolver_candidates"],
            product_title=d["catalog_title"],
            vendor=d["vendor"],
            domain=domain,
            catalog_price=d.get("catalog_price"),
        )

        if not rescored:
            continue

        new_winner = rescored[0]["url"]
        original_winner = d.get("final_url")

        if original_winner and new_winner == original_winner:
            passed += 1
        elif original_winner:
            regressed += 1
            regressions.append({
                "handle": d["handle"],
                "vendor": d["vendor"],
                "expected": original_winner,
                "got": new_winner,
                "expected_score": rescored[0].get("rescored_confidence", "?"),
            })
            if verbose:
                console.print(f"[red]REGRESSED[/red] {d['handle']}: expected {original_winner[:50]} got {new_winner[:50]}")
        else:
            # Original was no_match — check if we'd now find something
            if verbose:
                console.print(f"[dim]SKIP[/dim] {d['handle']}: original was no_match")

    console.print(f"\n[bold]Resolver Regression Results[/bold]")
    console.print(f"  Passed: {passed}")
    console.print(f"  Regressed: {regressed}")
    console.print(f"  Skipped (no cached candidates): {skipped}")
    console.print(f"  Total decisions: {len(decisions)}")

    if regressions:
        console.print(f"\n[red]Regressions:[/red]")
        for r in regressions:
            console.print(f"  {r['handle']} ({r['vendor']})")
            console.print(f"    expected: {r['expected'][:70]}")
            console.print(f"    got:      {r['got'][:70]}")

    if regressed > 0:
        sys.exit(1)
```

- [ ] **Step 5: Run full test suite**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/ -x -q
```

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/resolver.py lookout/cli.py tests/test_regression_runner.py && git commit -m "feat: add test-resolver command for scoring regression detection"
```

---

### Task 6: Bot-Blocked Vendor Config

**Files:**
- Modify: `lookout/enrich/models.py`
- Modify: `lookout/enrich/pipeline.py`
- Modify: `vendors.yaml`
- Modify: `scripts/batch_validate.py`

- [ ] **Step 1: Add `blocked` field to VendorConfig**

In `lookout/enrich/models.py`, add to the `VendorConfig` class (around line 191):

```python
class VendorConfig(BaseModel):
    """Configuration for a single vendor."""

    domain: str
    is_shopify: bool = False
    blocked: bool = False  # Vendor is bot-blocked, skip enrichment
    fallback_domains: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)
    product_url_patterns: list[str] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)
    search_brand_name: str | None = None
    swatch_selector: str | None = None
    gallery_selector: str | None = None
    wait_for: int | None = None
```

- [ ] **Step 2: Handle blocked vendors in pipeline**

In `pipeline.py`, find the vendor configuration check (around line 298-308). After checking if the vendor is configured, add a blocked check:

```python
            # Check vendor configuration
            vendor_config = self.vendors_config.vendors.get(vendor)
            if not vendor_config:
                handle_log.entries.append(
                    LogEntry(level="WARNING", message=f"Vendor not configured: {vendor}")
                )
                metadata["warnings"].append(f"VENDOR_NOT_CONFIGURED: {vendor}")
                return None, ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED, metadata

            if vendor_config.blocked:
                handle_log.entries.append(
                    LogEntry(level="INFO", message=f"Vendor blocked (bot protection): {vendor}")
                )
                return None, ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED, metadata
```

- [ ] **Step 3: Mark blocked vendors in vendors.yaml**

Add `blocked: true` to these vendor entries:

```yaml
"Teva":
  domain: "teva.com"
  blocked: true  # Akamai bot protection

"Smartwool":
  domain: "smartwool.com"
  blocked: true  # Bot protection

"Patagonia":
  domain: "patagonia.com"
  blocked: true  # Akamai bot protection

"Altra":
  domain: "altrarunning.com"
  blocked: true  # Bot protection
```

- [ ] **Step 4: Update batch_validate.py to use config instead of hardcoded list**

In `scripts/batch_validate.py`, replace the hardcoded `blocked_vendors` set in `main()`:

```python
    # Load blocked vendors from config instead of hardcoding
    from lookout.enrich.utils import load_vendors_config
    vc = load_vendors_config(Path("vendors.yaml"))
    blocked_vendors = {v for v, c in vc.vendors.items() if c.blocked}
```

Remove the hardcoded `blocked_vendors = {"Altra", "Smartwool", "Patagonia", "K2 Sports", "K2"}` line.

- [ ] **Step 5: Run tests**

```bash
cd /Users/andyking/Lookout && uv run pytest tests/ -x -q
```

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/models.py lookout/enrich/pipeline.py vendors.yaml scripts/batch_validate.py && git commit -m "config: mark bot-blocked vendors (Teva, Smartwool, Patagonia, Altra)"
```

---

### Task 7: Validation Run

Re-run a batch to verify price and color signals are now active.

- [ ] **Step 1: Run a small batch with known price/color products**

```bash
cd /Users/andyking/Lookout && uv run lookout enrich run \
  -h mens-reserve-gore-tex-2l-jacket \
  -h mens-pedroc-2-max \
  -h mens-pursuit-3 \
  -h skybox-nx-xl-18-cubic-feet \
  -h neoloft-sleeping-pad \
  -o ./output/validation-signals-test \
  --force --verbose 2>&1
```

- [ ] **Step 2: Check that price and color signals are non-neutral**

```bash
python3 -c "
import json
for line in open('output/validation-signals-test/match_decisions.jsonl'):
    d = json.loads(line)
    for c in d['candidates_tried']:
        if c.get('outcome') == 'accept' and 'signals' in c:
            s = c['signals']
            price = s.get('price_ratio', 0.5)
            color = s.get('color_overlap', 0.5)
            neutral = 'NEUTRAL' if price == 0.5 and color == 0.5 else 'ACTIVE'
            print(f'{d[\"handle\"]:40s} price={price:.2f} color={color:.2f} [{neutral}]')
"
```

Expected: at least some products show non-0.5 price or color values.

- [ ] **Step 3: Test the regression runner**

```bash
cd /Users/andyking/Lookout && uv run lookout enrich test-resolver -d output/validation-signals-test --verbose
```

Expected: reports results (may show 0 testable if this is the first run with cached candidates — the older decision logs don't have `resolver_candidates`).

- [ ] **Step 4: Verify blocked vendors are skipped**

```bash
cd /Users/andyking/Lookout && uv run lookout enrich run -h womens-tirra-sport-closed-toe -o ./output/test-blocked --force 2>&1 | grep -i "blocked\|skipped"
```

Expected: "Vendor blocked (bot protection): Teva"
