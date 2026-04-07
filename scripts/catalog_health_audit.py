#!/usr/bin/env python3
"""Catalog health audit — image validation + description quality checks.

Single-pass audit of active products. Vision checks validate hero images
against product title/type. Text checks flag weak descriptions and
title-description mismatches.

Usage:
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py --vendor Patagonia
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py --skip-vision
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py --resume
"""

import argparse
import asyncio
import base64
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlalchemy import text
from tvr.db.dolt_config import load_dolt_config
from tvr.db.store import ShopifyStore

from lookout.audit.health_checks import (
    check_description_quality,
    check_title_description_coherence,
)
from lookout.enrich.llm import OllamaVisionClient

_store = ShopifyStore(load_dolt_config().connection_string)
OUTPUT_DIR = Path(__file__).parent.parent / "output" / "catalog_health"
BATCH_SIZE = 10


def load_products(vendor: str | None = None) -> list[dict]:
    """Load active products with hero image URL and body HTML."""
    query = """
        SELECT
            p.id as product_id,
            p.handle,
            p.title as product_title,
            p.vendor,
            p.product_type,
            p.body_html,
            (SELECT pi.src FROM images pi
             WHERE pi.product_id = p.id AND pi.variant_id IS NULL
             ORDER BY pi.position ASC LIMIT 1) as hero_image_url
        FROM products p
        WHERE p.status = 'active'
    """
    params: dict = {}
    if vendor:
        query += " AND LOWER(p.vendor) = LOWER(:vendor)"
        params["vendor"] = vendor

    query += " ORDER BY p.vendor, p.title"

    with _store.session() as s:
        rows = s.execute(text(query), params).fetchall()
    return [dict(r._mapping) for r in rows]


async def check_image(
    vision: OllamaVisionClient,
    image_data: bytes,
    product_title: str,
    product_type: str,
) -> dict:
    """Ask E4B to validate the hero image against the product title/type.

    Returns:
        {"match": "yes"|"no", "image_type": "product"|"lifestyle"|"placeholder"|"other"}
    """
    b64 = base64.b64encode(image_data).decode()

    type_hint = f" (type: {product_type})" if product_type else ""
    prompt = (
        f'Look at this product image. The product is listed as: '
        f'"{product_title}"{type_hint}.\n\n'
        f'1. Does the image show this type of product? Answer YES or NO.\n'
        f'2. What kind of image is this? Answer one of: '
        f'PRODUCT, LIFESTYLE, PLACEHOLDER, OTHER.\n\n'
        f'Respond in exactly this format:\n'
        f'MATCH: YES or NO\n'
        f'TYPE: PRODUCT or LIFESTYLE or PLACEHOLDER or OTHER'
    )

    payload = {
        "model": vision.model,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "think": False,
        "options": {"num_predict": 20, "temperature": 0.1},
    }

    raw = await vision._post_vision(payload)

    # Parse response
    lines = raw.strip().upper().split("\n")
    match_val = "yes"
    type_val = "product"

    for line in lines:
        line = line.strip()
        if line.startswith("MATCH:"):
            val = line.replace("MATCH:", "").strip()
            match_val = "no" if "NO" in val else "yes"
        elif line.startswith("TYPE:"):
            val = line.replace("TYPE:", "").strip()
            for t in ("PRODUCT", "LIFESTYLE", "PLACEHOLDER", "OTHER"):
                if t in val:
                    type_val = t.lower()
                    break

    return {"match": match_val, "image_type": type_val}


def _image_verdict(vision_result: dict | None, has_image: bool) -> str:
    """Convert vision result to a verdict string."""
    if not has_image:
        return "no_image"
    if vision_result is None:
        return "error"
    if vision_result["match"] == "no":
        return "mismatch"
    return vision_result["image_type"]  # product, lifestyle, placeholder, other


def _overall_severity(image_verdict: str, desc_quality: str, coherence: str) -> str:
    """Determine overall severity from individual check results."""
    if image_verdict == "mismatch" or coherence == "mismatch":
        return "FAIL"
    if image_verdict in ("lifestyle", "placeholder", "no_image", "error"):
        return "WARN"
    if desc_quality in ("weak", "empty"):
        return "WARN"
    return "OK"


async def main():
    parser = argparse.ArgumentParser(description="Catalog health audit")
    parser.add_argument("--vendor", help="Audit only this vendor")
    parser.add_argument("--resume", action="store_true", help="Skip already-audited")
    parser.add_argument(
        "--skip-vision", action="store_true", help="Run text checks only"
    )
    parser.add_argument(
        "--output", default=str(OUTPUT_DIR), help="Output directory"
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    suffix = f"_{args.vendor.lower().replace(' ', '-')}" if args.vendor else ""
    output_csv = output_dir / f"catalog_health_report{suffix}_{timestamp}.csv"

    # Resume: collect already-audited handles
    audited_handles: set[str] = set()
    if args.resume:
        for existing in output_dir.glob(f"catalog_health_report{suffix}_*.csv"):
            try:
                with open(existing) as f:
                    for row in csv.DictReader(f):
                        audited_handles.add(row.get("handle", ""))
                print(f"Resuming: {len(audited_handles)} products already audited")
            except Exception:
                pass

    products = load_products(vendor=args.vendor)
    if args.resume:
        products = [p for p in products if p["handle"] not in audited_handles]

    mode = "text-only" if args.skip_vision else "full (vision + text)"
    print("Catalog Health Audit")
    print(f"  Mode: {mode}")
    print(f"  Products to audit: {len(products)}")
    if not args.skip_vision:
        print(f"  Estimated time: {len(products) * 1.5 / 60:.0f} minutes")
    print(f"  Output: {output_csv}")
    print()

    if not products:
        print("Nothing to audit.")
        return

    vision = None if args.skip_vision else OllamaVisionClient(model="vision")
    http = None
    if not args.skip_vision:
        http = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Lookout/1.0)"},
        )

    fieldnames = [
        "vendor", "handle", "product_title", "product_type",
        "image_verdict", "description_quality", "title_desc_coherence",
        "overall_severity", "notes",
    ]

    results: list[dict] = []
    severity_counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    t_start = time.time()

    try:
        for i, product in enumerate(products):
            notes_parts: list[str] = []

            # --- Vision checks ---
            image_verdict = "skipped"
            hero_url = product.get("hero_image_url")

            if not args.skip_vision:
                if not hero_url:
                    image_verdict = "no_image"
                else:
                    try:
                        resp = await http.get(hero_url)
                        resp.raise_for_status()
                        vision_result = await check_image(
                            vision,
                            resp.content,
                            product["product_title"],
                            product.get("product_type", ""),
                        )
                        image_verdict = _image_verdict(vision_result, True)
                        if image_verdict == "mismatch":
                            notes_parts.append("Hero image doesn't match product")
                        elif image_verdict == "lifestyle":
                            notes_parts.append("Hero image is a lifestyle shot")
                        elif image_verdict == "placeholder":
                            notes_parts.append("Hero image is a placeholder")
                    except Exception as e:
                        image_verdict = "error"
                        notes_parts.append(f"Vision error: {e}")

            # --- Text checks ---
            desc_result = check_description_quality(
                product.get("body_html"),
                product_title=product.get("product_title"),
            )
            desc_quality = desc_result["quality"]
            if desc_result["reason"]:
                notes_parts.append(desc_result["reason"])

            coherence_result = check_title_description_coherence(
                title=product.get("product_title", ""),
                product_type=product.get("product_type", ""),
                body_html=product.get("body_html") or "",
            )
            coherence = coherence_result["coherence"]
            if coherence_result["reason"]:
                notes_parts.append(coherence_result["reason"])

            # --- Overall ---
            severity = _overall_severity(image_verdict, desc_quality, coherence)
            severity_counts[severity] += 1

            row = {
                "vendor": product["vendor"],
                "handle": product["handle"],
                "product_title": product["product_title"],
                "product_type": product.get("product_type", ""),
                "image_verdict": image_verdict,
                "description_quality": desc_quality,
                "title_desc_coherence": coherence,
                "overall_severity": severity,
                "notes": "; ".join(notes_parts),
            }
            results.append(row)

            if severity == "FAIL":
                print(
                    f"  ✗ FAIL: {product['vendor']} / {product['product_title']} — "
                    f"{'; '.join(notes_parts)}"
                )

            # Progress + flush
            if (i + 1) % BATCH_SIZE == 0 or i == len(products) - 1:
                elapsed = time.time() - t_start
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                remaining = (len(products) - i - 1) / rate if rate > 0 else 0
                print(
                    f"  [{i+1}/{len(products)}] "
                    f"OK={severity_counts['OK']} WARN={severity_counts['WARN']} "
                    f"FAIL={severity_counts['FAIL']} "
                    f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)"
                )
    finally:
        if http:
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
    print("  CATALOG HEALTH AUDIT COMPLETE")
    print(f"{'='*60}")
    print(f"  Total products: {total}")
    print(f"  Time: {elapsed/60:.1f} minutes")
    print(f"  OK:   {severity_counts['OK']:5d} ({100*severity_counts['OK']/total:.1f}%)")
    print(f"  WARN: {severity_counts['WARN']:5d} ({100*severity_counts['WARN']/total:.1f}%)")
    print(f"  FAIL: {severity_counts['FAIL']:5d} ({100*severity_counts['FAIL']/total:.1f}%)")
    print(f"\n  Results: {output_csv}")


if __name__ == "__main__":
    asyncio.run(main())
