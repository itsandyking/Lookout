"""Push enrichment output to Shopify with manifest tracking."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import httpx

from lookout.push.manifest import (
    CreatedImage,
    ImageSnapshot,
    ProductBefore,
    ProductManifest,
    ProductPushed,
)

logger = logging.getLogger(__name__)

# Locally swatch URLs are color-indexed, not product-specific.
# Using them as variant images causes cross-product contamination.
_LOCALLY_SWATCH_PATTERNS = (
    "s3.amazonaws.com/media.locally.net/",
    "media2.locally.com/",
    "media.locally.com/images/",
)


class ShopifyPusher:
    """Pushes enrichment data (variant images, descriptions) to Shopify.

    Tracks all mutations via manifest models so pushes can be undone.
    """

    def __init__(
        self,
        config: dict,
        db_path: Path,
        dry_run: bool = False,
    ) -> None:
        self.store_url: str = config["store_url"]
        self.access_token: str = config["access_token"]
        self.api_version: str = config.get("api_version", "2026-04")
        self.db_path = db_path
        self.dry_run = dry_run
        self._conn: sqlite3.Connection | None = None

    # --------------------------------------------------------------------- #
    # Database helpers
    # --------------------------------------------------------------------- #

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --------------------------------------------------------------------- #
    # HTTP helpers
    # --------------------------------------------------------------------- #

    def _rest_url(self, path: str) -> str:
        return f"https://{self.store_url}/admin/api/{self.api_version}/{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token,
        }

    # --------------------------------------------------------------------- #
    # 1. snapshot_product
    # --------------------------------------------------------------------- #

    async def snapshot_product(
        self,
        product_id: int,
        client: httpx.AsyncClient,
    ) -> ProductBefore:
        """GET current product state (body_html + images) from Shopify."""
        url = self._rest_url(
            f"products/{product_id}.json?fields=id,body_html,images"
        )
        resp = await client.get(url, headers=self._headers())
        resp.raise_for_status()

        product = resp.json()["product"]
        images = [
            ImageSnapshot(
                id=img["id"],
                src=img.get("src", ""),
                position=img.get("position", 0),
                alt=img.get("alt") or "",
                variant_ids=img.get("variant_ids", []),
            )
            for img in product.get("images", [])
        ]
        return ProductBefore(
            body_html=product.get("body_html"),
            images=images,
        )

    # --------------------------------------------------------------------- #
    # 2. validate_url
    # --------------------------------------------------------------------- #

    async def validate_url(self, url: str, client: httpx.AsyncClient) -> bool:
        """HEAD request — return True if 200 and content-type contains 'image'."""
        try:
            resp = await client.head(url, timeout=5.0, follow_redirects=True)
            if resp.status_code != 200:
                logger.debug("URL validation failed (%d): %s", resp.status_code, url[:80])
                return False
            content_type = resp.headers.get("content-type", "")
            if "image" not in content_type:
                logger.debug("URL not an image (content-type=%s): %s", content_type, url[:80])
                return False
            return True
        except Exception as exc:
            logger.debug("URL validation error for %s: %s", url[:80], exc)
            return False

    # --------------------------------------------------------------------- #
    # 3. is_locally_swatch
    # --------------------------------------------------------------------- #

    @staticmethod
    def is_locally_swatch(url: str) -> bool:
        """Reject Locally swatch URLs (color-indexed, not product-specific)."""
        return any(pat in url for pat in _LOCALLY_SWATCH_PATTERNS)

    # --------------------------------------------------------------------- #
    # 4. get_variant_ids
    # --------------------------------------------------------------------- #

    def get_variant_ids(self, handle: str, color: str) -> list[dict]:
        """Find all variant IDs for a handle+color combo from TVR shopify.db."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT v.id, v.option1_value, v.option2_value, p.id as product_id
            FROM variants v
            JOIN products p ON v.product_id = p.id
            WHERE p.handle = ?
            AND (v.option1_value = ? OR v.option2_value = ?)
            """,
            (handle, color, color),
        ).fetchall()

        return [
            {
                "variant_id": r[0],
                "option1": r[1],
                "option2": r[2],
                "product_id": r[3],
            }
            for r in rows
        ]

    # --------------------------------------------------------------------- #
    # 5. create_image
    # --------------------------------------------------------------------- #

    async def create_image(
        self,
        product_id: int,
        image_url: str,
        variant_ids: list[int],
        alt_text: str,
        client: httpx.AsyncClient,
    ) -> CreatedImage | None:
        """POST to Shopify REST API to create a product image.

        Retries on 429 using Retry-After header.  Sleeps 0.5s after each
        successful call for rate-limit headroom.
        """
        url = self._rest_url(f"products/{product_id}/images.json")

        payload: dict = {
            "image": {
                "src": image_url,
                "variant_ids": variant_ids,
            }
        }
        if alt_text:
            payload["image"]["alt"] = alt_text

        try:
            resp = await client.post(url, json=payload, headers=self._headers())

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2.0"))
                logger.warning(
                    "Rate limited on image create for product %d, waiting %.1fs",
                    product_id,
                    retry_after,
                )
                await asyncio.sleep(retry_after)
                return await self.create_image(
                    product_id, image_url, variant_ids, alt_text, client
                )

            if resp.status_code >= 400:
                logger.error(
                    "Failed to create image for product %d: %d %s",
                    product_id,
                    resp.status_code,
                    resp.text[:200],
                )
                return None

            img = resp.json().get("image", {})
            # Rate-limit spacing
            await asyncio.sleep(0.5)

            return CreatedImage(
                id=img["id"],
                src_url=image_url,
                alt=alt_text,
                variant_ids=variant_ids,
            )

        except Exception as exc:
            logger.error(
                "Exception creating image for product %d: %s", product_id, exc
            )
            return None

    # --------------------------------------------------------------------- #
    # 6. update_body_html
    # --------------------------------------------------------------------- #

    async def update_body_html(
        self,
        product_id: int,
        body_html: str,
        client: httpx.AsyncClient,
    ) -> bool:
        """Update product body_html via Shopify GraphQL Admin API."""
        gid = f"gid://shopify/Product/{product_id}"
        mutation = """
        mutation productUpdate($input: ProductInput!) {
            productUpdate(input: $input) {
                product { id }
                userErrors { field message }
            }
        }
        """
        variables = {"input": {"id": gid, "bodyHtml": body_html}}

        url = f"https://{self.store_url}/admin/api/{self.api_version}/graphql.json"

        try:
            resp = await client.post(
                url,
                json={"query": mutation, "variables": variables},
                headers=self._headers(),
            )

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2.0"))
                logger.warning(
                    "Rate limited on body_html update for product %d, waiting %.1fs",
                    product_id,
                    retry_after,
                )
                await asyncio.sleep(retry_after)
                return await self.update_body_html(product_id, body_html, client)

            if resp.status_code >= 400:
                logger.error(
                    "GraphQL body_html update failed for product %d: %d %s",
                    product_id,
                    resp.status_code,
                    resp.text[:200],
                )
                return False

            data = resp.json()
            errors = (
                data.get("data", {})
                .get("productUpdate", {})
                .get("userErrors", [])
            )
            if errors:
                logger.error(
                    "GraphQL userErrors for product %d: %s", product_id, errors
                )
                return False

            await asyncio.sleep(0.5)
            return True

        except Exception as exc:
            logger.error(
                "Exception updating body_html for product %d: %s",
                product_id,
                exc,
            )
            return False

    # --------------------------------------------------------------------- #
    # 7. push_product
    # --------------------------------------------------------------------- #

    async def push_product(
        self,
        handle: str,
        assignments: list[dict],
        client: httpx.AsyncClient,
        body_html: str | None = None,
    ) -> ProductManifest:
        """Orchestrate a full push for one product.

        Steps:
          1. Resolve variant IDs from the first assignment to get product_id
          2. Snapshot current Shopify state
          3. Validate each image URL
          4. Create images (skip locally swatches and dead URLs)
          5. Optionally update body_html
          6. Return ProductManifest with before/pushed data

        Args:
            handle: Shopify product handle.
            assignments: List of dicts with keys: color, image_url, source.
            client: Shared httpx.AsyncClient.
            body_html: If provided, update the product description.
        """
        # -- Resolve product_id from first assignment --
        first_color = assignments[0]["color"]
        variants = self.get_variant_ids(handle, first_color)
        if not variants:
            logger.warning("No variants found for %s :: %s, skipping", handle, first_color)
            return ProductManifest(product_id=0)

        product_id = variants[0]["product_id"]

        # -- Snapshot --
        if not self.dry_run:
            before = await self.snapshot_product(product_id, client)
        else:
            before = ProductBefore()

        pushed = ProductPushed()
        created_images: list[CreatedImage] = []

        # -- Process each assignment --
        for assignment in assignments:
            color = assignment["color"]
            image_url = assignment["image_url"]

            # Locally swatch gate
            if self.is_locally_swatch(image_url):
                logger.warning(
                    "Rejecting Locally swatch for %s :: %s: %s",
                    handle,
                    color,
                    image_url[:60],
                )
                continue

            # Validate URL
            if not self.dry_run:
                valid = await self.validate_url(image_url, client)
                if not valid:
                    logger.warning(
                        "Dead URL for %s :: %s, skipping: %s",
                        handle,
                        color,
                        image_url[:80],
                    )
                    continue

            # Resolve variant IDs for this color
            color_variants = self.get_variant_ids(handle, color)
            if not color_variants:
                logger.warning(
                    "No variants for %s :: %s, skipping image", handle, color
                )
                continue

            variant_ids = [v["variant_id"] for v in color_variants]
            alt_text = f"{handle.replace('-', ' ').title()} - {color}"

            if self.dry_run:
                logger.info(
                    "[DRY RUN] Would push image to %s :: %s (%d variants)",
                    handle,
                    color,
                    len(variant_ids),
                )
                created_images.append(
                    CreatedImage(
                        id=0,
                        src_url=image_url,
                        alt=alt_text,
                        variant_ids=variant_ids,
                        color=color,
                    )
                )
                continue

            result = await self.create_image(
                product_id, image_url, variant_ids, alt_text, client
            )
            if result:
                result.color = color
                created_images.append(result)
                logger.info(
                    "Created image for %s :: %s (%d variants, id=%d)",
                    handle,
                    color,
                    len(variant_ids),
                    result.id,
                )
            else:
                logger.error("Failed to create image for %s :: %s", handle, color)

        pushed.images_created = created_images

        # -- Body HTML update --
        if body_html is not None:
            if self.dry_run:
                logger.info("[DRY RUN] Would update body_html for %s", handle)
                pushed.body_html = body_html
            else:
                ok = await self.update_body_html(product_id, body_html, client)
                if ok:
                    pushed.body_html = body_html
                    logger.info("Updated body_html for %s", handle)
                else:
                    logger.error("Failed to update body_html for %s", handle)

        return ProductManifest(
            product_id=product_id,
            before=before,
            pushed=pushed,
        )
