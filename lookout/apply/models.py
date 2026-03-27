"""Data models for the enrichment apply pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class ChangeStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    APPLIED = "applied"
    REVERTED = "reverted"
    FAILED = "failed"


@dataclass
class ProductChange:
    """A proposed change to a single product."""

    handle: str
    product_id: int
    title: str
    vendor: str

    # Proposed changes (None = no change)
    new_body_html: str | None = None
    new_images: list[dict] | None = None
    new_variant_image_map: dict | None = None

    # Current state (populated during backup)
    current_body_html: str | None = None
    current_images: list[dict] | None = None

    # Review disposition
    status: ChangeStatus = ChangeStatus.PENDING
    rejection_reason: str = ""
    edited_body_html: str | None = None
    confidence: int = 0

    # Display metadata (not persisted)
    variant_labels: list[str] = field(default_factory=list)
    inventory_count: int = 0
    inventory_value: float = 0.0  # cost on hand
    missing_fields: list[str] = field(default_factory=list)  # e.g. ["product_type", "tags", "google_shopping"]

    # Apply tracking
    applied_at: str | None = None
    reverted_at: str | None = None
    error: str | None = None


@dataclass
class ApplyRun:
    """A batch of product changes with metadata."""

    run_id: str
    created_at: str = ""
    source_dir: str = ""
    changes: list[ProductChange] = field(default_factory=list)

    @property
    def pending(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status == ChangeStatus.PENDING]

    @property
    def approved(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status in (ChangeStatus.APPROVED, ChangeStatus.EDITED)]

    @property
    def rejected(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status == ChangeStatus.REJECTED]

    @property
    def applied(self) -> list[ProductChange]:
        return [c for c in self.changes if c.status == ChangeStatus.APPLIED]

    def summary(self) -> dict:
        return {
            "total": len(self.changes),
            "pending": len(self.pending),
            "approved": len(self.approved),
            "rejected": len(self.rejected),
            "applied": len(self.applied),
            "approval_rate": len(self.approved) / max(len(self.changes) - len(self.pending), 1),
        }
