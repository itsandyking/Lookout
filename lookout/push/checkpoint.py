"""Dolt checkpoint — commit + tag before Shopify push.

Creates a labeled snapshot in the Dolt database on Pi5 so that
pushes can be reverted to a known-good state if something goes wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime

from tvr.db.store import ShopifyStore

logger = logging.getLogger(__name__)


class CheckpointError(Exception):
    """Raised when a Dolt checkpoint cannot be created."""


def create_dolt_checkpoint(db_url: str, run_id: str) -> str:
    """Create a Dolt commit + tag as a pre-push checkpoint.

    Args:
        db_url: SQLAlchemy connection string for the Dolt server.
        run_id: Push run identifier (e.g., "enrich-20260406").

    Returns:
        The tag name created (e.g., "pre-push/enrich-20260406_143022").

    Raises:
        CheckpointError: If the checkpoint cannot be created.
    """
    timestamp = datetime.now().strftime("%H%M%S")
    tag_name = f"pre-push/{run_id}_{timestamp}"

    try:
        store = ShopifyStore(db_url)
    except Exception as e:
        raise CheckpointError(f"Cannot connect to Dolt: {e}") from e

    commit_msg = f"lookout: pre-push checkpoint {run_id}"

    with store.session() as session:
        # Step 1: Commit any uncommitted changes.
        # Uses exec_driver_sql so plain SQL strings pass through the MySQL
        # wire protocol directly — appropriate for Dolt stored procedures.
        try:
            session.execute(
                "CALL dolt_commit('-Am', :msg)",
                {"msg": commit_msg},
            )
            logger.info("Dolt commit created for checkpoint %s", run_id)
        except Exception as e:
            if "nothing to commit" in str(e).lower():
                logger.info("Dolt: nothing to commit (livesync is current)")
            else:
                raise CheckpointError(f"Dolt commit failed: {e}") from e

        # Step 2: Tag the current HEAD
        try:
            session.execute(
                "CALL dolt_tag(:tag, 'HEAD')",
                {"tag": tag_name},
            )
            logger.info("Dolt tag created: %s", tag_name)
        except Exception as e:
            raise CheckpointError(f"Dolt tag failed: {e}") from e

    return tag_name
