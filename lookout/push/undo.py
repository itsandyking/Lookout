"""Undo pushed enrichment changes using manifest."""

from __future__ import annotations

import asyncio
import logging

import httpx

from lookout.push.manifest import ImageSnapshot, ProductManifest, PushManifest

logger = logging.getLogger(__name__)

RATE_LIMIT_SLEEP = 0.5


class PushUndoer:
    """Reverts pushed enrichment changes by deleting created images and restoring body_html."""

    def __init__(self, config: dict, dry_run: bool = False):
        raw_url = config["store_url"].rstrip("/")
        self.store_url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.access_token = config["access_token"]
        self.api_version = config.get("api_version", "2024-10")
        self.dry_run = dry_run
        self._headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

    async def delete_image(self, product_id: int, image_id: int) -> bool:
        """Delete a Shopify product image by ID.

        Returns True on success (200) or if already gone (404).
        Retries on 429 rate limit responses.
        """
        if self.dry_run:
            logger.info("[DRY RUN] Would delete image %d from product %d", image_id, product_id)
            return True

        url = (
            f"{self.store_url}/admin/api/{self.api_version}"
            f"/products/{product_id}/images/{image_id}.json"
        )

        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            while True:
                resp = await client.delete(url)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                    logger.warning(
                        "Rate limited deleting image %d, retrying in %.1fs", image_id, retry_after
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code in (200, 404):
                    if resp.status_code == 404:
                        logger.info("Image %d already deleted (404)", image_id)
                    else:
                        logger.info("Deleted image %d from product %d", image_id, product_id)
                    await asyncio.sleep(RATE_LIMIT_SLEEP)
                    return True
                logger.error(
                    "Failed to delete image %d from product %d: HTTP %d",
                    image_id,
                    product_id,
                    resp.status_code,
                )
                await asyncio.sleep(RATE_LIMIT_SLEEP)
                return False

    async def restore_body_html(self, product_id: int, body_html: str) -> bool:
        """Restore a product's body_html via GraphQL mutation.

        Returns True on success.
        """
        if self.dry_run:
            logger.info("[DRY RUN] Would restore body_html for product %d", product_id)
            return True

        gql_id = f"gid://shopify/Product/{product_id}"
        mutation = """
        mutation productUpdate($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id }
            userErrors { field message }
          }
        }
        """
        payload = {
            "query": mutation,
            "variables": {
                "input": {
                    "id": gql_id,
                    "bodyHtml": body_html,
                }
            },
        }

        url = f"{self.store_url}/admin/api/{self.api_version}/graphql.json"

        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            while True:
                resp = await client.post(url, json=payload)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                    logger.warning(
                        "Rate limited restoring body_html for %d, retrying in %.1fs",
                        product_id,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code != 200:
                    logger.error(
                        "GraphQL request failed for product %d: HTTP %d",
                        product_id,
                        resp.status_code,
                    )
                    return False
                data = resp.json()
                errors = data.get("data", {}).get("productUpdate", {}).get("userErrors", [])
                if errors:
                    logger.error("GraphQL userErrors restoring product %d: %s", product_id, errors)
                    return False
                logger.info("Restored body_html for product %d", product_id)
                await asyncio.sleep(RATE_LIMIT_SLEEP)
                return True

    async def restore_variant_assignments(
        self, product_id: int, before_images: list[ImageSnapshot]
    ) -> int:
        """Restore original variant-to-image assignments from the before snapshot.

        After deleting pushed images, variants lose their image assignments.
        This PUTs the original variant_ids back onto pre-existing images.

        Returns count of images whose assignments were restored.
        """
        restored = 0
        for img in before_images:
            if not img.variant_ids:
                continue

            if self.dry_run:
                logger.info(
                    "[DRY RUN] Would restore variant assignments for image %d (variants: %s)",
                    img.id,
                    img.variant_ids,
                )
                restored += 1
                continue

            url = (
                f"{self.store_url}/admin/api/{self.api_version}"
                f"/products/{product_id}/images/{img.id}.json"
            )
            payload = {
                "image": {
                    "id": img.id,
                    "variant_ids": img.variant_ids,
                }
            }

            async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
                while True:
                    resp = await client.put(url, json=payload)
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", "2"))
                        logger.warning(
                            "Rate limited restoring variant assignments for image %d, "
                            "retrying in %.1fs",
                            img.id,
                            retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status_code == 404:
                        logger.info(
                            "Image %d no longer exists (404), skipping variant restore",
                            img.id,
                        )
                        break
                    if resp.status_code == 200:
                        logger.info(
                            "Restored variant assignments for image %d (variants: %s)",
                            img.id,
                            img.variant_ids,
                        )
                        restored += 1
                    else:
                        logger.error(
                            "Failed to restore variant assignments for image %d: HTTP %d",
                            img.id,
                            resp.status_code,
                        )
                    await asyncio.sleep(RATE_LIMIT_SLEEP)
                    break

        return restored

    async def undo_product(self, handle: str, product_manifest: ProductManifest) -> dict:
        """Undo all changes for a single product.

        Deletes all images in pushed.images_created and restores body_html
        if it was changed.

        Returns a result dict with counts and errors.
        """
        errors: list[str] = []
        images_deleted = 0
        body_restored = False
        variant_assignments_restored = 0

        # Delete created images
        for img in product_manifest.pushed.images_created:
            ok = await self.delete_image(product_manifest.product_id, img.id)
            if ok:
                images_deleted += 1
            else:
                errors.append(f"Failed to delete image {img.id}")

        # Restore original variant-to-image assignments
        if product_manifest.before.images:
            variant_assignments_restored = await self.restore_variant_assignments(
                product_manifest.product_id, product_manifest.before.images
            )

        # Restore body_html if it was changed
        if product_manifest.pushed.body_html is not None:
            before_html = product_manifest.before.body_html or ""
            ok = await self.restore_body_html(product_manifest.product_id, before_html)
            if ok:
                body_restored = True
            else:
                errors.append(
                    f"Failed to restore body_html for product {product_manifest.product_id}"
                )

        return {
            "handle": handle,
            "images_deleted": images_deleted,
            "variant_assignments_restored": variant_assignments_restored,
            "body_restored": body_restored,
            "errors": errors,
        }

    async def undo_run(self, manifest: PushManifest, handles: list[str] | None = None) -> dict:
        """Undo an entire push run, or a subset filtered by handles.

        Returns a summary dict with aggregate counts.
        """
        products_to_undo = manifest.products
        if handles:
            products_to_undo = {h: m for h, m in manifest.products.items() if h in handles}
            missing = set(handles) - set(products_to_undo.keys())
            if missing:
                logger.warning("Handles not found in manifest: %s", missing)

        total_images = 0
        total_body = 0
        total_products = 0
        total_variant_assignments = 0
        all_errors: list[str] = []

        for handle, pm in products_to_undo.items():
            logger.info("Undoing product: %s", handle)
            result = await self.undo_product(handle, pm)
            total_products += 1
            total_images += result["images_deleted"]
            total_variant_assignments += result["variant_assignments_restored"]
            if result["body_restored"]:
                total_body += 1
            all_errors.extend(result["errors"])

        return {
            "products_undone": total_products,
            "images_deleted": total_images,
            "variant_assignments_restored": total_variant_assignments,
            "body_restored": total_body,
            "errors": all_errors,
        }
