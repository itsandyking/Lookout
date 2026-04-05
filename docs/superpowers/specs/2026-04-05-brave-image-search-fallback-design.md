# Brave Image Search Fallback for Variant Images

**Date**: 2026-04-05
**Status**: Draft
**Priority**: Variant color images (Google Shopping enablement) > General product images

## Problem

The enrichment pipeline produces `no_match` for products where:
- Vendor sites block scraping (Teva, Patagonia, Smartwool, Altra)
- The resolver can't find or confirm the vendor product page
- Scraped pages have low-confidence or missing images

These products can't be listed on Google Shopping without variant-specific color images. Currently 21 of 122 enriched products (17%) are `no_match`, with Teva (6) and Arc'teryx (4) as the largest clusters.

## Solution

Add a `BraveImageResolver` module that uses Brave's Image Search API as a fallback image source, with local Ollama vision verification to ensure quality.

## Architecture

### New Module: `lookout/enrich/brave_images.py`

Single class `BraveImageResolver`:

```python
async def find_variant_images(
    product_title: str,
    vendor: str,
    colors: list[str],
    max_candidates: int = 3,
) -> dict[str, ImageMatch]
```

`ImageMatch` dataclass:
- `url` — full-size image from Brave `properties.url`
- `thumbnail_url` — Brave-proxied 500px thumbnail
- `source_page` — page the image was found on
- `dimensions` — width × height of full-size image
- `color` — matched color variant
- `vision_verified` — bool
- `detected_color` — what Gemma reported
- `source` — `"brave_image_search"` (constant)

### Two-Pass Search Strategy

**Pass 1 — Broad sweep (one API call per product):**
1. Query: `"{vendor} {product_title}"` with `count=50`
2. Filter results: min 400×400 dimensions, high/medium confidence, deduplicate by source domain
3. Download top 10-15 thumbnails (~20KB each)
4. Send to Ollama vision: "What color is this item? Choose from: [list of needed colors]"
5. Map responses to needed variant colors

**Pass 2 — Targeted (only for unmatched colors):**
1. For each color not matched in Pass 1, query: `"{vendor} {product_title} {color}"`
2. Download top 3 thumbnails
3. Vision verify: product match + color match + e-commerce quality
4. Accept first passing candidate

Cost: ~$0.01/product (1-4 Brave queries at $0.005 each). Vision runs locally, free.

### Vision Verification

Ollama on Mac (localhost:11434), model `gemma4:e4b`.

```python
async def verify_image(
    image_path: Path,
    product_title: str,
    vendor: str,
    expected_color: str,
) -> VerifyResult
```

`VerifyResult`: `{accepted, product_match, color_match, ecommerce_suitable, detected_color}`

Prompt asks three questions:
1. Is this a product photo of `{vendor} {product_title}`?
2. Is it suitable for e-commerce (clean background, good quality)?
3. What color is the item?

**Conservative rule**: accepted only if all three pass. `think: false` for direct responses. 30s timeout per image — timeout treated as rejection.

### Pipeline Integration

**Variant images (high priority)** — new Tier 2c in `generator.py:_assign_variant_images()`:

```
Tier 1:   Explicit HTML/JS mappings from extractor
Tier 2a:  Vision-based matching from scraped images
Tier 2b:  Text-based LLM matching from scraped images
Tier 2c:  [NEW] Brave Image Search + vision verification
Tier 0:   Hero image fallback (first image → all variants)
```

Tier 2c only searches for colors not already mapped by earlier tiers.

**Product images (lower priority)** — fallback in `generator.py:_select_images()`:

If `facts.images` is empty or < 3 images after extraction, run a general Brave Image Search (`"{vendor} {product_title}"` without color). Validate and add tagged images.

**Blocked vendors** — currently the pipeline returns `SKIPPED_VENDOR_NOT_CONFIGURED` immediately. Change to: skip scrape/resolve, but still run Brave Image Search for images. No body_html generated (no vendor page), but variant images are produced for Google Shopping.

### Trigger Conditions

Brave Image Search activates when ANY of:
- Vendor is blocked
- Scrape succeeded but variant image matching failed for some colors (Tiers 1/2a/2b)
- Scrape succeeded but fewer than 3 product images extracted

### Configuration

In `vendors.yaml`:

```yaml
settings:
  brave_images:
    enabled: true
    ollama_host: "http://localhost:11434"
    ollama_model: "gemma4:e4b"
    max_candidates_per_color: 3
    min_image_dimensions: 400
    verify_timeout: 30
    brave_count: 50
    max_evaluate: 15
```

CLI override: `--brave-images / --no-brave-images`

### Output Format

Images tagged in existing output structures:

```json
{
  "url": "https://cdn.example.com/verra-black-full.jpg",
  "alt": "Women's Verra - Black",
  "source": "brave_image_search",
  "source_page": "https://www.zappos.com/teva-verra-black",
  "vision_verified": true,
  "variant_color": "Black"
}
```

Existing images get `source: "vendor_site"` or `source: "shopify_json"` for consistency. The review page (`enrich review`) displays source for auditing.

Logging in `match_decisions.jsonl`:

```json
"brave_image_search": {
  "colors_searched": ["Black", "Antiguous Green"],
  "colors_matched": ["Black"],
  "candidates_evaluated": 6,
  "images_accepted": 1
}
```

### Error Handling

Graceful degradation at every step, no retries:

| Failure | Behavior |
|---------|----------|
| Brave API error (rate limit, timeout, bad key) | Log warning, skip to Tier 0 |
| Ollama unreachable | Log warning, skip — don't use unverified images |
| Thumbnail download fails | Skip candidate, try next |
| Vision timeout (30s) | Treat as rejection, try next |
| All candidates fail for a color | No variant image for that color, move on |
| Full-size URL fails HEAD check | Skip — image can't be imported to Shopify |

### Cost

- Brave Image Search: ~$0.005-0.02/product (1-4 queries)
- Thumbnails: free (Brave CDN, ~20KB each)
- Ollama vision: free (local Mac hardware)
- Estimated total for 21 no_match products: ~$0.10-0.40

### Rate Limiting

Brave free tier: 1 query/second. Add a Brave-specific semaphore (separate from the existing per-domain semaphore) to enforce this. Paid tiers allow higher throughput.

## Future Work

- **E2B vs E4B comparison**: Run same image batch through both models, compare accuracy and speed. Both models available on Mac.
- **Centralized model config**: Consolidate all model references (vision, structured, generation) into `settings.models` block in vendors.yaml.
- **Pre-computed cache (Approach B)**: If Brave image fallback proves reliable at scale, batch pre-fetch and cache results in SQLite to eliminate latency during pipeline runs.
