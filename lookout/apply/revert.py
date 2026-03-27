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
