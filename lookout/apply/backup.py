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
