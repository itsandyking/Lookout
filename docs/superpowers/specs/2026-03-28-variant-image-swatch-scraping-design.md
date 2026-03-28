# Variant Image Swatch Scraping — Design Spec

**Date:** 2026-03-28
**Status:** Draft

## Problem

Firecrawl captures product descriptions well from JS-heavy vendor sites (Burton, K2, Patagonia, Arc'teryx), but misses color-variant images. These images are loaded dynamically when a user clicks a color swatch — they don't exist in the initial HTML. The current `_extract_variants()` in `extractor.py` only sees what's in static HTML/inline JS, so `variant_image_candidates` comes back empty for these vendors.

Result: the pipeline falls back to Tier 0 (hero image for all variants) or Tier 2 (LLM guessing from URL patterns), neither of which reliably produces per-color images.

## Approach

Add a new `/scrape-variants` endpoint to Firecrawl's existing Playwright service (`~/firecrawl-src/apps/playwright-service-ts/api.ts`). This reuses the already-hardened browser (Patchright + Apify fingerprints) with zero new infrastructure. The endpoint navigates to a product page, finds color swatches, clicks each one, and collects the resulting gallery images per color.

Called from `firecrawl_scraper.py` after the regular Firecrawl scrape, gated so it only fires when needed. Results flow into `ExtractedFacts.variant_image_candidates` — no downstream changes.

## Architecture

### New Endpoint: `POST /scrape-variants`

Lives in `~/firecrawl-src/apps/playwright-service-ts/api.ts` alongside the existing `/scrape` endpoint.

**Request:**
```json
{
  "url": "https://www.burton.com/us/en/p/...",
  "swatch_selector": ".color-chip",
  "gallery_selector": ".product-gallery img",
  "timeout": 30000,
  "wait_after_click": 1500
}
```

- `swatch_selector` — optional vendor-specific CSS selector override. Omit to use generic cascade.
- `gallery_selector` — optional vendor-specific gallery area. Omit to use generic detection.
- `timeout` — page navigation timeout.
- `wait_after_click` — ms to wait after each swatch click for gallery to update.

**Behavior:**
1. Create browser context via existing `createContext()` (Patchright + fingerprint hardening).
2. Navigate to URL, wait for load.
3. Find swatch elements — vendor selectors first, then generic cascade.
4. Capture initial gallery state (first color, already selected on page load).
5. For each remaining swatch: read color name, click, wait, collect gallery images.
6. Return color→image mapping.

**Response:**
```json
{
  "variant_images": {
    "Kelp": ["https://burton.com/.../img1.png", "..."],
    "True Black": ["https://burton.com/.../img2.png", "..."]
  },
  "swatch_count": 7,
  "method": "generic"
}
```

Returns empty `variant_images` on failure — never errors out. Includes `method` ("generic" or "vendor_override") for diagnostics.

### Generic Swatch Detection

Selectors tried in priority order, first match wins:

1. `[data-color]`
2. `[data-variant-color]`
3. `.color-swatch, .color-chip, .color-option`
4. `button[aria-label*="color" i]`
5. `input[name*="color" i][type="radio"] + label`
6. `[data-option-name="Color" i] [data-option-value]`

**Color name extraction** (per swatch, first non-empty wins):
1. `data-color` or `data-variant-color` attribute
2. `aria-label` attribute
3. `title` attribute
4. Visible text content

### Gallery Change Detection

1. Before first click: snapshot all `img[src]` in gallery area → first color's images.
2. Click swatch, wait up to `wait_after_click` ms.
3. Collect all `img[src]` in gallery area → that color's images (full set, not diff).

**Gallery area detection** (generic, overridable via `gallery_selector`):
1. Vendor-provided selector if configured
2. `[class*="gallery"], [class*="carousel"], [class*="product-image"], [class*="pdp-image"]`
3. Fall back to all images within `main` or `.pdp` container

**Edge cases:**
- Swatch already selected on load → captured before any clicks as first color.
- Swatches that are navigation links → detect URL change, abort, return partial results.
- Lazy-loaded images → filter out `data:` URIs and 1x1 pixel placeholders.

### Firecrawl Scraper Integration

In `lookout/enrich/firecrawl_scraper.py`, after the regular Firecrawl scrape:

```python
if not vendor.is_shopify and not facts.variant_image_candidates:
    variant_result = await self._scrape_variant_images(
        url=url,
        vendor=vendor,
    )
    if variant_result:
        facts.variant_image_candidates = variant_result
```

**`_scrape_variant_images()` method:**
- Builds request to `http://playwright-service:3000/scrape-variants`
- Passes `swatch_selector` and `gallery_selector` from `vendors.yaml` if configured
- Timeout + error handling — returns `None` on failure, logs diagnostics
- Never blocks the pipeline

### Vendor Config (`vendors.yaml`)

Optional per-vendor swatch configuration:

```yaml
Burton:
  domain: "burton.com"
  swatch_selector: ".color-chip"
  gallery_selector: ".product-gallery img"
```

Most vendors won't need these fields — generic selectors handle common patterns. Overrides only needed for unusual markup. Hybrid approach: generic first, vendor override as fallback when generic fails.

## Files Changed

| File | Change |
|------|--------|
| `~/firecrawl-src/apps/playwright-service-ts/api.ts` | New `/scrape-variants` endpoint |
| `lookout/enrich/firecrawl_scraper.py` | New `_scrape_variant_images()` method, called after regular scrape |
| `vendors.yaml` | Optional `swatch_selectors` config per vendor |

**No changes to:** `generator.py`, `extractor.py`, `models.py`, review UI, or any downstream code. Variant images land in `ExtractedFacts.variant_image_candidates` through the existing data path.

## Testing & Validation

**Primary test product:** Burton Reserve 2L Jacket — 7 color variants, known image URLs from prior URL pattern analysis.

**Acceptance criteria:**
1. `/scrape-variants` returns 7 color entries for Reserve 2L jacket
2. Each color has at least 1 valid image URL (HEAD check returns 200)
3. Color names are recognizable (match or closely match Shopify store variant data)
4. Full pipeline run on a Burton product populates `variant_image_candidates` where previously empty

**Test sequence:**
1. Direct HTTP test against Playwright service (curl/pytest)
2. Integration test through `firecrawl_scraper.py`
3. Full `lookout enrich run` on Burton Reserve 2L

**Follow-up vendors:** K2, Patagonia, Arc'teryx — validate generic selectors work or add vendor overrides.

## Out of Scope

- Review UI image editing (drop/reassign) — deferred, tracked separately
- Burton URL pattern construction (Approach B from prior session) — fallback if swatch scraping doesn't work for Burton specifically
- Altra (PerimeterX blocked) — needs residential proxy, separate effort
- Writing images to Shopify via apply step — separate feature
