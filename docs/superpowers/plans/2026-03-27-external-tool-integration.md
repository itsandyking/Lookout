# External Tool Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Playwright scraper + BeautifulSoup extractor with self-hosted Firecrawl, then add GMC compliance rules to content generation and feed export.

**Architecture:** Self-hosted Firecrawl (Docker Desktop on Mac) provides scraping and structured extraction via `AsyncFirecrawl`. The pipeline calls Firecrawl's extract endpoint with a JSON schema matching `ExtractedFacts`, bypassing the current scraper and extractor. GMC rules are pure Python functions extracted from Google Shoptimizer (Apache-2.0) that validate generated content and harden the Google Shopping export.

**Tech Stack:** firecrawl-py (async SDK), Docker Compose, Pydantic JSON Schema, pytest

**Spec:** `docs/superpowers/specs/2026-03-27-external-tool-integration-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `infra/firecrawl/docker-compose.yml` | Create | Firecrawl self-hosted stack (5 services) |
| `infra/firecrawl/.env.example` | Create | Environment config template |
| `lookout/enrich/firecrawl_scraper.py` | Create | AsyncFirecrawl client, schema definition, ExtractedFacts mapping |
| `lookout/enrich/gmc_rules.py` | Create | GMC compliance validators (title, GTIN, color map, prohibited terms) |
| `lookout/enrich/models.py` | Modify | Add `gmc_flags` field to MerchOutput |
| `lookout/enrich/pipeline.py` | Modify | Swap WebScraper for FirecrawlScraper |
| `lookout/enrich/prompts/generate_body_html.prompt` | Modify | Add GMC-aware generation instructions |
| `lookout/output/google_shopping.py` | Modify | Add GTIN validation, export-only color mapping, attribute checks |
| `lookout/cli.py` | Modify | Add `lookout infra` commands |
| `vendors.yaml` | Modify | Remove Playwright configs |
| `pyproject.toml` | Modify | Add firecrawl-py, remove playwright |
| `tests/test_firecrawl_scraper.py` | Create | Unit tests for FirecrawlScraper |
| `tests/test_gmc_rules.py` | Create | Unit tests for GMC rules |
| `tests/firecrawl_validation.py` | Create | Integration test harness (13 vendors × 3 modes) |

---

## Phase 1: Firecrawl Infrastructure & Validation

### Task 1: Docker Compose Setup

**Files:**
- Create: `infra/firecrawl/docker-compose.yml`
- Create: `infra/firecrawl/.env.example`

- [ ] **Step 1: Create docker-compose.yml**

```yaml
# infra/firecrawl/docker-compose.yml
services:
  playwright-service:
    image: ghcr.io/firecrawl/playwright-service:latest
    environment:
      - PORT=3000
      - PROXY_SERVER=${PROXY_SERVER:-}
      - PROXY_USERNAME=${PROXY_USERNAME:-}
      - PROXY_PASSWORD=${PROXY_PASSWORD:-}
      - BLOCK_MEDIA=${BLOCK_MEDIA:-false}
    networks:
      - backend
    deploy:
      resources:
        limits:
          cpus: "2"
          memory: 4096M

  api:
    image: ghcr.io/firecrawl/firecrawl:latest
    environment:
      - REDIS_URL=redis://redis:6379
      - REDIS_RATE_LIMIT_URL=redis://redis:6379
      - PLAYWRIGHT_MICROSERVICE_URL=http://playwright-service:3000
      - USE_DB_AUTHENTICATION=false
      - NUM_WORKERS_PER_QUEUE=${NUM_WORKERS:-2}
      - SCRAPING_BEE_API_KEY=${SCRAPING_BEE_API_KEY:-}
      - HOST=0.0.0.0
      - PORT=3002
      - BULL_AUTH_KEY=${BULL_AUTH_KEY:-lookout-local}
    ports:
      - "${FIRECRAWL_PORT:-3002}:3002"
    depends_on:
      - redis
      - playwright-service
    networks:
      - backend
    command: ["pnpm", "run", "start:workers-and-api"]

  redis:
    image: redis:alpine
    networks:
      - backend

networks:
  backend:
    driver: bridge
```

- [ ] **Step 2: Create .env.example**

```bash
# infra/firecrawl/.env.example
# Firecrawl self-hosted configuration

# API port (default 3002)
FIRECRAWL_PORT=3002

# Worker count (reduce for constrained environments)
NUM_WORKERS=2

# Optional proxy (leave empty for direct connections)
PROXY_SERVER=
PROXY_USERNAME=
PROXY_PASSWORD=

# Block media for faster scrapes (set true to skip images/video loading)
BLOCK_MEDIA=false

# Bull dashboard auth
BULL_AUTH_KEY=lookout-local
```

- [ ] **Step 3: Start Firecrawl and verify it's running**

Run:
```bash
cd infra/firecrawl && docker compose up -d
```

Wait 30 seconds for services to start, then:
```bash
curl -s http://localhost:3002/ | head -20
```
Expected: JSON response or health check from the Firecrawl API.

- [ ] **Step 4: Commit**

```bash
git add infra/firecrawl/docker-compose.yml infra/firecrawl/.env.example
git commit --no-gpg-sign -m "infra: add self-hosted Firecrawl docker-compose"
```

---

### Task 2: CLI Infra Commands

**Files:**
- Modify: `lookout/cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_infra.py`:

```python
"""Tests for lookout infra CLI commands."""

from unittest.mock import patch

from click.testing import CliRunner

from lookout.cli import cli


class TestInfraCommands:
    def test_infra_group_exists(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["infra", "--help"])
        assert result.exit_code == 0
        assert "up" in result.output
        assert "down" in result.output

    def test_infra_up_calls_docker_compose(self):
        runner = CliRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = runner.invoke(cli, ["infra", "up"])
            assert result.exit_code == 0
            args = mock_run.call_args[0][0]
            assert "docker" in args
            assert "compose" in args
            assert "up" in args

    def test_infra_down_calls_docker_compose(self):
        runner = CliRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = runner.invoke(cli, ["infra", "down"])
            assert result.exit_code == 0
            args = mock_run.call_args[0][0]
            assert "down" in args
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_infra.py -v`
Expected: FAIL — `infra` command not found

- [ ] **Step 3: Implement infra commands**

Add to `lookout/cli.py` after the existing imports:

```python
import subprocess
```

Add before the `# audit` section (around line 52):

```python
# ---------------------------------------------------------------------------
# infra
# ---------------------------------------------------------------------------

FIRECRAWL_COMPOSE_DIR = Path(__file__).parent.parent / "infra" / "firecrawl"


@cli.group()
def infra():
    """Manage infrastructure services (Firecrawl, etc.)."""


@infra.command()
def up():
    """Start Firecrawl services via Docker Compose."""
    compose_file = FIRECRAWL_COMPOSE_DIR / "docker-compose.yml"
    if not compose_file.exists():
        console.print("[red]docker-compose.yml not found at {compose_file}[/red]")
        raise SystemExit(1)
    console.print("[bold]Starting Firecrawl...[/bold]")
    result = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=FIRECRAWL_COMPOSE_DIR,
    )
    if result.returncode == 0:
        console.print("[green]Firecrawl running at http://localhost:3002[/green]")
    raise SystemExit(result.returncode)


@infra.command()
def down():
    """Stop Firecrawl services."""
    console.print("[bold]Stopping Firecrawl...[/bold]")
    result = subprocess.run(
        ["docker", "compose", "down"],
        cwd=FIRECRAWL_COMPOSE_DIR,
    )
    raise SystemExit(result.returncode)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_infra.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add lookout/cli.py tests/test_cli_infra.py
git commit --no-gpg-sign -m "feat: add lookout infra up/down CLI commands"
```

---

### Task 3: FirecrawlScraper Module

**Files:**
- Create: `lookout/enrich/firecrawl_scraper.py`
- Create: `tests/test_firecrawl_scraper.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add firecrawl-py dependency**

In `pyproject.toml`, add `firecrawl-py>=1.0` to the `enrich` optional dependencies:

```toml
[project.optional-dependencies]
enrich = [
    "httpx>=0.27",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
    "playwright>=1.40",
    "anthropic>=0.30",
    "tenacity>=8.0",
    "firecrawl-py>=1.0",
]
```

Run: `pip install firecrawl-py`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_firecrawl_scraper.py`:

```python
"""Tests for FirecrawlScraper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lookout.enrich.firecrawl_scraper import (
    EXTRACTION_SCHEMA,
    FirecrawlScraper,
    _firecrawl_json_to_facts,
)
from lookout.enrich.models import ExtractedFacts, ImageInfo


class TestExtractionSchema:
    def test_schema_has_required_fields(self):
        props = EXTRACTION_SCHEMA["properties"]
        assert "product_name" in props
        assert "description_blocks" in props
        assert "feature_bullets" in props
        assert "specs" in props
        assert "images" in props
        assert "brand" in props

    def test_schema_is_valid_json_schema(self):
        assert EXTRACTION_SCHEMA["type"] == "object"
        for prop in EXTRACTION_SCHEMA["properties"].values():
            assert "type" in prop


class TestFirecrawlJsonToFacts:
    def test_maps_basic_fields(self):
        data = {
            "product_name": "Alpine Jacket",
            "brand": "Patagonia",
            "description_blocks": ["A warm jacket for cold days."],
            "feature_bullets": ["Waterproof", "Breathable"],
            "specs": {"Weight": "400g", "Material": "Gore-Tex"},
            "images": ["https://example.com/img1.jpg", "https://example.com/img2.jpg"],
            "colors": ["Blue", "Red"],
            "materials": "Gore-Tex Pro",
            "price": "$299",
        }
        facts = _firecrawl_json_to_facts(data, "https://patagonia.com/product/alpine-jacket")

        assert isinstance(facts, ExtractedFacts)
        assert facts.product_name == "Alpine Jacket"
        assert facts.brand == "Patagonia"
        assert facts.description_blocks == ["A warm jacket for cold days."]
        assert facts.feature_bullets == ["Waterproof", "Breathable"]
        assert facts.specs == {"Weight": "400g", "Material": "Gore-Tex"}
        assert len(facts.images) == 2
        assert facts.images[0].url == "https://example.com/img1.jpg"
        assert facts.materials == "Gore-Tex Pro"
        assert facts.canonical_url == "https://patagonia.com/product/alpine-jacket"

    def test_handles_empty_data(self):
        facts = _firecrawl_json_to_facts({}, "https://example.com")
        assert facts.product_name == ""
        assert facts.images == []
        assert facts.specs == {}

    def test_handles_missing_fields(self):
        data = {"product_name": "Test Product"}
        facts = _firecrawl_json_to_facts(data, "https://example.com")
        assert facts.product_name == "Test Product"
        assert facts.description_blocks == []
        assert facts.feature_bullets == []


class TestFirecrawlScraper:
    @pytest.mark.asyncio
    async def test_extract_calls_firecrawl_with_schema(self):
        mock_client = AsyncMock()
        mock_doc = MagicMock()
        mock_doc.json = {
            "product_name": "Test Product",
            "brand": "TestBrand",
            "description_blocks": ["A test product."],
            "feature_bullets": [],
            "specs": {},
            "images": [],
            "colors": [],
            "materials": "",
            "price": "",
        }
        mock_doc.metadata = {"sourceURL": "https://example.com/product"}
        mock_client.scrape.return_value = mock_doc

        scraper = FirecrawlScraper(client=mock_client)
        facts = await scraper.extract("https://example.com/product")

        assert isinstance(facts, ExtractedFacts)
        assert facts.product_name == "Test Product"
        mock_client.scrape.assert_called_once()

    @pytest.mark.asyncio
    async def test_extract_returns_none_on_failure(self):
        mock_client = AsyncMock()
        mock_client.scrape.side_effect = Exception("Connection refused")

        scraper = FirecrawlScraper(client=mock_client)
        result = await scraper.extract("https://example.com/product")

        assert result is None

    @pytest.mark.asyncio
    async def test_scrape_html_returns_scraped_page(self):
        mock_client = AsyncMock()
        mock_doc = MagicMock()
        mock_doc.html = "<html><body><h1>Product</h1></body></html>"
        mock_doc.metadata = {"sourceURL": "https://example.com/product", "title": "Product"}
        mock_client.scrape.return_value = mock_doc

        scraper = FirecrawlScraper(client=mock_client)
        page = await scraper.scrape_html("https://example.com/product")

        assert page.html == "<html><body><h1>Product</h1></body></html>"
        assert page.success
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_firecrawl_scraper.py -v`
Expected: FAIL — module `firecrawl_scraper` not found

- [ ] **Step 4: Implement FirecrawlScraper**

Create `lookout/enrich/firecrawl_scraper.py`:

```python
"""Firecrawl-based scraper for vendor product pages.

Replaces the Playwright WebScraper with self-hosted Firecrawl.
Supports three modes:
  - extract: structured JSON extraction (returns ExtractedFacts directly)
  - html: raw HTML (returns ScrapedPage for extractor compatibility)
  - markdown: clean markdown output
"""

import asyncio
import logging
import random

from firecrawl import AsyncFirecrawl
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import ExtractedFacts, ImageInfo
from .scraper import ScrapedPage

logger = logging.getLogger(__name__)

# JSON Schema for Firecrawl structured extraction.
# Maps to ExtractedFacts fields.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string", "description": "The product name/title"},
        "brand": {"type": "string", "description": "The brand or manufacturer"},
        "description_blocks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Product description paragraphs",
        },
        "feature_bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key feature bullet points",
        },
        "specs": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Product specifications as key-value pairs (e.g. Weight: 400g)",
        },
        "images": {
            "type": "array",
            "items": {"type": "string", "format": "uri"},
            "description": "All product image URLs (full size, not thumbnails)",
        },
        "colors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Available color options",
        },
        "materials": {
            "type": "string",
            "description": "Materials/fabric composition",
        },
        "price": {
            "type": "string",
            "description": "Product price as displayed",
        },
    },
}

EXTRACTION_PROMPT = (
    "Extract all product information from this page. "
    "Include every image URL you can find for the product (not icons or logos). "
    "For specs, include materials, weight, dimensions, ratings, and certifications."
)


def _firecrawl_json_to_facts(data: dict, url: str) -> ExtractedFacts:
    """Convert Firecrawl structured extraction output to ExtractedFacts."""
    images = []
    for img_url in data.get("images", []):
        if isinstance(img_url, str) and img_url.startswith("http"):
            images.append(ImageInfo(url=img_url, source_hint="firecrawl"))

    return ExtractedFacts(
        canonical_url=url,
        product_name=data.get("product_name", ""),
        brand=data.get("brand", ""),
        description_blocks=data.get("description_blocks", []),
        feature_bullets=data.get("feature_bullets", []),
        specs=data.get("specs", {}),
        materials=data.get("materials", ""),
        images=images,
        variant_image_candidates={},
        json_ld_data=None,
        evidence_snippets={},
        extraction_warnings=[],
    )


class FirecrawlScraper:
    """Scraper that delegates to a self-hosted Firecrawl instance."""

    def __init__(
        self,
        base_url: str = "http://localhost:3002",
        client: AsyncFirecrawl | None = None,
        min_delay_ms: int = 500,
        max_delay_ms: int = 2000,
    ) -> None:
        self._client = client or AsyncFirecrawl(api_url=base_url)
        self._min_delay = min_delay_ms / 1000
        self._max_delay = max_delay_ms / 1000

    async def _polite_delay(self) -> None:
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def extract(self, url: str) -> ExtractedFacts | None:
        """Structured extraction — returns ExtractedFacts directly.

        This is the primary mode. Firecrawl scrapes the page, uses an LLM
        to extract structured data matching EXTRACTION_SCHEMA, and we map
        the result to ExtractedFacts.
        """
        await self._polite_delay()
        try:
            doc = await self._client.scrape(
                url,
                formats=[
                    {
                        "type": "json",
                        "schema": EXTRACTION_SCHEMA,
                        "prompt": EXTRACTION_PROMPT,
                    }
                ],
            )
            if not doc.json:
                logger.warning("Firecrawl returned no JSON for %s", url)
                return None

            final_url = url
            if doc.metadata and doc.metadata.get("sourceURL"):
                final_url = doc.metadata["sourceURL"]

            return _firecrawl_json_to_facts(doc.json, final_url)

        except Exception:
            logger.exception("Firecrawl extract failed for %s", url)
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def scrape_html(self, url: str) -> ScrapedPage:
        """HTML mode — returns ScrapedPage for extractor compatibility."""
        await self._polite_delay()
        try:
            doc = await self._client.scrape(url, formats=["html"])
            final_url = url
            if doc.metadata and doc.metadata.get("sourceURL"):
                final_url = doc.metadata["sourceURL"]
            return ScrapedPage(
                url=url,
                html=doc.html or "",
                status_code=200,
                final_url=final_url,
            )
        except Exception as e:
            logger.exception("Firecrawl HTML scrape failed for %s", url)
            return ScrapedPage(url=url, html="", status_code=0, error=str(e))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def scrape_markdown(self, url: str) -> str | None:
        """Markdown mode — returns clean markdown text."""
        await self._polite_delay()
        try:
            doc = await self._client.scrape(url, formats=["markdown"])
            return doc.markdown
        except Exception:
            logger.exception("Firecrawl markdown scrape failed for %s", url)
            return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_firecrawl_scraper.py -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add lookout/enrich/firecrawl_scraper.py tests/test_firecrawl_scraper.py pyproject.toml
git commit --no-gpg-sign -m "feat: add FirecrawlScraper with structured extraction support"
```

---

### Task 4: Validation Harness

**Files:**
- Create: `tests/firecrawl_validation.py`

This is a standalone script (not a pytest test) that compares Firecrawl vs the current Playwright pipeline across all 13 vendors. Run it manually after Firecrawl is up.

- [ ] **Step 1: Create the validation harness**

Create `tests/firecrawl_validation.py`:

```python
#!/usr/bin/env python3
"""Compare Firecrawl vs Playwright across all vendors.

Usage:
    lookout infra up
    python tests/firecrawl_validation.py

Requires: Firecrawl running at localhost:3002, test product URLs.
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Lazy imports to avoid requiring all deps at parse time


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


# Known product URLs per vendor (curate from test CSVs or manually)
# Format: {vendor_name: product_url}
VENDOR_TEST_URLS: dict[str, str] = {
    # Populate with one known product URL per vendor.
    # These should be stable URLs unlikely to 404.
    # Example:
    # "Patagonia": "https://www.patagonia.com/product/...",
}


def load_vendor_urls(csv_path: Path | None = None) -> dict[str, str]:
    """Load one product URL per vendor from test CSV or hardcoded map.

    Falls back to VENDOR_TEST_URLS if no CSV provided.
    """
    if csv_path and csv_path.exists():
        urls: dict[str, str] = {}
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                vendor = row.get("Vendor", "")
                handle = row.get("Product Handle", "")
                if vendor and handle and vendor not in urls:
                    # We need to resolve handles to vendor URLs.
                    # For validation, we'll need to populate VENDOR_TEST_URLS
                    # with actual vendor product page URLs.
                    pass
        return urls or VENDOR_TEST_URLS
    return VENDOR_TEST_URLS


async def test_firecrawl_extract(url: str, vendor: str) -> ScrapeResult:
    """Test Firecrawl structured extraction mode."""
    from lookout.enrich.firecrawl_scraper import FirecrawlScraper

    start = time.time()
    scraper = FirecrawlScraper()
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
    """Test Firecrawl HTML mode."""
    from lookout.enrich.firecrawl_scraper import FirecrawlScraper

    start = time.time()
    scraper = FirecrawlScraper()
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
    """Test Firecrawl markdown mode."""
    from lookout.enrich.firecrawl_scraper import FirecrawlScraper

    start = time.time()
    scraper = FirecrawlScraper()
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
    """Test current Playwright pipeline as baseline."""
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
    # Load vendors config
    vendors_path = Path(__file__).parent.parent / "vendors.yaml"
    with open(vendors_path) as f:
        config = yaml.safe_load(f)
    vendors_config = config.get("vendors", {})

    urls = load_vendor_urls()
    if not urls:
        print("ERROR: No test URLs configured. Populate VENDOR_TEST_URLS in this file.")
        print("Add one known product page URL per vendor.")
        return

    results: list[ScrapeResult] = []

    for vendor, url in urls.items():
        print(f"\n{'='*60}")
        print(f"Vendor: {vendor}")
        print(f"URL: {url}")
        print(f"{'='*60}")

        # Run all 4 modes
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

    # Print summary
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

    # Save to CSV
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
```

- [ ] **Step 2: Populate VENDOR_TEST_URLS with one product page per vendor**

Find one stable product URL per vendor from the test CSV data or by manual lookup. Update the `VENDOR_TEST_URLS` dict.

- [ ] **Step 3: Run the validation harness**

Run:
```bash
python tests/firecrawl_validation.py
```

Review the summary table and CSV output. The decision gate:
- Structured extraction succeeds on 11+/13 vendors with product name + description + images → proceed with extract mode
- Otherwise → assess which mode works best and adjust

- [ ] **Step 4: Commit**

```bash
git add tests/firecrawl_validation.py
git commit --no-gpg-sign -m "test: add Firecrawl vs Playwright validation harness"
```

---

## Phase 2: Pipeline Integration

### Task 5: Wire FirecrawlScraper into Pipeline

**Files:**
- Modify: `lookout/enrich/pipeline.py:240-248` (ProductProcessor.__init__)
- Modify: `lookout/enrich/pipeline.py:419-456` (scrape + extract steps)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_firecrawl_scraper.py`:

```python
class TestPipelineIntegration:
    """Test that FirecrawlScraper can be used as a drop-in for WebScraper in the pipeline."""

    @pytest.mark.asyncio
    async def test_extract_produces_valid_extracted_facts(self):
        """Verify that extract() output is compatible with Generator input."""
        from lookout.enrich.models import ExtractedFacts

        mock_client = AsyncMock()
        mock_doc = MagicMock()
        mock_doc.json = {
            "product_name": "Alpine Pro Jacket",
            "brand": "Patagonia",
            "description_blocks": ["A versatile alpine jacket."],
            "feature_bullets": ["Waterproof", "Breathable", "Packable"],
            "specs": {"Weight": "340g", "Material": "Gore-Tex"},
            "images": ["https://cdn.example.com/jacket-front.jpg"],
            "colors": ["Blue", "Black"],
            "materials": "Gore-Tex Pro 3L",
            "price": "$399",
        }
        mock_doc.metadata = {"sourceURL": "https://patagonia.com/product/alpine-pro"}
        mock_client.scrape.return_value = mock_doc

        scraper = FirecrawlScraper(client=mock_client)
        facts = await scraper.extract("https://patagonia.com/product/alpine-pro")

        # Verify all fields the Generator expects are present
        assert isinstance(facts, ExtractedFacts)
        assert facts.canonical_url == "https://patagonia.com/product/alpine-pro"
        assert facts.product_name == "Alpine Pro Jacket"
        assert len(facts.description_blocks) == 1
        assert len(facts.feature_bullets) == 3
        assert len(facts.specs) == 2
        assert len(facts.images) == 1
        assert facts.images[0].url == "https://cdn.example.com/jacket-front.jpg"
        assert facts.materials == "Gore-Tex Pro 3L"
```

- [ ] **Step 2: Run test to verify it passes** (it should, since we already implemented the module)

Run: `pytest tests/test_firecrawl_scraper.py::TestPipelineIntegration -v`
Expected: PASS

- [ ] **Step 3: Modify ProductProcessor to use FirecrawlScraper**

In `lookout/enrich/pipeline.py`, update the import section (top of file):

```python
from .firecrawl_scraper import FirecrawlScraper
```

Update `ProductProcessor.__init__` (around line 247-248). Replace:

```python
        self.resolver = URLResolver(http_client=http_client)
        self.scraper = WebScraper(http_client=http_client)
        self.generator = Generator(llm_client=llm_client)
```

With:

```python
        self.resolver = URLResolver(http_client=http_client)
        self.firecrawl = FirecrawlScraper()
        self.generator = Generator(llm_client=llm_client)
```

Update the scrape + extract steps (around lines 419-456). Replace the Step 2 (scrape) and Step 3 (extract) blocks:

```python
            # Step 2+3: Scrape and extract via Firecrawl (structured extraction)
            handle_log.entries.append(
                LogEntry(
                    message=f"Extracting via Firecrawl: {resolver_output.selected_url}",
                )
            )

            facts = await self.firecrawl.extract(resolver_output.selected_url)

            if facts is None:
                handle_log.entries.append(
                    LogEntry(
                        level="ERROR",
                        message="Firecrawl extraction returned no data",
                    )
                )
                metadata["error"] = "Firecrawl extraction failed"
                return None, ProcessingStatus.FAILED, metadata

            # Save extraction outputs
            facts_path = artifacts_dir / "extracted_facts.json"
            facts_path.parent.mkdir(parents=True, exist_ok=True)
            facts_path.write_text(facts.model_dump_json(indent=2))
```

Remove the lines that reference `self.scraper`, `scraped_page`, `extract_content()`, and `extractor.save_outputs()`. Keep everything from Step 3b (quality check) onward unchanged — it already works with `ExtractedFacts`.

- [ ] **Step 4: Run existing tests to check nothing is broken**

Run: `pytest tests/ -v --timeout=30`
Expected: All existing tests pass (some may need mock adjustments if they instantiate ProductProcessor directly)

- [ ] **Step 5: Commit**

```bash
git add lookout/enrich/pipeline.py
git commit --no-gpg-sign -m "feat: swap WebScraper for FirecrawlScraper in enrichment pipeline"
```

---

### Task 6: Simplify Vendor Config

**Files:**
- Modify: `vendors.yaml`
- Modify: `lookout/enrich/models.py:152-189` (remove PlaywrightConfig, simplify VendorConfig)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (or create if it doesn't exist):

```python
def test_vendor_config_no_playwright_fields():
    """VendorConfig should not require Playwright-specific fields."""
    from lookout.enrich.models import VendorConfig

    config = VendorConfig(
        domain="patagonia.com",
        blocked_paths=["/blog", "/support"],
        product_url_patterns=["/product/"],
        search={"method": "site_search", "query_template": "site:{domain} {query}"},
    )
    assert config.domain == "patagonia.com"
    assert not hasattr(config, "use_playwright") or not config.use_playwright
```

- [ ] **Step 2: Run test — it will pass since use_playwright defaults to False**

Run: `pytest tests/test_config.py::test_vendor_config_no_playwright_fields -v`

- [ ] **Step 3: Remove Playwright fields from VendorConfig**

In `lookout/enrich/models.py`, update VendorConfig (lines 179-189). Remove `use_playwright`, `playwright_config`, and `selectors` fields:

```python
class VendorConfig(BaseModel):
    domain: str
    blocked_paths: list[str] = Field(default_factory=list)
    product_url_patterns: list[str] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)
```

Keep `PlaywrightConfig` and `SelectorsConfig` classes in the file for now (they may be referenced elsewhere). They can be removed in a cleanup pass.

- [ ] **Step 4: Strip Playwright configs from vendors.yaml**

Remove `use_playwright`, `playwright_config`, and `selectors` from every vendor entry. Each entry should look like:

```yaml
  Patagonia:
    domain: "patagonia.com"
    blocked_paths:
      - "/blog"
      - "/support"
      - "/worn-wear"
      - "/activism"
      - "/stories"
      - "/about"
      - "/help"
      - "/account"
      - "/cart"
      - "/search"
    product_url_patterns:
      - "/product/"
      - "/p/"
    search:
      method: "site_search"
      query_template: "site:{domain} {query}"
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/ -v --timeout=30`
Expected: PASS (fix any tests that reference removed fields)

- [ ] **Step 6: Commit**

```bash
git add vendors.yaml lookout/enrich/models.py
git commit --no-gpg-sign -m "refactor: remove Playwright configs from vendor config"
```

---

### Task 7: Update Dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Remove playwright, add firecrawl-py (if not already done in Task 3)**

In `pyproject.toml`, update the `enrich` optional dependencies:

```toml
[project.optional-dependencies]
enrich = [
    "httpx>=0.27",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
    "anthropic>=0.30",
    "tenacity>=8.0",
    "firecrawl-py>=1.0",
]
```

Note: `playwright>=1.40` is removed. `beautifulsoup4` and `lxml` are kept — they may be used elsewhere (e.g., `google_shopping.py` uses `HTMLParser`, and `extractor.py` is still in the codebase until full cleanup).

- [ ] **Step 2: Install updated deps**

Run: `pip install -e ".[enrich]"`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit --no-gpg-sign -m "deps: replace playwright with firecrawl-py"
```

---

## Phase 3: GMC Rule Integration

### Task 8: GMC Rules Module

**Files:**
- Create: `lookout/enrich/gmc_rules.py`
- Create: `tests/test_gmc_rules.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gmc_rules.py`:

```python
"""Tests for GMC compliance rules."""

import pytest

from lookout.enrich.gmc_rules import (
    GMC_COLOR_MAP,
    check_prohibited_terms,
    check_required_attributes,
    map_color_for_gmc,
    structure_title,
    validate_gtin,
    validate_title,
)


class TestValidateGtin:
    def test_valid_upc_12(self):
        assert validate_gtin("012345678905") is True

    def test_valid_ean_13(self):
        assert validate_gtin("4006381333931") is True

    def test_invalid_check_digit(self):
        assert validate_gtin("012345678900") is False

    def test_wrong_length(self):
        assert validate_gtin("12345") is False

    def test_non_numeric(self):
        assert validate_gtin("ABCDEFGHIJKL") is False

    def test_empty_string(self):
        assert validate_gtin("") is False

    def test_valid_ean_8(self):
        assert validate_gtin("96385074") is True

    def test_valid_gtin_14(self):
        assert validate_gtin("00012345678905") is True


class TestValidateTitle:
    def test_valid_title(self):
        violations = validate_title("Patagonia Nano Puff Jacket - Blue")
        assert violations == []

    def test_title_too_long(self):
        long_title = "A" * 151
        violations = validate_title(long_title)
        assert any("150" in v for v in violations)

    def test_empty_title(self):
        violations = validate_title("")
        assert any("empty" in v.lower() for v in violations)

    def test_all_caps(self):
        violations = validate_title("PATAGONIA NANO PUFF JACKET")
        assert any("caps" in v.lower() for v in violations)


class TestCheckProhibitedTerms:
    def test_clean_text(self):
        result = check_prohibited_terms("A warm jacket for cold weather hiking.")
        assert result == []

    def test_promotional_language(self):
        result = check_prohibited_terms("The best jacket ever! Free shipping included.")
        assert len(result) > 0

    def test_superlatives(self):
        result = check_prohibited_terms("This incredible, amazing premium jacket.")
        assert len(result) > 0

    def test_price_mention(self):
        result = check_prohibited_terms("Only $99.99 while supplies last!")
        assert len(result) > 0


class TestMapColorForGmc:
    def test_known_mapping(self):
        # Whatever is in GMC_COLOR_MAP should map correctly
        if "Midnight" in GMC_COLOR_MAP:
            result = map_color_for_gmc("Midnight")
            assert result == GMC_COLOR_MAP["Midnight"]

    def test_unmapped_color_passes_through(self):
        result = map_color_for_gmc("Blue")
        assert result == "Blue"

    def test_case_insensitive_lookup(self):
        if "midnight" in {k.lower() for k in GMC_COLOR_MAP}:
            result = map_color_for_gmc("midnight")
            assert result != ""

    def test_empty_string(self):
        result = map_color_for_gmc("")
        assert result == ""


class TestStructureTitle:
    def test_basic_structure(self):
        title = structure_title(
            brand="Patagonia",
            product_type="Jacket",
            attributes={"color": "Blue", "gender": "Men's"},
        )
        assert "Patagonia" in title
        assert "Jacket" in title
        assert len(title) <= 150

    def test_truncation(self):
        title = structure_title(
            brand="Patagonia",
            product_type="Ultra-Lightweight Down Insulated Waterproof Jacket",
            attributes={"color": "Midnight Navy Blue", "size": "Extra Large Tall"},
        )
        assert len(title) <= 150


class TestCheckRequiredAttributes:
    def test_complete_product(self):
        product = {
            "title": "Patagonia Jacket",
            "body_html": "A warm jacket.",
            "image": "https://example.com/img.jpg",
            "price": "299.00",
            "barcode": "012345678905",
        }
        missing = check_required_attributes(product)
        assert missing == []

    def test_missing_fields(self):
        product = {"title": "Jacket"}
        missing = check_required_attributes(product)
        assert len(missing) > 0
        assert any("image" in m.lower() for m in missing)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gmc_rules.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement gmc_rules.py**

Create `lookout/enrich/gmc_rules.py`:

```python
"""Google Merchant Center compliance rules.

Rule logic extracted from Google Shoptimizer (Apache-2.0) and adapted
for Lookout's data models. Pure Python, no external dependencies.

CRITICAL: Color mapping is EXPORT-ONLY. Internal color names are never
modified anywhere in the system. map_color_for_gmc() is called only
by lookout/output/google_shopping.py when writing the GMC color attribute.
"""

from __future__ import annotations

import re

# ── GTIN Validation ──────────────────────────────────────────────────

VALID_GTIN_LENGTHS = (8, 12, 13, 14)


def validate_gtin(gtin: str) -> bool:
    """Validate a GTIN (UPC/EAN/JAN) check digit.

    Supports 8, 12, 13, and 14 digit GTINs.
    Returns True if valid, False if invalid or malformed.
    """
    if not gtin or not gtin.isdigit():
        return False
    if len(gtin) not in VALID_GTIN_LENGTHS:
        return False

    digits = [int(d) for d in gtin]
    check = digits[-1]
    payload = digits[:-1]

    # Alternate multiply by 3 and 1, starting from rightmost payload digit
    total = 0
    for i, d in enumerate(reversed(payload)):
        total += d * (3 if i % 2 == 0 else 1)

    expected = (10 - (total % 10)) % 10
    return check == expected


# ── Title Validation ─────────────────────────────────────────────────

MAX_TITLE_LENGTH = 150


def validate_title(title: str) -> list[str]:
    """Check a product title for GMC violations.

    Returns a list of violation descriptions (empty if compliant).
    """
    violations = []

    if not title or not title.strip():
        violations.append("Title is empty")
        return violations

    if len(title) > MAX_TITLE_LENGTH:
        violations.append(f"Title exceeds {MAX_TITLE_LENGTH} characters ({len(title)})")

    if title == title.upper() and len(title) > 10:
        violations.append("Title is all caps (GMC may flag as spammy)")

    if re.search(r"[!]{2,}|[?]{2,}", title):
        violations.append("Title contains excessive punctuation")

    return violations


def structure_title(
    brand: str,
    product_type: str,
    attributes: dict[str, str] | None = None,
) -> str:
    """Build a GMC-optimal title from components.

    Format: [Brand] [Product Type] [Key Attributes]
    Adapted from Google FeedGen title structuring patterns.
    """
    parts = []
    if brand:
        parts.append(brand)
    if product_type:
        parts.append(product_type)

    if attributes:
        for key in ("gender", "color", "size"):
            val = attributes.get(key, "")
            if val:
                parts.append(f"- {val}")

    title = " ".join(parts)

    # Truncate to max length at word boundary
    if len(title) > MAX_TITLE_LENGTH:
        title = title[: MAX_TITLE_LENGTH - 3].rsplit(" ", 1)[0] + "..."

    return title


# ── Prohibited Terms ─────────────────────────────────────────────────

# Patterns that GMC flags or disapproves.
# Sourced from Shoptimizer's adult_optimizer and title optimizers.
PROHIBITED_PATTERNS = [
    (r"\bfree shipping\b", "Promotional: 'free shipping'"),
    (r"\bbuy now\b", "Promotional: 'buy now'"),
    (r"\bon sale\b", "Promotional: 'on sale'"),
    (r"\blimited time\b", "Promotional: 'limited time'"),
    (r"\bwhile supplies last\b", "Promotional: 'while supplies last'"),
    (r"\bbest\b", "Superlative: 'best'"),
    (r"\bcheapest\b", "Superlative: 'cheapest'"),
    (r"\b(?:incredible|amazing|unbelievable)\b", "Superlative: exaggerated claim"),
    (r"\b(?:premium|superior|ultimate)\b", "Marketing superlative"),
    (r"\$\d+", "Price mention in description"),
    (r"\bguarantee\b", "Unsubstantiated guarantee claim"),
    (r"\b#\d+\s*(?:rated|selling|choice)\b", "Ranking claim"),
]

_COMPILED_PROHIBITED = [(re.compile(p, re.IGNORECASE), msg) for p, msg in PROHIBITED_PATTERNS]


def check_prohibited_terms(text: str) -> list[str]:
    """Flag promotional language, superlatives, and claims GMC rejects.

    Returns list of violation descriptions (empty if clean).
    """
    violations = []
    for pattern, message in _COMPILED_PROHIBITED:
        if pattern.search(text):
            violations.append(message)
    return violations


# ── Color Mapping (EXPORT-ONLY) ──────────────────────────────────────

# Maps display color names to GMC-recognized values.
# ONLY used in google_shopping.py export. Internal colors stay untouched.
# Sourced from Shoptimizer color data + common outdoor brand color names.
GMC_COLOR_MAP: dict[str, str] = {
    # Dark/night shades
    "Midnight": "Navy",
    "Midnight Navy": "Navy",
    "Deep Navy": "Navy",
    "Abyss": "Navy",
    "Dark Navy": "Navy",
    # Greens
    "Deep Forest": "Green",
    "Forest": "Green",
    "Pine": "Green",
    "Sage": "Green",
    "Olive": "Green",
    "Moss": "Green",
    "Hemlock": "Green",
    "Nouveau Green": "Green",
    # Grays
    "Slate": "Gray",
    "Forge Grey": "Gray",
    "Smolder": "Gray",
    "Carbon": "Gray",
    "Graphite": "Gray",
    "Ash": "Gray",
    "Stone": "Gray",
    "Plume Grey": "Gray",
    # Browns/Earth
    "Mocha": "Brown",
    "Espresso": "Brown",
    "Earth": "Brown",
    "Coriander": "Brown",
    "Dark Walnut": "Brown",
    # Reds/Warm
    "Sumac Red": "Red",
    "Barn Red": "Red",
    "Paintbrush Red": "Red",
    "Touring Red": "Red",
    "Coral": "Pink",
    "Quartz Coral": "Pink",
    # Blues
    "Storm Blue": "Blue",
    "Tidepool Blue": "Blue",
    "Anacapa Blue": "Blue",
    "Wavy Blue": "Blue",
    "Lagom Blue": "Blue",
    # Whites/Light
    "Birch White": "White",
    "Natural": "White",
    "Oatmeal": "Beige",
    "Pumice": "Beige",
    # Yellows
    "Shrub": "Yellow",
    "Phosphorus": "Yellow",
    "Mango": "Orange",
    "Pufferfish Gold": "Gold",
}

# Build case-insensitive lookup
_COLOR_MAP_LOWER = {k.lower(): v for k, v in GMC_COLOR_MAP.items()}


def map_color_for_gmc(color: str) -> str:
    """Map a display color name to a GMC-recognized color value.

    EXPORT-ONLY: This function is called only when writing the GMC
    color attribute in google_shopping.py. It never modifies internal
    color names, variant options, or storefront display.

    Unmapped colors pass through unchanged.
    """
    if not color:
        return ""
    return _COLOR_MAP_LOWER.get(color.lower(), color)


# ── Required Attributes ──────────────────────────────────────────────

REQUIRED_FIELDS = {
    "title": "Product title",
    "body_html": "Product description",
    "image": "Product image",
    "price": "Product price",
}


def check_required_attributes(product: dict) -> list[str]:
    """Flag missing required GMC attributes.

    Args:
        product: Dict with keys matching REQUIRED_FIELDS.

    Returns:
        List of missing attribute descriptions.
    """
    missing = []
    for field, label in REQUIRED_FIELDS.items():
        val = product.get(field)
        if not val or (isinstance(val, str) and not val.strip()):
            missing.append(f"Missing required attribute: {label}")
    return missing
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gmc_rules.py -v`
Expected: All PASSED

- [ ] **Step 5: Commit**

```bash
git add lookout/enrich/gmc_rules.py tests/test_gmc_rules.py
git commit --no-gpg-sign -m "feat: add GMC compliance rules (GTIN, title, color map, prohibited terms)"
```

---

### Task 9: Add gmc_flags to MerchOutput

**Files:**
- Modify: `lookout/enrich/models.py:339-353`
- Modify: `lookout/enrich/pipeline.py` (post-generation validation)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gmc_rules.py`:

```python
class TestMerchOutputGmcFlags:
    def test_merch_output_has_gmc_flags_field(self):
        from lookout.enrich.models import MerchOutput

        output = MerchOutput(handle="test-product")
        assert hasattr(output, "gmc_flags")
        assert output.gmc_flags == []

    def test_gmc_flags_stores_violations(self):
        from lookout.enrich.models import MerchOutput

        output = MerchOutput(
            handle="test-product",
            gmc_flags=["Title exceeds 150 characters", "Superlative: 'best'"],
        )
        assert len(output.gmc_flags) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gmc_rules.py::TestMerchOutputGmcFlags -v`
Expected: FAIL — `gmc_flags` not in MerchOutput

- [ ] **Step 3: Add gmc_flags to MerchOutput**

In `lookout/enrich/models.py`, update MerchOutput (line 339-353):

```python
class MerchOutput(BaseModel):
    """
    Final merchandising output for a product.

    This is what gets written to merch_output.json and used
    to generate the Shopify CSV.
    """

    handle: str
    body_html: str | None = None
    images: list[OutputImage] = Field(default_factory=list)
    variant_image_map: dict[str, str | list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    gmc_flags: list[str] = Field(default_factory=list)
    confidence: int = Field(ge=0, le=100, default=0)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gmc_rules.py::TestMerchOutputGmcFlags -v`
Expected: PASS

- [ ] **Step 5: Add post-generation GMC validation to pipeline**

In `lookout/enrich/pipeline.py`, add import at top:

```python
from .gmc_rules import check_prohibited_terms, validate_title
```

After the generation step (around line 572, after `merch_output = await self.generator.generate_output(input_row, facts)`), add:

```python
            # Step 4a: GMC compliance check
            gmc_flags = []
            if merch_output.body_html:
                gmc_flags.extend(check_prohibited_terms(merch_output.body_html))
            if input_row.title:
                gmc_flags.extend(validate_title(input_row.title))
            if gmc_flags:
                merch_output.gmc_flags = gmc_flags
                handle_log.entries.append(
                    LogEntry(
                        level="WARNING",
                        message=f"GMC flags: {', '.join(gmc_flags[:3])}",
                        data={"gmc_flags": gmc_flags},
                    )
                )
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/ -v --timeout=30`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add lookout/enrich/models.py lookout/enrich/pipeline.py
git commit --no-gpg-sign -m "feat: add GMC compliance flags to enrichment pipeline"
```

---

### Task 10: Update Generator Prompt with GMC Patterns

**Files:**
- Modify: `lookout/enrich/prompts/generate_body_html.prompt`

- [ ] **Step 1: Update the prompt**

Add GMC-aware instructions to `generate_body_html.prompt`. Insert after the existing rule 4 ("Be selective with specs"):

```
5. **GMC compliance.** Avoid promotional language (free shipping, buy now, on sale, limited time). No superlatives (best, cheapest, incredible, amazing, premium, ultimate). No price mentions. No unsubstantiated claims or guarantees. Google Merchant Center will disapprove listings with this language.

6. **Title-aware structure.** The opening prose should complement the product title, not repeat it. Lead with the product's primary benefit or use case, not a restatement of the product name.
```

- [ ] **Step 2: Verify the prompt file is valid**

Run: `cat lookout/enrich/prompts/generate_body_html.prompt | wc -l`
Expected: ~44 lines (up from ~38)

- [ ] **Step 3: Commit**

```bash
git add lookout/enrich/prompts/generate_body_html.prompt
git commit --no-gpg-sign -m "feat: add GMC compliance rules to generator prompt"
```

---

### Task 11: Harden Google Shopping Export

**Files:**
- Modify: `lookout/output/google_shopping.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_google_shopping_gmc.py`:

```python
"""Tests for GMC hardening in Google Shopping export."""

import pytest


class TestGtinValidationInExport:
    def test_valid_barcode_included(self):
        from lookout.enrich.gmc_rules import validate_gtin

        assert validate_gtin("012345678905") is True

    def test_invalid_barcode_flagged(self):
        from lookout.enrich.gmc_rules import validate_gtin

        assert validate_gtin("000000000000") is False


class TestColorMappingInExport:
    def test_export_maps_color(self):
        from lookout.enrich.gmc_rules import map_color_for_gmc

        assert map_color_for_gmc("Midnight") == "Navy"

    def test_export_passes_through_standard_color(self):
        from lookout.enrich.gmc_rules import map_color_for_gmc

        assert map_color_for_gmc("Blue") == "Blue"

    def test_export_preserves_internal_color():
        """Verify that map_color_for_gmc is a pure function that
        does not modify its input — it returns a new string."""
        from lookout.enrich.gmc_rules import map_color_for_gmc

        internal = "Midnight"
        _ = map_color_for_gmc(internal)
        assert internal == "Midnight"  # unchanged
```

- [ ] **Step 2: Run tests to verify they pass** (these test the rules module, which exists)

Run: `pytest tests/test_google_shopping_gmc.py -v`
Expected: PASS

- [ ] **Step 3: Add GTIN validation and color mapping to google_shopping.py**

In `lookout/output/google_shopping.py`, add import at top:

```python
from lookout.enrich.gmc_rules import map_color_for_gmc, validate_gtin
```

In the `extract_color` function (line 143-158), the function stays unchanged — it extracts the internal color value. The mapping happens at export time.

Find the section in `generate_google_shopping()` where the color metafield is written (around the row-building loop). Where `extract_color()` result is used, wrap it with `map_color_for_gmc()`:

Replace the color assignment pattern (find where `color` is assigned to the row dict):

```python
                color = extract_color(option1_name, option1_value, option2_value)
```

Change the line where color is written to the export row to:

```python
                color_internal = extract_color(option1_name, option1_value, option2_value)
                color_gmc = map_color_for_gmc(color_internal)
```

Use `color_gmc` for the `"Metafield: mm-google-shopping.color [string]"` column.

For GTIN validation, find where barcode is written and add a check:

```python
                barcode = variant.get("barcode", "")
                if barcode and not validate_gtin(barcode):
                    logger.warning("Invalid GTIN for %s: %s", handle, barcode)
```

- [ ] **Step 4: Run all tests**

Run: `pytest tests/ -v --timeout=30`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/output/google_shopping.py tests/test_google_shopping_gmc.py
git commit --no-gpg-sign -m "feat: add GTIN validation and export-only color mapping to Google Shopping export"
```

---

### Task 12: Push to GitHub

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --timeout=60`
Expected: All PASS

- [ ] **Step 2: Push**

```bash
git push origin main
```

---

## Self-Review Checklist

- **Spec coverage:** All 3 phases covered. Firecrawl infrastructure (Task 1), CLI (Task 2), scraper module (Task 3), validation (Task 4), pipeline integration (Task 5), vendor config cleanup (Task 6), dependency update (Task 7), GMC rules (Task 8), MerchOutput flags (Task 9), prompt update (Task 10), export hardening (Task 11), push (Task 12).
- **Placeholder scan:** No TBDs. `VENDOR_TEST_URLS` dict is intentionally empty — Task 4 Step 2 explicitly says to populate it.
- **Type consistency:** `ExtractedFacts`, `MerchOutput`, `ScrapedPage`, `VendorConfig` — same names and field references throughout. `_firecrawl_json_to_facts` returns `ExtractedFacts` in Task 3, consumed as `ExtractedFacts` in Task 5.
- **Color boundary:** `map_color_for_gmc()` is only called in Task 11 (google_shopping.py export). Never in pipeline, generator, or models.
