#!/usr/bin/env python3
"""
RQ Worker entrypoint for processing pipeline jobs.

Usage:
    python -m lookout.enrich_web.worker

Or via the worker script:
    rq worker lookout
"""

import logging
import os
import sys

from redis import Redis
from rq import Worker

from .job_queue import QUEUE_NAME, get_redis_connection

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> None:
    """Start the RQ worker."""
    logger.info(f"Starting worker for queue: {QUEUE_NAME}")

    redis_conn = get_redis_connection()

    # Create worker with the lookout queue
    worker = Worker(
        queues=[QUEUE_NAME],
        connection=redis_conn,
        name=f"lookout-worker-{os.getpid()}",
    )

    logger.info("Worker started, waiting for jobs...")

    # Start working
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
