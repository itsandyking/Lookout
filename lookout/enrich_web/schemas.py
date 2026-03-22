"""
Pydantic models for the web layer.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    """Status of a pipeline run."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class RunConfig(BaseModel):
    """Configuration for a pipeline run."""

    concurrency: int = Field(default=5, ge=1, le=20)
    max_rows: int | None = Field(default=None, ge=1)
    force: bool = Field(default=False)
    dry_run: bool = Field(default=False)


class RunStats(BaseModel):
    """Statistics for a pipeline run."""

    total: int = 0
    completed: int = 0
    updated: int = 0
    skipped: int = 0
    no_match: int = 0
    failed: int = 0
    warnings: int = 0


class RunMeta(BaseModel):
    """Metadata for a pipeline run stored in meta.json."""

    run_id: str
    created_at: datetime
    status: RunStatus = RunStatus.QUEUED
    config: RunConfig = Field(default_factory=RunConfig)
    stats: RunStats = Field(default_factory=RunStats)
    last_event_at: datetime | None = None
    error_message: str | None = None


class RunEvent(BaseModel):
    """An event emitted during pipeline execution."""

    type: str
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    """Summary of a run for the list view."""

    run_id: str
    created_at: datetime
    status: RunStatus
    total: int = 0
    completed: int = 0
    updated: int = 0
    failed: int = 0


class ItemResult(BaseModel):
    """Result for a single item from run_report.csv."""

    handle: str
    vendor: str
    status: str
    match_confidence: int = 0
    warnings: str = ""
    output_rows_count: int = 0
    error_message: str = ""
    processing_time_ms: int = 0


class ItemArtifacts(BaseModel):
    """Artifact data for a single item."""

    handle: str
    resolver: dict[str, Any] | None = None
    extracted_facts: dict[str, Any] | None = None
    merch_output: dict[str, Any] | None = None
    log: dict[str, Any] | None = None
    has_source_html: bool = False


class CreateRunRequest(BaseModel):
    """Request body for creating a new run."""

    concurrency: int = Field(default=5, ge=1, le=20)
    max_rows: int | None = Field(default=None, ge=1)
    force: bool = Field(default=False)
    dry_run: bool = Field(default=False)


class ValidationError(BaseModel):
    """CSV validation error."""

    row: int | None = None
    column: str | None = None
    message: str


class ValidationResult(BaseModel):
    """Result of CSV validation."""

    valid: bool
    row_count: int = 0
    errors: list[ValidationError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
