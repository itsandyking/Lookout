# Unified Push & Undo for Enrichment Output

**Date:** 2026-04-06
**Status:** Approved

## Problem

Variant image pushes to Shopify go through a standalone script (`push_variant_images.py`) that bypasses the apply module's backup system. No snapshots are taken before pushing, no record of created Shopify image IDs is kept, and there's no undo mechanism. When bad images are pushed (as happened with Locally swatch contamination), cleanup requires manual API work.

## Design

### Unified `enrich push` Command

Replaces both `enrich apply --push` and `scripts/push_variant_images.py` with a single CLI command:

```
lookout enrich push --run-dir output/enrich-20260406 \
                    --dispositions dispositions_v2.json \
                    --dry-run
```

**Execution order:**
1. Load dispositions (approved variant image assignments + description changes)
2. Snapshot current Shopify state for every affected product (full image state + body_html)
3. Save snapshots to a push manifest
4. Validate image URLs via HEAD request, skip dead ones
5. Push descriptions and variant images in a single pass per product
6. Record created Shopify image IDs in the manifest
7. Print summary: pushed, skipped, failed

### Manifest Format

One manifest file per push run at `output/push-manifests/{run_dir_name}_{timestamp}.json`:

```json
{
  "run_id": "enrich-20260406",
  "pushed_at": "2026-04-06T18:30:00Z",
  "dispositions_path": "dispositions_v2.json",
  "summary": {
    "products_pushed": 155,
    "images_created": 342,
    "images_skipped": 3,
    "descriptions_updated": 0,
    "failed": 0
  },
  "products": {
    "patagonia-black-hole-wheeled-duffel-100l": {
      "product_id": 8396214993143,
      "before": {
        "body_html": "<div>...",
        "images": [
          {
            "id": 61790878007543,
            "src": "https://cdn.shopify.com/...",
            "position": 1,
            "alt": "...",
            "variant_ids": [123, 456]
          }
        ]
      },
      "pushed": {
        "body_html": null,
        "images_created": [
          {
            "id": 62672937877751,
            "src_url": "https://external-url...",
            "alt": "...",
            "variant_ids": [789],
            "color": "Black"
          }
        ]
      }
    }
  }
}
```

### Undo Command

```
lookout enrich undo --manifest output/push-manifests/enrich-20260406_20260406T1830.json
lookout enrich undo --manifest ... --handle some-product-handle
lookout enrich undo --manifest ... --dry-run
```

**Granularity:**
- No flags: undo entire run (all products in manifest)
- `--handle`: undo a single product

**Strategy: delete what we added.**
- Delete all images listed in `pushed.images_created` by Shopify image ID
- If body_html was changed, restore from `before.body_html` via GraphQL
- Does NOT re-upload original images or restore old variant assignments — just removes what we added, returning the product to its pre-push state

**Safety:**
- Dry run by default — prints what would be deleted/restored without acting
- Requires `--confirm` flag to execute

### Module Structure

New `lookout/push/` module:

- `pusher.py` — core push logic: snapshot Shopify state, validate URLs, create images, update descriptions, write manifest
- `manifest.py` — manifest dataclass, read/write/query helpers
- `undo.py` — load manifest, delete created images, restore body_html

CLI commands added to the `enrich` group in `lookout/cli.py`:
- `enrich push` — unified push with snapshot + manifest
- `enrich undo` — revert from manifest

`scripts/push_variant_images.py` is deleted.

### Dispositions Input

The push command accepts the existing `approved_matches` format:

```json
{
  "approved_matches": [
    {"handle": "...", "color": "...", "image_url": "...", "source": "brave"}
  ]
}
```

It also reads `merch_output.json` files from the run directory for description changes (body_html), using the standard dispositions handle → status map to filter approved products.

### Safety Rails

- **URL validation:** HEAD request before each image push. Dead URLs logged and skipped, not treated as failures.
- **Locally swatch rejection:** Second gate in pusher — reject any `locally.net` / `locally.com` image URLs even if they made it into dispositions.
- **Rate limiting:** 0.5s between Shopify API calls. Retry-after on 429 responses.
- **Manifest required for undo:** No guessing or timestamp-based heuristics. Must provide the manifest from the push run.
- **Dry run:** Both push and undo support `--dry-run` to preview without writing.

### Variant ID Resolution

Uses TVR's `shopify.db` to find variant IDs by handle + color (same as current script). Query:

```sql
SELECT v.id, v.option1_value, v.option2_value, p.id as product_id
FROM variants v JOIN products p ON v.product_id = p.id
WHERE p.handle = ? AND (v.option1_value = ? OR v.option2_value = ?)
```

### Dependencies

- Shopify REST API (image create/delete, product read)
- Shopify GraphQL API (body_html update for undo restore)
- TVR shopify.db (variant ID lookup)
- httpx (async HTTP client)
