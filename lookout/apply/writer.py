"""Write approved enrichment changes to Shopify via GraphQL."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from lookout.apply.backup import create_backup
from lookout.apply.models import ApplyRun, ChangeStatus, ProductChange

logger = logging.getLogger(__name__)


async def apply_change(
    change: ProductChange,
    api,
    backup_dir: Path | None = None,
) -> ProductChange:
    """Apply a single approved product change to Shopify.

    Only processes changes with status APPROVED or EDITED.
    Creates a backup before writing. Sets status to APPLIED or FAILED.
    """
    if change.status not in (ChangeStatus.APPROVED, ChangeStatus.EDITED):
        logger.debug("Skipping %s (status=%s)", change.handle, change.status)
        return change

    # Backup current state before writing
    if backup_dir:
        create_backup(change, backup_dir)

    # Determine what body to write
    body_html = change.edited_body_html if change.status == ChangeStatus.EDITED else change.new_body_html

    try:
        result = await api.update_product(
            product_id=change.product_id,
            body_html=body_html,
        )

        errors = result.get("userErrors", [])
        if errors:
            change.status = ChangeStatus.FAILED
            change.error = "; ".join(e.get("message", "") for e in errors)
            logger.error("Failed to apply %s: %s", change.handle, change.error)
        else:
            change.status = ChangeStatus.APPLIED
            change.applied_at = datetime.now(timezone.utc).isoformat()
            logger.info("Applied %s to Shopify", change.handle)

    except Exception as e:
        change.status = ChangeStatus.FAILED
        change.error = str(e)
        logger.error("Exception applying %s: %s", change.handle, e)

    return change


async def apply_run(
    run: ApplyRun,
    api,
    backup_dir: Path,
) -> ApplyRun:
    """Apply all approved changes in a run, sequentially.

    Each product is independent -- a failure on one does not stop others.
    """
    for change in run.changes:
        if change.status in (ChangeStatus.APPROVED, ChangeStatus.EDITED):
            await apply_change(change, api, backup_dir)

    applied = len(run.applied)
    failed = sum(1 for c in run.changes if c.status == ChangeStatus.FAILED)
    logger.info("Apply run %s complete: %d applied, %d failed", run.run_id, applied, failed)
    return run
