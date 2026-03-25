# Lookout Remodel: Merchandising Command Center

**Date:** 2026-03-25
**Status:** Draft
**Implementation:** Dual-path — remodel (worktree A) and rewrite (worktree B), compare outcomes

## Context

Lookout was assembled from modules extracted from TVR, Product-Reconciliation, CE (catalog enrichment), and a new Merchfill pipeline. The result is a working codebase (72 tests passing, all imports clean) with an architectural identity crisis: half the code acts like it still lives inside TVR, importing raw SQLAlchemy models and mounting into TVR's web app. The other half is a self-contained enrichment pipeline with its own web UI and job queue.

TVR has evolved to be the connective tissue between Shopify, vendor data, and downstream tools. Lookout should be the merchandising command center — owning the full loop of audit, enrich, rank, and output.

## Decisions Made

- **Lookout is the merchandising command center**, not a TVR satellite.
- **CLI-first**, no web UI in this phase. Web comes later as a layer on top.
- **Consume TVR via store APIs**, not raw model imports. If TVR lacks a needed method, add it to TVR.
- **Smart retries built into the pipeline** (Tier 1), with external agent loops for the campaign cycle (Tier 2).
- **Dual implementation paths** to evaluate existing code quality: remodel (refactor toward spec) and rewrite (fresh build from spec), then compare.
- **Output tools**: Matrixify for variant image assignments, Ablestar for everything else. Direct Shopify API is future work.

## Module Structure

```
lookout/
  __init__.py
  cli.py                  # single Click entry point
  store.py                # Lookout's TVR interface layer

  audit/
    __init__.py
    auditor.py            # content gap detection + priority scoring
    models.py             # ProductScore, AuditResult

  enrich/
    __init__.py
    pipeline.py           # orchestrator: resolve → scrape → extract → generate
    resolver.py           # URL discovery via search engines
    scraper.py            # static (httpx) + dynamic (Playwright) scraping
    extractor.py          # HTML → structured facts (deterministic, no LLM)
    generator.py          # facts → body HTML, image selection, variant assignment
    llm.py                # provider-agnostic LLM client (Anthropic first)
    models.py             # pipeline data models
    prompts/              # .prompt template files
      extract_facts.prompt
      generate_body_html.prompt
      select_variant_images.prompt

  ranking/
    __init__.py
    ranker.py             # collection sort scoring (velocity, margin, inventory, newness)

  output/
    __init__.py
    matrixify.py          # Matrixify CSV: variant image assignments + image enrichment export
    ablestar.py           # Ablestar-friendly formats (descriptions, tags, SEO, etc.)
    alt_text.py           # WCAG alt text generation
    google_shopping.py    # Google Shopping metafields + SEO meta

  taxonomy/
    __init__.py
    mappings.py           # product type → Google category, gender, sizing, weights, etc.

vendors.yaml                # per-vendor scraping config (project root, not in package)
```

### Boundary rules

1. **Only `store.py` imports from `tvr`**. Every other module receives dicts, not SQLAlchemy models.
2. **`enrich/` is self-contained** for its core work (scraping, extraction, LLM). It uses `store.py` only when it needs product/variant context from TVR.
3. **`output/` modules** read from TVR (via store) and from enrich pipeline results. They produce files.
4. **`taxonomy/`** is pure data — no IO, no dependencies. Everyone can import from it.
5. **Configuration constants** (MERCH_WEIGHTS, NEW_ARRIVAL_DAYS, LOW_INVENTORY_THRESHOLD, LOCATIONS, EXCLUDED_VENDORS) live in `taxonomy/mappings.py` or a new `taxonomy/config.py`. These are Lookout's own values — not imported from `tvr.core.config` at runtime. If TVR's values change, Lookout updates its own copy deliberately.
6. **`vendors.yaml`** lives at the project root (`/Lookout/vendors.yaml`), outside the package. It's user-editable config, not shipped with the package. The CLI looks for it in cwd first, then accepts `--vendors` override.

## The Store Layer

`store.py` wraps TVR's `ShopifyStore` and `VendorStore`. It returns plain dicts, never ORM objects.

```python
class LookoutStore:
    """Lookout's interface to TVR data."""

    def __init__(self, db_url: str | None = None):
        # Wraps TVR's ShopifyStore and VendorStore connection setup.
        # db_url defaults to TVR's configured PostgreSQL connection.

    # --- Product data (audit + ranking) ---
    def list_products(vendor=None, product_type=None, status="active") -> list[dict]
    def get_product(handle: str) -> dict | None
    def list_vendors() -> list[str]
    def list_product_types() -> list[str]
    def list_collections() -> list[dict]

    # --- Inventory + sales (audit scoring + ranking) ---
    def get_inventory(product_id: int) -> dict
        # Target shape (pending TVR verification):
        # {total, by_location: {loc_name: qty}, value, full_price_value}
    def get_sales_velocity(product_id: int, days: int = 28) -> dict
        # Target shape (pending TVR verification):
        # {units, weekly_avg}

    # --- Variant data (image assignment + enrichment) ---
    def get_variants(product_id: int) -> list[dict]
    def get_variant_by_barcode(barcode: str) -> dict | None

    # --- Catalog data (image enrichment from vendor catalogs) ---
    def find_catalog_image(barcode: str) -> str | None
    def find_catalog_image_by_style(vendor: str, style: str, color: str) -> str | None
    def find_catalog_description(product_id: int) -> str | None

    # --- Collections (ranking) ---
    def get_collection_products(handle: str) -> list[dict]
```

### TVR methods that may need to be added

Some `LookoutStore` methods can be composed from existing TVR `ShopifyStore` methods. Others may need new methods added to TVR:

- `get_inventory()` — aggregates across Variant, InventoryItem, InventoryLevel. May need a dedicated TVR method.
- `get_sales_velocity()` — joins Order + OrderLineItem with date filtering. May need a dedicated TVR method.
- Catalog methods — `VendorStore` may already expose these; needs verification.

### Error handling

`LookoutStore` methods return `None` or empty lists when data is not found — they don't raise exceptions for missing data. Connection failures (TVR unavailable, bad db_url) raise on construction, not on individual calls. The enrich pipeline can operate without a store (CSV-driven mode), but audit, ranking, and output commands require it and should fail fast with a clear error if the store can't connect.

## LLM Client Interface

`enrich/llm.py` provides a provider-agnostic LLM interface. The existing interface is kept:

- `LLMProvider` — abstract base with `complete()` and `complete_json()` methods
- `AnthropicProvider` — Claude implementation (default: claude-sonnet)
- `LLMClient` — high-level wrapper with prompt template loading and JSON schema validation

The `prompts/` directory contains `.prompt` template files using Python `str.format()` interpolation (e.g., `{facts}`, `{handle}`). This is the existing convention from the current codebase — no template engine. Three prompt files exist: `extract_facts.prompt`, `generate_body_html.prompt`, `select_variant_images.prompt`.

## CLI Surface

```
lookout audit [--vendor X] [--out priority.csv]
    Run content audit. Print summary to terminal.
    Optional --out exports priority CSV for review or external use.

lookout enrich run [--vendor X] [--max-rows N] [--concurrency 5] [--out ./output] [--force] [--dry-run]
    Run enrichment on products with gaps. Audits internally to find and
    prioritize targets — no input file needed. Outputs: run report,
    Matrixify variant images CSV, enriched content.

lookout enrich run -i priority.csv [--out ./output] [--concurrency 5] [--max-rows N] [--force] [--dry-run]
    Alternative: run from an explicit CSV (e.g., a previously exported
    audit, or a hand-curated list). Same pipeline, manual input.

lookout enrich validate input.csv
    Validate an explicit input CSV format before running.

lookout rank [--collection X | --vendor X | --product-type X] [--out rankings.csv]
    Score and rank products for collection merchandising.

lookout vendors
    List configured scraping vendors from vendors.yaml.

lookout output matrixify-images [--vendor X] [--dry-run]
    Catalog-based image enrichment (barcode + style-map matching).
    Export: Matrixify CSV for variant image assignments.

lookout output alt-text [--out alt_text.xlsx]
    Generate alt text XLSX for all active products.

lookout output google-shopping [--out google_shopping.xlsx]
    Generate Google Shopping metafields + SEO meta XLSX.

lookout output weights [--out weights.xlsx]
    Generate weight corrections XLSX for variants with type-based defaults.

lookout output weight-audit [--out weight_audit.csv]
    Generate CSV of dimensional/bulky products for manual weight review.
```

### Output module notes

**`output/ablestar.py`** — placeholder for now. Ablestar accepts standard Shopify CSV format. As we identify specific Ablestar workflows (bulk description updates, tag management, SEO fields), this module grows to produce the right format for each. Initially it may just re-export the enrich pipeline's body HTML and SEO output in Ablestar-compatible CSV. Not a priority for the first implementation pass.

### Entry point

`pyproject.toml` entry point changes from `lookout.enrich.cli:main` to `lookout.cli:main`.

### Audit → Enrich handoff

The default path is **no intermediate file**. `lookout enrich run --vendor Patagonia` runs the audit internally, picks the top priorities, and enriches them in one shot. The audit produces an in-memory list of prioritized products that feeds directly into the pipeline.

The CSV is an optional export (`lookout audit --out priority.csv`) for when you want to:
- Review the audit before enriching
- Hand-edit the list (remove products, reorder)
- Run audit and enrich at different times
- Share the priority list with someone

When an explicit CSV is provided (`enrich run -i file.csv`), it skips the internal audit and uses that file directly.

### Canonical CSV schema (audit output / enrich input)

This is the single schema that `lookout audit --out` produces and `lookout enrich run -i` accepts:

| Column | Required | Type | Description |
|---|---|---|---|
| Product Handle | yes | string | Shopify handle |
| Vendor | yes | string | Vendor name (must match vendors.yaml) |
| Title | no | string | Product title (improves search accuracy) |
| Barcode | no | string | Any variant barcode/UPC (enables barcode search) |
| Has Image | yes | bool | Product has at least one image |
| Has Variant Images | yes | bool | All variants have images |
| Has Description | yes | bool | Description meets minimum length |
| Has Product Type | no | bool | Product type is set (default: true) |
| Has Tags | no | bool | Tags are set (default: true) |
| Gaps | no | string | Comma-separated gap descriptions |
| Suggestions | no | string | Semicolon-separated suggestions |
| Priority Score | no | float | Inventory-weighted priority (for sorting) |
| Admin Link | no | string | Shopify admin URL |

Boolean columns accept: true/false, yes/no, 1/0, t/f, y/n (case-insensitive).

## Three-Tier Loop Architecture

### Tier 1: Enrichment (inner loop, built into pipeline)

```
resolve URL → scrape → extract → assess confidence
     ↑                              |
     └──── retry with different      |
           search strategy ←────────┘ (if confidence < threshold)
```

**Retry triggers:** The resolver already tries multiple search strategies (barcode → title → handle) in a single pass. The retry loop wraps the *entire* resolve-scrape-extract sequence, not just resolution. A retry is triggered when:
- Resolver confidence < `warn_threshold` (70) AND at least one untried strategy remains
- Scrape fails (HTTP error, empty page) — retry with Playwright if static failed, or vice versa
- Extraction yields no usable content (no product name, no description blocks, no images)

**Retry scope:** Each retry re-resolves (with a different/broadened query), re-scrapes, and re-extracts. Max 2 retries per product (3 total attempts).

**Strategy escalation:**
1. First attempt: barcode search (if available) + title search + handle search (current behavior)
2. First retry: broadened query (drop vendor-specific terms, use product type + key words)
3. Second retry: direct URL construction from vendor domain + handle pattern

**Confidence thresholds** from `vendors.yaml` settings:
- `auto_proceed`: 85 — accept result without warning
- `warn_threshold`: 70 — accept but flag for review
- `reject_threshold`: 70 — reject, trigger retry if attempts remain

All attempts and artifacts saved for review regardless of outcome.

### Tier 2: Campaign (external agent loop)

```
audit → identify gaps → enrich top N → verify content quality → report
  ↑                                                                |
  └──────────── re-audit to measure progress ←─────────────────────┘
```

- Not built into the CLI itself — driven by an external agent (Ralph Loop or similar)
- The agent runs CLI commands, reviews output, decides next action
- Typical sequence: `lookout audit` → `lookout enrich run` → `lookout audit` (to see what improved)

### Tier 3: Outcomes (future — design for, don't build)

```
enrich products → measure sales/traffic impact → re-prioritize → repeat
```

- Requires: "last enriched at" timestamp per product, before/after sales velocity comparison
- Data source: TVR order data, potentially Shopify analytics or GA
- The audit model should have room for an `enriched_at` field and the ranking model should be able to compare pre/post enrichment periods
- **Not implemented in this phase.** We just don't close doors.

## Consolidation Plan

| Current duplication | Resolution |
|---|---|
| `enrich/merchandiser.py` Merchandiser | Renamed to `enrich/generator.py` Generator |
| `ranking/collection_ranker.py` Merchandiser | Renamed to `ranking/ranker.py` CollectionRanker |
| Two variant image assignment paths | Both kept — different data sources. `enrich/generator.py` assigns from web-scraped color swatches. `output/matrixify.py` assigns from vendor catalog DB. Both valid for different scenarios. |
| Two description generators | Both kept — LLM-generated (enrich) vs. catalog lookup (output/matrixify). Different use cases. |
| Two alt text functions | Consolidated into `output/alt_text.py`. Generator imports from there. |
| `EXCLUDED_VENDORS` in two places | One source: `taxonomy/mappings.py`. Everyone imports from there. |
| `enrich/csv_parser.py` (380 lines) | Split: input parsing stays in `enrich/models.py` or a thin `enrich/io.py`. Output writing moves to `output/`. |
| `enrich/shopify_output.py` (354 lines) | Merged into `output/` — the ShopifyOutputBuilder logic becomes part of the output modules. |
| Two web apps (`web/`, `enrich_web/`) | Both deleted. Web UI is a future phase. |

## Testing Strategy

### Carry forward (enrich internals)
- Extractor tests (8) — HTML parsing, JSON-LD, image extraction
- Model tests (7) — input validation, boolean parsing, gap detection
- Helper tests (15) — handle-to-query, URL normalization, filename sanitization
- CSV output tests (9) — Shopify CSV generation

### New tests needed
- **`store.py`** — mock TVR's ShopifyStore, verify dict translation and error handling
- **`audit/auditor.py`** — mock store, verify gap detection, priority scoring, CSV output format
- **`ranking/ranker.py`** — mock store, verify scoring formula, rank assignment, overrides
- **`cli.py`** — Click test runner for each command (audit, enrich, rank, vendors, output)
- **Integration test** — audit → CSV → enrich pipeline (with mocked scraper/LLM) → verify output matches expected format

### Tests to drop
- `test_web_routes.py` (13 tests) — web UI removed
- `test_web_storage.py` (15 tests) — web UI removed

## Implementation: Dual Path

Both paths implement this same spec.

### Worktree A: Remodel
- Refactor existing code toward the spec
- Keep modules whose internals are compatible; rewrite those that aren't
- Preserve existing tests where applicable

### Worktree B: Rewrite
- Fresh `lookout/` directory
- Build module by module from the spec
- Port individual functions/classes only when they clearly match the spec

### Comparison criteria
- **Simplicity** — LOC, number of files, cognitive load to understand each module
- **Correctness** — does it run end-to-end on a real product?
- **Test quality** — coverage and meaningfulness
- **Awkward compromises** — places where remodel bent the spec to fit existing code
- **Unnecessary duplication** — places where rewrite recreated existing code verbatim
