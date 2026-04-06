"""Tests for lookout.push.manifest — round-trip, dir creation, file naming."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lookout.push.manifest import (
    CreatedImage,
    ImageSnapshot,
    ProductBefore,
    ProductManifest,
    ProductPushed,
    PushManifest,
    PushSummary,
    load_manifest,
    save_manifest,
)


@pytest.fixture
def sample_manifest() -> PushManifest:
    """Build a realistic manifest for testing."""
    return PushManifest(
        run_id="enrich-20260406",
        pushed_at=datetime(2026, 4, 6, 18, 30, 0, tzinfo=timezone.utc),
        dispositions_path="dispositions_v2.json",
        summary=PushSummary(
            products_pushed=1,
            images_created=2,
            images_skipped=1,
            descriptions_updated=0,
            failed=0,
        ),
        products={
            "patagonia-black-hole-duffel": ProductManifest(
                product_id=8396214993143,
                before=ProductBefore(
                    body_html="<div>Old description</div>",
                    images=[
                        ImageSnapshot(
                            id=61790878007543,
                            src="https://cdn.shopify.com/old.jpg",
                            position=1,
                            alt="Old image",
                            variant_ids=[123, 456],
                        )
                    ],
                ),
                pushed=ProductPushed(
                    body_html=None,
                    images_created=[
                        CreatedImage(
                            id=62672937877751,
                            src_url="https://vendor.com/black.jpg",
                            alt="Black Hole Duffel - Black",
                            variant_ids=[789],
                            color="Black",
                        ),
                        CreatedImage(
                            id=62672937877752,
                            src_url="https://vendor.com/blue.jpg",
                            alt="Black Hole Duffel - Cobalt Blue",
                            variant_ids=[790],
                            color="Cobalt Blue",
                        ),
                    ],
                ),
            )
        },
    )


class TestRoundTrip:
    """Save then load produces identical manifest."""

    def test_round_trip_preserves_data(self, tmp_path: Path, sample_manifest: PushManifest):
        saved_path = save_manifest(sample_manifest, tmp_path)
        loaded = load_manifest(saved_path)

        assert loaded.run_id == sample_manifest.run_id
        assert loaded.pushed_at == sample_manifest.pushed_at
        assert loaded.dispositions_path == sample_manifest.dispositions_path
        assert loaded.summary == sample_manifest.summary
        assert loaded.products.keys() == sample_manifest.products.keys()

        handle = "patagonia-black-hole-duffel"
        assert loaded.products[handle].product_id == 8396214993143
        assert len(loaded.products[handle].before.images) == 1
        assert loaded.products[handle].before.images[0].variant_ids == [123, 456]
        assert len(loaded.products[handle].pushed.images_created) == 2
        assert loaded.products[handle].pushed.images_created[0].color == "Black"

    def test_round_trip_empty_manifest(self, tmp_path: Path):
        manifest = PushManifest(
            run_id="empty-run",
            pushed_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            dispositions_path="none.json",
        )
        saved_path = save_manifest(manifest, tmp_path)
        loaded = load_manifest(saved_path)

        assert loaded.run_id == "empty-run"
        assert loaded.products == {}
        assert loaded.summary.products_pushed == 0


class TestDirCreation:
    """save_manifest creates push-manifests/ if missing."""

    def test_creates_push_manifests_dir(self, tmp_path: Path, sample_manifest: PushManifest):
        manifests_dir = tmp_path / "push-manifests"
        assert not manifests_dir.exists()

        save_manifest(sample_manifest, tmp_path)
        assert manifests_dir.is_dir()

    def test_works_with_nested_output_dir(self, tmp_path: Path, sample_manifest: PushManifest):
        nested = tmp_path / "output" / "enrich-20260406"
        saved_path = save_manifest(sample_manifest, nested)
        assert saved_path.exists()
        assert saved_path.parent.name == "push-manifests"


class TestFileNaming:
    """File is named {run_id}_{timestamp}.json."""

    def test_filename_format(self, tmp_path: Path, sample_manifest: PushManifest):
        saved_path = save_manifest(sample_manifest, tmp_path)
        assert saved_path.name == "enrich-20260406_20260406T1830.json"

    def test_filename_uses_run_id(self, tmp_path: Path):
        manifest = PushManifest(
            run_id="custom-run-name",
            pushed_at=datetime(2026, 12, 25, 9, 15, 0, tzinfo=timezone.utc),
            dispositions_path="test.json",
        )
        saved_path = save_manifest(manifest, tmp_path)
        assert saved_path.name == "custom-run-name_20261225T0915.json"
