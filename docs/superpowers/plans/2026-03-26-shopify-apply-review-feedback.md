# Shopify Apply + Review + Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable enrichment output to be reviewed, approved/rejected with feedback, applied directly to Shopify via API, and reverted — with rejections feeding back into the optimization loops.

**Architecture:** Three new modules: (1) `apply/` handles Shopify writes with backup+revert, (2) `review/` generates an HTML review report with approve/reject/edit per product and collects dispositions, (3) `feedback/` processes dispositions into test set entries and scorer training data. All writes require explicit approval. Every write is backed up first and revertable.

**Tech Stack:** Shopify Admin GraphQL API (via TVR), Jinja2 for HTML review report, Click CLI commands, JSON for state/backup/feedback storage.

---

## File Structure

### New files to create:

| File | Responsibility |
|------|---------------|
| `lookout/apply/__init__.py` | Package marker |
| `lookout/apply/backup.py` | Read current Shopify state, save to backup JSON before any write |
| `lookout/apply/writer.py` | Write body_html + images to Shopify via GraphQL mutation. Atomic per-product. |
| `lookout/apply/revert.py` | Restore products from backup files |
| `lookout/apply/models.py` | `ApplyRun`, `ProductChange`, `ChangeStatus` dataclasses |
| `lookout/review/__init__.py` | Package marker |
| `lookout/review/report.py` | Generate HTML review report from enrichment run |
| `lookout/review/dispositions.py` | Load/save review dispositions (approve/reject/edit per product) |
| `lookout/review/templates/review.html` | Jinja2 template for the review report |
| `lookout/feedback/__init__.py` | Package marker |
| `lookout/feedback/collector.py` | Process dispositions into feedback entries, update test set |
| `tests/test_apply.py` | Tests for backup, write, revert |
| `tests/test_review.py` | Tests for report generation and disposition handling |
| `tests/test_feedback.py` | Tests for feedback collection |

### Files to modify:

| File | Changes |
|------|---------|
| `lookout/store.py` | Add `update_product_body()` and `read_product_current()` methods wrapping TVR |
| `lookout/cli.py` | Add `enrich apply`, `enrich revert`, `enrich review`, `enrich feedback` commands |

### Upstream (TVR) — minimal additions:

| File | Changes |
|------|---------|
| `/Users/andyking/The-Variant-Range/tvr/mcp/api.py` | Add `product_update` GraphQL mutation method |

---

## Task 1: Shopify GraphQL Mutation in TVR

**Files:**
- Modify: `/Users/andyking/The-Variant-Range/tvr/mcp/api.py`
- Test: `/Users/andyking/The-Variant-Range/tests/test_api_mutations.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for Shopify GraphQL product mutations."""
import pytest
from unittest.mock import AsyncMock, patch
from tvr.mcp.api import ShopifyAdminAPI


class TestProductUpdate:
    @pytest.mark.asyncio
    async def test_update_body_html(self):
        api = ShopifyAdminAPI.__new__(ShopifyAdminAPI)
        api._client = AsyncMock()
        api._client.post = AsyncMock(return_value=AsyncMock(
            status_code=200,
            json=lambda: {"data": {"productUpdate": {"product": {"id": "gid://shopify/Product/123"}, "userErrors": []}}}
        ))

        result = await api.update_product(
            product_id=123,
            body_html="<p>New description</p>",
        )
        assert result["product"]["id"] == "gid://shopify/Product/123"
        assert result["userErrors"] == []

    @pytest.mark.asyncio
    async def test_update_with_user_errors(self):
        api = ShopifyAdminAPI.__new__(ShopifyAdminAPI)
        api._client = AsyncMock()
        api._client.post = AsyncMock(return_value=AsyncMock(
            status_code=200,
            json=lambda: {"data": {"productUpdate": {"product": None, "userErrors": [{"field": ["body_html"], "message": "too long"}]}}}
        ))

        result = await api.update_product(product_id=123, body_html="x" * 100000)
        assert len(result["userErrors"]) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andyking/The-Variant-Range && .venv/bin/python -m pytest tests/test_api_mutations.py -v`
Expected: FAIL — `update_product` not defined

- [ ] **Step 3: Implement the mutation method**

Add to `/Users/andyking/The-Variant-Range/tvr/mcp/api.py` in the `ShopifyAdminAPI` class:

```python
PRODUCT_UPDATE_MUTATION = """
mutation productUpdate($input: ProductInput!) {
    productUpdate(input: $input) {
        product {
            id
            handle
            title
            bodyHtml
        }
        userErrors {
            field
            message
        }
    }
}
"""

async def update_product(
    self,
    product_id: int,
    body_html: str | None = None,
    title: str | None = None,
    product_type: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Update a product via GraphQL Admin API.

    Only updates fields that are explicitly provided (not None).
    Returns the productUpdate response with product data and userErrors.
    """
    gid = f"gid://shopify/Product/{product_id}"
    input_fields: dict = {"id": gid}

    if body_html is not None:
        input_fields["bodyHtml"] = body_html
    if title is not None:
        input_fields["title"] = title
    if product_type is not None:
        input_fields["productType"] = product_type
    if tags is not None:
        input_fields["tags"] = tags

    result = await self.execute(
        PRODUCT_UPDATE_MUTATION,
        variables={"input": input_fields},
    )
    return result.get("data", {}).get("productUpdate", {})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andyking/The-Variant-Range && .venv/bin/python -m pytest tests/test_api_mutations.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/andyking/The-Variant-Range
git add tvr/mcp/api.py tests/test_api_mutations.py
git commit --no-gpg-sign -m "feat: add productUpdate GraphQL mutation to ShopifyAdminAPI"
```

---

## Task 2: Apply Models

**Files:**
- Create: `lookout/apply/__init__.py`
- Create: `lookout/apply/models.py`
- Test: `tests/test_apply.py`

- [ ] **Step 1: Create package and models**

```python
# lookout/apply/__init__.py
# (empty)
```

```python
# lookout/apply/models.py
"""Data models for the enrichment apply pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class ChangeStatus(str, Enum):
    PENDING = "pending"        # Generated, not yet reviewed
    APPROVED = "approved"      # Reviewed and approved for apply
    REJECTED = "rejected"      # Reviewed and rejected (with reason)
    EDITED = "edited"          # Reviewed, modified, then approved
    APPLIED = "applied"        # Successfully written to Shopify
    REVERTED = "reverted"      # Applied then rolled back
    FAILED = "failed"          # Apply attempted but failed


@dataclass
class ProductChange:
    """A proposed change to a single product."""

    handle: str
    product_id: int
    title: str
    vendor: str

    # Proposed changes (None = no change)
    new_body_html: str | None = None
    new_images: list[dict] | None = None
    new_variant_image_map: dict | None = None

    # Current state (populated during backup)
    current_body_html: str | None = None
    current_images: list[dict] | None = None

    # Review disposition
    status: ChangeStatus = ChangeStatus.PENDING
    rejection_reason: str = ""
    edited_body_html: str | None = None  # If status=EDITED, what user changed it to
    confidence: int = 0

    # Apply tracking
    applied_at: str | None = None
    reverted_at: str | None = None
    error: str | None = None


@dataclass
class ApplyRun:
    """A batch of product changes with metadata."""

    run_id: str  # e.g. "run_2026-03-26_001"
    created_at: str = ""
    source_dir: str = ""  # enrichment output directory
    changes: list[ProductChange] = field(default_factory=list)

    @property
    def pending(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status == ChangeStatus.PENDING]

    @property
    def approved(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status in (ChangeStatus.APPROVED, ChangeStatus.EDITED)]

    @property
    def rejected(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status == ChangeStatus.REJECTED]

    @property
    def applied(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status == ChangeStatus.APPLIED]

    def summary(self) -> dict:
        return {
            "total": len(self.changes),
            "pending": len(self.pending),
            "approved": len(self.approved),
            "rejected": len(self.rejected),
            "applied": len(self.applied),
            "approval_rate": len(self.approved) / max(len(self.changes) - len(self.pending), 1),
        }
```

- [ ] **Step 2: Write basic model tests**

```python
# tests/test_apply.py
"""Tests for enrichment apply pipeline."""

from lookout.apply.models import ApplyRun, ChangeStatus, ProductChange


class TestProductChange:
    def test_default_status_is_pending(self):
        c = ProductChange(handle="test", product_id=1, title="Test", vendor="V")
        assert c.status == ChangeStatus.PENDING

    def test_rejection_has_reason(self):
        c = ProductChange(handle="test", product_id=1, title="Test", vendor="V",
                          status=ChangeStatus.REJECTED, rejection_reason="wrong_facts")
        assert c.rejection_reason == "wrong_facts"


class TestApplyRun:
    def test_summary(self):
        changes = [
            ProductChange(handle="a", product_id=1, title="A", vendor="V", status=ChangeStatus.APPROVED),
            ProductChange(handle="b", product_id=2, title="B", vendor="V", status=ChangeStatus.REJECTED, rejection_reason="bad_structure"),
            ProductChange(handle="c", product_id=3, title="C", vendor="V", status=ChangeStatus.PENDING),
        ]
        run = ApplyRun(run_id="test", changes=changes)
        s = run.summary()
        assert s["total"] == 3
        assert s["approved"] == 1
        assert s["rejected"] == 1
        assert s["pending"] == 1
```

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest tests/test_apply.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add lookout/apply/ tests/test_apply.py
git commit --no-gpg-sign -m "feat: add apply models — ProductChange, ApplyRun, ChangeStatus"
```

---

## Task 3: Backup + Read Current State

**Files:**
- Create: `lookout/apply/backup.py`
- Modify: `lookout/store.py` (add `read_product_current()`)
- Test: `tests/test_apply.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_apply.py`:

```python
import json
from pathlib import Path
from unittest.mock import MagicMock

from lookout.apply.backup import create_backup, load_backup
from lookout.apply.models import ProductChange, ChangeStatus


class TestBackup:
    def test_create_and_load_backup(self, tmp_path):
        change = ProductChange(
            handle="test-product", product_id=123, title="Test", vendor="V",
            current_body_html="<p>Old description</p>",
            current_images=[{"src": "https://img.com/1.jpg", "position": 1}],
            new_body_html="<p>New description</p>",
        )

        backup_path = create_backup(change, tmp_path)
        assert backup_path.exists()

        loaded = load_backup(backup_path)
        assert loaded["handle"] == "test-product"
        assert loaded["body_html"] == "<p>Old description</p>"
        assert len(loaded["images"]) == 1

    def test_backup_filename_includes_handle_and_timestamp(self, tmp_path):
        change = ProductChange(handle="my-product", product_id=1, title="T", vendor="V")
        path = create_backup(change, tmp_path)
        assert "my-product" in path.name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_apply.py::TestBackup -v`
Expected: FAIL — `create_backup` not defined

- [ ] **Step 3: Implement backup module**

```python
# lookout/apply/backup.py
"""Backup and restore Shopify product state before writes."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from lookout.apply.models import ProductChange

logger = logging.getLogger(__name__)


def create_backup(change: ProductChange, backup_dir: Path) -> Path:
    """Save the current Shopify state of a product before writing.

    Returns the path to the backup file.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{change.handle}_{timestamp}.json"
    path = backup_dir / filename

    backup_data = {
        "handle": change.handle,
        "product_id": change.product_id,
        "title": change.title,
        "vendor": change.vendor,
        "body_html": change.current_body_html,
        "images": change.current_images or [],
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
    }

    path.write_text(json.dumps(backup_data, indent=2))
    logger.info("Backed up %s to %s", change.handle, path)
    return path


def load_backup(path: Path) -> dict:
    """Load a backup file and return the saved state."""
    return json.loads(path.read_text())


def find_latest_backup(handle: str, backup_dir: Path) -> Path | None:
    """Find the most recent backup for a product handle."""
    matches = sorted(
        backup_dir.glob(f"{handle}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_apply.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/apply/backup.py tests/test_apply.py
git commit --no-gpg-sign -m "feat: add backup/restore for Shopify product state"
```

---

## Task 4: Shopify Writer

**Files:**
- Create: `lookout/apply/writer.py`
- Modify: `lookout/store.py`
- Test: `tests/test_apply.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_apply.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from lookout.apply.writer import apply_change
from lookout.apply.models import ProductChange, ChangeStatus


class TestWriter:
    def test_apply_change_updates_status(self):
        change = ProductChange(
            handle="test", product_id=123, title="Test", vendor="V",
            new_body_html="<p>New</p>",
            current_body_html="<p>Old</p>",
            status=ChangeStatus.APPROVED,
        )

        mock_api = AsyncMock()
        mock_api.update_product = AsyncMock(return_value={
            "product": {"id": "gid://shopify/Product/123"},
            "userErrors": [],
        })

        result = asyncio.run(apply_change(change, mock_api, backup_dir=None))
        assert result.status == ChangeStatus.APPLIED
        assert result.applied_at is not None

    def test_apply_change_with_user_errors_fails(self):
        change = ProductChange(
            handle="test", product_id=123, title="Test", vendor="V",
            new_body_html="<p>Bad</p>",
            status=ChangeStatus.APPROVED,
        )

        mock_api = AsyncMock()
        mock_api.update_product = AsyncMock(return_value={
            "product": None,
            "userErrors": [{"field": ["bodyHtml"], "message": "too long"}],
        })

        result = asyncio.run(apply_change(change, mock_api, backup_dir=None))
        assert result.status == ChangeStatus.FAILED
        assert "too long" in result.error

    def test_apply_skips_non_approved(self):
        change = ProductChange(
            handle="test", product_id=123, title="Test", vendor="V",
            status=ChangeStatus.PENDING,
        )
        mock_api = AsyncMock()
        result = asyncio.run(apply_change(change, mock_api, backup_dir=None))
        assert result.status == ChangeStatus.PENDING
        mock_api.update_product.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_apply.py::TestWriter -v`
Expected: FAIL — `apply_change` not defined

- [ ] **Step 3: Implement writer**

```python
# lookout/apply/writer.py
"""Write approved enrichment changes to Shopify via GraphQL."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from lookout.apply.backup import create_backup
from lookout.apply.models import ApplyRun, ChangeStatus, ProductChange

logger = logging.getLogger(__name__)


async def apply_change(
    change: ProductChange,
    api,
    backup_dir: Path | None = None,
) -> ProductChange:
    """Apply a single approved product change to Shopify.

    Only processes changes with status APPROVED or EDITED.
    Creates a backup before writing. Sets status to APPLIED or FAILED.
    """
    if change.status not in (ChangeStatus.APPROVED, ChangeStatus.EDITED):
        logger.debug("Skipping %s (status=%s)", change.handle, change.status)
        return change

    # Backup current state before writing
    if backup_dir:
        create_backup(change, backup_dir)

    # Determine what body to write
    body_html = change.edited_body_html if change.status == ChangeStatus.EDITED else change.new_body_html

    try:
        result = await api.update_product(
            product_id=change.product_id,
            body_html=body_html,
        )

        errors = result.get("userErrors", [])
        if errors:
            change.status = ChangeStatus.FAILED
            change.error = "; ".join(e.get("message", "") for e in errors)
            logger.error("Failed to apply %s: %s", change.handle, change.error)
        else:
            change.status = ChangeStatus.APPLIED
            change.applied_at = datetime.now(timezone.utc).isoformat()
            logger.info("Applied %s to Shopify", change.handle)

    except Exception as e:
        change.status = ChangeStatus.FAILED
        change.error = str(e)
        logger.error("Exception applying %s: %s", change.handle, e)

    return change


async def apply_run(
    run: ApplyRun,
    api,
    backup_dir: Path,
) -> ApplyRun:
    """Apply all approved changes in a run, sequentially.

    Each product is independent — a failure on one does not stop others.
    """
    for change in run.changes:
        if change.status in (ChangeStatus.APPROVED, ChangeStatus.EDITED):
            await apply_change(change, api, backup_dir)

    applied = len(run.applied)
    failed = sum(1 for c in run.changes if c.status == ChangeStatus.FAILED)
    logger.info("Apply run %s complete: %d applied, %d failed", run.run_id, applied, failed)
    return run
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_apply.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/apply/writer.py tests/test_apply.py
git commit --no-gpg-sign -m "feat: add Shopify writer with backup-before-write and per-product error handling"
```

---

## Task 5: Revert

**Files:**
- Create: `lookout/apply/revert.py`
- Test: `tests/test_apply.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_apply.py`:

```python
from lookout.apply.revert import revert_change


class TestRevert:
    def test_revert_restores_from_backup(self, tmp_path):
        # Create a backup manually
        import json
        backup_data = {
            "handle": "test-product",
            "product_id": 123,
            "body_html": "<p>Original</p>",
            "images": [],
        }
        backup_path = tmp_path / "test-product_20260326_120000.json"
        backup_path.write_text(json.dumps(backup_data))

        mock_api = AsyncMock()
        mock_api.update_product = AsyncMock(return_value={
            "product": {"id": "gid://shopify/Product/123"},
            "userErrors": [],
        })

        result = asyncio.run(revert_change("test-product", tmp_path, mock_api))
        assert result is True
        mock_api.update_product.assert_called_once_with(
            product_id=123,
            body_html="<p>Original</p>",
        )

    def test_revert_no_backup_returns_false(self, tmp_path):
        mock_api = AsyncMock()
        result = asyncio.run(revert_change("nonexistent", tmp_path, mock_api))
        assert result is False
        mock_api.update_product.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_apply.py::TestRevert -v`
Expected: FAIL — `revert_change` not defined

- [ ] **Step 3: Implement revert**

```python
# lookout/apply/revert.py
"""Revert applied enrichment changes using backup files."""

from __future__ import annotations

import logging
from pathlib import Path

from lookout.apply.backup import find_latest_backup, load_backup

logger = logging.getLogger(__name__)


async def revert_change(handle: str, backup_dir: Path, api) -> bool:
    """Revert a single product to its backed-up state.

    Finds the most recent backup for the handle and writes it back
    to Shopify. Returns True if reverted, False if no backup found.
    """
    backup_path = find_latest_backup(handle, backup_dir)
    if not backup_path:
        logger.warning("No backup found for %s in %s", handle, backup_dir)
        return False

    backup = load_backup(backup_path)

    try:
        result = await api.update_product(
            product_id=backup["product_id"],
            body_html=backup.get("body_html"),
        )
        errors = result.get("userErrors", [])
        if errors:
            logger.error("Failed to revert %s: %s", handle, errors)
            return False

        logger.info("Reverted %s from backup %s", handle, backup_path.name)
        return True

    except Exception as e:
        logger.error("Exception reverting %s: %s", handle, e)
        return False
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_apply.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/apply/revert.py tests/test_apply.py
git commit --no-gpg-sign -m "feat: add revert command — restores products from backup"
```

---

## Task 6: Review Report Generator

**Files:**
- Create: `lookout/review/__init__.py`
- Create: `lookout/review/report.py`
- Create: `lookout/review/templates/review.html`
- Create: `lookout/review/dispositions.py`
- Test: `tests/test_review.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review.py
"""Tests for review report generation and disposition handling."""

import json
from pathlib import Path

from lookout.apply.models import ApplyRun, ChangeStatus, ProductChange
from lookout.review.report import generate_review_report
from lookout.review.dispositions import save_dispositions, load_dispositions


class TestReviewReport:
    def test_generates_html_file(self, tmp_path):
        changes = [
            ProductChange(
                handle="test-product", product_id=1, title="Test Product",
                vendor="TestVendor", confidence=85,
                current_body_html="<p>Old description</p>",
                new_body_html="<p>New improved description with features</p>",
            ),
        ]
        run = ApplyRun(run_id="test-run", changes=changes)

        output_path = tmp_path / "review.html"
        generate_review_report(run, output_path)
        assert output_path.exists()

        html = output_path.read_text()
        assert "test-product" in html
        assert "Old description" in html
        assert "New improved description" in html

    def test_report_includes_all_products(self, tmp_path):
        changes = [
            ProductChange(handle=f"product-{i}", product_id=i, title=f"Product {i}",
                          vendor="V", new_body_html=f"<p>Desc {i}</p>")
            for i in range(5)
        ]
        run = ApplyRun(run_id="test", changes=changes)
        output_path = tmp_path / "review.html"
        generate_review_report(run, output_path)
        html = output_path.read_text()
        for i in range(5):
            assert f"product-{i}" in html


class TestDispositions:
    def test_save_and_load_roundtrip(self, tmp_path):
        dispositions = {
            "product-a": {"status": "approved"},
            "product-b": {"status": "rejected", "reason": "wrong_facts"},
            "product-c": {"status": "edited", "edited_body_html": "<p>Fixed</p>"},
        }

        path = tmp_path / "dispositions.json"
        save_dispositions(dispositions, path)
        loaded = load_dispositions(path)

        assert loaded["product-a"]["status"] == "approved"
        assert loaded["product-b"]["reason"] == "wrong_facts"
        assert loaded["product-c"]["edited_body_html"] == "<p>Fixed</p>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_review.py -v`
Expected: FAIL — modules not found

- [ ] **Step 3: Implement dispositions module**

```python
# lookout/review/__init__.py
# (empty)

# lookout/review/dispositions.py
"""Load and save review dispositions."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def save_dispositions(dispositions: dict, path: Path) -> None:
    """Save dispositions dict to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dispositions, indent=2))


def load_dispositions(path: Path) -> dict:
    """Load dispositions from JSON file."""
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def apply_dispositions_to_run(run, dispositions: dict) -> None:
    """Apply loaded dispositions to an ApplyRun's changes."""
    from lookout.apply.models import ChangeStatus

    for change in run.changes:
        d = dispositions.get(change.handle)
        if not d:
            continue
        status = d.get("status", "")
        if status == "approved":
            change.status = ChangeStatus.APPROVED
        elif status == "rejected":
            change.status = ChangeStatus.REJECTED
            change.rejection_reason = d.get("reason", "")
        elif status == "edited":
            change.status = ChangeStatus.EDITED
            change.edited_body_html = d.get("edited_body_html", "")
```

- [ ] **Step 4: Implement report generator**

```python
# lookout/review/report.py
"""Generate HTML review reports for enrichment runs."""

from __future__ import annotations

import html
import logging
from pathlib import Path

from lookout.apply.models import ApplyRun

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def generate_review_report(run: ApplyRun, output_path: Path) -> None:
    """Generate an HTML review report showing side-by-side diffs.

    The report includes current vs proposed description for each product,
    with approve/reject/edit controls that save to a dispositions JSON file.
    """
    template_path = TEMPLATE_DIR / "review.html"
    if template_path.exists():
        template = template_path.read_text()
    else:
        template = _FALLBACK_TEMPLATE

    products_html = []
    for change in run.changes:
        current = html.escape(change.current_body_html or "(empty)")
        proposed = html.escape(
            change.new_body_html or "(no change)"
        )
        products_html.append(
            _PRODUCT_TEMPLATE.format(
                handle=change.handle,
                title=html.escape(change.title),
                vendor=html.escape(change.vendor),
                confidence=change.confidence,
                current=current,
                proposed=proposed,
                product_id=change.product_id,
            )
        )

    output = template.format(
        run_id=run.run_id,
        product_count=len(run.changes),
        products="\n".join(products_html),
        dispositions_filename=f"{run.run_id}_dispositions.json",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output)
    logger.info("Review report written to %s", output_path)


_PRODUCT_TEMPLATE = """
<div class="product" data-handle="{handle}" data-product-id="{product_id}">
  <div class="product-header">
    <h3>{title}</h3>
    <span class="vendor">{vendor}</span>
    <span class="confidence">Confidence: {confidence}%</span>
    <span class="handle">{handle}</span>
  </div>
  <div class="comparison">
    <div class="side current">
      <h4>Current</h4>
      <div class="content">{current}</div>
    </div>
    <div class="side proposed">
      <h4>Proposed</h4>
      <div class="content">{proposed}</div>
    </div>
  </div>
  <div class="actions">
    <label><input type="radio" name="disposition-{handle}" value="approved" /> Approve</label>
    <label><input type="radio" name="disposition-{handle}" value="rejected" /> Reject</label>
    <select class="rejection-reason" style="display:none">
      <option value="">Select reason...</option>
      <option value="wrong_facts">Wrong facts</option>
      <option value="bad_structure">Bad structure</option>
      <option value="wrong_image">Wrong image</option>
      <option value="tone">Wrong tone</option>
      <option value="incomplete">Incomplete</option>
      <option value="other">Other</option>
    </select>
  </div>
</div>
"""

_FALLBACK_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<title>Enrichment Review: {run_id}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }}
  .product {{ border: 1px solid #ddd; border-radius: 8px; margin: 20px 0; padding: 20px; }}
  .product-header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }}
  .product-header h3 {{ margin: 0; }}
  .vendor {{ color: #666; }}
  .confidence {{ background: #e8f5e9; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }}
  .handle {{ color: #999; font-family: monospace; font-size: 0.85em; }}
  .comparison {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .side {{ border: 1px solid #eee; border-radius: 4px; padding: 12px; }}
  .side h4 {{ margin: 0 0 8px 0; color: #666; }}
  .current {{ background: #fff5f5; }}
  .proposed {{ background: #f5fff5; }}
  .content {{ white-space: pre-wrap; font-size: 0.9em; line-height: 1.5; }}
  .actions {{ margin-top: 12px; display: flex; gap: 16px; align-items: center; }}
  .actions label {{ cursor: pointer; }}
  .summary {{ background: #f0f0f0; padding: 16px; border-radius: 8px; margin-bottom: 20px; }}
  #save-btn {{ background: #4CAF50; color: white; border: none; padding: 12px 24px; font-size: 16px; border-radius: 4px; cursor: pointer; position: fixed; bottom: 20px; right: 20px; }}
  #save-btn:hover {{ background: #45a049; }}
</style>
</head>
<body>
<h1>Enrichment Review: {run_id}</h1>
<div class="summary">
  <strong>{product_count} products</strong> to review.
  Approve, reject (with reason), or skip each product.
  Then click Save to export your dispositions.
</div>

{products}

<button id="save-btn" onclick="saveDispositions()">Save Dispositions</button>

<script>
// Show rejection reason dropdown when "Reject" is selected
document.querySelectorAll('input[type=radio]').forEach(radio => {{
  radio.addEventListener('change', function() {{
    const product = this.closest('.product');
    const reasonSelect = product.querySelector('.rejection-reason');
    reasonSelect.style.display = this.value === 'rejected' ? 'inline-block' : 'none';
  }});
}});

function saveDispositions() {{
  const dispositions = {{}};
  document.querySelectorAll('.product').forEach(product => {{
    const handle = product.dataset.handle;
    const checked = product.querySelector('input[type=radio]:checked');
    if (!checked) return;
    const d = {{ status: checked.value }};
    if (checked.value === 'rejected') {{
      const reason = product.querySelector('.rejection-reason').value;
      if (reason) d.reason = reason;
    }}
    dispositions[handle] = d;
  }});

  const blob = new Blob([JSON.stringify(dispositions, null, 2)], {{ type: 'application/json' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = '{dispositions_filename}';
  a.click();
  URL.revokeObjectURL(url);
}}
</script>
</body>
</html>"""
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_review.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add lookout/review/ tests/test_review.py
git commit --no-gpg-sign -m "feat: add review report generator with side-by-side diffs and disposition export"
```

---

## Task 7: Feedback Collector

**Files:**
- Create: `lookout/feedback/__init__.py`
- Create: `lookout/feedback/collector.py`
- Test: `tests/test_feedback.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_feedback.py
"""Tests for the feedback collection system."""

import json
from pathlib import Path

from lookout.apply.models import ApplyRun, ChangeStatus, ProductChange
from lookout.feedback.collector import collect_feedback, FeedbackEntry


class TestCollectFeedback:
    def test_rejected_creates_feedback_entry(self):
        changes = [
            ProductChange(
                handle="bad-product", product_id=1, title="Bad", vendor="V",
                new_body_html="<p>Generated</p>",
                status=ChangeStatus.REJECTED,
                rejection_reason="wrong_facts",
                confidence=80,
            ),
        ]
        run = ApplyRun(run_id="test", changes=changes)
        entries = collect_feedback(run)

        assert len(entries) == 1
        assert entries[0].handle == "bad-product"
        assert entries[0].disposition == "rejected"
        assert entries[0].reason == "wrong_facts"

    def test_edited_captures_diff(self):
        changes = [
            ProductChange(
                handle="fixed-product", product_id=2, title="Fixed", vendor="V",
                new_body_html="<p>Generated version</p>",
                edited_body_html="<p>Human-corrected version</p>",
                status=ChangeStatus.EDITED,
                confidence=70,
            ),
        ]
        run = ApplyRun(run_id="test", changes=changes)
        entries = collect_feedback(run)

        assert len(entries) == 1
        assert entries[0].disposition == "edited"
        assert entries[0].generated_html == "<p>Generated version</p>"
        assert entries[0].final_html == "<p>Human-corrected version</p>"

    def test_approved_creates_positive_feedback(self):
        changes = [
            ProductChange(
                handle="good-product", product_id=3, title="Good", vendor="V",
                new_body_html="<p>Perfect description</p>",
                status=ChangeStatus.APPROVED,
                confidence=95,
            ),
        ]
        run = ApplyRun(run_id="test", changes=changes)
        entries = collect_feedback(run)

        assert len(entries) == 1
        assert entries[0].disposition == "approved"

    def test_save_feedback_to_dir(self, tmp_path):
        entries = [
            FeedbackEntry(
                handle="test", run_id="run-1", disposition="rejected",
                reason="wrong_facts", generated_html="<p>Bad</p>",
                confidence=60,
            ),
        ]
        from lookout.feedback.collector import save_feedback
        save_feedback(entries, tmp_path)

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["handle"] == "test"
        assert data["disposition"] == "rejected"

    def test_pending_skipped(self):
        changes = [
            ProductChange(handle="skip", product_id=4, title="Skip", vendor="V",
                          status=ChangeStatus.PENDING),
        ]
        run = ApplyRun(run_id="test", changes=changes)
        entries = collect_feedback(run)
        assert len(entries) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_feedback.py -v`
Expected: FAIL — modules not found

- [ ] **Step 3: Implement feedback collector**

```python
# lookout/feedback/__init__.py
# (empty)

# lookout/feedback/collector.py
"""Collect review dispositions as structured feedback for optimization loops.

Approved products become positive training examples.
Rejected products (with reasons) become negative examples / regression tests.
Edited products capture the gap between generated and human-corrected output.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from lookout.apply.models import ApplyRun, ChangeStatus

logger = logging.getLogger(__name__)


@dataclass
class FeedbackEntry:
    """A single piece of human feedback on generated output."""

    handle: str
    run_id: str
    disposition: str  # "approved", "rejected", "edited"
    reason: str = ""  # rejection reason tag
    generated_html: str = ""
    final_html: str = ""  # what was actually used (= generated for approved, edited for edited)
    confidence: int = 0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


def collect_feedback(run: ApplyRun) -> list[FeedbackEntry]:
    """Extract feedback entries from a reviewed ApplyRun.

    Processes all non-pending changes and creates structured feedback.
    """
    entries = []

    for change in run.changes:
        if change.status == ChangeStatus.PENDING:
            continue

        if change.status == ChangeStatus.APPROVED:
            entries.append(FeedbackEntry(
                handle=change.handle,
                run_id=run.run_id,
                disposition="approved",
                generated_html=change.new_body_html or "",
                final_html=change.new_body_html or "",
                confidence=change.confidence,
            ))

        elif change.status == ChangeStatus.REJECTED:
            entries.append(FeedbackEntry(
                handle=change.handle,
                run_id=run.run_id,
                disposition="rejected",
                reason=change.rejection_reason,
                generated_html=change.new_body_html or "",
                confidence=change.confidence,
            ))

        elif change.status == ChangeStatus.EDITED:
            entries.append(FeedbackEntry(
                handle=change.handle,
                run_id=run.run_id,
                disposition="edited",
                generated_html=change.new_body_html or "",
                final_html=change.edited_body_html or "",
                confidence=change.confidence,
            ))

        elif change.status in (ChangeStatus.APPLIED, ChangeStatus.REVERTED, ChangeStatus.FAILED):
            # Applied/reverted/failed still carry their original disposition
            disp = "approved"
            if change.rejection_reason:
                disp = "rejected"
            elif change.edited_body_html:
                disp = "edited"
            entries.append(FeedbackEntry(
                handle=change.handle,
                run_id=run.run_id,
                disposition=disp,
                reason=change.rejection_reason,
                generated_html=change.new_body_html or "",
                final_html=change.edited_body_html or change.new_body_html or "",
                confidence=change.confidence,
            ))

    return entries


def save_feedback(entries: list[FeedbackEntry], feedback_dir: Path) -> None:
    """Save feedback entries to individual JSON files."""
    feedback_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        filename = f"{entry.handle}_{entry.run_id}_{entry.disposition}.json"
        path = feedback_dir / filename
        path.write_text(json.dumps(asdict(entry), indent=2))

    logger.info("Saved %d feedback entries to %s", len(entries), feedback_dir)


def load_all_feedback(feedback_dir: Path) -> list[FeedbackEntry]:
    """Load all feedback entries from a directory."""
    entries = []
    for path in sorted(feedback_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            entries.append(FeedbackEntry(**data))
        except Exception as e:
            logger.warning("Skipping %s: %s", path, e)
    return entries


def feedback_summary(entries: list[FeedbackEntry]) -> dict:
    """Summarize feedback entries."""
    total = len(entries)
    approved = sum(1 for e in entries if e.disposition == "approved")
    rejected = sum(1 for e in entries if e.disposition == "rejected")
    edited = sum(1 for e in entries if e.disposition == "edited")

    # Rejection reason breakdown
    reasons: dict[str, int] = {}
    for e in entries:
        if e.disposition == "rejected" and e.reason:
            reasons[e.reason] = reasons.get(e.reason, 0) + 1

    return {
        "total": total,
        "approved": approved,
        "rejected": rejected,
        "edited": edited,
        "approval_rate": approved / max(total, 1),
        "rejection_reasons": reasons,
    }
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_feedback.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/feedback/ tests/test_feedback.py
git commit --no-gpg-sign -m "feat: add feedback collector — captures approve/reject/edit dispositions for loop optimization"
```

---

## Task 8: CLI Commands

**Files:**
- Modify: `lookout/cli.py`
- Test: manual verification

- [ ] **Step 1: Add `enrich review` command**

Add to `lookout/cli.py` in the `enrich` group:

```python
@enrich.command("review")
@click.option("--run-dir", "-d", required=True, type=click.Path(exists=True, path_type=Path),
              help="Enrichment output directory to review")
@click.option("--out", "-o", type=click.Path(path_type=Path), default=None,
              help="Output HTML report path (default: {run-dir}/review.html)")
@click.option("--verbose", is_flag=True)
def review(run_dir, out, verbose):
    """Generate a review report for enrichment output.

    Creates an HTML file with side-by-side current vs proposed descriptions.
    Open it in a browser, approve/reject each product, then save dispositions.
    """
    setup_logging(verbose)
    from lookout.apply.models import ApplyRun, ProductChange
    from lookout.enrich.models import MerchOutput
    from lookout.enrich.scorer import load_artifacts
    from lookout.review.report import generate_review_report
    from lookout.store import LookoutStore

    store = LookoutStore()
    run_dir = Path(run_dir)

    # Build changes from enrichment artifacts
    changes = []
    for handle_dir in sorted(run_dir.iterdir()):
        if not handle_dir.is_dir():
            continue
        merch_path = handle_dir / "merch_output.json"
        if not merch_path.exists():
            continue

        import json
        merch = MerchOutput(**json.loads(merch_path.read_text()))

        # Look up current state from store
        product = store.get_product_by_handle(merch.handle)
        if not product:
            console.print(f"[yellow]Skipping {merch.handle}: not found in store[/yellow]")
            continue

        changes.append(ProductChange(
            handle=merch.handle,
            product_id=product["id"],
            title=product.get("title", ""),
            vendor=product.get("vendor", ""),
            current_body_html=product.get("body_html", ""),
            new_body_html=merch.body_html,
            confidence=merch.confidence,
        ))

    run_id = run_dir.name
    run = ApplyRun(run_id=run_id, source_dir=str(run_dir), changes=changes)

    output_path = out or (run_dir / "review.html")
    generate_review_report(run, output_path)
    console.print(f"Review report: [green]{output_path}[/green] ({len(changes)} products)")
    console.print("Open in your browser, review each product, then Save Dispositions.")
```

- [ ] **Step 2: Add `enrich apply` command**

```python
@enrich.command("apply")
@click.option("--run-dir", "-d", required=True, type=click.Path(exists=True, path_type=Path),
              help="Enrichment output directory")
@click.option("--dispositions", "-r", required=True, type=click.Path(exists=True, path_type=Path),
              help="Dispositions JSON from review")
@click.option("--backup-dir", type=click.Path(path_type=Path), default=Path("./backups"),
              help="Directory for pre-write backups")
@click.option("--dry-run", is_flag=True, help="Show what would be applied without writing")
@click.option("--verbose", is_flag=True)
def apply(run_dir, dispositions, backup_dir, dry_run, verbose):
    """Apply approved enrichment changes to Shopify.

    Reads dispositions from the review step, backs up current state,
    then writes approved/edited descriptions to Shopify via API.
    """
    setup_logging(verbose)
    import json as json_mod
    from lookout.apply.models import ApplyRun, ProductChange, ChangeStatus
    from lookout.apply.writer import apply_run
    from lookout.enrich.models import MerchOutput
    from lookout.feedback.collector import collect_feedback, save_feedback
    from lookout.review.dispositions import load_dispositions, apply_dispositions_to_run
    from lookout.store import LookoutStore

    store = LookoutStore()

    # Build changes (same as review command)
    changes = []
    for handle_dir in sorted(Path(run_dir).iterdir()):
        if not handle_dir.is_dir():
            continue
        merch_path = handle_dir / "merch_output.json"
        if not merch_path.exists():
            continue
        merch = MerchOutput(**json_mod.loads(merch_path.read_text()))
        product = store.get_product_by_handle(merch.handle)
        if not product:
            continue
        changes.append(ProductChange(
            handle=merch.handle, product_id=product["id"],
            title=product.get("title", ""), vendor=product.get("vendor", ""),
            current_body_html=product.get("body_html", ""),
            new_body_html=merch.body_html, confidence=merch.confidence,
        ))

    run = ApplyRun(run_id=Path(run_dir).name, source_dir=str(run_dir), changes=changes)

    # Apply dispositions
    disps = load_dispositions(Path(dispositions))
    apply_dispositions_to_run(run, disps)

    approved = run.approved
    rejected = run.rejected

    console.print(f"Approved: [green]{len(approved)}[/green]  Rejected: [red]{len(rejected)}[/red]  Pending: {len(run.pending)}")

    if dry_run:
        console.print("\n[yellow]DRY RUN — no changes will be made[/yellow]")
        for c in approved:
            label = "EDITED" if c.status == ChangeStatus.EDITED else "APPROVED"
            console.print(f"  [{label}] {c.handle} ({c.vendor})")
        return

    if not approved:
        console.print("[yellow]No approved changes to apply.[/yellow]")
    else:
        # Apply to Shopify
        from tvr.mcp.api import ShopifyAdminAPI
        api = ShopifyAdminAPI()
        asyncio.run(apply_run(run, api, Path(backup_dir)))

        applied = run.applied
        failed = [c for c in run.changes if c.status == ChangeStatus.FAILED]
        console.print(f"\nApplied: [green]{len(applied)}[/green]  Failed: [red]{len(failed)}[/red]")

        for c in failed:
            console.print(f"  [red]FAILED[/red] {c.handle}: {c.error}")

    # Collect and save feedback (from ALL dispositions, not just applied)
    feedback_entries = collect_feedback(run)
    if feedback_entries:
        feedback_dir = Path(run_dir) / "feedback"
        save_feedback(feedback_entries, feedback_dir)
        console.print(f"\nFeedback saved: {len(feedback_entries)} entries to {feedback_dir}")
```

- [ ] **Step 3: Add `enrich revert` command**

```python
@enrich.command("revert")
@click.option("--handle", "-h", "handles", multiple=True, help="Product handles to revert (repeatable)")
@click.option("--run-dir", "-d", type=click.Path(exists=True, path_type=Path), help="Revert all products from a run")
@click.option("--backup-dir", type=click.Path(exists=True, path_type=Path), default=Path("./backups"),
              help="Directory containing backups")
@click.option("--verbose", is_flag=True)
def revert_cmd(handles, run_dir, backup_dir, verbose):
    """Revert applied enrichment changes from backup.

    Restores the previous Shopify state for specified products.
    """
    setup_logging(verbose)
    from lookout.apply.revert import revert_change

    if not handles and not run_dir:
        console.print("[red]Provide --handle or --run-dir[/red]")
        sys.exit(1)

    if run_dir:
        # Find all applied products from run feedback
        feedback_dir = Path(run_dir) / "feedback"
        if feedback_dir.exists():
            import json as json_mod
            for f in feedback_dir.glob("*_approved.json"):
                data = json_mod.loads(f.read_text())
                handles = list(handles) + [data["handle"]]

    from tvr.mcp.api import ShopifyAdminAPI
    api = ShopifyAdminAPI()

    reverted = 0
    for handle in handles:
        success = asyncio.run(revert_change(handle, Path(backup_dir), api))
        if success:
            console.print(f"  [green]Reverted[/green] {handle}")
            reverted += 1
        else:
            console.print(f"  [red]No backup[/red] {handle}")

    console.print(f"\nReverted {reverted}/{len(handles)} products")
```

- [ ] **Step 4: Add `enrich feedback` command**

```python
@enrich.command("feedback")
@click.option("--feedback-dir", "-d", type=click.Path(exists=True, path_type=Path),
              default=None, help="Feedback directory to summarize")
@click.option("--all-runs", is_flag=True, help="Aggregate feedback across all campaign runs")
@click.option("--verbose", is_flag=True)
def feedback_cmd(feedback_dir, all_runs, verbose):
    """Show feedback summary from review dispositions.

    Displays approval rate, rejection reasons, and trends over time.
    """
    setup_logging(verbose)
    from lookout.feedback.collector import load_all_feedback, feedback_summary

    if all_runs:
        from pathlib import Path as P
        all_entries = []
        for run_dir in sorted(P("campaign").glob("run_*/feedback")):
            all_entries.extend(load_all_feedback(run_dir))
        entries = all_entries
    elif feedback_dir:
        entries = load_all_feedback(Path(feedback_dir))
    else:
        console.print("[red]Provide --feedback-dir or --all-runs[/red]")
        sys.exit(1)

    if not entries:
        console.print("[yellow]No feedback entries found.[/yellow]")
        return

    summary = feedback_summary(entries)

    table = Table(title="Feedback Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total reviewed", str(summary["total"]))
    table.add_row("Approved", f"[green]{summary['approved']}[/green]")
    table.add_row("Rejected", f"[red]{summary['rejected']}[/red]")
    table.add_row("Edited", f"[yellow]{summary['edited']}[/yellow]")
    table.add_row("Approval rate", f"{summary['approval_rate']:.0%}")
    console.print(table)

    if summary["rejection_reasons"]:
        rtable = Table(title="Rejection Reasons")
        rtable.add_column("Reason", style="cyan")
        rtable.add_column("Count", style="red")
        for reason, count in sorted(summary["rejection_reasons"].items(), key=lambda x: -x[1]):
            rtable.add_row(reason, str(count))
        console.print(rtable)
```

- [ ] **Step 5: Add `get_product_by_handle` to LookoutStore**

Add to `lookout/store.py`:

```python
def get_product_by_handle(self, handle: str) -> dict | None:
    """Look up a product by handle."""
    products = self.list_products()
    for p in products:
        if p.get("handle") == handle:
            return p
    return None
```

- [ ] **Step 6: Run tests and verify CLI**

Run: `.venv/bin/python -m pytest tests/ -q`
Then: `.venv/bin/lookout enrich review --help`
Then: `.venv/bin/lookout enrich apply --help`
Then: `.venv/bin/lookout enrich revert --help`
Then: `.venv/bin/lookout enrich feedback --help`

- [ ] **Step 7: Commit**

```bash
git add lookout/cli.py lookout/store.py
git commit --no-gpg-sign -m "feat: add enrich review/apply/revert/feedback CLI commands"
```

---

## Workflow Summary

After all tasks are complete, the full workflow is:

```bash
# 1. Run enrichment (existing)
lookout enrich run -v "Jones Snowboards" -o campaign/run_4

# 2. Generate review report
lookout enrich review -d campaign/run_4
# → Opens review.html in browser

# 3. Review in browser: approve/reject/edit each product
# → Saves dispositions JSON

# 4. Apply approved changes (with --dry-run first)
lookout enrich apply -d campaign/run_4 -r campaign/run_4/run_4_dispositions.json --dry-run
lookout enrich apply -d campaign/run_4 -r campaign/run_4/run_4_dispositions.json

# 5. If something goes wrong
lookout enrich revert -h jones-snowboard-handle

# 6. Review feedback over time
lookout enrich feedback --all-runs
```
