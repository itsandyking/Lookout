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
