# Lookout Remodel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure Lookout as a standalone merchandising command center that consumes TVR via store APIs, with a unified CLI, consolidated modules, and no web UI.

**Architecture:** Single `store.py` wraps TVR's ShopifyStore/VendorStore — all other modules receive dicts, never ORM objects. Unified CLI with `audit`, `enrich`, `rank`, `vendors`, and `output` subcommands. Enrich pipeline internals (extractor, scraper, resolver, models) are preserved; TVR-coupled modules (audit, ranking, output) are rewritten against the store layer.

**Tech Stack:** Python 3.12, Click (CLI), Pydantic (models), httpx/Playwright (scraping), Anthropic SDK (LLM), SQLAlchemy (via TVR only), Rich (terminal output), uv (package management)

**Spec:** `docs/superpowers/specs/2026-03-25-lookout-remodel-design.md`

**Dual-path note:** This plan is executed in two worktrees simultaneously. Worktree A (remodel) refactors existing code. Worktree B (rewrite) builds fresh. Both follow the same task sequence and tests. The comparison happens after both complete.

---

## File Map

### New files
| File | Responsibility |
|------|---------------|
| `lookout/store.py` | TVR interface layer — wraps ShopifyStore + VendorStore, returns dicts |
| `lookout/cli.py` | Unified Click CLI with audit/enrich/rank/vendors/output groups |
| `lookout/audit/models.py` | ProductScore, AuditResult dataclasses |
| `lookout/enrich/generator.py` | Renamed from merchandiser.py — content generation from facts |
| `lookout/enrich/llm.py` | Renamed from llm_client.py — LLM provider abstraction |
| `lookout/ranking/ranker.py` | Renamed from collection_ranker.py — rewritten against store |
| `tests/test_store.py` | Store layer tests (mocked TVR) |
| `tests/test_audit.py` | Audit module tests (mocked store) |
| `tests/test_ranking.py` | Ranking module tests (mocked store) |
| `tests/test_cli.py` | CLI integration tests (Click test runner) |

### Files to keep as-is
| File | Reason |
|------|--------|
| `lookout/enrich/utils/__init__.py` | Re-exports helpers used by pipeline |
| `lookout/enrich/utils/config.py` | load_vendors_config() — used by pipeline and CLI |
| `lookout/enrich/utils/helpers.py` | Utility functions (sanitize_filename, handle_to_query, etc.) |
| `lookout/enrich/extractor.py` | Working, well-tested HTML extractor |
| `lookout/enrich/scraper.py` | Working static + Playwright scraper |
| `lookout/enrich/resolver.py` | Working URL resolver |
| `lookout/enrich/models.py` | Pydantic models for pipeline |
| `lookout/enrich/prompts/*.prompt` | LLM prompt templates (str.format interpolation) |

### Files to modify
| File | Change |
|------|--------|
| `lookout/taxonomy/mappings.py` | Add EXCLUDED_VENDORS, MERCH_WEIGHTS, merchandising config constants |
| `lookout/audit/auditor.py` | Rewrite to use LookoutStore instead of raw SQLAlchemy |
| `lookout/output/matrixify.py` | Rewrite to use LookoutStore; keep exporter, rewrite enricher |
| `lookout/output/alt_text.py` | Rewrite to use LookoutStore |
| `lookout/output/google_shopping.py` | Rewrite to use LookoutStore |
| `lookout/enrich/pipeline.py` | Update imports (generator, llm), add internal audit path |
| `lookout/enrich/models.py` | Keep as-is, ensure CSV schema matches spec |
| `pyproject.toml` | Update entry point to `lookout.cli:main` |

### Files to delete
| File | Reason |
|------|--------|
| `lookout/web/` (entire directory) | Web UI deferred |
| `lookout/enrich_web/` (entire directory) | Web UI deferred |
| `lookout/enrich/cli.py` | Replaced by top-level `lookout/cli.py` |
| `lookout/enrich/shopify_output.py` | Merged into `output/` |
| `lookout/enrich/shopify_api_placeholder.py` | Not needed |
| `lookout/enrich/csv_parser.py` | Input parsing moves to pipeline/models, output to output/ |
| `lookout/ranking/collection_ranker.py` | Replaced by `ranking/ranker.py` |
| `tests/test_web_routes.py` | Web UI removed |
| `tests/test_web_storage.py` | Web UI removed |

---

## Task 1: Consolidate constants into taxonomy

**Files:**
- Modify: `lookout/taxonomy/mappings.py`
- Test: `tests/test_taxonomy.py` (create)

- [ ] **Step 1: Write test for consolidated constants**

```python
# tests/test_taxonomy.py
from lookout.taxonomy.mappings import (
    EXCLUDED_VENDORS,
    MERCH_WEIGHTS,
    NEW_ARRIVAL_DAYS,
    LOW_INVENTORY_THRESHOLD,
    LOCATIONS,
    PRODUCT_TYPE_TO_GOOGLE_CATEGORY,
)


def test_excluded_vendors():
    assert "The Switchback" in EXCLUDED_VENDORS
    assert "The Mountain Air" in EXCLUDED_VENDORS


def test_merch_weights_sum_to_one():
    total = sum(MERCH_WEIGHTS.values())
    assert abs(total - 1.0) < 0.01


def test_merch_weights_keys():
    expected = {"sales_velocity", "margin", "inventory_health", "new_arrival_boost", "low_inventory_penalty"}
    assert set(MERCH_WEIGHTS.keys()) == expected


def test_location_ids():
    assert LOCATIONS["The Mountain Air"]["id"] > 0


def test_new_arrival_days():
    assert NEW_ARRIVAL_DAYS == 30


def test_low_inventory_threshold():
    assert LOW_INVENTORY_THRESHOLD == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_taxonomy.py -v`
Expected: ImportError for EXCLUDED_VENDORS, MERCH_WEIGHTS, etc.

- [ ] **Step 3: Add constants to taxonomy/mappings.py**

Add to the end of `lookout/taxonomy/mappings.py`:

```python
# ---------------------------------------------------------------------------
# Merchandising Configuration
# ---------------------------------------------------------------------------

EXCLUDED_VENDORS = (
    "The Switchback",
    "The Mountain Air",
    "The Mountain Air Back Shop",
    "The Mountain Air Deposits",
)

MERCH_WEIGHTS = {
    "sales_velocity": 0.35,
    "margin": 0.20,
    "inventory_health": 0.20,
    "new_arrival_boost": 0.15,
    "low_inventory_penalty": 0.10,
}

NEW_ARRIVAL_DAYS = 30
LOW_INVENTORY_THRESHOLD = 3

LOCATIONS = {
    "The Mountain Air": {"id": 44797132845, "active": True},
    "The Switchback": {"id": 71628587255, "active": True},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_taxonomy.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Run all existing tests to verify nothing broke**

Run: `cd /Users/andyking/Lookout && uv run pytest -v`
Expected: All 72 existing tests still pass + 6 new

- [ ] **Step 6: Commit**

```bash
git add lookout/taxonomy/mappings.py tests/test_taxonomy.py
git commit --no-gpg-sign -m "Add merchandising constants to taxonomy module"
```

---

## Task 2: Create the store layer

**Files:**
- Create: `lookout/store.py`
- Create: `tests/test_store.py`

- [ ] **Step 1: Write store tests with mocked TVR**

```python
# tests/test_store.py
"""Tests for LookoutStore — mocks TVR's ShopifyStore."""
from unittest.mock import MagicMock, patch

import pytest

from lookout.store import LookoutStore


@pytest.fixture
def mock_tvr_store():
    """Create a mock ShopifyStore."""
    store = MagicMock()
    store.list_vendors.return_value = ["Patagonia", "Altra", "Burton"]
    store.list_product_types.return_value = ["Jacket", "Shoe", "Backpack"]
    store.list_collections.return_value = [
        {"id": 1, "title": "Winter Jackets", "handle": "winter-jackets", "product_count": 15},
    ]
    return store


@pytest.fixture
def lookout_store(mock_tvr_store):
    """Create a LookoutStore with mocked TVR."""
    with patch("lookout.store.ShopifyStore", return_value=mock_tvr_store):
        return LookoutStore()


def test_list_vendors(lookout_store):
    vendors = lookout_store.list_vendors()
    assert "Patagonia" in vendors
    assert isinstance(vendors, list)


def test_list_product_types(lookout_store):
    types = lookout_store.list_product_types()
    assert "Jacket" in types


def test_list_collections(lookout_store):
    collections = lookout_store.list_collections()
    assert len(collections) == 1
    assert collections[0]["handle"] == "winter-jackets"


def test_get_product_not_found(lookout_store, mock_tvr_store):
    mock_tvr_store.search_products.return_value = []
    result = lookout_store.get_product("nonexistent-handle")
    assert result is None


def test_list_vendors_returns_strings(lookout_store):
    vendors = lookout_store.list_vendors()
    assert all(isinstance(v, str) for v in vendors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_store.py -v`
Expected: ImportError — `lookout.store` does not exist

- [ ] **Step 3: Implement store.py**

**Note:** Several methods (`get_inventory`, `get_sales_velocity`, `find_catalog_*`, `get_collection_products`) use raw SQLAlchemy queries against TVR models. This is intentional scaffolding — these queries live in `store.py` (satisfying the boundary rule) but should be replaced by proper TVR store methods as TVR is updated. Add `# TODO: Move to TVR ShopifyStore` comments on each one.

```python
# lookout/store.py
"""Lookout's interface to TVR data.

This is the ONLY module that imports from tvr. All other Lookout modules
receive plain dicts, never SQLAlchemy models.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class LookoutStore:
    """Wraps TVR's ShopifyStore and VendorStore for Lookout's needs."""

    def __init__(self, db_url: str | None = None) -> None:
        from tvr.db.store import ShopifyStore

        self._store = ShopifyStore(db_url) if db_url else ShopifyStore()
        self._vendor_store = None  # Lazy-loaded if needed

    # --- Product data ---

    def list_vendors(self) -> list[str]:
        return self._store.list_vendors()

    def list_product_types(self) -> list[str]:
        return self._store.list_product_types()

    def list_collections(self) -> list[dict]:
        return self._store.list_collections()

    def list_products(
        self,
        vendor: str | None = None,
        product_type: str | None = None,
        status: str = "active",
    ) -> list[dict]:
        products = self._store.list_products(
            vendor=vendor, product_type=product_type, status=status
        )
        return [self._product_to_dict(p) for p in products]

    def get_product(self, handle: str) -> dict | None:
        results = self._store.search_products(handle, limit=1)
        if not results:
            return None
        for p in results:
            if p.handle == handle:
                return self._product_to_dict(p)
        return None

    # --- Variant data ---

    def get_variants(self, product_id: int) -> list[dict]:
        variants = self._store.get_variants_by_product(product_id)
        return [self._variant_to_dict(v) for v in variants]

    def get_variant_by_barcode(self, barcode: str) -> dict | None:
        v = self._store.get_variant_by_barcode(barcode)
        return self._variant_to_dict(v) if v else None

    # --- Inventory + sales ---

    def get_inventory(self, product_id: int) -> dict:
        """Get aggregated inventory data for a product."""
        with self._store.session() as s:
            from tvr.db.models import InventoryItem, InventoryLevel, Variant
            from sqlalchemy import func

            variants = s.query(Variant).filter(Variant.product_id == product_id).all()

            total = 0
            value = 0.0
            full_price_value = 0.0
            by_location: dict[str, int] = {}

            from tvr.db.models import Location

            # Build location name lookup
            locations = {loc.id: loc.name for loc in s.query(Location).all()}

            for v in variants:
                levels = (
                    s.query(InventoryLevel)
                    .join(InventoryItem)
                    .filter(InventoryItem.variant_id == v.id)
                    .all()
                )
                for level in levels:
                    qty = max(0, level.available or 0)
                    total += qty
                    cost = v.cost or 0.0
                    value += qty * cost
                    if not v.compare_at_price:
                        full_price_value += qty * cost

                    loc_name = locations.get(level.location_id, f"loc_{level.location_id}")
                    by_location[loc_name] = by_location.get(loc_name, 0) + qty

            return {
                "total": total,
                "value": round(value, 2),
                "full_price_value": round(full_price_value, 2),
                "by_location": by_location,
            }

    def get_sales_velocity(self, product_id: int, days: int = 28) -> dict:
        """Get sales velocity for a product over a period."""
        with self._store.session() as s:
            from datetime import UTC, datetime, timedelta
            from sqlalchemy import func
            from tvr.db.models import Order, OrderLineItem, Variant

            cutoff = datetime.now(UTC) - timedelta(days=days)
            variant_skus = [
                v.sku
                for v in s.query(Variant).filter(Variant.product_id == product_id).all()
                if v.sku
            ]

            if not variant_skus:
                return {"units": 0, "weekly_avg": 0.0}

            total_units = (
                s.query(func.coalesce(func.sum(OrderLineItem.quantity), 0))
                .join(Order)
                .filter(OrderLineItem.sku.in_(variant_skus))
                .filter(Order.created_at >= cutoff)
                .scalar()
            )

            weeks = days / 7.0
            return {
                "units": int(total_units),
                "weekly_avg": round(float(total_units) / weeks, 2),
            }

    # --- Catalog data ---

    def find_catalog_image(self, barcode: str) -> str | None:
        """Find a catalog image URL by barcode."""
        with self._store.session() as s:
            from tvr.db.models_vendor import CatalogItem

            item = (
                s.query(CatalogItem)
                .filter(
                    CatalogItem.upc == barcode,
                    CatalogItem.image_url.isnot(None),
                    CatalogItem.image_url != "",
                )
                .first()
            )
            return item.image_url if item else None

    def find_catalog_image_by_style(
        self, vendor: str, style: str, color: str
    ) -> str | None:
        """Find a catalog image URL by vendor style code and color."""
        with self._store.session() as s:
            from tvr.db.models_vendor import CatalogItem

            items = (
                s.query(CatalogItem)
                .filter(
                    CatalogItem.vendor == vendor,
                    CatalogItem.style == style,
                    CatalogItem.image_url.isnot(None),
                    CatalogItem.image_url != "",
                )
                .all()
            )

            color_lower = color.lower().strip()
            for item in items:
                if item.color_name and item.color_name.lower().strip() == color_lower:
                    return item.image_url

            for item in items:
                if not item.color_name:
                    continue
                catalog_color = item.color_name.lower().strip()
                if color_lower in catalog_color or catalog_color in color_lower:
                    return item.image_url

            return None

    def find_catalog_description(self, product_id: int) -> str | None:
        """Find a catalog description for a product."""
        with self._store.session() as s:
            from tvr.db.models_vendor import CatalogItem, VendorStyleMap

            mapping = (
                s.query(VendorStyleMap)
                .filter(VendorStyleMap.product_id == product_id)
                .first()
            )
            if mapping:
                item = (
                    s.query(CatalogItem)
                    .filter(
                        CatalogItem.vendor == mapping.vendor,
                        CatalogItem.style == mapping.style_code,
                        CatalogItem.description.isnot(None),
                        CatalogItem.description != "",
                    )
                    .first()
                )
                if item:
                    return item.description.strip()
            return None

    # --- Collections ---

    def get_collection_products(self, handle: str) -> list[dict]:
        """Get products in a collection."""
        with self._store.session() as s:
            from tvr.db.models import Collection, CollectionProduct, Product

            collection = (
                s.query(Collection).filter(Collection.handle == handle).first()
            )
            if not collection:
                return []

            product_ids = (
                s.query(CollectionProduct.product_id)
                .filter(CollectionProduct.collection_id == collection.id)
                .all()
            )
            pids = [pid for (pid,) in product_ids]
            products = (
                s.query(Product)
                .filter(Product.id.in_(pids), Product.status == "active")
                .all()
            )
            return [self._product_to_dict(p) for p in products]

    # --- Conversion helpers ---

    @staticmethod
    def _product_to_dict(p: Any) -> dict:
        return {
            "id": p.id,
            "handle": p.handle,
            "title": p.title or "",
            "body_html": p.body_html or "",
            "vendor": p.vendor or "",
            "product_type": p.product_type or "",
            "tags": p.tags or "",
            "status": p.status or "",
            "created_at": p.created_at,
        }

    @staticmethod
    def _variant_to_dict(v: Any) -> dict:
        return {
            "id": v.id,
            "product_id": v.product_id,
            "sku": v.sku or "",
            "barcode": v.barcode or "",
            "price": v.price or 0.0,
            "compare_at_price": v.compare_at_price,
            "cost": v.cost or 0.0,
            "option1_name": v.option1_name or "",
            "option1_value": v.option1_value or "",
            "option2_name": v.option2_name or "",
            "option2_value": v.option2_value or "",
            "option3_name": v.option3_name or "",
            "option3_value": v.option3_value or "",
            "image_src": v.image_src or "",
            "position": v.position,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_store.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/store.py tests/test_store.py
git commit --no-gpg-sign -m "Add LookoutStore: TVR interface layer returning dicts"
```

---

## Task 3: Rewrite audit module against store

**Files:**
- Create: `lookout/audit/models.py`
- Rewrite: `lookout/audit/auditor.py`
- Create: `tests/test_audit.py`

- [ ] **Step 1: Write audit model and test**

```python
# tests/test_audit.py
import pytest

from lookout.audit.models import ProductScore, AuditResult


def test_product_score_gaps():
    score = ProductScore(
        product_id=1,
        handle="test-jacket",
        title="Test Jacket",
        vendor="Patagonia",
        product_type="Jacket",
        has_product_image=False,
        has_all_variant_images=True,
        has_description=False,
        has_product_type=True,
        has_tags=True,
        variant_count=3,
        variants_missing_images=0,
        inventory_value=500.0,
    )
    score.calculate_gaps()
    assert score.gap_count == 2.0  # missing image (1) + missing description (1)
    assert "Missing product image" in score.gaps
    assert score.priority_score > 0


def test_product_score_no_gaps():
    score = ProductScore(
        product_id=2,
        handle="complete-product",
        title="Complete",
        vendor="Altra",
        product_type="Shoe",
    )
    score.calculate_gaps()
    assert score.gap_count == 0
    assert score.is_complete


def test_audit_result_priority_sort():
    scores = [
        ProductScore(product_id=1, handle="low", title="Low", vendor="V", product_type="T",
                     has_description=False, inventory_value=100.0),
        ProductScore(product_id=2, handle="high", title="High", vendor="V", product_type="T",
                     has_description=False, has_product_image=False, inventory_value=1000.0),
    ]
    for s in scores:
        s.calculate_gaps()

    result = AuditResult(scores=scores)
    priority = result.priority_items
    assert priority[0].handle == "high"  # higher inventory value * more gaps


def test_audit_result_summary():
    scores = [
        ProductScore(product_id=1, handle="a", title="A", vendor="V", product_type="T",
                     has_description=False, inventory_value=100.0),
        ProductScore(product_id=2, handle="b", title="B", vendor="V", product_type="T"),
    ]
    for s in scores:
        s.calculate_gaps()

    result = AuditResult(scores=scores)
    summary = result.summary()
    assert summary["total_products"] == 2
    assert summary["products_with_gaps"] == 1


def test_audit_result_to_csv():
    scores = [
        ProductScore(product_id=1, handle="test", title="Test", vendor="Patagonia",
                     product_type="Jacket", has_description=False, inventory_value=100.0),
    ]
    for s in scores:
        s.calculate_gaps()

    result = AuditResult(scores=scores)
    csv_bytes = result.to_priority_csv()
    csv_text = csv_bytes.decode("utf-8")
    assert "Product Handle" in csv_text
    assert "Has Description" in csv_text
    assert "Barcode" in csv_text  # canonical schema
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_audit.py -v`
Expected: ImportError — `lookout.audit.models` doesn't exist yet

- [ ] **Step 3: Implement audit/models.py**

Create `lookout/audit/models.py` with `ProductScore` dataclass and `AuditResult` class. Port the scoring logic from the existing `auditor.py` (lines 33-148).

**Field name change from existing code:** The existing `auditor.py` uses `product_handle` and `product_title`. The new models use `handle` and `title` (shorter, consistent with how other modules refer to these). The CSV export maps these back to the canonical column names.

The `to_priority_csv()` method must output these columns: Product Handle, Vendor, Title, Barcode, Has Image, Has Variant Images, Has Description, Has Product Type, Has Tags, Gaps, Suggestions, Priority Score, Admin Link.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_audit.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Write auditor test against mocked store**

```python
# Append to tests/test_audit.py
from unittest.mock import MagicMock
from lookout.audit.auditor import ContentAuditor


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.list_products.return_value = [
        {
            "id": 1,
            "handle": "nano-puff-jacket",
            "title": "Nano Puff Jacket",
            "vendor": "Patagonia",
            "product_type": "Jacket",
            "body_html": "",
            "tags": "mens, jacket",
            "status": "active",
            "created_at": None,
        },
    ]
    store.get_variants.return_value = [
        {
            "id": 101, "product_id": 1, "sku": "PAT-NPJ-BLK-M",
            "barcode": "194187123456", "price": 229.0, "compare_at_price": None,
            "cost": 114.5, "option1_name": "Color", "option1_value": "Black",
            "option2_name": "Size", "option2_value": "M",
            "option3_name": "", "option3_value": "",
            "image_src": "", "position": 1,
        },
    ]
    store.get_inventory.return_value = {
        "total": 5, "value": 572.5, "full_price_value": 572.5, "by_location": {},
    }
    return store


def test_auditor_finds_gaps(mock_store):
    auditor = ContentAuditor(mock_store)
    result = auditor.audit()
    assert len(result.scores) == 1
    score = result.scores[0]
    assert not score.has_description  # empty body_html
    assert score.gap_count > 0


def test_auditor_vendor_filter(mock_store):
    auditor = ContentAuditor(mock_store)
    result = auditor.audit(vendor="Patagonia")
    mock_store.list_products.assert_called_with(vendor="Patagonia", status="active")
```

- [ ] **Step 6: Rewrite auditor.py against store**

Rewrite `lookout/audit/auditor.py` to accept a `LookoutStore` instead of `ShopifyStore`. Remove all `from tvr.*` imports. Use `store.list_products()`, `store.get_variants()`, `store.get_inventory()` to get data as dicts. Import `ProductScore` and `AuditResult` from `lookout.audit.models`.

Key changes from existing code:
- Constructor takes `LookoutStore` not `ShopifyStore`
- No `session()` context manager — store methods return dicts
- Image check: `variant["image_src"]` instead of `v.image_src`
- Description check: `product["body_html"]` instead of `product.body_html`
- Inventory: `store.get_inventory(product_id)` returns aggregated dict

- [ ] **Step 7: Run all audit tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_audit.py -v`
Expected: All 7 tests PASS

- [ ] **Step 8: Commit**

```bash
git add lookout/audit/models.py lookout/audit/auditor.py tests/test_audit.py
git commit --no-gpg-sign -m "Rewrite audit module against LookoutStore"
```

---

## Task 4: Rename enrich internals (generator, llm)

**Files:**
- Create: `lookout/enrich/generator.py` (from merchandiser.py)
- Create: `lookout/enrich/llm.py` (from llm_client.py)
- Modify: `lookout/enrich/pipeline.py` (update imports)

- [ ] **Step 1: Copy and rename merchandiser.py → generator.py**

Copy `lookout/enrich/merchandiser.py` to `lookout/enrich/generator.py`. Rename the class from `Merchandiser` to `Generator`. Update the module docstring. Update the convenience function from `generate_merch_output` to `generate_output`.

- [ ] **Step 2: Copy and rename llm_client.py → llm.py**

Copy `lookout/enrich/llm_client.py` to `lookout/enrich/llm.py`. No class renames needed.

- [ ] **Step 3: Update pipeline.py imports**

In `lookout/enrich/pipeline.py`, change:
- `from .llm_client import LLMClient, get_llm_client` → `from .llm import LLMClient, get_llm_client`
- `from .merchandiser import Merchandiser` → `from .generator import Generator`
- Update the `ProductProcessor.__init__` to use `self.generator = Generator(llm_client=llm_client)`
- Update `process()` to call `self.generator.generate_output()` instead of `self.merchandiser.generate_output()`

- [ ] **Step 4: Update enrich/__init__.py exports if needed**

- [ ] **Step 5: Run existing tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_extractor.py tests/test_models.py tests/test_helpers.py tests/test_csv_output.py tests/test_config.py -v`
Expected: All pass (these test internals that didn't change)

- [ ] **Step 6: Delete old files**

Remove `lookout/enrich/merchandiser.py` and `lookout/enrich/llm_client.py`.

- [ ] **Step 7: Run tests again to confirm**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/ -v --ignore=tests/test_web_routes.py --ignore=tests/test_web_storage.py`
Expected: All non-web tests pass

- [ ] **Step 8: Commit**

```bash
git add lookout/enrich/generator.py lookout/enrich/llm.py lookout/enrich/pipeline.py lookout/enrich/__init__.py
git rm lookout/enrich/merchandiser.py lookout/enrich/llm_client.py
git commit --no-gpg-sign -m "Rename merchandiser→generator, llm_client→llm in enrich module"
```

---

## Task 5: Rewrite ranking module against store

**Files:**
- Create: `lookout/ranking/ranker.py`
- Create: `tests/test_ranking.py`
- Delete: `lookout/ranking/collection_ranker.py`

- [ ] **Step 1: Write ranking tests**

```python
# tests/test_ranking.py
from unittest.mock import MagicMock
from datetime import UTC, datetime

import pytest

from lookout.ranking.ranker import CollectionRanker, RankedProduct, RankingResult


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.list_products.return_value = [
        {"id": 1, "handle": "fast-seller", "title": "Fast Seller", "vendor": "Patagonia",
         "product_type": "Jacket", "created_at": datetime(2026, 3, 1, tzinfo=UTC)},
        {"id": 2, "handle": "slow-mover", "title": "Slow Mover", "vendor": "Burton",
         "product_type": "Jacket", "created_at": datetime(2025, 6, 1, tzinfo=UTC)},
    ]
    store.get_variants.side_effect = lambda pid: [
        {"id": pid * 100, "product_id": pid, "sku": f"SKU-{pid}", "price": 200.0,
         "cost": 100.0, "compare_at_price": None, "barcode": "", "position": 1,
         "option1_name": "", "option1_value": "", "option2_name": "", "option2_value": "",
         "option3_name": "", "option3_value": "", "image_src": ""},
    ]
    store.get_inventory.side_effect = lambda pid: {
        "total": 10 if pid == 1 else 50,
        "value": 1000.0 if pid == 1 else 5000.0,
        "full_price_value": 1000.0,
        "by_location": {},
    }
    store.get_sales_velocity.side_effect = lambda pid, days=28: {
        "units": 8 if pid == 1 else 1,
        "weekly_avg": 2.0 if pid == 1 else 0.25,
    }
    return store


def test_rank_by_vendor(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank(vendor="Patagonia")
    assert isinstance(result, RankingResult)
    assert len(result.products) == 2


def test_fast_seller_ranks_higher(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank()
    ranked = result.ranked
    assert ranked[0].handle == "fast-seller"


def test_pin_override(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank(overrides={"slow-mover": {"pin": 1}})
    ranked = result.ranked
    assert ranked[0].handle == "slow-mover"


def test_bury_override(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank(overrides={"fast-seller": {"bury": True}})
    ranked = result.ranked
    assert ranked[-1].handle == "fast-seller"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_ranking.py -v`
Expected: ImportError

- [ ] **Step 3: Implement ranking/ranker.py**

Port the logic from `collection_ranker.py` but rewrite to use `LookoutStore` instead of raw SQLAlchemy. Import weights/thresholds from `lookout.taxonomy.mappings`. Key changes:

- `CollectionRanker.__init__(self, store: LookoutStore)` — not `ShopifyStore`
- `rank()` method replaces `rank_collection()` — takes `collection=None, vendor=None, product_type=None, overrides=None, limit=200`
- Uses `store.list_products()`, `store.get_variants()`, `store.get_inventory()`, `store.get_sales_velocity()`, `store.get_collection_products()` — all return dicts
- Imports `MERCH_WEIGHTS, NEW_ARRIVAL_DAYS, LOW_INVENTORY_THRESHOLD, LOCATIONS` from `lookout.taxonomy.mappings`
- `RankedProduct` and `RankingResult` dataclasses stay similar to existing

- [ ] **Step 4: Run tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_ranking.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Delete old file**

Remove `lookout/ranking/collection_ranker.py`.

- [ ] **Step 6: Commit**

```bash
git add lookout/ranking/ranker.py tests/test_ranking.py
git rm lookout/ranking/collection_ranker.py
git commit --no-gpg-sign -m "Rewrite ranking module against LookoutStore"
```

---

## Task 6: Rewrite output modules against store

**Files:**
- Rewrite: `lookout/output/alt_text.py`
- Rewrite: `lookout/output/google_shopping.py`
- Rewrite: `lookout/output/matrixify.py`

- [ ] **Step 1: Rewrite alt_text.py**

Replace `from tvr.db.models import Product, Variant` with `LookoutStore` usage. The `generate_alt_text_xlsx(output_path, store)` function takes a `LookoutStore` instead of `ShopifyStore`. Uses `store.list_products()` and `store.get_variants()`. Import `EXCLUDED_VENDORS` from `lookout.taxonomy.mappings`.

- [ ] **Step 2: Rewrite google_shopping.py**

Same pattern — replace raw TVR model queries with `store.list_products()` and `store.get_variants()`. Import `EXCLUDED_VENDORS` from `lookout.taxonomy.mappings` (remove the local duplicate). The `generate_google_shopping()`, `generate_weights()`, and `generate_weight_audit()` functions take `LookoutStore`.

- [ ] **Step 3: Rewrite matrixify.py**

This is the largest rewrite. Key changes:
- `MatrixifyImporter` — keep as-is for now (it reads XLSX, writes to DB — this is a TVR-side operation and may move to TVR later)
- `ImageEnricher` — rewrite to use `store.list_products()`, `store.get_variants()`, `store.find_catalog_image()`, `store.find_catalog_image_by_style()`, `store.find_catalog_description()`
- `MatrixifyExporter` — keep as-is (pure CSV generation, no DB access)
- Remove all `from tvr.*` imports
- Import `EXCLUDED_VENDORS` from `lookout.taxonomy.mappings`

- [ ] **Step 4: Run existing tests that touch output**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_csv_output.py -v`
Expected: All pass (CSV output tests don't depend on TVR)

- [ ] **Step 5: Verify no tvr imports remain outside store.py**

Run: `cd /Users/andyking/Lookout && grep -r "from tvr\.\|import tvr" lookout/ --include="*.py" | grep -v store.py | grep -v __pycache__`
Expected: No output (all TVR imports consolidated in store.py)

- [ ] **Step 6: Commit**

```bash
git add lookout/output/alt_text.py lookout/output/google_shopping.py lookout/output/matrixify.py
git commit --no-gpg-sign -m "Rewrite output modules against LookoutStore"
```

---

## Task 7: Build unified CLI

**Files:**
- Create: `lookout/cli.py`
- Modify: `pyproject.toml`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write CLI tests**

```python
# tests/test_cli.py
from click.testing import CliRunner

from lookout.cli import cli


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "audit" in result.output
    assert "enrich" in result.output
    assert "rank" in result.output
    assert "vendors" in result.output
    assert "output" in result.output


def test_audit_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "--help"])
    assert result.exit_code == 0
    assert "--vendor" in result.output
    assert "--out" in result.output


def test_enrich_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["enrich", "--help"])
    assert result.exit_code == 0


def test_enrich_run_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["enrich", "run", "--help"])
    assert result.exit_code == 0
    assert "--vendor" in result.output
    assert "--max-rows" in result.output
    assert "-i" in result.output


def test_rank_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["rank", "--help"])
    assert result.exit_code == 0
    assert "--collection" in result.output
    assert "--vendor" in result.output


def test_vendors_command():
    runner = CliRunner()
    result = runner.invoke(cli, ["vendors"])
    # Should work since vendors.yaml exists at project root
    assert result.exit_code == 0
    assert "Patagonia" in result.output


def test_output_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["output", "--help"])
    assert result.exit_code == 0
    assert "matrixify-images" in result.output
    assert "alt-text" in result.output
    assert "google-shopping" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_cli.py -v`
Expected: ImportError

- [ ] **Step 3: Implement cli.py**

Build `lookout/cli.py` with Click groups:
- `cli` — top-level group with `--version`
- `audit` — command with `--vendor`, `--out` options. Instantiates `LookoutStore` and `ContentAuditor`.
- `enrich` — group
  - `enrich run` — with `--vendor`, `-i`, `--max-rows`, `--concurrency`, `--out`, `--force`, `--dry-run`. When `--vendor` is given without `-i`, runs audit internally and feeds results to pipeline.
  - `enrich validate` — validates input CSV
- `rank` — command with `--collection`, `--vendor`, `--product-type`, `--out`
- `vendors` — lists vendors from vendors.yaml
- `output` — group
  - `output matrixify-images`
  - `output alt-text`
  - `output google-shopping`
  - `output weights`
  - `output weight-audit`

Use Rich for terminal output (tables, panels, progress bars).

- [ ] **Step 4: Update pyproject.toml**

Change entry point (line 45) from `lookout.enrich.cli:main` to `lookout.cli:main`.

Move `click` and `rich` from `[project.optional-dependencies] enrich` to core `[project] dependencies` — the CLI is now the primary interface and must work without optional extras.

```toml
[project.scripts]
lookout = "lookout.cli:main"
```

- [ ] **Step 5: Run CLI tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_cli.py -v`
Expected: All 7 tests PASS

- [ ] **Step 6: Verify CLI works**

Run: `cd /Users/andyking/Lookout && uv run pip install -e . && uv run lookout --help`
Expected: Shows all subcommands

- [ ] **Step 7: Commit**

```bash
git add lookout/cli.py tests/test_cli.py pyproject.toml
git commit --no-gpg-sign -m "Add unified CLI with audit/enrich/rank/vendors/output commands"
```

---

## Task 8: Delete web modules and old files

**Files:**
- Delete: `lookout/web/` (entire directory)
- Delete: `lookout/enrich_web/` (entire directory)
- Delete: `lookout/enrich/cli.py`
- Delete: `lookout/enrich/shopify_output.py`
- Delete: `lookout/enrich/shopify_api_placeholder.py`
- Delete: `lookout/enrich/csv_parser.py` (after extracting needed functions)
- Delete: `tests/test_web_routes.py`
- Delete: `tests/test_web_storage.py`

- [ ] **Step 1: Create enrich/io.py with migrated CSV functions**

Create `lookout/enrich/io.py` with functions extracted from `csv_parser.py`:
- `parse_input_csv(csv_path, max_rows=None)` — generator yielding InputRow (lines 27-64 of csv_parser.py)
- `count_input_rows(csv_path)` — row counter (lines 67-81)
- `parse_shopify_export(csv_path)` — Shopify CSV parser (lines 111-147)
- `ShopifyExportRow` class (lines 89-109)

Create `lookout/output/enrich_export.py` with output functions from `csv_parser.py`:
- `write_shopify_csv()` (lines 221-240)
- `merch_output_to_shopify_rows()` (lines 243-289)
- `write_variant_image_assignments()` (lines 306-336)
- `write_run_report()` (lines 355-386)
- Column constant lists: `SHOPIFY_CSV_COLUMNS`, `VARIANT_IMAGE_COLUMNS`, `RUN_REPORT_COLUMNS`

- [ ] **Step 2: Update pipeline.py imports to use new locations**

Change `from .csv_parser import parse_input_csv` to `from .io import parse_input_csv`.
Change `from .csv_parser import ...` in `shopify_output.py` to `from ..output.enrich_export import ...` (or update the ShopifyOutputBuilder to import from the new location).

- [ ] **Step 3: Run tests to verify migration works**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_csv_output.py -v`
Expected: All 9 CSV output tests still pass

- [ ] **Step 4: Delete web directories**

```bash
git rm -r lookout/web/ lookout/enrich_web/
git rm tests/test_web_routes.py tests/test_web_storage.py
```

- [ ] **Step 5: Delete old enrich files**

```bash
git rm lookout/enrich/cli.py lookout/enrich/shopify_api_placeholder.py
git rm lookout/enrich/csv_parser.py lookout/enrich/shopify_output.py
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/andyking/Lookout && uv run pytest -v`
Expected: All remaining tests pass. Web tests are gone. Enrich tests pass with updated imports.

- [ ] **Step 7: Verify no broken imports**

Run: `cd /Users/andyking/Lookout && uv run python -c "from lookout.cli import cli; print('CLI OK')"`
Run: `cd /Users/andyking/Lookout && uv run python -c "from lookout.enrich.pipeline import Pipeline; print('Pipeline OK')"`
Run: `cd /Users/andyking/Lookout && uv run python -c "from lookout.audit.auditor import ContentAuditor; print('Audit OK')"`
Expected: All print OK

- [ ] **Step 8: Commit**

```bash
git add -A
git commit --no-gpg-sign -m "Remove web UI modules and consolidate enrich file layout"
```

---

## Task 9: Integration test — audit to enrich

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration.py
"""Integration test: audit finds gaps, enrich pipeline processes them."""
import csv
import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lookout.audit.auditor import ContentAuditor
from lookout.audit.models import AuditResult


def test_audit_csv_is_valid_enrich_input():
    """Verify audit CSV output can be parsed as enrich pipeline input."""
    from lookout.enrich.models import InputRow

    # Create a mock audit result
    mock_store = MagicMock()
    mock_store.list_products.return_value = [
        {
            "id": 1, "handle": "nano-puff", "title": "Nano Puff",
            "vendor": "Patagonia", "product_type": "Jacket",
            "body_html": "", "tags": "mens", "status": "active",
            "created_at": None,
        },
    ]
    mock_store.get_variants.return_value = [
        {
            "id": 101, "product_id": 1, "sku": "PAT-NP-M",
            "barcode": "123456789", "price": 229.0, "compare_at_price": None,
            "cost": 100.0, "option1_name": "Color", "option1_value": "Black",
            "option2_name": "Size", "option2_value": "M",
            "option3_name": "", "option3_value": "",
            "image_src": "", "position": 1,
        },
    ]
    mock_store.get_inventory.return_value = {
        "total": 5, "value": 500.0, "full_price_value": 500.0, "by_location": {},
    }

    # Run audit
    auditor = ContentAuditor(mock_store)
    result = auditor.audit()

    # Export to CSV
    csv_bytes = result.to_priority_csv()
    csv_text = csv_bytes.decode("utf-8")

    # Parse as enrich input
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert len(rows) == 1

    # Verify it can be parsed as InputRow
    row = rows[0]
    input_row = InputRow.model_validate(row)
    assert input_row.product_handle == "nano-puff"
    assert input_row.vendor == "Patagonia"
    assert input_row.needs_description  # body_html was empty
```

- [ ] **Step 2: Run test**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit --no-gpg-sign -m "Add integration test: audit CSV → enrich pipeline input"
```

---

## Task 10: Final verification and cleanup

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/andyking/Lookout && uv run pytest -v`
Expected: All tests pass

- [ ] **Step 2: Run ruff**

Run: `cd /Users/andyking/Lookout && uv run ruff check lookout/ tests/`
Fix any issues.

- [ ] **Step 3: Verify no TVR imports outside store.py**

Run: `grep -r "from tvr\.\|import tvr" lookout/ --include="*.py" | grep -v store.py | grep -v __pycache__`
Expected: No output

- [ ] **Step 4: Verify CLI end-to-end**

Run: `cd /Users/andyking/Lookout && uv run lookout --help`
Run: `cd /Users/andyking/Lookout && uv run lookout vendors`
Run: `cd /Users/andyking/Lookout && uv run lookout audit --help`
Run: `cd /Users/andyking/Lookout && uv run lookout enrich run --help`
Expected: All show correct output

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit --no-gpg-sign -m "Lookout remodel: final cleanup and verification"
```
