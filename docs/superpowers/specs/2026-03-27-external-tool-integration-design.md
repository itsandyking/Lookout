# External Tool Integration Design

**Date:** 2026-03-27
**Status:** Draft
**Scope:** Integrate Firecrawl (scraping), Shoptimizer rules, and FeedGen prompt patterns into Lookout

---

## Context

Lookout's enrichment pipeline currently uses a custom Playwright scraper with per-vendor CSS selector configs (`vendors.yaml`) and a BeautifulSoup extractor. This works but requires ongoing maintenance as vendor sites redesign. Google Merchant Center compliance rules are not systematically enforced during content generation or feed export.

Three external open-source resources address these gaps:

1. **Firecrawl** (99.5k stars, YC-backed, AGPL-3.0) — web scraper that returns LLM-ready markdown or structured data. Replaces per-vendor selector maintenance.
2. **Google Shoptimizer** (`google/shoptimizer`, Apache-2.0) — rule-based GMC feed optimizer. Project is dying (last push Aug 2025, Content API sunset Aug 2026), but the optimizer rule logic is valuable to extract.
3. **Google FeedGen** (`google-marketing-solutions/feedgen`, Apache-2.0) — LLM-based Shopping feed title/description optimization. Active. Prompt patterns worth adapting.

### Security Assessment

| Tool | Approach | Risk |
|------|----------|------|
| Firecrawl | Self-hosted Docker, no data leaves local network | Low — 2 patched SSRF CVEs (expected for URL-fetching), funded company, active security patching |
| Shoptimizer | Extract rule logic only, no runtime dependency | Minimal — copying Apache-2.0 pure Python, no package install |
| FeedGen | Study prompt patterns only, no runtime dependency | Minimal — reading prompts for adaptation, no package install |

---

## Decision Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Integration approach | Phased (validate, integrate, harden) | Test before committing; each phase has a decision gate |
| Firecrawl hosting | Self-hosted via Docker Desktop on Mac | Data stays local, free, full control |
| Firecrawl vs Playwright | Full replacement (target), pending validation | Eliminates per-vendor selector maintenance; fallback to Playwright only if validation fails |
| Extractor fate | Bypass if structured extraction wins | Firecrawl structured extraction maps directly to ExtractedFacts |
| GMC color handling | Export-only mapping, internal colors untouched | Variant option names and storefront display stay exactly as-is; GMC feed gets Google-recognized color values via a lookup table in the export layer |
| Pi deployment | Deferred | Validate on Mac first; Pi 5 (8GB) feasible later with single-worker tuning |
| Apple Containers | Not used | No compose support, networking immature; Docker Desktop is the right tool |

---

## Phase 1: Firecrawl Validation

**Goal:** Determine if Firecrawl can replace the Playwright scraper + BeautifulSoup extractor with equal or better output quality.

**No pipeline changes in this phase.**

### Infrastructure

- New directory: `infra/firecrawl/`
  - `docker-compose.yml` — Firecrawl stack (API, Playwright service, Redis, RabbitMQ, PostgreSQL) using official arm64 images from `ghcr.io/firecrawl/`
  - `.env.example` — environment config (API port defaults to 3002, worker counts, no external API keys needed for self-hosted)
- CLI convenience: `lookout infra up` / `lookout infra down` wraps `docker compose up -d` / `docker compose down` in the `infra/firecrawl/` directory
- Firecrawl API endpoint: `http://localhost:3002` (configurable via env)

### Test Harness

A standalone script (`tests/firecrawl_validation.py`) that:

1. Loads all 13 vendors from `vendors.yaml` with known product URLs (from test CSVs or manually curated)
2. For each vendor, scrapes in 3 Firecrawl modes:
   - **HTML mode** — raw HTML output (like current Playwright)
   - **Markdown mode** — clean markdown, LLM-ready
   - **Structured extraction** — schema-based, returns JSON matching `ExtractedFacts` fields (product_name, description, bullets, specs, images, price)
3. Also scrapes with the current Playwright pipeline as baseline
4. Compares:
   - Content completeness (did we get product name, description, specs, images?)
   - HTML/text length
   - Image URL count
   - Speed (wall-clock time per scrape)
   - Failure rate (bot blocks, timeouts, empty responses)
5. Outputs a comparison report (CSV + terminal summary)

### Extraction Schema

For structured extraction mode, define a schema that maps to `ExtractedFacts`:

```python
extraction_schema = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string"},
        "description": {"type": "string"},
        "bullet_points": {"type": "array", "items": {"type": "string"}},
        "specs": {"type": "object"},
        "images": {"type": "array", "items": {"type": "string", "format": "uri"}},
        "price": {"type": "string"},
        "colors": {"type": "array", "items": {"type": "string"}},
        "json_ld": {"type": "object"}
    }
}
```

### Decision Gate

After running the harness:
- If structured extraction covers 11+/13 vendors with comparable or better quality: proceed with structured extraction mode (bypass extractor)
- If markdown mode is more reliable: proceed with markdown mode (keep extractor but simplify it)
- If specific vendors fail: assess whether per-vendor fallback is needed or if Firecrawl config adjustments fix it

---

## Phase 2: Pipeline Integration

**Goal:** Replace the Playwright scraper (and possibly the extractor) with Firecrawl in the enrichment pipeline.

### New Module: `lookout/enrich/firecrawl_scraper.py`

```python
class FirecrawlScraper:
    """Scraper that delegates to self-hosted Firecrawl API."""

    def __init__(self, base_url: str = "http://localhost:3002", mode: str = "extract"):
        ...

    async def scrape(self, url: str, vendor_config: VendorConfig) -> ScrapedPage:
        """HTML or markdown mode — returns ScrapedPage like current WebScraper."""
        ...

    async def extract(self, url: str, schema: dict) -> ExtractedFacts:
        """Structured extraction — returns ExtractedFacts directly, bypassing extractor."""
        ...
```

- Implements the same async interface as `WebScraper` for drop-in replacement
- If using structured extraction: `pipeline.py` calls `extract()` and skips `extractor.py`
- If using HTML/markdown: `pipeline.py` calls `scrape()` and routes through extractor as before
- Retries via tenacity (same pattern as existing scraper)
- Polite delay between requests (same as existing scraper)

### Pipeline Changes (`pipeline.py`)

- Replace `WebScraper` instantiation with `FirecrawlScraper`
- If structured extraction mode:
  - `extract()` returns `ExtractedFacts` directly
  - Skip `ContentExtractor` step
  - Feed `ExtractedFacts` into `Generator` as before
- Artifact caching unchanged (still saves facts.json per product)

### Vendor Config Simplification (`vendors.yaml`)

Remove from each vendor entry:
- `playwright_config` (wait_for_selector, wait_timeout_ms, extra_wait_ms)
- `selectors` (product_name, description, features, specs, images, price)
- `use_playwright` flag

Retain:
- `domain`
- `blocked_paths`
- `product_url_patterns`
- `search` config

### Cleanup

After Phase 2 is validated on a test batch:
- Remove `lookout/enrich/scraper.py` (Playwright scraper)
- Remove Playwright from `pyproject.toml` dependencies
- Remove `_is_bot_blocked()`, `_has_meaningful_content()` (Firecrawl handles this)

---

## Phase 3: GMC Rule Integration

**Goal:** Enforce Google Merchant Center compliance in content generation and feed export.

### Source Material

- **Shoptimizer** (`google/shoptimizer`, Apache-2.0): Extract rule logic from `shoptimizer_api/optimizers_builtin/`. Key modules:
  - `title_length_optimizer.py` — truncate/restructure titles to 150 char limit
  - `color_length_optimizer.py` — color value normalization for GMC
  - `gtin_optimizer.py` — GTIN check digit validation, format correction
  - `adult_optimizer.py` — adult content term detection
  - `identifier_exists_optimizer.py` — identifier_exists field logic
  - `size_length_optimizer.py` — size value normalization

- **FeedGen**: Prompt patterns for title/description optimization. Key areas:
  - Title structure: `[Brand] [Product Type] [Key Attribute] [Color] [Size]`
  - Description: feature-benefit structure, keyword density
  - Attribute inference from product data

### New Module: `lookout/enrich/gmc_rules.py`

Pure Python, no external dependencies. Contains:

```python
# Title compliance
def validate_title(title: str) -> list[str]:
    """Returns list of GMC violations (empty if compliant)."""

def structure_title(brand: str, product_type: str, attributes: dict) -> str:
    """Build GMC-optimal title from components."""

# GTIN validation
def validate_gtin(gtin: str) -> bool:
    """Check digit validation for UPC/EAN/JAN (8, 12, 13, 14 digit)."""

# Color mapping (EXPORT ONLY — never modifies internal color names)
def map_color_for_gmc(color: str) -> str:
    """Map display color name to GMC-recognized color value.
    Used ONLY in google_shopping.py export. Internal colors stay untouched."""

GMC_COLOR_MAP = {
    "Midnight": "Navy",
    "Deep Forest": "Green",
    "Slate": "Gray",
    # ... populated from Shoptimizer's color data + vendor-specific mappings
}

# Prohibited terms
def check_prohibited_terms(text: str) -> list[str]:
    """Flag promotional language, superlatives, claims GMC rejects."""

# Attribute completeness
def check_required_attributes(product: dict) -> list[str]:
    """Flag missing required GMC attributes (title, description, image, price, availability)."""
```

### Upstream: Generator Prompt Updates

Update `lookout/enrich/prompts/generate_body_html.prompt`:
- Add GMC-aware instructions adapted from FeedGen patterns
- Title structure guidance (Brand + Type + Key Attributes)
- Prohibited term avoidance (no "best", "cheapest", "free shipping", etc.)
- Feature-benefit description structure
- These are prompt changes only — no code changes to `generator.py`

### Upstream: Post-Generation Validation

After `generator.py` produces `MerchOutput`, run `gmc_rules.py` validators:
- `validate_title()` on generated title
- `check_prohibited_terms()` on body_html
- Results stored as `gmc_flags` in `MerchOutput` (new optional field)
- Flags surface in the review UI alongside existing quality scores

### Downstream: Feed Export Hardening

In `lookout/output/google_shopping.py`:
- Call `validate_gtin()` before including barcode in export
- Call `map_color_for_gmc()` when populating the color attribute (export-only, internal color unchanged)
- Call `check_required_attributes()` and warn/skip incomplete products
- Add title length enforcement (truncate with warning if > 150 chars)
- Add image URL validation (format check, not reachability — that's too slow for export)

### Color Boundary (Critical Constraint)

Internal color names are **never modified** anywhere in the system:
- Variant option values: untouched
- Product data in Shopify: untouched
- Review UI display: untouched
- `ExtractedFacts.colors`: untouched
- `MerchOutput` variant assignments: untouched

The ONLY place color mapping occurs is in `google_shopping.py` when writing the GMC `color` attribute to the export file, via `map_color_for_gmc()`. This is a one-way, export-time transformation.

---

## Files Created/Modified

### New Files

| File | Purpose |
|------|---------|
| `infra/firecrawl/docker-compose.yml` | Firecrawl self-hosted stack |
| `infra/firecrawl/.env.example` | Environment config template |
| `lookout/enrich/firecrawl_scraper.py` | Firecrawl API client |
| `lookout/enrich/gmc_rules.py` | GMC compliance rules (pure Python) |
| `tests/firecrawl_validation.py` | Validation harness (Phase 1) |

### Modified Files

| File | Change |
|------|--------|
| `lookout/cli.py` | Add `lookout infra up/down` commands |
| `lookout/enrich/pipeline.py` | Swap WebScraper for FirecrawlScraper |
| `lookout/enrich/models.py` | Add `gmc_flags` field to MerchOutput |
| `lookout/enrich/prompts/generate_body_html.prompt` | Add GMC-aware generation instructions |
| `lookout/output/google_shopping.py` | Add GTIN validation, color mapping, attribute checks |
| `vendors.yaml` | Remove Playwright configs, retain domain/paths/search |

### Removed Files (after Phase 2 validation)

| File | Reason |
|------|--------|
| `lookout/enrich/scraper.py` | Replaced by FirecrawlScraper |

### Dependencies

| Added | Removed (after Phase 2) |
|-------|------------------------|
| `firecrawl-py` (PyPI client for local API) | `playwright>=1.40` |
| | `playwright-stealth` (if present) |

---

## Testing Strategy

### Phase 1
- Validation harness covers all 13 vendors × 3 modes
- Comparison metrics: completeness, speed, failure rate
- Pass criteria: structured extraction succeeds on 11+/13 vendors

### Phase 2
- Run enrichment on existing test batch (`test_set_input.csv`)
- Compare MerchOutput quality scores (factual fidelity, structure, coverage) to baseline
- Manual review of 5-10 products in review UI
- Pass criteria: quality scores equal or better than Playwright baseline

### Phase 3
- Unit tests for each rule in `gmc_rules.py` (GTIN validation, color mapping, prohibited terms)
- Run `google_shopping.py` export on full catalog, compare to previous export
- Verify color mapping is export-only (internal colors unchanged in DB)
- Check generated content against known GMC disapproval reasons from `gmc_signals.py` data
