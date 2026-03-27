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
