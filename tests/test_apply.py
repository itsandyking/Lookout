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
