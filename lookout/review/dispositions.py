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
