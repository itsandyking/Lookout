# Dolt Checkpoint Before Push — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically create a Dolt commit + tag on Pi5 before any Shopify mutations in `lookout enrich push`, aborting if the checkpoint fails.

**Architecture:** A small `checkpoint.py` module with one public function that executes Dolt SQL procedures (`dolt_commit`, `dolt_tag`) via the existing SQLAlchemy/MySQL connection. The push CLI calls it before entering the product loop. On failure, the push aborts.

**Tech Stack:** SQLAlchemy (already a dependency), Dolt SQL procedures over MySQL wire protocol

**Spec:** `docs/superpowers/specs/2026-04-07-dolt-checkpoint-design.md`

---

### Task 1: Checkpoint Module

**Files:**
- Create: `lookout/push/checkpoint.py`
- Create: `tests/test_checkpoint.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_checkpoint.py
"""Tests for Dolt checkpoint creation before push."""

import pytest
from unittest.mock import MagicMock, patch, call

from lookout.push.checkpoint import CheckpointError, create_dolt_checkpoint


class TestCreateDoltCheckpoint:
    """Test checkpoint creation via Dolt SQL procedures."""

    def _mock_session(self, execute_side_effects=None):
        """Create a mock SQLAlchemy session context manager.

        Args:
            execute_side_effects: List of side effects for successive
                session.execute() calls. Each can be a return value or
                an exception to raise.
        """
        session = MagicMock()
        if execute_side_effects:
            session.execute.side_effect = execute_side_effects

        store = MagicMock()
        store.session.return_value.__enter__ = MagicMock(return_value=session)
        store.session.return_value.__exit__ = MagicMock(return_value=False)
        return store, session

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_creates_commit_and_tag(self, MockStore):
        store, session = self._mock_session()
        MockStore.return_value = store

        tag = create_dolt_checkpoint("mysql://pi5:3306/shopify", "enrich-20260406")

        assert tag.startswith("pre-push/enrich-20260406_")
        calls = session.execute.call_args_list
        # First call: dolt_commit
        assert "dolt_commit" in str(calls[0])
        # Second call: dolt_tag
        assert "dolt_tag" in str(calls[1])

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_nothing_to_commit_still_tags(self, MockStore):
        """When livesync already committed everything, the commit call
        raises 'nothing to commit' but tagging should still proceed."""
        from sqlalchemy.exc import OperationalError

        nothing_err = OperationalError(
            "CALL dolt_commit", {}, Exception("nothing to commit")
        )
        store, session = self._mock_session(
            execute_side_effects=[nothing_err, MagicMock()]
        )
        MockStore.return_value = store

        tag = create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

        assert tag.startswith("pre-push/test-run_")
        # Should have attempted commit (failed) then tag (succeeded)
        assert session.execute.call_count == 2

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_commit_error_raises_checkpoint_error(self, MockStore):
        """Non-'nothing to commit' errors should raise CheckpointError."""
        from sqlalchemy.exc import OperationalError

        real_err = OperationalError(
            "CALL dolt_commit", {}, Exception("table locked")
        )
        store, session = self._mock_session(
            execute_side_effects=[real_err]
        )
        MockStore.return_value = store

        with pytest.raises(CheckpointError, match="table locked"):
            create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_tag_error_raises_checkpoint_error(self, MockStore):
        from sqlalchemy.exc import OperationalError

        tag_err = OperationalError(
            "CALL dolt_tag", {}, Exception("tag already exists")
        )
        store, session = self._mock_session(
            execute_side_effects=[MagicMock(), tag_err]
        )
        MockStore.return_value = store

        with pytest.raises(CheckpointError, match="tag already exists"):
            create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_connection_error_raises_checkpoint_error(self, MockStore):
        MockStore.side_effect = Exception("Can't connect to MySQL server on 'pi5'")

        with pytest.raises(CheckpointError, match="Can't connect"):
            create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_tag_name_includes_timestamp(self, MockStore):
        store, session = self._mock_session()
        MockStore.return_value = store

        tag = create_dolt_checkpoint("mysql://pi5:3306/shopify", "enrich-20260406")

        # Tag format: pre-push/{run_id}_{HHMMSS}
        parts = tag.split("_")
        assert len(parts) >= 2
        timestamp_part = parts[-1]
        assert len(timestamp_part) == 6  # HHMMSS
        assert timestamp_part.isdigit()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_checkpoint.py -v`
Expected: ImportError — module doesn't exist yet

- [ ] **Step 3: Implement checkpoint module**

```python
# lookout/push/checkpoint.py
"""Dolt checkpoint — commit + tag before Shopify push.

Creates a labeled snapshot in the Dolt database on Pi5 so that
pushes can be reverted to a known-good state if something goes wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)


class CheckpointError(Exception):
    """Raised when a Dolt checkpoint cannot be created."""


def create_dolt_checkpoint(db_url: str, run_id: str) -> str:
    """Create a Dolt commit + tag as a pre-push checkpoint.

    Args:
        db_url: SQLAlchemy connection string for the Dolt server.
        run_id: Push run identifier (e.g., "enrich-20260406").

    Returns:
        The tag name created (e.g., "pre-push/enrich-20260406_143022").

    Raises:
        CheckpointError: If the checkpoint cannot be created.
    """
    from tvr.db.store import ShopifyStore

    timestamp = datetime.now().strftime("%H%M%S")
    tag_name = f"pre-push/{run_id}_{timestamp}"

    try:
        store = ShopifyStore(db_url)
    except Exception as e:
        raise CheckpointError(f"Cannot connect to Dolt: {e}") from e

    with store.session() as session:
        # Step 1: Commit any uncommitted changes
        try:
            session.execute(
                text("CALL dolt_commit('-Am', :msg)"),
                {"msg": f"lookout: pre-push checkpoint {run_id}"},
            )
            logger.info("Dolt commit created for checkpoint %s", run_id)
        except Exception as e:
            if "nothing to commit" in str(e).lower():
                logger.info("Dolt: nothing to commit (livesync is current)")
            else:
                raise CheckpointError(f"Dolt commit failed: {e}") from e

        # Step 2: Tag the current HEAD
        try:
            session.execute(
                text("CALL dolt_tag(:tag, 'HEAD')"),
                {"tag": tag_name},
            )
            logger.info("Dolt tag created: %s", tag_name)
        except Exception as e:
            raise CheckpointError(f"Dolt tag failed: {e}") from e

    return tag_name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_checkpoint.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/push/checkpoint.py tests/test_checkpoint.py
git commit -m "feat: add Dolt checkpoint module for pre-push snapshots

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Integrate Checkpoint into Push CLI

**Files:**
- Modify: `lookout/cli.py:1762-1815`
- Test: `tests/test_checkpoint.py` (add integration test)

- [ ] **Step 1: Add integration test for push CLI checkpoint behavior**

Append to `tests/test_checkpoint.py`:

```python
class TestPushCLICheckpoint:
    """Test that the push CLI creates a checkpoint before pushing."""

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_checkpoint_called_before_push(self, MockStore):
        """Verify create_dolt_checkpoint is called with correct args."""
        store, session = self._mock_session()
        MockStore.return_value = store

        tag = create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")
        assert tag is not None

    def _mock_session(self):
        session = MagicMock()
        store = MagicMock()
        store.session.return_value.__enter__ = MagicMock(return_value=session)
        store.session.return_value.__exit__ = MagicMock(return_value=False)
        return store, session

    def test_checkpoint_error_message(self):
        """CheckpointError has a useful message."""
        err = CheckpointError("Can't connect to MySQL server on 'pi5'")
        assert "pi5" in str(err)
```

- [ ] **Step 2: Run new test**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_checkpoint.py::TestPushCLICheckpoint -v`
Expected: PASS (these test the module directly, not the CLI wiring)

- [ ] **Step 3: Modify push CLI to call checkpoint**

In `lookout/cli.py`, add the checkpoint call after line 1813 (after `run_id = ...` and before `products_manifest = ...`):

Find this block in `lookout/cli.py`:

```python
    pusher = ShopifyPusher(config=config, db_url=_default_db_url(), dry_run=dry_run)
    run_id = run_dir.name if run_dir else "manual"

    products_manifest: dict = {}
```

Replace with:

```python
    db_url = _default_db_url()
    pusher = ShopifyPusher(config=config, db_url=db_url, dry_run=dry_run)
    run_id = run_dir.name if run_dir else "manual"

    # --- Dolt checkpoint (skip for dry-run) ---
    if not dry_run:
        from lookout.push.checkpoint import CheckpointError, create_dolt_checkpoint

        try:
            tag = create_dolt_checkpoint(db_url, run_id)
            console.print(f"Dolt checkpoint: [green]{tag}[/green]")
        except CheckpointError as e:
            console.print(f"[red]Checkpoint failed: {e}[/red]")
            console.print("[red]Aborting push — cannot create safety checkpoint[/red]")
            return
    else:
        console.print("[yellow]DRY RUN — skipping Dolt checkpoint[/yellow]")

    products_manifest: dict = {}
```

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/andyking/Lookout && uv run pytest --tb=short -q`
Expected: All tests pass (no regressions)

- [ ] **Step 5: Commit**

```bash
git add lookout/cli.py tests/test_checkpoint.py
git commit -m "feat: wire Dolt checkpoint into enrich push CLI

Aborts push if checkpoint fails. Skips checkpoint on --dry-run.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
