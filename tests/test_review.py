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
