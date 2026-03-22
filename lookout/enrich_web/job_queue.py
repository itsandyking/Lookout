"""
Background job queue using Redis Queue (RQ).

Handles:
- Redis connection configuration
- Queue management
- Job enqueueing for pipeline runs
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from redis import Redis
from rq import Queue

from .schemas import RunStats, RunStatus
from .storage import (
    append_event,
    get_cancel_flag_path,
    get_input_csv_path,
    get_outputs_dir,
    get_vendors_yaml_path,
    load_run_meta,
    save_run_meta,
    update_run_stats,
    update_run_status,
)

logger = logging.getLogger(__name__)

# Default Redis URL
DEFAULT_REDIS_URL = "redis://redis:6379/0"

# Queue name
QUEUE_NAME = "merchfill"


def get_redis_url() -> str:
    """Get Redis URL from environment or default."""
    return os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)


def get_redis_connection() -> Redis:
    """Get a Redis connection."""
    return Redis.from_url(get_redis_url())


def get_queue() -> Queue:
    """Get the RQ queue for pipeline jobs."""
    return Queue(QUEUE_NAME, connection=get_redis_connection())


def enqueue_run(run_id: str) -> str | None:
    """
    Enqueue a pipeline run job.

    Args:
        run_id: The run ID to process.

    Returns:
        Job ID if enqueued successfully, None otherwise.
    """
    try:
        queue = get_queue()
        job = queue.enqueue(
            "merchfill_web.job_queue.run_job",
            run_id,
            job_timeout="2h",  # 2 hour timeout
            result_ttl=86400,  # Keep result for 24 hours
        )
        logger.info(f"Enqueued job {job.id} for run {run_id}")
        return job.id
    except Exception as e:
        logger.error(f"Failed to enqueue job for run {run_id}: {e}")
        return None


def run_job(run_id: str) -> dict[str, Any]:
    """
    Execute a pipeline run job.

    This is the main job function that RQ workers execute.

    Args:
        run_id: The run ID to process.

    Returns:
        Dictionary with job results.
    """
    logger.info(f"Starting job for run {run_id}")

    # Load run metadata
    meta = load_run_meta(run_id)
    if not meta:
        logger.error(f"Run {run_id} not found")
        return {"success": False, "error": "Run not found"}

    # Check if already processed or cancelled
    if meta.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
        logger.warning(f"Run {run_id} is not in a runnable state: {meta.status}")
        return {"success": False, "error": f"Run is {meta.status.value}"}

    # Update status to RUNNING
    update_run_status(run_id, RunStatus.RUNNING)

    # Get paths
    input_path = get_input_csv_path(run_id)
    vendors_path = get_vendors_yaml_path(run_id)
    outputs_dir = get_outputs_dir(run_id)
    cancel_flag_path = get_cancel_flag_path(run_id)

    if not input_path:
        update_run_status(run_id, RunStatus.FAILED, "Input CSV not found")
        return {"success": False, "error": "Input CSV not found"}

    if not vendors_path:
        # Fall back to default vendors.yaml
        default_vendors = Path(os.environ.get("VENDORS_YAML_PATH", "./vendors.yaml"))
        if not default_vendors.exists():
            update_run_status(run_id, RunStatus.FAILED, "vendors.yaml not found")
            return {"success": False, "error": "vendors.yaml not found"}
        vendors_path = default_vendors

    # Create event callback
    stats = RunStats()

    def event_callback(event: dict[str, Any]) -> None:
        """Handle pipeline events."""
        nonlocal stats

        # Append to events log
        append_event(run_id, event)

        event_type = event.get("type", "")

        if event_type == "RUN_STARTED":
            stats.total = event.get("total", 0)
            update_run_stats(run_id, stats)

        elif event_type == "ITEM_DONE":
            stats.completed += 1
            status = event.get("status", "")
            if status == "UPDATED":
                stats.updated += 1
            elif status in ("SKIPPED", "SKIPPED_NO_GAPS", "SKIPPED_VENDOR_NOT_CONFIGURED"):
                stats.skipped += 1
            elif status == "NO_MATCH":
                stats.no_match += 1

            if event.get("warnings_count", 0) > 0:
                stats.warnings += event.get("warnings_count", 0)

            update_run_stats(run_id, stats)

        elif event_type == "ITEM_FAILED":
            stats.completed += 1
            stats.failed += 1
            update_run_stats(run_id, stats)

        elif event_type == "RUN_DONE":
            # Final stats update
            stats.total = event.get("total", stats.total)
            stats.updated = event.get("updated", stats.updated)
            stats.skipped = event.get("skipped", stats.skipped)
            stats.no_match = event.get("no_match", stats.no_match)
            stats.failed = event.get("failed", stats.failed)
            update_run_stats(run_id, stats)

    # Import pipeline components
    from merchfill.pipeline import PipelineConfig, run_pipeline

    # Create pipeline config
    config = PipelineConfig(
        input_path=input_path,
        output_dir=outputs_dir,
        vendors_path=vendors_path,
        concurrency=meta.config.concurrency,
        max_rows=meta.config.max_rows,
        force=meta.config.force,
        dry_run=meta.config.dry_run,
    )

    try:
        # Run the pipeline
        asyncio.run(
            run_pipeline(
                config,
                event_cb=event_callback,
                cancel_flag_path=cancel_flag_path,
            )
        )

        # Check if cancelled
        if cancel_flag_path.exists():
            update_run_status(run_id, RunStatus.CANCELED)
            logger.info(f"Run {run_id} was cancelled")
            return {"success": True, "cancelled": True}

        # Update status to DONE
        update_run_status(run_id, RunStatus.DONE)
        logger.info(f"Run {run_id} completed successfully")
        return {"success": True, "stats": stats.model_dump()}

    except Exception as e:
        logger.exception(f"Run {run_id} failed with error: {e}")
        update_run_status(run_id, RunStatus.FAILED, str(e))
        return {"success": False, "error": str(e)}
