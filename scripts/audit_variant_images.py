#!/usr/bin/env python3
"""Audit variant-image assignments across the Shopify catalog.

For each variant with an assigned image, asks Gemma 4 E2B whether
the image actually shows that color. Surfaces mismatches.

Usage:
    # Full audit (~80 minutes)
    cd ~/Lookout && uv run python scripts/audit_variant_images.py

    # Top N products by variant count (quick check)
    cd ~/Lookout && uv run python scripts/audit_variant_images.py --limit 50

    # Single vendor
    cd ~/Lookout && uv run python scripts/audit_variant_images.py --vendor Patagonia

    # Resume after interruption (skips already-audited pairs)
    cd ~/Lookout && uv run python scripts/audit_variant_images.py --resume
"""

import argparse
import asyncio
import csv
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from lookout.enrich.llm import OllamaVisionClient

SHOPIFY_DB = Path.home() / "The-Variant-Range" / "tvr" / "db" / "shopify.db"
OUTPUT_DIR = Path(__file__).parent.parent / "output" / "audits"
BATCH_SIZE = 10  # Write results every N images


def load_audit_pairs(
    vendor: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Load unique (product, color, image_url) pairs to audit."""
    conn = sqlite3.connect(str(SHOPIFY_DB))
    conn.row_factory = sqlite3.Row

    query = """
        SELECT
            p.id as product_id,
            p.handle,
            p.title as product_title,
            p.vendor,
            v.option1_value as color,
            v.image_src as image_url
        FROM variants v
        JOIN products p ON p.id = v.product_id
        WHERE v.option1_name IN ('Color', 'color', 'Colour')
          AND v.option1_value != 'Default Title'
          AND v.image_src IS NOT NULL AND v.image_src != ''
          AND p.status = 'active'
    """
    params = []
    if vendor:
        query += " AND p.vendor = ? COLLATE NOCASE"
        params.append(vendor)

    query += " GROUP BY p.id, v.option1_value, SUBSTR(v.image_src, 1, INSTR(v.image_src || '?', '?') - 1)"
    query += " ORDER BY p.vendor, p.title, v.option1_value"

    if limit:
        query += f" LIMIT {limit}"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def audit_image(
    vision: OllamaVisionClient,
    image_data: bytes,
    color: str,
    image_url: str,
) -> dict:
    """Ask the vision model if this image matches the stated color.

    Returns dict with verdict and details.
    """
    from urllib.parse import unquote, urlparse
    path = unquote(urlparse(image_url).path)

    prompt = (
        f"A product variant is labeled as color: \"{color}\"\n"
        f"Image URL path: {path}\n\n"
        f"Does this image show a product in that color?\n\n"
        f"Reply with one of:\n"
        f"- MATCH — the product color clearly matches \"{color}\"\n"
        f"- MISMATCH — the product is a clearly different color\n"
        f"- UNCLEAR — can't determine (lifestyle shot, too small, "
        f"ambiguous color, or not a product photo)\n\n"
        f"Then on a new line, briefly describe what color you actually see."
    )

    import base64
    b64 = base64.b64encode(image_data).decode()

    payload = {
        "model": vision.model,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "think": False,
        "options": {"num_predict": 40, "temperature": 0.1},
    }

    raw = await vision._post_vision(payload)

    # Parse verdict from first line
    lines = raw.strip().split("\n")
    first_line = lines[0].strip().upper()
    description = " ".join(lines[1:]).strip() if len(lines) > 1 else ""

    if "MISMATCH" in first_line:
        verdict = "MISMATCH"
    elif "MATCH" in first_line:
        verdict = "MATCH"
    else:
        verdict = "UNCLEAR"

    return {
        "verdict": verdict,
        "model_description": description,
        "raw_response": raw,
    }


async def main():
    parser = argparse.ArgumentParser(description="Audit variant-image assignments")
    parser.add_argument("--vendor", help="Audit only this vendor")
    parser.add_argument("--limit", type=int, help="Limit number of pairs to audit")
    parser.add_argument("--resume", action="store_true", help="Skip already-audited pairs")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    suffix = f"_{args.vendor.lower().replace(' ', '-')}" if args.vendor else ""
    output_csv = OUTPUT_DIR / f"variant_image_audit{suffix}_{timestamp}.csv"
    mismatches_csv = OUTPUT_DIR / f"mismatches{suffix}_{timestamp}.csv"

    # Load already-audited pairs if resuming
    audited_keys: set[str] = set()
    if args.resume:
        for existing in OUTPUT_DIR.glob(f"variant_image_audit{suffix}_*.csv"):
            try:
                with open(existing) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        key = f"{row.get('product_id', '')}|{row.get('color', '')}|{row.get('image_url', '').split('?')[0]}"
                        audited_keys.add(key)
                print(f"Resuming: {len(audited_keys)} pairs already audited")
            except Exception:
                pass

    pairs = load_audit_pairs(vendor=args.vendor, limit=args.limit)
    if args.resume:
        pairs = [
            p for p in pairs
            if f"{p['product_id']}|{p['color']}|{p['image_url'].split('?')[0]}" not in audited_keys
        ]

    print(f"Variant Image Audit")
    print(f"  Pairs to audit: {len(pairs)}")
    print(f"  Estimated time: {len(pairs) * 1.5 / 60:.0f} minutes")
    print(f"  Output: {output_csv}")
    print()

    if not pairs:
        print("Nothing to audit.")
        return

    vision = OllamaVisionClient(model="vision")

    fieldnames = [
        "vendor", "product_title", "handle", "color", "image_url",
        "verdict", "model_description",
    ]

    results: list[dict] = []
    stats = {"MATCH": 0, "MISMATCH": 0, "UNCLEAR": 0, "ERROR": 0}
    mismatches: list[dict] = []

    # Download client for images
    http = httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Lookout/1.0)"},
    )

    t_start = time.time()

    try:
        for i, pair in enumerate(pairs):
            # Download image
            try:
                resp = await http.get(pair["image_url"])
                resp.raise_for_status()
                image_data = resp.content
            except Exception as e:
                stats["ERROR"] += 1
                results.append({**pair, "verdict": "ERROR", "model_description": str(e)})
                continue

            # Audit
            try:
                result = await audit_image(vision, image_data, pair["color"], pair["image_url"])
            except Exception as e:
                stats["ERROR"] += 1
                results.append({**pair, "verdict": "ERROR", "model_description": str(e)})
                continue

            verdict = result["verdict"]
            stats[verdict] += 1

            row = {
                "vendor": pair["vendor"],
                "product_title": pair["product_title"],
                "handle": pair["handle"],
                "color": pair["color"],
                "image_url": pair["image_url"],
                "verdict": verdict,
                "model_description": result["model_description"],
            }
            results.append(row)

            if verdict == "MISMATCH":
                mismatches.append(row)
                print(f"  ⚠ MISMATCH: {pair['vendor']} / {pair['product_title']} / {pair['color']}")
                print(f"              Model sees: {result['model_description']}")

            # Progress
            if (i + 1) % BATCH_SIZE == 0 or i == len(pairs) - 1:
                elapsed = time.time() - t_start
                rate = (i + 1) / elapsed
                remaining = (len(pairs) - i - 1) / rate if rate > 0 else 0
                print(
                    f"  [{i+1}/{len(pairs)}] "
                    f"match={stats['MATCH']} mismatch={stats['MISMATCH']} "
                    f"unclear={stats['UNCLEAR']} error={stats['ERROR']} "
                    f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)"
                )

                # Flush to CSV periodically
                mode = "w" if i < BATCH_SIZE else "a"
                write_header = i < BATCH_SIZE
                with open(output_csv, mode, newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    if write_header:
                        writer.writeheader()
                    start_idx = max(0, len(results) - BATCH_SIZE) if mode == "a" else 0
                    writer.writerows(results[start_idx:])

    finally:
        await http.aclose()

    # Write final CSV
    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Write mismatches-only file
    if mismatches:
        with open(mismatches_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(mismatches)

    # Summary
    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"  AUDIT COMPLETE")
    print(f"{'='*60}")
    print(f"  Total pairs audited: {len(results)}")
    print(f"  Time: {elapsed/60:.1f} minutes ({elapsed/len(results):.1f}s/pair)")
    print(f"  Match:    {stats['MATCH']:5d} ({100*stats['MATCH']/len(results):.1f}%)")
    print(f"  Mismatch: {stats['MISMATCH']:5d} ({100*stats['MISMATCH']/len(results):.1f}%)")
    print(f"  Unclear:  {stats['UNCLEAR']:5d} ({100*stats['UNCLEAR']/len(results):.1f}%)")
    print(f"  Error:    {stats['ERROR']:5d} ({100*stats['ERROR']/len(results):.1f}%)")
    print(f"\n  Results: {output_csv}")
    if mismatches:
        print(f"  Mismatches: {mismatches_csv}")
        print(f"\n  Top mismatched vendors:")
        vendor_mismatches: dict[str, int] = {}
        for m in mismatches:
            vendor_mismatches[m["vendor"]] = vendor_mismatches.get(m["vendor"], 0) + 1
        for vendor, count in sorted(vendor_mismatches.items(), key=lambda x: -x[1])[:10]:
            print(f"    {vendor:30s} {count}")


if __name__ == "__main__":
    asyncio.run(main())
