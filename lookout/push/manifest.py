"""Push manifest — tracks what was pushed and what to undo."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


# -----------------------------------------------------------------------------
# Snapshot / Before Models
# -----------------------------------------------------------------------------


class ImageSnapshot(BaseModel):
    """Snapshot of a Shopify image before push."""

    id: int
    src: str
    position: int
    alt: str
    variant_ids: list[int] = Field(default_factory=list)


class ProductBefore(BaseModel):
    """Pre-push state of a product."""

    body_html: str | None = None
    images: list[ImageSnapshot] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Pushed Models
# -----------------------------------------------------------------------------


class CreatedImage(BaseModel):
    """Record of an image created during push."""

    id: int
    src_url: str
    alt: str
    variant_ids: list[int] = Field(default_factory=list)
    color: str = ""


class ProductPushed(BaseModel):
    """What was pushed for a product."""

    body_html: str | None = None
    images_created: list[CreatedImage] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Product Manifest
# -----------------------------------------------------------------------------


class ProductManifest(BaseModel):
    """Combined before/after record for a single product."""

    product_id: int
    before: ProductBefore = Field(default_factory=ProductBefore)
    pushed: ProductPushed = Field(default_factory=ProductPushed)


# -----------------------------------------------------------------------------
# Push Summary & Manifest
# -----------------------------------------------------------------------------


class PushSummary(BaseModel):
    """Aggregate counts for a push run."""

    products_pushed: int = 0
    images_created: int = 0
    images_skipped: int = 0
    descriptions_updated: int = 0
    failed: int = 0


class PushManifest(BaseModel):
    """Top-level manifest for a push run."""

    run_id: str
    pushed_at: datetime
    dispositions_path: str
    summary: PushSummary = Field(default_factory=PushSummary)
    products: dict[str, ProductManifest] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Read / Write Helpers
# -----------------------------------------------------------------------------


def save_manifest(manifest: PushManifest, output_dir: Path) -> Path:
    """Save manifest to output_dir/push-manifests/{run_id}_{timestamp}.json.

    Creates the push-manifests directory if it doesn't exist.
    Returns the path to the written file.
    """
    manifests_dir = output_dir / "push-manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    timestamp = manifest.pushed_at.strftime("%Y%m%dT%H%M")
    filename = f"{manifest.run_id}_{timestamp}.json"
    path = manifests_dir / filename

    path.write_text(manifest.model_dump_json(indent=2))
    return path


def load_manifest(path: Path) -> PushManifest:
    """Load a PushManifest from a JSON file."""
    data = json.loads(path.read_text())
    return PushManifest.model_validate(data)
