"""Tests for push undo module."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lookout.push.manifest import (
    CreatedImage,
    ImageSnapshot,
    ProductBefore,
    ProductManifest,
    ProductPushed,
    PushManifest,
    PushSummary,
)
from lookout.push.undo import PushUndoer


def run(coro):
    """Helper to run async tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


STORE_CONFIG = {
    "store_url": "https://test-store.myshopify.com",
    "access_token": "shpat_test_token",
    "api_version": "2024-10",
}


def _make_product_manifest(
    product_id=123,
    images_created=None,
    pushed_body_html=None,
    before_body_html=None,
    before_images=None,
):
    """Helper to build a ProductManifest for tests."""
    return ProductManifest(
        product_id=product_id,
        before=ProductBefore(body_html=before_body_html, images=before_images or []),
        pushed=ProductPushed(
            body_html=pushed_body_html,
            images_created=images_created or [],
        ),
    )


def _make_manifest(products: dict[str, ProductManifest] | None = None):
    """Helper to build a PushManifest."""
    from datetime import datetime

    return PushManifest(
        run_id="test-run",
        pushed_at=datetime(2026, 4, 6, 12, 0),
        dispositions_path="dispositions.json",
        summary=PushSummary(),
        products=products or {},
    )


def _mock_response(status_code=200, json_data=None, headers=None):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    return resp


class TestDeleteImage:
    """Test image deletion via REST API."""

    def test_success(self):
        undoer = PushUndoer(STORE_CONFIG)

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.delete = AsyncMock(return_value=_mock_response(200))
                mock_cls.return_value = mock_client

                result = run(undoer.delete_image(123, 456))

        assert result is True
        mock_client.delete.assert_called_once()
        call_url = mock_client.delete.call_args[0][0]
        assert "/products/123/images/456.json" in call_url

    def test_404_already_deleted(self):
        undoer = PushUndoer(STORE_CONFIG)

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.delete = AsyncMock(return_value=_mock_response(404))
                mock_cls.return_value = mock_client

                result = run(undoer.delete_image(123, 456))

        assert result is True

    def test_429_retry(self):
        undoer = PushUndoer(STORE_CONFIG)

        rate_resp = _mock_response(429, headers={"Retry-After": "0.1"})
        ok_resp = _mock_response(200)

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.delete = AsyncMock(side_effect=[rate_resp, ok_resp])
                mock_cls.return_value = mock_client

                result = run(undoer.delete_image(123, 456))

        assert result is True
        assert mock_client.delete.call_count == 2

    def test_500_failure(self):
        undoer = PushUndoer(STORE_CONFIG)

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.delete = AsyncMock(return_value=_mock_response(500))
                mock_cls.return_value = mock_client

                result = run(undoer.delete_image(123, 456))

        assert result is False


class TestRestoreBodyHtml:
    """Test body_html restoration via GraphQL."""

    def test_success(self):
        undoer = PushUndoer(STORE_CONFIG)

        gql_response = {
            "data": {
                "productUpdate": {
                    "product": {"id": "gid://shopify/Product/123"},
                    "userErrors": [],
                }
            }
        }

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=_mock_response(200, gql_response))
                mock_cls.return_value = mock_client

                result = run(undoer.restore_body_html(123, "<p>Original</p>"))

        assert result is True

    def test_graphql_user_errors(self):
        undoer = PushUndoer(STORE_CONFIG)

        gql_response = {
            "data": {
                "productUpdate": {
                    "product": None,
                    "userErrors": [{"field": "id", "message": "not found"}],
                }
            }
        }

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=_mock_response(200, gql_response))
                mock_cls.return_value = mock_client

                result = run(undoer.restore_body_html(123, "<p>Original</p>"))

        assert result is False


class TestRestoreVariantAssignments:
    """Test restoring variant-to-image assignments."""

    def test_restores_assignments(self):
        undoer = PushUndoer(STORE_CONFIG)

        before_images = [
            ImageSnapshot(id=50, src="https://cdn.shopify.com/a.jpg", position=1, alt="A", variant_ids=[10, 11]),
            ImageSnapshot(id=51, src="https://cdn.shopify.com/b.jpg", position=2, alt="B", variant_ids=[12]),
        ]

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.put = AsyncMock(return_value=_mock_response(200))
                mock_cls.return_value = mock_client

                count = run(undoer.restore_variant_assignments(100, before_images))

        assert count == 2
        assert mock_client.put.call_count == 2

    def test_skips_images_without_variant_ids(self):
        undoer = PushUndoer(STORE_CONFIG)

        before_images = [
            ImageSnapshot(id=50, src="https://cdn.shopify.com/a.jpg", position=1, alt="A", variant_ids=[]),
            ImageSnapshot(id=51, src="https://cdn.shopify.com/b.jpg", position=2, alt="B", variant_ids=[12]),
        ]

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.put = AsyncMock(return_value=_mock_response(200))
                mock_cls.return_value = mock_client

                count = run(undoer.restore_variant_assignments(100, before_images))

        assert count == 1
        assert mock_client.put.call_count == 1

    def test_handles_404_gracefully(self):
        undoer = PushUndoer(STORE_CONFIG)

        before_images = [
            ImageSnapshot(id=50, src="https://cdn.shopify.com/a.jpg", position=1, alt="A", variant_ids=[10]),
        ]

        with patch("lookout.push.undo.asyncio.sleep", new_callable=AsyncMock):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.put = AsyncMock(return_value=_mock_response(404))
                mock_cls.return_value = mock_client

                count = run(undoer.restore_variant_assignments(100, before_images))

        assert count == 0  # 404 means image gone, not restored


class TestUndoProduct:
    """Test undo_product orchestration."""

    def test_deletes_images_and_restores_body(self):
        undoer = PushUndoer(STORE_CONFIG)
        undoer.delete_image = AsyncMock(return_value=True)
        undoer.restore_body_html = AsyncMock(return_value=True)
        undoer.restore_variant_assignments = AsyncMock(return_value=2)

        pm = _make_product_manifest(
            product_id=100,
            images_created=[
                CreatedImage(id=201, src_url="https://cdn.ex.com/a.jpg", alt="A", variant_ids=[1]),
                CreatedImage(id=202, src_url="https://cdn.ex.com/b.jpg", alt="B", variant_ids=[2]),
            ],
            pushed_body_html="<p>New</p>",
            before_body_html="<p>Old</p>",
            before_images=[
                ImageSnapshot(id=50, src="https://cdn.shopify.com/orig.jpg", position=1, alt="Orig", variant_ids=[1, 2]),
            ],
        )

        result = run(undoer.undo_product("test-product", pm))

        assert result["handle"] == "test-product"
        assert result["images_deleted"] == 2
        assert result["variant_assignments_restored"] == 2
        assert result["body_restored"] is True
        assert result["errors"] == []
        assert undoer.delete_image.call_count == 2
        undoer.restore_variant_assignments.assert_called_once()
        undoer.restore_body_html.assert_called_once_with(100, "<p>Old</p>")

    def test_handles_404_gracefully(self):
        """404 responses (already deleted) still count as success."""
        undoer = PushUndoer(STORE_CONFIG)
        undoer.delete_image = AsyncMock(return_value=True)  # 404 returns True
        undoer.restore_variant_assignments = AsyncMock(return_value=0)

        pm = _make_product_manifest(
            product_id=100,
            images_created=[
                CreatedImage(id=201, src_url="https://cdn.ex.com/a.jpg", alt="A", variant_ids=[1]),
            ],
        )

        result = run(undoer.undo_product("test-product", pm))

        assert result["images_deleted"] == 1
        assert result["body_restored"] is False
        assert result["errors"] == []

    def test_skips_body_restore_when_not_pushed(self):
        undoer = PushUndoer(STORE_CONFIG)
        undoer.delete_image = AsyncMock(return_value=True)
        undoer.restore_body_html = AsyncMock()
        undoer.restore_variant_assignments = AsyncMock(return_value=0)

        pm = _make_product_manifest(
            product_id=100,
            images_created=[
                CreatedImage(id=201, src_url="https://cdn.ex.com/a.jpg", alt="A", variant_ids=[1]),
            ],
            pushed_body_html=None,  # body wasn't changed
        )

        result = run(undoer.undo_product("test-product", pm))

        assert result["body_restored"] is False
        undoer.restore_body_html.assert_not_called()

    def test_collects_errors(self):
        undoer = PushUndoer(STORE_CONFIG)
        undoer.delete_image = AsyncMock(return_value=False)
        undoer.restore_body_html = AsyncMock(return_value=False)
        undoer.restore_variant_assignments = AsyncMock(return_value=0)

        pm = _make_product_manifest(
            product_id=100,
            images_created=[
                CreatedImage(id=201, src_url="https://cdn.ex.com/a.jpg", alt="A", variant_ids=[1]),
            ],
            pushed_body_html="<p>New</p>",
            before_body_html="<p>Old</p>",
        )

        result = run(undoer.undo_product("test-product", pm))

        assert result["images_deleted"] == 0
        assert result["body_restored"] is False
        assert len(result["errors"]) == 2


class TestUndoRun:
    """Test undo_run with manifest-level orchestration."""

    def test_undoes_all_products(self):
        undoer = PushUndoer(STORE_CONFIG)
        undoer.delete_image = AsyncMock(return_value=True)
        undoer.restore_body_html = AsyncMock(return_value=True)
        undoer.restore_variant_assignments = AsyncMock(return_value=1)

        manifest = _make_manifest({
            "product-a": _make_product_manifest(
                product_id=100,
                images_created=[
                    CreatedImage(id=201, src_url="https://cdn.ex.com/a.jpg", alt="A", variant_ids=[1]),
                ],
                pushed_body_html="<p>New A</p>",
                before_body_html="<p>Old A</p>",
                before_images=[
                    ImageSnapshot(id=50, src="https://cdn.shopify.com/orig.jpg", position=1, alt="Orig", variant_ids=[1]),
                ],
            ),
            "product-b": _make_product_manifest(
                product_id=200,
                images_created=[
                    CreatedImage(id=301, src_url="https://cdn.ex.com/b.jpg", alt="B", variant_ids=[2]),
                    CreatedImage(id=302, src_url="https://cdn.ex.com/c.jpg", alt="C", variant_ids=[3]),
                ],
                before_images=[
                    ImageSnapshot(id=60, src="https://cdn.shopify.com/orig2.jpg", position=1, alt="Orig2", variant_ids=[2]),
                ],
            ),
        })

        result = run(undoer.undo_run(manifest))

        assert result["products_undone"] == 2
        assert result["images_deleted"] == 3
        assert result["variant_assignments_restored"] == 2
        assert result["body_restored"] == 1
        assert result["errors"] == []

    def test_filters_by_handle(self):
        undoer = PushUndoer(STORE_CONFIG)
        undoer.delete_image = AsyncMock(return_value=True)
        undoer.restore_variant_assignments = AsyncMock(return_value=0)

        manifest = _make_manifest({
            "product-a": _make_product_manifest(
                product_id=100,
                images_created=[
                    CreatedImage(id=201, src_url="https://cdn.ex.com/a.jpg", alt="A", variant_ids=[1]),
                ],
            ),
            "product-b": _make_product_manifest(
                product_id=200,
                images_created=[
                    CreatedImage(id=301, src_url="https://cdn.ex.com/b.jpg", alt="B", variant_ids=[2]),
                ],
            ),
        })

        result = run(undoer.undo_run(manifest, handles=["product-a"]))

        assert result["products_undone"] == 1
        assert result["images_deleted"] == 1
        # Only product-a's image should be deleted
        undoer.delete_image.assert_called_once_with(100, 201)


class TestDryRun:
    """Test dry_run mode logs but doesn't call API."""

    def test_delete_image_dry_run(self, caplog):
        undoer = PushUndoer(STORE_CONFIG, dry_run=True)

        with caplog.at_level(logging.INFO):
            result = run(undoer.delete_image(123, 456))

        assert result is True
        assert "DRY RUN" in caplog.text
        assert "456" in caplog.text

    def test_restore_body_html_dry_run(self, caplog):
        undoer = PushUndoer(STORE_CONFIG, dry_run=True)

        with caplog.at_level(logging.INFO):
            result = run(undoer.restore_body_html(123, "<p>Old</p>"))

        assert result is True
        assert "DRY RUN" in caplog.text

    def test_full_undo_dry_run_no_http(self):
        """Full undo in dry_run mode should not make any HTTP calls."""
        undoer = PushUndoer(STORE_CONFIG, dry_run=True)

        manifest = _make_manifest({
            "product-a": _make_product_manifest(
                product_id=100,
                images_created=[
                    CreatedImage(id=201, src_url="https://cdn.ex.com/a.jpg", alt="A", variant_ids=[1]),
                ],
                pushed_body_html="<p>New</p>",
                before_body_html="<p>Old</p>",
                before_images=[
                    ImageSnapshot(id=50, src="https://cdn.shopify.com/orig.jpg", position=1, alt="Orig", variant_ids=[1]),
                ],
            ),
        })

        with patch("httpx.AsyncClient") as mock_cls:
            result = run(undoer.undo_run(manifest))

        assert result["products_undone"] == 1
        assert result["images_deleted"] == 1
        assert result["variant_assignments_restored"] == 1
        assert result["body_restored"] == 1
        assert result["errors"] == []
        # httpx.AsyncClient should never be instantiated in dry run
        mock_cls.assert_not_called()
