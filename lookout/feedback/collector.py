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
