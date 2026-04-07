# Vision Catalog Tools Design

**Date:** 2026-04-07
**Beads:** Lookout-918 (color family), Lookout-075 (catalog health)

## Overview

Two standalone scripts that use Gemma 4 E4B vision model to audit and enrich the product catalog. Both run as batch jobs against active products in TVR, producing CSV reports for manual review.

---

## 1. Color Family Classification (`scripts/classify_color_families.py`)

### Purpose

Classify each product's color variants into a fixed set of color families (Black, White, Gray, Navy, Blue, Green, Red, Pink, Orange, Yellow, Brown, Beige, Purple, Gold, Silver, Multi). Enables Shopify Search & Discovery color swatch filtering on collection pages.

### Input

Active products from TVR via `LookoutStore.list_products()` + `get_variants()`.

### Processing

1. For each product, group variants by color option value (option1 where option1_name in Color/color/Colour).
2. For each color, collect unique image URLs assigned to those variants.
3. Per unique image URL → one E4B vision call. Prompt asks model to pick exactly one family from the fixed list.
4. If a color has multiple distinct image URLs → flag as anomaly. Classify each image. Note if families disagree across images.
5. If a color has zero images → text fallback: keyword match color name against family list, plus a small lookup table for creative names (e.g., "Obsidian" → Black, "Brine" → Green).
6. If text fallback cannot resolve → mark as "Unknown".

### Vision Prompt

Menu-based, same pattern as `match_image_to_color()` in `llm.py`:

```
What color family does this product image belong to?
Pick exactly one: Black, White, Gray, Navy, Blue, Green, Red, Pink, Orange, Yellow, Brown, Beige, Purple, Gold, Silver, Multi

Respond with only the color family name.
```

### Output

`{output_dir}/color_families/color_family_report_{timestamp}.csv`

Columns:
- `vendor` — vendor name
- `handle` — product handle
- `product_title` — product title
- `color_name` — variant color option value
- `color_family` — classified family
- `source` — how family was determined: `vision`, `text`, `unknown`
- `unique_image_count` — number of distinct image URLs for this color
- `anomaly_flag` — true if multiple distinct images exist for one color
- `image_urls` — semicolon-separated list of distinct URLs

### CLI

```
python scripts/classify_color_families.py --output output/ [--vendor X] [--resume]
```

- `--vendor` — filter to a single vendor
- `--resume` — skip products already in the output CSV

---

## 2. Catalog Health Audit (`scripts/catalog_health_audit.py`)

### Purpose

Single-pass quality audit of active products. Combines vision-based image validation with text-based description checks. Produces one report with per-check severity columns.

### Input

Active products from TVR via `LookoutStore`.

### Checks

#### Vision checks (one Ollama call per product, using hero image):

**Image-product mismatch (severity: FAIL)**
Hero image doesn't show the expected product type. E.g., title says "Women's Down Jacket" but image shows boots.

**Lifestyle/placeholder detection (severity: WARN)**
Image is a lifestyle editorial shot with no clear product, a placeholder graphic, a vendor logo, or an "image coming soon" graphic.

Both checks come from a single vision prompt that asks for a structured response:

```
Look at this product image. The product is listed as: "{product_title}" (type: {product_type}).

1. Does the image show this type of product? Answer YES or NO.
2. What kind of image is this? Answer one of: PRODUCT, LIFESTYLE, PLACEHOLDER, OTHER.

Respond in exactly this format:
MATCH: YES or NO
TYPE: PRODUCT or LIFESTYLE or PLACEHOLDER or OTHER
```

#### Text checks (no model needed):

**Description quality (severity: WARN)**
Flags if:
- Body HTML is empty or under 50 characters (after stripping tags)
- Contains boilerplate patterns: "buy locally", "visit us", "contact dealer", "description coming soon", "check with your local"
- Is just the product title repeated

**Title-description coherence (severity: WARN → FAIL if egregious)**
Key terms from the title (product type words) should appear somewhere in the description. If title says "Women's Down Jacket" and description talks about hiking boots → FAIL.

### Source of Truth

Product title, product type, option names, barcode/SKU are assumed correct. The audit validates images and descriptions against these.

### Output

`{output_dir}/catalog_health/catalog_health_report_{timestamp}.csv`

Columns:
- `vendor` — vendor name
- `handle` — product handle
- `product_title` — product title
- `product_type` — product type
- `image_verdict` — match / mismatch / lifestyle / placeholder / no_image
- `description_quality` — ok / weak / empty
- `title_desc_coherence` — ok / mismatch
- `overall_severity` — OK / WARN / FAIL (worst of all checks)
- `notes` — human-readable explanation of any flags

### CLI

```
python scripts/catalog_health_audit.py --output output/ [--vendor X] [--resume] [--skip-vision]
```

- `--skip-vision` — run only text checks (fast, no Ollama needed)
- `--vendor` — filter to a single vendor
- `--resume` — skip products already in the output CSV

---

## 3. Shared Infrastructure

**Vision client:** Existing `OllamaVisionClient` in `lookout/enrich/llm.py`. Both scripts use it the same way the variant image audit does. Model: E4B (constructor arg `model="vision"`).

**Store access:** `LookoutStore.list_products()` and `get_variants()`. No new store methods needed.

**Resume pattern:** On `--resume`, read existing output CSV, collect processed handles, skip them. Same approach as `scripts/audit_variant_images.py`.

**No shared module between scripts.** They are independent. If common patterns emerge after building both, refactor then.

**Concurrency:** Sequential image processing. Ollama on Mac handles one request at a time.

---

## Out of Scope

- Writing color_family metafields to Shopify (export concern, separate step)
- Feeding audit results into the enrichment pipeline automatically
- Running these checks as part of `lookout enrich run`
- Splitting text and vision checks into separate scripts (can be done later if needed)
