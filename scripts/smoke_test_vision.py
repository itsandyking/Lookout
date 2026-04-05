#!/usr/bin/env python3
"""Smoke test: vision-based variant image matching against real products.

Pulls products from TVR's shopify.db, downloads their images from Shopify CDN,
and runs Gemma 4 E2B to match images to color variants.

Usage:
    cd ~/Lookout && uv run python scripts/smoke_test_vision.py
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from lookout.enrich.llm import OllamaVisionClient

SHOPIFY_DB = Path.home() / "The-Variant-Range" / "tvr" / "db" / "shopify.db"

# Test products: handle → description (for reporting)
TEST_PRODUCTS = [
    # 11 colors, complex names, colorblocked
    "black-hole-duffel-40l",
    # Multiple greens/blues
    "508470-patagonia-mens-better-sweater-1-4-zip",
    # Headlamps — not apparel, different visual challenge
    "black-diamond-cosmo-350-a",
    # Multi-color outdoor pack
    "hikelite-18",
    # Trucker hats — pattern/graphic heavy
    "557377-patagonia-p-6-logo-lopro-trucker-hat",
]


def load_product_data(handle: str) -> dict | None:
    """Load colors and image URLs from shopify.db."""
    conn = sqlite3.connect(str(SHOPIFY_DB))
    conn.row_factory = sqlite3.Row

    product = conn.execute(
        "SELECT id, title, vendor FROM products WHERE handle = ?", (handle,)
    ).fetchone()
    if not product:
        return None

    colors = [
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT option1_value FROM variants
               WHERE product_id = ? AND option1_name IN ('Color', 'color')
               AND option1_value != 'Default Title'""",
            (product["id"],),
        ).fetchall()
    ]

    images = [
        row[0]
        for row in conn.execute(
            "SELECT src FROM images WHERE product_id = ? ORDER BY position",
            (product["id"],),
        ).fetchall()
    ]

    conn.close()
    return {
        "handle": handle,
        "title": product["title"],
        "vendor": product["vendor"],
        "colors": colors,
        "image_urls": images,
    }


async def test_product(product: dict) -> dict:
    """Run vision matching on a single product."""
    vision = OllamaVisionClient(model="vision")

    print(f"\n{'='*70}")
    print(f"  {product['vendor']} — {product['title']}")
    print(f"  Colors ({len(product['colors'])}): {product['colors']}")
    print(f"  Images: {len(product['image_urls'])}")
    print(f"{'='*70}")

    # Deduplicate URLs
    seen = set()
    unique_urls = []
    for url in product["image_urls"]:
        normalized = url.split("?")[0]
        if normalized not in seen:
            seen.add(normalized)
            unique_urls.append(url)
    if len(unique_urls) < len(product["image_urls"]):
        print(f"  Deduplicated: {len(product['image_urls'])} → {len(unique_urls)} unique URLs")

    # Download images
    t0 = time.time()
    downloaded = await OllamaVisionClient.download_images(
        unique_urls, max_images=20
    )
    dl_time = time.time() - t0
    print(f"\n  Downloaded: {len(downloaded)}/{len(product['image_urls'])} images ({dl_time:.1f}s)")

    if not downloaded:
        print("  ⚠ No images downloaded — skipping")
        return {"product": product["title"], "matched": 0, "total": len(product["colors"]), "results": {}}

    # Run vision matching
    t0 = time.time()
    mapping = await vision.match_images_batch(downloaded, product["colors"])
    vision_time = time.time() - t0

    print(f"  Vision time: {vision_time:.1f}s ({vision_time/len(downloaded):.1f}s/image)")
    print(f"\n  Results ({len(mapping)}/{len(product['colors'])} matched):")

    for color in product["colors"]:
        url = mapping.get(color)
        if url:
            # Show just the filename part
            filename = url.split("/")[-1].split("?")[0]
            print(f"    ✓ {color:35s} → {filename}")
        else:
            print(f"    ✗ {color:35s} → (no match)")

    return {
        "product": product["title"],
        "vendor": product["vendor"],
        "matched": len(mapping),
        "total": len(product["colors"]),
        "vision_time": round(vision_time, 1),
        "results": mapping,
    }


async def main():
    print("Vision Variant Image Matching — Smoke Test")
    print(f"Model: vision (Gemma 4 E2B)")
    print(f"Database: {SHOPIFY_DB}")

    results = []
    for handle in TEST_PRODUCTS:
        product = load_product_data(handle)
        if not product:
            print(f"\n⚠ Product not found: {handle}")
            continue
        result = await test_product(product)
        results.append(result)

    # Summary
    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    total_colors = sum(r["total"] for r in results)
    total_matched = sum(r["matched"] for r in results)
    print(f"  Products tested: {len(results)}")
    print(f"  Colors matched: {total_matched}/{total_colors} ({100*total_matched/total_colors:.0f}%)")
    for r in results:
        pct = 100 * r["matched"] / r["total"] if r["total"] else 0
        print(f"    {r['vendor']:20s} {r['product']:35s} {r['matched']}/{r['total']} ({pct:.0f}%) in {r.get('vision_time', '?')}s")


if __name__ == "__main__":
    asyncio.run(main())
