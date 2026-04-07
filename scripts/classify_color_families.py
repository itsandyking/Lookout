#!/usr/bin/env python3
"""Classify variant colors into color families for Search & Discovery filtering.

Groups variants by color option, deduplicates by image URL, sends one
vision call per unique image. Falls back to text-based inference when
no image exists.

Usage:
    cd ~/Lookout && uv run python scripts/classify_color_families.py
    cd ~/Lookout && uv run python scripts/classify_color_families.py --vendor Patagonia
    cd ~/Lookout && uv run python scripts/classify_color_families.py --resume
"""

import argparse
import asyncio
import base64
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlalchemy import text
from tvr.db.dolt_config import load_dolt_config
from tvr.db.store import ShopifyStore

from lookout.enrich.color_families import infer_color_family
from lookout.enrich.llm import OllamaVisionClient

_store = ShopifyStore(load_dolt_config().connection_string)
OUTPUT_DIR = Path(__file__).parent.parent / "output" / "color_families"
BATCH_SIZE = 10

COLOR_FAMILIES_MENU = (
    "Black, White, Gray, Navy, Blue, Green, Red, Pink, "
    "Orange, Yellow, Brown, Beige, Purple, Gold, Silver, Multi"
)
VALID_FAMILIES = {f.strip() for f in COLOR_FAMILIES_MENU.split(",")}


def load_color_groups(
    vendor: str | None = None,
) -> list[dict]:
    """Load unique (product, color, image_urls) groups.

    Returns one row per (product, color) with all distinct image URLs
    for that color collected into a semicolon-separated string.
    """
    query = """
        SELECT
            p.id as product_id,
            p.handle,
            p.title as product_title,
            p.vendor,
            v.option1_value as color,
            GROUP_CONCAT(DISTINCT SUBSTRING_INDEX(v.image_src, '?', 1)
                         SEPARATOR ';;') as image_urls
        FROM variants v
        JOIN products p ON p.id = v.product_id
        WHERE v.option1_name IN ('Color', 'color', 'Colour')
          AND v.option1_value != 'Default Title'
          AND p.status = 'active'
        GROUP BY p.id, v.option1_value
        ORDER BY p.vendor, p.title, v.option1_value
    """
    params: dict = {}
    if vendor:
        query = query.replace(
            "AND p.status = 'active'",
            "AND p.status = 'active' AND LOWER(p.vendor) = LOWER(:vendor)",
        )
        params["vendor"] = vendor

    with _store.session() as s:
        rows = s.execute(text(query), params).fetchall()

    result = []
    for r in rows:
        mapping = dict(r._mapping)
        raw_urls = mapping.get("image_urls") or ""
        # Filter out empty strings and None
        urls = [u for u in raw_urls.split(";;") if u and u.strip()]
        mapping["image_urls"] = urls
        result.append(mapping)
    return result


async def classify_image(
    vision: OllamaVisionClient,
    image_data: bytes,
) -> str | None:
    """Ask E4B which color family this product image belongs to."""
    b64 = base64.b64encode(image_data).decode()

    payload = {
        "model": vision.model,
        "prompt": (
            "What color family does this product image belong to?\n"
            f"Pick exactly one: {COLOR_FAMILIES_MENU}\n\n"
            "Respond with only the color family name."
        ),
        "images": [b64],
        "stream": False,
        "think": False,
        "options": {"num_predict": 10, "temperature": 0.1},
    }

    raw = await vision._post_vision(payload)
    # Clean and validate
    cleaned = raw.strip().strip(".")
    for family in VALID_FAMILIES:
        if cleaned.lower() == family.lower():
            return family
    return None


async def main():
    parser = argparse.ArgumentParser(
        description="Classify variant colors into color families"
    )
    parser.add_argument("--vendor", help="Classify only this vendor")
    parser.add_argument("--resume", action="store_true", help="Skip already-classified")
    parser.add_argument(
        "--output", default=str(OUTPUT_DIR), help="Output directory"
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    suffix = f"_{args.vendor.lower().replace(' ', '-')}" if args.vendor else ""
    output_csv = output_dir / f"color_family_report{suffix}_{timestamp}.csv"

    # Resume: collect already-classified (handle, color) pairs
    classified_keys: set[str] = set()
    if args.resume:
        for existing in output_dir.glob(f"color_family_report{suffix}_*.csv"):
            try:
                with open(existing) as f:
                    for row in csv.DictReader(f):
                        key = f"{row.get('handle', '')}|{row.get('color_name', '')}"
                        classified_keys.add(key)
                print(f"Resuming: {len(classified_keys)} color groups already classified")
            except Exception:
                pass

    groups = load_color_groups(vendor=args.vendor)
    if args.resume:
        groups = [
            g for g in groups
            if f"{g['handle']}|{g['color']}" not in classified_keys
        ]

    print("Color Family Classification")
    print(f"  Color groups to classify: {len(groups)}")
    print(f"  Output: {output_csv}")
    print()

    if not groups:
        print("Nothing to classify.")
        return

    vision = OllamaVisionClient(model="vision")
    http = httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Lookout/1.0)"},
    )

    fieldnames = [
        "vendor", "handle", "product_title", "color_name", "color_family",
        "source", "unique_image_count", "anomaly_flag", "image_urls",
    ]

    results: list[dict] = []
    stats = {"vision": 0, "text": 0, "unknown": 0, "error": 0, "anomaly": 0}
    t_start = time.time()

    try:
        for i, group in enumerate(groups):
            color = group["color"]
            urls = group["image_urls"]
            unique_count = len(urls)
            anomaly = unique_count > 1

            if anomaly:
                stats["anomaly"] += 1

            family = None
            source = "unknown"

            if urls:
                # Classify each unique image URL
                families_seen: list[str] = []
                for url in urls:
                    try:
                        resp = await http.get(url)
                        resp.raise_for_status()
                        result = await classify_image(vision, resp.content)
                        if result:
                            families_seen.append(result)
                    except Exception:
                        pass

                if families_seen:
                    # Use the most common classification
                    family = max(set(families_seen), key=families_seen.count)
                    source = "vision"
                    stats["vision"] += 1

            # Text fallback if vision didn't resolve
            if family is None:
                family = infer_color_family(color)
                if family:
                    source = "text"
                    stats["text"] += 1
                else:
                    source = "unknown"
                    stats["unknown"] += 1

            row = {
                "vendor": group["vendor"],
                "handle": group["handle"],
                "product_title": group["product_title"],
                "color_name": color,
                "color_family": family or "",
                "source": source,
                "unique_image_count": unique_count,
                "anomaly_flag": "true" if anomaly else "",
                "image_urls": ";;".join(urls),
            }
            results.append(row)

            if anomaly:
                print(
                    f"  ! ANOMALY: {group['vendor']} / {group['product_title']} / "
                    f"{color} — {unique_count} distinct images"
                )

            # Progress + flush
            if (i + 1) % BATCH_SIZE == 0 or i == len(groups) - 1:
                elapsed = time.time() - t_start
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                remaining = (len(groups) - i - 1) / rate if rate > 0 else 0
                print(
                    f"  [{i+1}/{len(groups)}] "
                    f"vision={stats['vision']} text={stats['text']} "
                    f"unknown={stats['unknown']} anomalies={stats['anomaly']} "
                    f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)"
                )
    finally:
        await http.aclose()

    # Write final CSV
    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Summary
    elapsed = time.time() - t_start
    total = len(results)
    print(f"\n{'='*60}")
    print("  COLOR FAMILY CLASSIFICATION COMPLETE")
    print(f"{'='*60}")
    print(f"  Total color groups: {total}")
    print(f"  Time: {elapsed/60:.1f} minutes")
    print(f"  Vision:   {stats['vision']:5d} ({100*stats['vision']/total:.1f}%)")
    print(f"  Text:     {stats['text']:5d} ({100*stats['text']/total:.1f}%)")
    print(f"  Unknown:  {stats['unknown']:5d} ({100*stats['unknown']/total:.1f}%)")
    print(f"  Anomalies:{stats['anomaly']:5d}")
    print(f"\n  Results: {output_csv}")


if __name__ == "__main__":
    asyncio.run(main())
