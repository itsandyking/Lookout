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


from lookout.apply.revert import revert_change


class TestRevert:
    def test_revert_restores_from_backup(self, tmp_path):
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
