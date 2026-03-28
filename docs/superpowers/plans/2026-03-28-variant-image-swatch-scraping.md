# Variant Image Swatch Scraping — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract per-color variant images by integrating swatch-clicking into Firecrawl's existing page rendering — one page load, one browser context, all data.

**Architecture:** Extend the Playwright service's `/scrape` endpoint to accept optional swatch parameters. After Firecrawl renders the page, the swatch logic runs on the same live page before context teardown. Results flow through Firecrawl API back to the Python pipeline.

**Tech Stack:** TypeScript (Playwright service), TypeScript (Firecrawl API), Python (pipeline caller), Docker

**Prior work:** The swatch-clicking logic (helpers, selectors, constants) is already implemented in `api.ts` and tested on Burton. The standalone `/scrape-variants` endpoint works. This plan integrates that logic into the `/scrape` flow.

---

### Task 1: Extend Playwright `/scrape` to Support Swatch Extraction

Add optional swatch fields to the existing `/scrape` endpoint. When present, run swatch extraction on the rendered page before returning.

**Files:**
- Modify: `~/firecrawl-src/apps/playwright-service-ts/api.ts`

- [ ] **Step 1: Extend the UrlModel interface**

Add optional swatch fields to the existing `UrlModel` interface:

```typescript
interface UrlModel {
  url: string;
  wait_after_load?: number;
  timeout?: number;
  headers?: { [key: string]: string };
  check_selector?: string;
  skip_tls_verification?: boolean;
  // Swatch extraction (optional — when present, runs after page render)
  swatch_selector?: string;
  gallery_selector?: string;
  wait_after_click?: number;
}
```

- [ ] **Step 2: Add swatch extraction to the `/scrape` endpoint**

In the existing `app.post('/scrape', ...)` handler, after the `scrapePage()` call succeeds and before `res.json(...)`, add the swatch extraction logic:

```typescript
    // Swatch extraction (if requested)
    let variantImages: Record<string, string[]> | undefined;
    let swatchCount: number | undefined;
    let swatchMethod: string | undefined;

    if (swatch_selector || gallery_selector || wait_after_click) {
      try {
        const swatchResult = await findSwatches(page, swatch_selector);
        if (swatchResult) {
          const swatches = page.locator(swatchResult.selector);
          swatchCount = await swatches.count();
          swatchMethod = swatchResult.method;
          variantImages = {};

          const waitMs = wait_after_click ?? 1500;

          // Capture initial color's images
          const initialImages = await collectGalleryImages(page, gallery_selector);

          let initialColorCaptured = false;
          for (let i = 0; i < swatchCount; i++) {
            const swatch = swatches.nth(i);
            const isActive = await swatch.evaluate((el: Element) => {
              return el.classList.contains('active') ||
                     el.classList.contains('selected') ||
                     el.getAttribute('aria-checked') === 'true' ||
                     el.getAttribute('aria-selected') === 'true' ||
                     el.closest('.active, .selected') !== null;
            });
            if (isActive) {
              const colorName = await extractColorName(swatch);
              if (colorName && initialImages.length > 0) {
                variantImages[colorName] = initialImages;
                initialColorCaptured = true;
                console.log(`Initial color: "${colorName}" — ${initialImages.length} images`);
              }
              break;
            }
          }

          // Click each swatch and collect images
          for (let i = 0; i < swatchCount; i++) {
            const swatch = swatches.nth(i);
            const colorName = await extractColorName(swatch);
            if (!colorName || variantImages[colorName]) continue;

            try {
              const href = await swatch.getAttribute('href');
              const currentUrl = page.url();

              if (href && href.startsWith('http')) {
                await page.goto(href, { waitUntil: 'load', timeout });
                await page.waitForTimeout(waitMs);
              } else {
                await swatch.click({ timeout: 5000 });
                await page.waitForTimeout(waitMs);
                const newUrl = page.url();
                if (newUrl !== currentUrl) {
                  const currentHost = new URL(currentUrl).hostname;
                  const newHost = new URL(newUrl).hostname;
                  if (currentHost !== newHost) {
                    console.log(`Swatch "${colorName}" navigated to different domain — aborting`);
                    break;
                  }
                }
              }

              const images = await collectGalleryImages(page, gallery_selector);
              if (images.length > 0) {
                variantImages[colorName] = images;
                console.log(`Color "${colorName}": ${images.length} images`);
              }
            } catch (clickError) {
              console.log(`Failed to click swatch "${colorName}": ${clickError}`);
            }
          }

          // Assign initial images to first swatch if not captured
          if (!initialColorCaptured && initialImages.length > 0 && swatchCount > 0) {
            const firstColor = await extractColorName(swatches.nth(0));
            if (firstColor && !variantImages[firstColor]) {
              variantImages[firstColor] = initialImages;
            }
          }

          console.log(`Swatch extraction: ${Object.keys(variantImages).length} colors from ${swatchCount} swatches`);
        } else {
          swatchCount = 0;
          swatchMethod = 'none';
        }
      } catch (swatchError) {
        console.error('Swatch extraction error (non-fatal):', swatchError);
      }
    }
```

Then update the `res.json(...)` response to include the swatch data:

```typescript
    res.json({
      content: result.content,
      pageStatusCode: result.status,
      contentType: result.contentType,
      ...(pageError && { pageError }),
      ...(variantImages !== undefined && { variant_images: variantImages }),
      ...(swatchCount !== undefined && { swatch_count: swatchCount }),
      ...(swatchMethod !== undefined && { swatch_method: swatchMethod }),
    });
```

- [ ] **Step 3: Destructure new fields from request body**

Update the destructuring at the top of the `/scrape` handler to include the new fields:

```typescript
  const { url, wait_after_load = 0, timeout = 15000, headers, check_selector, skip_tls_verification = false, swatch_selector, gallery_selector, wait_after_click }: UrlModel = req.body;
```

- [ ] **Step 4: Rebuild and test**

```bash
cd ~/firecrawl-src && docker compose up -d --build playwright-service
```

Test with Burton (swatch params):
```bash
curl -s -X POST http://localhost:3003/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html", "timeout": 30000, "wait_after_load": 2000, "swatch_selector": "a.variant-swatch.variationColor", "gallery_selector": ".product-image img", "wait_after_click": 3000}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'HTML: {len(d.get(\"content\",\"\"))} chars, variant_images: {len(d.get(\"variant_images\",{}))}, swatches: {d.get(\"swatch_count\")}')"
```

Expected: HTML content + 7 variant_images colors.

Test without swatch params (unchanged behavior):
```bash
curl -s -X POST http://localhost:3003/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html", "timeout": 30000}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'HTML: {len(d.get(\"content\",\"\"))} chars, has variant_images: {\"variant_images\" in d}')"
```

Expected: HTML content, no variant_images field.

- [ ] **Step 5: Commit**

```bash
cd ~/firecrawl-src && git add apps/playwright-service-ts/api.ts && git commit -m "feat: integrate swatch extraction into /scrape endpoint"
```

---

### Task 2: Thread Swatch Options Through Firecrawl API

Pass swatch parameters from the Firecrawl API to the Playwright service and include variant_images in the response.

**Files:**
- Modify: `~/firecrawl-src/apps/api/src/controllers/v2/types.ts`
- Modify: `~/firecrawl-src/apps/api/src/scraper/scrapeURL/engines/playwright/index.ts`

- [ ] **Step 1: Add swatch fields to ScrapeOptionsBase**

In `types.ts`, add optional swatch fields to the `baseScrapeOptions` schema (around line 570, after `waitFor`):

```typescript
  waitFor: z.int().nonnegative().max(60000).prefault(0),
  // Swatch extraction (variant color images)
  swatchSelector: z.string().optional(),
  gallerySelector: z.string().optional(),
  waitAfterClick: z.int().nonnegative().max(30000).optional(),
```

- [ ] **Step 2: Pass swatch fields to Playwright service**

In `playwright/index.ts`, add the swatch fields to the request body:

```typescript
export async function scrapeURLWithPlaywright(
  meta: Meta,
): Promise<EngineScrapeResult> {
  const response = await robustFetch({
    url: config.PLAYWRIGHT_MICROSERVICE_URL!,
    headers: { "Content-Type": "application/json" },
    body: {
      url: meta.rewrittenUrl ?? meta.url,
      wait_after_load: meta.options.waitFor,
      timeout: meta.abort.scrapeTimeout(),
      headers: meta.options.headers,
      skip_tls_verification: meta.options.skipTlsVerification,
      // Swatch extraction
      ...(meta.options.swatchSelector && { swatch_selector: meta.options.swatchSelector }),
      ...(meta.options.gallerySelector && { gallery_selector: meta.options.gallerySelector }),
      ...(meta.options.waitAfterClick && { wait_after_click: meta.options.waitAfterClick }),
    },
    method: "POST",
    logger: meta.logger.child("scrapeURLWithPlaywright/robustFetch"),
    schema: z.object({
      content: z.string(),
      pageStatusCode: z.number(),
      pageError: z.string().optional(),
      contentType: z.string().optional(),
      variant_images: z.record(z.array(z.string())).optional(),
      swatch_count: z.number().optional(),
      swatch_method: z.string().optional(),
    }),
    mock: meta.mock,
    abort: meta.abort.asSignal(),
  });
```

- [ ] **Step 3: Include variant_images in EngineScrapeResult**

After the response handling, pass through variant data. First check if `EngineScrapeResult` needs a new field:

```typescript
  return {
    url: meta.rewrittenUrl ?? meta.url,
    html: response.content,
    statusCode: response.pageStatusCode,
    error: response.pageError,
    contentType: response.contentType,
    proxyUsed: "basic",
    // Pass through variant images if present
    ...(response.variant_images && { variantImages: response.variant_images }),
    ...(response.swatch_count !== undefined && { swatchCount: response.swatch_count }),
    ...(response.swatch_method && { swatchMethod: response.swatch_method }),
  };
```

Check and update the `EngineScrapeResult` type to accept these optional fields.

- [ ] **Step 4: Rebuild Firecrawl API**

```bash
cd ~/firecrawl-src && docker compose up -d --build api
```

- [ ] **Step 5: Test via Firecrawl API**

```bash
curl -s -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html", "waitFor": 2000, "swatchSelector": "a.variant-swatch.variationColor", "gallerySelector": ".product-image img", "waitAfterClick": 3000}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({k:v for k,v in d.items() if k != 'content'}, indent=2)[:2000])"
```

Expected: Response includes variant_images alongside normal scrape data.

- [ ] **Step 6: Commit**

```bash
cd ~/firecrawl-src && git add apps/api/src/controllers/v2/types.ts apps/api/src/scraper/scrapeURL/engines/playwright/index.ts && git commit -m "feat: thread swatch extraction options through Firecrawl API"
```

---

### Task 3: Update Python Pipeline to Use Integrated Scrape

Modify the Python pipeline to pass swatch parameters via the Firecrawl scrape call and handle variant_images in the response.

**Files:**
- Modify: `lookout/enrich/firecrawl_scraper.py`
- Modify: `lookout/enrich/pipeline.py`

- [ ] **Step 1: Update `scrape_markdown()` to accept and pass swatch params**

Modify the `scrape_markdown` method in `FirecrawlScraper` to accept optional swatch parameters and return variant images alongside markdown:

```python
    async def scrape_markdown(
        self,
        url: str,
        swatch_selector: str | None = None,
        gallery_selector: str | None = None,
        wait_after_click: int | None = None,
    ) -> tuple[str | None, dict[str, list[str]] | None]:
        """Markdown mode — returns (markdown, variant_images).

        When swatch params are provided, the Playwright service runs
        swatch extraction on the same rendered page. variant_images is
        None if no swatch params or no swatches found.
        """
        await self._polite_delay()
        try:
            kwargs = {
                "formats": ["markdown"],
                "only_main_content": True,
                "exclude_tags": [
                    "nav", "footer", "header",
                    "[role='navigation']",
                    "[role='banner']",
                    "[role='contentinfo']",
                    ".site-footer", ".site-header", ".site-nav",
                    "#cookie-banner", ".cookie-notice",
                    ".announcement-bar",
                ],
            }
            if swatch_selector:
                kwargs["swatchSelector"] = swatch_selector
            if gallery_selector:
                kwargs["gallerySelector"] = gallery_selector
            if wait_after_click:
                kwargs["waitAfterClick"] = wait_after_click

            doc = await self._client.scrape(url, **kwargs)

            # Extract variant images from response if present
            variant_images = None
            if hasattr(doc, 'variant_images') and doc.variant_images:
                variant_images = doc.variant_images
                logger.info(
                    "Firecrawl returned variant images for %d colors from %s",
                    len(variant_images), url,
                )

            return doc.markdown, variant_images
        except Exception:
            logger.exception("Firecrawl markdown scrape failed for %s", url)
            return None, None
```

- [ ] **Step 2: Update all `scrape_markdown` callers in pipeline.py**

The `scrape_markdown` signature changed from returning `str | None` to `tuple[str | None, dict | None]`. Update all callers:

In `pipeline.py`, find the main scrape call (around line 479):
```python
                markdown, variant_images = await self.firecrawl.scrape_markdown(
                    scrape_url,
                    swatch_selector=vendor_config.swatch_selector,
                    gallery_selector=vendor_config.gallery_selector,
                    wait_after_click=1500 if vendor_config.swatch_selector else None,
                )
```

Also find the fallback scrape call (around line 515):
```python
                            fallback_md, _ = await self.firecrawl.scrape_markdown(
                                fallback_output.selected_url
                            )
```

And update the `is_bot_blocked` check line to use `markdown` instead of the old variable name.

- [ ] **Step 3: Inject Firecrawl variant images into facts**

After the LLM fact extraction step (around the catalog image injection), add:

```python
            # Step 3c-pre: Inject variant images from Firecrawl swatch extraction
            if variant_images and not facts.variant_image_candidates:
                facts.variant_image_candidates = {
                    color: urls if isinstance(urls, list) else [urls]
                    for color, urls in variant_images.items()
                }
                handle_log.entries.append(
                    LogEntry(
                        message=f"Firecrawl swatch extraction found images for {len(variant_images)} colors",
                        data={"colors": list(variant_images.keys())},
                    )
                )
```

- [ ] **Step 4: Remove or gate the standalone swatch scrape call (Step 3d)**

The Step 3d block that calls `self.firecrawl.scrape_variant_images()` should now only fire as a fallback — when Firecrawl's integrated swatch extraction returned nothing but we still have no variant images:

```python
            # Step 3d: Standalone swatch scrape fallback
            # Only if Firecrawl's integrated extraction didn't find anything
            if (
                not vendor_config.is_shopify
                and not facts.variant_image_candidates
                and (vendor_config.swatch_selector or vendor_config.gallery_selector)
            ):
```

This gates the standalone call to only run when the vendor has explicit selectors configured, since generic selectors already ran during the Firecrawl scrape.

- [ ] **Step 5: Test end-to-end**

```bash
cd /Users/andyking/Lookout && uv run lookout enrich run --handle mens-burton-reserve-2l-insulated-jacket --vendor Burton
```

Check logs for:
- "Firecrawl swatch extraction found images for N colors"
- `variant_image_map` in `merch_output.json` has per-color entries

- [ ] **Step 6: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/firecrawl_scraper.py lookout/enrich/pipeline.py && git commit -m "feat: use integrated Firecrawl swatch extraction in pipeline"
```

---

### Task 4: Validate SPA Vendors

The key test — do SPA vendors (K2, Patagonia, Arc'teryx) now return variant images when swatch extraction runs on Firecrawl's rendered page?

**Files:**
- Possibly modify: `vendors.yaml` (add vendor-specific selectors)

- [ ] **Step 1: Test K2 via Firecrawl API with swatch params**

```bash
curl -s -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://k2snow.com/en-us/product/standard-snowboard", "waitFor": 5000, "swatchSelector": "", "gallerySelector": "", "waitAfterClick": 2000}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); vi=d.get('variant_images',{}); print(f'K2: {len(vi)} colors, swatch_count={d.get(\"swatch_count\",\"N/A\")}')"
```

- [ ] **Step 2: Test Patagonia**

```bash
curl -s -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.patagonia.com/product/mens-nano-puff-jacket/84212.html", "waitFor": 5000, "swatchSelector": "", "gallerySelector": "", "waitAfterClick": 2000}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); vi=d.get('variant_images',{}); print(f'Patagonia: {len(vi)} colors')"
```

- [ ] **Step 3: Test Arc'teryx**

```bash
curl -s -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://arcteryx.com/us/en/shop/mens/beta-ar-jacket", "waitFor": 5000, "swatchSelector": "", "gallerySelector": "", "waitAfterClick": 2000}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); vi=d.get('variant_images',{}); print(f'Arcteryx: {len(vi)} colors')"
```

- [ ] **Step 4: Inspect HTML and add vendor selectors for any that need them**

For vendors that returned 0 swatches, inspect the Firecrawl-rendered HTML for swatch patterns and add `swatch_selector` / `gallery_selector` to `vendors.yaml`.

- [ ] **Step 5: Commit vendor config updates**

```bash
cd /Users/andyking/Lookout && git add vendors.yaml && git commit -m "config: add swatch selectors for additional vendors"
```
