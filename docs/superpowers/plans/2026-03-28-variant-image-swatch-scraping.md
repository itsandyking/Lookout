# Variant Image Swatch Scraping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract per-color variant images from JS-heavy vendor sites by clicking color swatches in a headless browser.

**Architecture:** New `/scrape-variants` endpoint on Firecrawl's existing Playwright service. Called from `pipeline.py` after the Firecrawl markdown scrape when `variant_image_candidates` is empty and the vendor isn't Shopify JSON. The Playwright service port (3000) is exposed to the host so the Python pipeline can call it directly. Results flow into `ExtractedFacts.variant_image_candidates` — no downstream changes.

**Tech Stack:** TypeScript (Playwright/Patchright endpoint), Python (caller in pipeline), Docker Compose (port exposure)

---

### Task 1: Expose Playwright Service Port to Host

The Playwright service currently runs on Docker's internal `backend` network with no host port mapping. The Python pipeline runs on the host and needs to reach `/scrape-variants` directly.

**Files:**
- Modify: `~/firecrawl-src/docker-compose.yaml:60-87` (playwright-service section)

- [ ] **Step 1: Add port mapping to docker-compose**

In `~/firecrawl-src/docker-compose.yaml`, add a `ports` entry to the `playwright-service` section:

```yaml
  playwright-service:
    build: apps/playwright-service-ts
    environment:
      PORT: 3000
      PROXY_SERVER: ${PROXY_SERVER}
      PROXY_USERNAME: ${PROXY_USERNAME}
      PROXY_PASSWORD: ${PROXY_PASSWORD}
      ALLOW_LOCAL_WEBHOOKS: ${ALLOW_LOCAL_WEBHOOKS}
      BLOCK_MEDIA: ${BLOCK_MEDIA}
      MAX_CONCURRENT_PAGES: ${CRAWL_CONCURRENT_REQUESTS:-10}
    ports:
      - "3003:3000"
    networks:
      - backend
```

Port 3003 on the host maps to port 3000 in the container. This avoids conflicting with Firecrawl API on 3002.

- [ ] **Step 2: Restart the stack and verify**

```bash
cd ~/firecrawl-src && docker compose up -d --build playwright-service
```

Expected: Playwright service rebuilds and starts with port 3003 exposed.

- [ ] **Step 3: Test health endpoint from host**

```bash
curl -s http://localhost:3003/health | python3 -m json.tool
```

Expected:
```json
{
    "status": "healthy",
    "maxConcurrentPages": 10,
    "activePages": 0
}
```

- [ ] **Step 4: Commit**

```bash
cd ~/firecrawl-src && git add docker-compose.yaml && git commit -m "infra: expose playwright service on host port 3003"
```

---

### Task 2: Implement `/scrape-variants` Endpoint

The core swatch-clicking logic. Navigates to a product page, finds color swatches, clicks each one, collects gallery images per color.

**Files:**
- Modify: `~/firecrawl-src/apps/playwright-service-ts/api.ts:468` (before `app.listen`)

- [ ] **Step 1: Add the VariantScrapeRequest interface**

Add this after the existing `UrlModel` interface (line ~186) in `api.ts`:

```typescript
interface VariantScrapeRequest {
  url: string;
  swatch_selector?: string;
  gallery_selector?: string;
  timeout?: number;
  wait_after_click?: number;
}
```

- [ ] **Step 2: Add the generic selector constants**

Add these after the `AD_SERVING_DOMAINS` array (line ~177):

```typescript
const GENERIC_SWATCH_SELECTORS = [
  '[data-color]',
  '[data-variant-color]',
  '.color-swatch, .color-chip, .color-option',
  'button[aria-label*="color" i]',
  'input[name*="color" i][type="radio"] + label',
  '[data-option-name="Color" i] [data-option-value]',
];

const GENERIC_GALLERY_SELECTORS = [
  '[class*="gallery"] img',
  '[class*="carousel"] img',
  '[class*="product-image"] img',
  '[class*="pdp-image"] img',
  'main img',
  '.pdp img',
];

const PLACEHOLDER_PATTERNS = [
  /^data:/,
  /placeholder/i,
  /1x1/,
  /spacer/i,
  /blank\.(gif|png)/i,
];
```

- [ ] **Step 3: Add helper functions for swatch scraping**

Add these before the `/scrape-variants` endpoint:

```typescript
/**
 * Find swatch elements on the page using vendor-specific or generic selectors.
 * Returns the selector that matched and the elements found.
 */
const findSwatches = async (
  page: Page,
  vendorSelector?: string,
): Promise<{ selector: string; method: 'vendor_override' | 'generic' } | null> => {
  // Try vendor-specific selector first
  if (vendorSelector) {
    const count = await page.locator(vendorSelector).count();
    if (count > 0) {
      console.log(`Found ${count} swatches via vendor selector: ${vendorSelector}`);
      return { selector: vendorSelector, method: 'vendor_override' };
    }
    console.log(`Vendor selector "${vendorSelector}" found 0 swatches, trying generic`);
  }

  // Try generic selectors in priority order
  for (const selector of GENERIC_SWATCH_SELECTORS) {
    const count = await page.locator(selector).count();
    if (count > 0) {
      console.log(`Found ${count} swatches via generic selector: ${selector}`);
      return { selector, method: 'generic' };
    }
  }

  return null;
};

/**
 * Extract the color name from a swatch element.
 */
const extractColorName = async (swatch: any): Promise<string> => {
  const dataColor = await swatch.getAttribute('data-color');
  if (dataColor) return dataColor.trim();

  const dataVariantColor = await swatch.getAttribute('data-variant-color');
  if (dataVariantColor) return dataVariantColor.trim();

  const ariaLabel = await swatch.getAttribute('aria-label');
  if (ariaLabel) return ariaLabel.trim();

  const title = await swatch.getAttribute('title');
  if (title) return title.trim();

  const text = await swatch.textContent();
  if (text && text.trim().length > 0 && text.trim().length < 50) return text.trim();

  return '';
};

/**
 * Collect all visible product image URLs in the gallery area.
 * Filters out placeholders, icons, and data URIs.
 */
const collectGalleryImages = async (
  page: Page,
  gallerySelector?: string,
): Promise<string[]> => {
  let imgSelector: string | null = null;

  // Try vendor-specific gallery selector
  if (gallerySelector) {
    const count = await page.locator(gallerySelector).count();
    if (count > 0) imgSelector = gallerySelector;
  }

  // Try generic gallery selectors
  if (!imgSelector) {
    for (const selector of GENERIC_GALLERY_SELECTORS) {
      const count = await page.locator(selector).count();
      if (count > 0) {
        imgSelector = selector;
        break;
      }
    }
  }

  if (!imgSelector) return [];

  const images = await page.locator(imgSelector).evaluateAll((imgs: HTMLImageElement[]) => {
    return imgs
      .map(img => img.src || img.dataset.src || img.dataset.lazySrc || '')
      .filter(src => src.startsWith('http'));
  });

  // Filter out placeholders
  return images.filter(url => !PLACEHOLDER_PATTERNS.some(p => p.test(url)));
};
```

- [ ] **Step 4: Add the `/scrape-variants` endpoint**

Add this before `app.listen(...)` (line ~470):

```typescript
app.post('/scrape-variants', async (req: Request, res: Response) => {
  const {
    url,
    swatch_selector,
    gallery_selector,
    timeout = 30000,
    wait_after_click = 1500,
  }: VariantScrapeRequest = req.body;

  console.log(`================= Variant Scrape Request =================`);
  console.log(`URL: ${url}`);
  console.log(`Swatch Selector: ${swatch_selector || 'generic'}`);
  console.log(`Gallery Selector: ${gallery_selector || 'generic'}`);
  console.log(`Timeout: ${timeout}`);
  console.log(`Wait After Click: ${wait_after_click}`);
  console.log(`==========================================================`);

  if (!url) {
    return res.status(400).json({ error: 'URL is required' });
  }

  if (!isValidUrl(url)) {
    return res.status(400).json({ error: 'Invalid URL' });
  }

  try {
    await assertSafeTargetUrl(url);
  } catch (error) {
    if (error instanceof InsecureConnectionError) {
      return res.json({ variant_images: {}, swatch_count: 0, error: error.message });
    }
    throw error;
  }

  if (!browser) {
    await initializeBrowser();
  }

  await pageSemaphore.acquire();

  let requestContext: BrowserContext | null = null;
  let page: Page | null = null;

  try {
    const contextBundle = await createContext();
    requestContext = contextBundle.context;
    const securityState = contextBundle.securityState;
    page = await requestContext.newPage();

    // Navigate to the product page
    await scrapePage(page, url, 'load', 1000, timeout, undefined, securityState);

    // Find swatches
    const swatchResult = await findSwatches(page, swatch_selector);
    if (!swatchResult) {
      console.log('No swatches found on page');
      return res.json({ variant_images: {}, swatch_count: 0, method: 'none' });
    }

    const swatches = page.locator(swatchResult.selector);
    const swatchCount = await swatches.count();
    const variantImages: Record<string, string[]> = {};

    // Capture the initially-selected color's images (before any clicks)
    const initialImages = await collectGalleryImages(page, gallery_selector);

    // Try to get the initially-selected color name
    // Many sites visually indicate which swatch is active
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

      if (!colorName) {
        console.log(`Swatch ${i}: no color name found, skipping`);
        continue;
      }

      // Skip if we already captured this color as the initial selection
      if (variantImages[colorName]) {
        continue;
      }

      try {
        // Check if clicking will navigate away
        const currentUrl = page.url();
        await swatch.click({ timeout: 5000 });
        await page.waitForTimeout(wait_after_click);

        // Detect navigation — abort if URL changed
        if (page.url() !== currentUrl) {
          console.log(`Swatch "${colorName}" navigated away — aborting`);
          break;
        }

        const images = await collectGalleryImages(page, gallery_selector);
        if (images.length > 0) {
          variantImages[colorName] = images;
          console.log(`Color "${colorName}": ${images.length} images`);
        } else {
          console.log(`Color "${colorName}": no images found after click`);
        }
      } catch (clickError) {
        console.log(`Failed to click swatch "${colorName}": ${clickError}`);
      }
    }

    // If we got images but didn't capture the initial color, assign first swatch's images
    if (!initialColorCaptured && initialImages.length > 0 && swatchCount > 0) {
      const firstColor = await extractColorName(swatches.nth(0));
      if (firstColor && !variantImages[firstColor]) {
        variantImages[firstColor] = initialImages;
        console.log(`Assigned initial images to first swatch: "${firstColor}"`);
      }
    }

    console.log(`Variant scrape complete: ${Object.keys(variantImages).length} colors from ${swatchCount} swatches`);

    res.json({
      variant_images: variantImages,
      swatch_count: swatchCount,
      method: swatchResult.method,
    });

  } catch (error) {
    console.error('Variant scrape error:', error);
    res.json({
      variant_images: {},
      swatch_count: 0,
      error: error instanceof Error ? error.message : 'Unknown error',
    });
  } finally {
    if (page) await page.close();
    if (requestContext) await requestContext.close();
    pageSemaphore.release();
  }
});
```

- [ ] **Step 5: Rebuild the Playwright service**

```bash
cd ~/firecrawl-src && docker compose up -d --build playwright-service
```

Expected: Service rebuilds and restarts without errors.

- [ ] **Step 6: Smoke test the endpoint with curl**

```bash
curl -s -X POST http://localhost:3003/scrape-variants \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html", "timeout": 30000, "wait_after_click": 2000}' \
  | python3 -m json.tool
```

Expected: JSON response with `variant_images` containing multiple color entries, each with image URLs. `swatch_count` > 0. If Burton's swatch markup doesn't match generic selectors, the response will have `swatch_count: 0` — that's OK, we'll add vendor overrides in Task 4.

- [ ] **Step 7: Commit**

```bash
cd ~/firecrawl-src && git add apps/playwright-service-ts/api.ts && git commit -m "feat: add /scrape-variants endpoint for color swatch image extraction"
```

---

### Task 3: Add `_scrape_variant_images()` to Firecrawl Scraper + Pipeline Integration

Connect the Python pipeline to the new endpoint. Call it from `pipeline.py` when variant images are missing.

**Files:**
- Modify: `lookout/enrich/firecrawl_scraper.py:159` (FirecrawlScraper class)
- Modify: `lookout/enrich/pipeline.py:634` (Step 3d section)

- [ ] **Step 1: Add `_scrape_variant_images` method to FirecrawlScraper**

Add this method to the `FirecrawlScraper` class in `lookout/enrich/firecrawl_scraper.py`, after the `scrape_markdown` method:

```python
    async def scrape_variant_images(
        self,
        url: str,
        swatch_selector: str | None = None,
        gallery_selector: str | None = None,
        playwright_url: str = "http://localhost:3003",
        timeout: int = 30000,
        wait_after_click: int = 1500,
    ) -> dict[str, list[str]] | None:
        """Scrape color variant images by clicking swatches.

        Calls the /scrape-variants endpoint on the Playwright service.
        Returns {color: [image_urls]} or None on failure.
        """
        import httpx

        payload: dict = {"url": url, "timeout": timeout, "wait_after_click": wait_after_click}
        if swatch_selector:
            payload["swatch_selector"] = swatch_selector
        if gallery_selector:
            payload["gallery_selector"] = gallery_selector

        try:
            async with httpx.AsyncClient(timeout=timeout / 1000 + 30) as client:
                resp = await client.post(
                    f"{playwright_url}/scrape-variants",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            variant_images = data.get("variant_images", {})
            swatch_count = data.get("swatch_count", 0)
            method = data.get("method", "unknown")

            if variant_images:
                logger.info(
                    "Swatch scrape found %d colors (%d swatches, method=%s) for %s",
                    len(variant_images), swatch_count, method, url,
                )
                return variant_images
            else:
                logger.info(
                    "Swatch scrape found no variant images (%d swatches) for %s",
                    swatch_count, url,
                )
                return None

        except Exception:
            logger.warning("Swatch scrape failed for %s", url, exc_info=True)
            return None
```

- [ ] **Step 2: Add swatch scrape call to pipeline.py**

In `lookout/enrich/pipeline.py`, find the comment `# Step 3d: Color-specific image search` (around line 634). Insert the swatch scrape **before** the existing color-specific image search so it gets first crack:

```python
            # Step 3d: Swatch-based variant image extraction
            # Only for non-Shopify vendors when HTML extraction found nothing
            if (
                not vendor_config.is_shopify
                and not facts.variant_image_candidates
            ):
                handle_log.entries.append(
                    LogEntry(message="Attempting swatch scrape for variant images")
                )
                swatch_images = await self.firecrawl.scrape_variant_images(
                    url=scrape_url,
                    swatch_selector=getattr(vendor_config, 'swatch_selector', None),
                    gallery_selector=getattr(vendor_config, 'gallery_selector', None),
                )
                if swatch_images:
                    facts.variant_image_candidates = swatch_images
                    handle_log.entries.append(
                        LogEntry(
                            message=f"Swatch scrape found images for {len(swatch_images)} colors",
                            data={"colors": list(swatch_images.keys())},
                        )
                    )

            # Step 3e: Color-specific image search (fallback if swatch scrape didn't find anything)
```

Also update the existing comment from `# Step 3d:` to `# Step 3e:` on the color-specific image search block that follows.

- [ ] **Step 3: Test with a Burton product**

```bash
cd /Users/andyking/Lookout && uv run python -c "
import asyncio
from lookout.enrich.firecrawl_scraper import FirecrawlScraper

async def test():
    scraper = FirecrawlScraper()
    result = await scraper.scrape_variant_images(
        'https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html',
        wait_after_click=2000,
    )
    if result:
        for color, imgs in result.items():
            print(f'{color}: {len(imgs)} images')
            for img in imgs[:2]:
                print(f'  {img[:80]}')
    else:
        print('No variant images found')

asyncio.run(test())
"
```

Expected: Multiple colors printed with image URLs. If 0 colors, check Playwright service logs (`docker compose logs playwright-service`) to see which selectors were tried.

- [ ] **Step 4: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/firecrawl_scraper.py lookout/enrich/pipeline.py && git commit -m "feat: integrate swatch scrape into enrichment pipeline"
```

---

### Task 4: Add Vendor Config Support for Swatch Selectors

Add optional `swatch_selector` and `gallery_selector` fields to `VendorConfig` and `vendors.yaml`.

**Files:**
- Modify: `lookout/enrich/models.py:179` (VendorConfig class)
- Modify: `vendors.yaml` (vendor template)

- [ ] **Step 1: Add optional fields to VendorConfig**

In `lookout/enrich/models.py`, add two fields to the `VendorConfig` class:

```python
class VendorConfig(BaseModel):
    """Configuration for a single vendor."""

    domain: str
    is_shopify: bool = False
    fallback_domains: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)
    product_url_patterns: list[str] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)
    swatch_selector: str | None = None
    gallery_selector: str | None = None
```

- [ ] **Step 2: Update pipeline.py to use VendorConfig fields directly**

In `pipeline.py`, replace the `getattr` calls from Task 3 with direct attribute access now that the fields exist:

```python
                swatch_images = await self.firecrawl.scrape_variant_images(
                    url=scrape_url,
                    swatch_selector=vendor_config.swatch_selector,
                    gallery_selector=vendor_config.gallery_selector,
                )
```

- [ ] **Step 3: Update vendors.yaml template comment**

Add the new fields to the vendor template at the bottom of `vendors.yaml`:

```yaml
  # Template for adding new vendors
  # VendorName:
  #   domain: "vendor-domain.com"
  #   swatch_selector: ".custom-color-swatch"  # optional, for variant image extraction
  #   gallery_selector: ".product-gallery img"  # optional, for variant image extraction
  #   blocked_paths:
  #     - "/blog"
  #     - "/support"
  #   product_url_patterns:
  #     - "/product/"
  #   search:
  #     method: "site_search"
  #     query_template: "site:{domain} {query}"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/andyking/Lookout && git add lookout/enrich/models.py lookout/enrich/pipeline.py vendors.yaml && git commit -m "feat: add swatch_selector and gallery_selector to vendor config"
```

---

### Task 5: Validate on Burton Reserve 2L Jacket

End-to-end validation with the primary test product. This tests the full flow: pipeline → swatch scrape → variant_image_candidates → generator → review.

**Files:**
- No new files — this is a validation task

- [ ] **Step 1: Check Docker service is running**

```bash
curl -s http://localhost:3003/health | python3 -m json.tool
```

Expected: `{"status": "healthy", ...}`

- [ ] **Step 2: Direct endpoint test**

```bash
curl -s -X POST http://localhost:3003/scrape-variants \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html", "timeout": 30000, "wait_after_click": 2000}' \
  | python3 -m json.tool
```

Check:
- `swatch_count` should be ≥ 5 (Burton Reserve has 7 color variants)
- `variant_images` should have multiple color keys
- Each color should have ≥ 1 image URL starting with `https://`

If `swatch_count` is 0: inspect Burton's page source to find the actual swatch selector and add it to `vendors.yaml` as `swatch_selector`.

- [ ] **Step 3: Validate image URLs**

```bash
# Take the first image URL from the curl output and HEAD-check it
curl -sI "FIRST_IMAGE_URL" | head -5
```

Expected: `HTTP/2 200` with `content-type: image/...`

- [ ] **Step 4: Run enrichment on the Burton product**

```bash
cd /Users/andyking/Lookout && uv run lookout enrich run --handle mens-burton-reserve-2l-insulated-jacket --vendor Burton
```

Check the output for:
- "Attempting swatch scrape for variant images" in log
- "Swatch scrape found images for N colors" in log
- `extracted_facts.json` should have `variant_image_candidates` populated
- `merch_output.json` should have `variant_image_map` with per-color entries (not `__all__`)

- [ ] **Step 5: If generic selectors fail, add Burton-specific config**

If Step 2 returned 0 swatches, inspect the page to find the right selector:

```bash
curl -s -X POST http://localhost:3003/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html", "timeout": 30000, "wait_after_load": 2000}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['content'][:5000])"
```

Look for color swatch elements in the HTML and add the selector to `vendors.yaml`:

```yaml
  Burton:
    domain: "burton.com"
    swatch_selector: ".the-actual-selector"
    gallery_selector: ".the-actual-gallery-selector"
```

Then rebuild and retest.

- [ ] **Step 6: Commit any vendor config additions**

```bash
cd /Users/andyking/Lookout && git add vendors.yaml && git commit -m "config: add Burton swatch selectors (if needed)"
```

---

### Task 6: Validate on Additional Vendors

Test generic selectors on K2, Patagonia, and Arc'teryx. Add vendor-specific overrides where needed.

**Files:**
- Possibly modify: `vendors.yaml` (vendor-specific overrides)

- [ ] **Step 1: Test K2**

```bash
# Find a K2 product URL first, then test
curl -s -X POST http://localhost:3003/scrape-variants \
  -H "Content-Type: application/json" \
  -d '{"url": "K2_PRODUCT_URL", "timeout": 30000, "wait_after_click": 2000}' \
  | python3 -m json.tool
```

Record: swatch_count, number of colors found, method used.

- [ ] **Step 2: Test Patagonia**

```bash
curl -s -X POST http://localhost:3003/scrape-variants \
  -H "Content-Type: application/json" \
  -d '{"url": "PATAGONIA_PRODUCT_URL", "timeout": 30000, "wait_after_click": 2000}' \
  | python3 -m json.tool
```

Record results.

- [ ] **Step 3: Test Arc'teryx**

```bash
curl -s -X POST http://localhost:3003/scrape-variants \
  -H "Content-Type: application/json" \
  -d '{"url": "ARCTERYX_PRODUCT_URL", "timeout": 30000, "wait_after_click": 2000}' \
  | python3 -m json.tool
```

Record results.

- [ ] **Step 4: Add vendor overrides for any that need them**

For each vendor where generic selectors returned 0 swatches, inspect the HTML and add `swatch_selector` and/or `gallery_selector` to `vendors.yaml`.

- [ ] **Step 5: Commit**

```bash
cd /Users/andyking/Lookout && git add vendors.yaml && git commit -m "config: add vendor-specific swatch selectors where needed"
```
