#!/usr/bin/env python3
"""Compare Firecrawl vs Playwright across vendor product pages.

Usage:
    lookout infra up
    python tests/firecrawl_validation.py

Requires: Firecrawl running at localhost:3002.
"""

from __future__ import annotations

import asyncio
import csv
import time
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class ScrapeResult:
    vendor: str
    url: str
    mode: str
    elapsed_sec: float
    product_name: str = ""
    description_len: int = 0
    bullet_count: int = 0
    spec_count: int = 0
    image_count: int = 0
    error: str = ""
    success: bool = False


VENDOR_TEST_URLS: dict[str, str] = {
    "Patagonia": "https://www.patagonia.com/product/womens-unity-fitz-easy-cut-responsibili-tee/37769.html",
    "Altra": "https://www.altrarunning.com/en-us/product-families/lone-peak-family/9",
    "Rossignol": "https://www.rossignol.com/us-en/pivot-2.0-13-gw-b95-orange-metal-FCOPC19000.html",
    "K2 Sports": "https://k2snow.com/en-us/p/reverb-youth-ski-boots-2026",
    "BlueWater": "https://www.bluewaterropes.com/product/3mm-niteline-reflective-cord/",
    "Smith Optics": "https://www.smithoptics.com/en_US/p/helmet/vantage-mips-helmet/E006559KS5155.html",
    "Burton": "https://www.burton.com/us/en/p/mens-burton-reserve-2l-insulated-jacket/W26-3025310EWZRGXXX.html",
    "Black Crows": "https://www.black-crows.com/products/102382",
    "Faction": "https://us.factionskis.com/products/dancer-1-ski-2024",
    "Jones Snowboards": "https://www.jonessnowboards.com/products/quick-tension-tail-clip-2025",
    "Arc'teryx": "https://arcteryx.com/us/en/shop/mens/kyanite-lightweight-jacket-9640",
}


async def test_firecrawl_extract(url: str, vendor: str) -> ScrapeResult:
    from lookout.enrich.firecrawl_scraper import FirecrawlScraper

    start = time.time()
    scraper = FirecrawlScraper(min_delay_ms=0, max_delay_ms=0)
    try:
        facts = await scraper.extract(url)
        elapsed = time.time() - start
        if facts is None:
            return ScrapeResult(
                vendor=vendor, url=url, mode="extract",
                elapsed_sec=round(elapsed, 2), error="No facts returned",
            )
        return ScrapeResult(
            vendor=vendor, url=url, mode="extract",
            elapsed_sec=round(elapsed, 2),
            product_name=facts.product_name,
            description_len=sum(len(b) for b in facts.description_blocks),
            bullet_count=len(facts.feature_bullets),
            spec_count=len(facts.specs),
            image_count=len(facts.images),
            success=True,
        )
    except Exception as e:
        return ScrapeResult(
            vendor=vendor, url=url, mode="extract",
            elapsed_sec=round(time.time() - start, 2), error=str(e)[:100],
        )


async def test_firecrawl_html(url: str, vendor: str) -> ScrapeResult:
    from lookout.enrich.firecrawl_scraper import FirecrawlScraper

    start = time.time()
    scraper = FirecrawlScraper(min_delay_ms=0, max_delay_ms=0)
    try:
        page = await scraper.scrape_html(url)
        elapsed = time.time() - start
        return ScrapeResult(
            vendor=vendor, url=url, mode="html",
            elapsed_sec=round(elapsed, 2),
            description_len=len(page.html),
            success=page.success,
            error=page.error or "",
        )
    except Exception as e:
        return ScrapeResult(
            vendor=vendor, url=url, mode="html",
            elapsed_sec=round(time.time() - start, 2), error=str(e)[:100],
        )


async def test_firecrawl_markdown(url: str, vendor: str) -> ScrapeResult:
    from lookout.enrich.firecrawl_scraper import FirecrawlScraper

    start = time.time()
    scraper = FirecrawlScraper(min_delay_ms=0, max_delay_ms=0)
    try:
        md = await scraper.scrape_markdown(url)
        elapsed = time.time() - start
        return ScrapeResult(
            vendor=vendor, url=url, mode="markdown",
            elapsed_sec=round(elapsed, 2),
            description_len=len(md) if md else 0,
            success=bool(md),
            error="" if md else "Empty markdown",
        )
    except Exception as e:
        return ScrapeResult(
            vendor=vendor, url=url, mode="markdown",
            elapsed_sec=round(time.time() - start, 2), error=str(e)[:100],
        )


async def test_playwright_baseline(url: str, vendor: str, vendors_config: dict) -> ScrapeResult:
    from lookout.enrich.extractor import extract_content
    from lookout.enrich.models import VendorConfig
    from lookout.enrich.scraper import WebScraper

    vendor_data = vendors_config.get(vendor, {})
    vendor_config = VendorConfig(**vendor_data)

    start = time.time()
    try:
        async with WebScraper() as scraper:
            page = await scraper.scrape(url, vendor_config)

        if not page.success:
            return ScrapeResult(
                vendor=vendor, url=url, mode="playwright",
                elapsed_sec=round(time.time() - start, 2),
                error=page.error or "Scrape failed",
            )

        _, facts = extract_content(page.html, page.final_url, vendor_config.selectors)
        elapsed = time.time() - start

        return ScrapeResult(
            vendor=vendor, url=url, mode="playwright",
            elapsed_sec=round(elapsed, 2),
            product_name=facts.product_name,
            description_len=sum(len(b) for b in facts.description_blocks),
            bullet_count=len(facts.feature_bullets),
            spec_count=len(facts.specs),
            image_count=len(facts.images),
            success=True,
        )
    except Exception as e:
        return ScrapeResult(
            vendor=vendor, url=url, mode="playwright",
            elapsed_sec=round(time.time() - start, 2), error=str(e)[:100],
        )


async def main():
    vendors_path = Path(__file__).parent.parent / "vendors.yaml"
    with open(vendors_path) as f:
        config = yaml.safe_load(f)
    vendors_config = config.get("vendors", {})

    if not VENDOR_TEST_URLS:
        print("ERROR: No test URLs configured.")
        return

    results: list[ScrapeResult] = []

    for vendor, url in VENDOR_TEST_URLS.items():
        print(f"\n{'='*60}")
        print(f"Vendor: {vendor}")
        print(f"URL: {url}")
        print(f"{'='*60}")

        for mode_name, test_fn in [
            ("playwright", lambda u, v: test_playwright_baseline(u, v, vendors_config)),
            ("extract", test_firecrawl_extract),
            ("html", test_firecrawl_html),
            ("markdown", test_firecrawl_markdown),
        ]:
            print(f"  {mode_name}...", end=" ", flush=True)
            result = await test_fn(url, vendor)
            results.append(result)
            status = "OK" if result.success else f"FAIL: {result.error[:40]}"
            print(f"{result.elapsed_sec}s — {status}")

    # Summary
    print(f"\n\n{'='*100}")
    print("VALIDATION SUMMARY")
    print(f"{'='*100}")
    print(
        f"{'Vendor':<18} {'Mode':<12} {'Time':>6} {'Name':>5} {'Desc':>6} "
        f"{'Bullets':>8} {'Specs':>6} {'Images':>7} {'Status'}"
    )
    print("-" * 100)
    for r in results:
        name_ok = "Y" if r.product_name else "N"
        status = "OK" if r.success else r.error[:30]
        print(
            f"{r.vendor:<18} {r.mode:<12} {r.elapsed_sec:>5.1f}s {name_ok:>5} "
            f"{r.description_len:>6} {r.bullet_count:>8} {r.spec_count:>6} "
            f"{r.image_count:>7} {status}"
        )

    # Save CSV
    out_path = Path(__file__).parent.parent / "firecrawl_validation_results.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "vendor", "url", "mode", "elapsed_sec", "product_name",
            "description_len", "bullet_count", "spec_count", "image_count",
            "success", "error",
        ])
        for r in results:
            writer.writerow([
                r.vendor, r.url, r.mode, r.elapsed_sec, r.product_name,
                r.description_len, r.bullet_count, r.spec_count, r.image_count,
                r.success, r.error,
            ])
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
