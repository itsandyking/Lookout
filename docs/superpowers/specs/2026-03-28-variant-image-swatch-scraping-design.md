# Variant Image Swatch Scraping — Design Spec

**Date:** 2026-03-28
**Status:** Revised (v2 — integrated into Playwright `/scrape` flow)

## Problem

Firecrawl captures product descriptions well from JS-heavy vendor sites (Burton, K2, Patagonia, Arc'teryx), but misses color-variant images. These images are loaded dynamically when a user clicks a color swatch — they don't exist in the initial HTML. The current `_extract_variants()` in `extractor.py` only sees what's in static HTML/inline JS, so `variant_image_candidates` comes back empty for these vendors.

Result: the pipeline falls back to Tier 0 (hero image for all variants) or Tier 2 (LLM guessing from URL patterns), neither of which reliably produces per-color images.

## V1 Learnings

The initial approach added a standalone `/scrape-variants` endpoint to the Playwright service and called it directly from the Python pipeline. This worked for Burton (7 colors, 4 images each) but failed for SPA vendors (Patagonia, K2, Arc'teryx) because the endpoint's basic `page.goto()` doesn't render SPA content the same way Firecrawl's full scrape pipeline does.

Key insight: Firecrawl already renders these SPA pages successfully for descriptions. The swatch-clicking logic should run on the same live page that Firecrawl renders, before the browser context is torn down.

## Approach (v2)

Extend the Playwright service's existing `/scrape` endpoint to accept optional swatch parameters. When present, after the page is fully rendered and HTML is captured, the swatch-clicking logic runs on the same live page — same browser context, same fingerprint, same session. Variant images are returned alongside the normal HTML response.

The Firecrawl API passes these swatch parameters through to the Playwright service and returns the variant images in its response. The Python pipeline requests swatch extraction as part of the normal Firecrawl scrape call.

**One page load. One browser context. All data.**

## Architecture

### Layer 1: Playwright Service `/scrape` Extension

Extend the existing `/scrape` endpoint in `~/firecrawl-src/apps/playwright-service-ts/api.ts`.

**New optional request fields:**
```json
{
  "url": "https://www.burton.com/...",
  "wait_after_load": 0,
  "timeout": 30000,
  "swatch_selector": ".color-chip",
  "gallery_selector": ".product-image img",
  "wait_after_click": 1500
}
```

When `swatch_selector` or any swatch field is present, after the normal page scrape completes:
1. Run `findSwatches()` (vendor selector → generic cascade)
2. Capture initial color's images
3. Click each swatch, wait, collect gallery images per color
4. Return variant_images in the response

**Extended response:**
```json
{
  "content": "<html>...</html>",
  "pageStatusCode": 200,
  "variant_images": {
    "True Black": ["https://...", "..."],
    "Kelp": ["https://...", "..."]
  },
  "swatch_count": 7,
  "swatch_method": "vendor_override"
}
```

When no swatch fields are in the request, behavior is identical to today — no variant_images in response.

### Layer 2: Firecrawl API Pass-Through

Extend `scrapeURLWithPlaywright()` in `~/firecrawl-src/apps/api/src/scraper/scrapeURL/engines/playwright/index.ts`.

**Changes:**
1. Pass `swatch_selector`, `gallery_selector`, `wait_after_click` from `meta.options` to the Playwright request body
2. Include `variant_images`, `swatch_count`, `swatch_method` in the response schema (all optional)
3. Return variant data in `EngineScrapeResult` (add optional field)

**How options flow:**
- Add optional swatch fields to `ScrapeOptionsBase` in `types.ts`
- These flow through `meta.options` to the Playwright handler
- The Python SDK passes arbitrary kwargs through to the API

### Layer 3: Python Pipeline Integration

In `lookout/enrich/firecrawl_scraper.py`, modify `scrape_markdown()` to accept and pass swatch parameters. When the response includes `variant_images`, return them alongside the markdown.

In `lookout/enrich/pipeline.py`, pass vendor swatch config when calling `scrape_markdown()`. If variant_images come back from Firecrawl, inject them into `facts.variant_image_candidates` immediately — no separate HTTP call needed.

**Fallback:** The existing `/scrape-variants` standalone endpoint remains as a fallback for cases where the integrated approach doesn't work (e.g., when the pipeline needs to retry variant extraction with different parameters without re-scraping the whole page).

### Swatch Logic (unchanged from v1)

The swatch-clicking logic is already implemented and tested:

**Generic selector cascade:**
1. `[data-color]`
2. `[data-variant-color]`
3. `.color-swatch, .color-chip, .color-option`
4. `button[aria-label*="color" i]`
5. `input[name*="color" i][type="radio"] + label`
6. `[data-option-name="Color" i] [data-option-value]`

**Color name extraction:** `data-color` → `data-variant-color` → `data-value` → `aria-label` → `title` → text content

**Gallery detection:** vendor selector → generic cascade (`[class*="gallery"] img`, `[class*="carousel"] img`, etc.)

**Edge cases:** active swatch detection, link-based swatches (navigate via href), same-domain navigation allowed, placeholder filtering, lazy-load handling.

### Vendor Config (`vendors.yaml`)

Unchanged — optional `swatch_selector` and `gallery_selector` per vendor:

```yaml
Burton:
  domain: "burton.com"
  swatch_selector: "a.variant-swatch.variationColor"
  gallery_selector: ".product-image img"
```

## Files Changed

| File | Change |
|------|--------|
| `~/firecrawl-src/apps/playwright-service-ts/api.ts` | Extend `/scrape` to accept swatch params and return variant_images. Keep `/scrape-variants` as standalone fallback. |
| `~/firecrawl-src/apps/api/src/scraper/scrapeURL/engines/playwright/index.ts` | Pass swatch fields to Playwright, include variant_images in response |
| `~/firecrawl-src/apps/api/src/controllers/v2/types.ts` | Add optional swatch fields to ScrapeOptionsBase |
| `lookout/enrich/firecrawl_scraper.py` | Pass swatch params in `scrape_markdown()`, return variant_images |
| `lookout/enrich/pipeline.py` | Pass vendor swatch config, handle variant_images from Firecrawl response |

## Testing & Validation

**Primary test:** Burton Reserve 2L Jacket — already proven with 7 colors, 4 images each.

**SPA vendor tests:** K2, Patagonia, Arc'teryx — these should now work because the page is rendered by Firecrawl's full pipeline before swatch clicking happens.

**Acceptance criteria:**
1. Burton still returns 7 colors with correct images via integrated path
2. At least one SPA vendor (K2 or Arc'teryx) returns variant images where standalone `/scrape-variants` returned 0
3. Full pipeline run populates `variant_image_candidates` without a separate HTTP call
4. Non-swatch scrapes (no swatch params) are unaffected — same response as before

## Out of Scope

- Review UI image editing (drop/reassign) — deferred
- Writing images to Shopify via apply step — separate feature
- Altra (PerimeterX blocked) — needs residential proxy
