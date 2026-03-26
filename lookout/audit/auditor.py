"""Content Auditor.

Audits Shopify product content for gaps -- missing images, short descriptions,
missing product type, missing tags -- and prioritizes them by inventory value
for merchandising improvement.
"""

from __future__ import annotations

import re

from lookout.audit.models import MIN_DESCRIPTION_LENGTH, AuditResult, ProductScore
from lookout.store import LookoutStore


class ContentAuditor:
    """Audits Shopify product content for gaps.

    Uses LookoutStore to fetch product, variant, and inventory data as plain
    dicts and evaluates each product for:
    - Product-level image (inferred from variant images)
    - Variant-level images (variant["image_src"])
    - Description length (HTML stripped, >= MIN_DESCRIPTION_LENGTH chars)
    - Product type
    - Tags

    Returns an AuditResult with per-product scores and priority ranking.
    """

    def __init__(
        self, store: LookoutStore, min_description_length: int = MIN_DESCRIPTION_LENGTH
    ) -> None:
        self.store = store
        self.min_description_length = min_description_length

    def audit(self, vendor: str | None = None) -> AuditResult:
        """Run content audit on all active products (or filtered by vendor)."""
        products = self.store.list_products(vendor=vendor, status="active")
        scores: list[ProductScore] = []

        for product in products:
            score = self._score_product(product)
            scores.append(score)

        return AuditResult(scores=scores, vendor=vendor or "")

    def _score_product(self, product: dict) -> ProductScore:
        """Score a single product for content gaps."""
        product_id = product["id"]
        variants = self.store.get_variants(product_id)
        inventory = self.store.get_inventory(product_id)

        # Determine product-level image: if any variant has an image_src
        has_product_image = any(
            bool(v.get("image_src", "").strip()) for v in variants
        ) if variants else False

        # Variant-level image check
        variants_missing = 0
        for v in variants:
            has_variant_img = bool(v.get("image_src", "").strip())
            if not has_variant_img and not has_product_image:
                variants_missing += 1

        # Description check
        body_html = product.get("body_html", "") or ""
        clean_text = re.sub(r"<[^>]+>", "", body_html).strip()
        description_length = len(clean_text)
        has_description = description_length >= self.min_description_length

        # Product type and tags
        has_product_type = bool((product.get("product_type", "") or "").strip())
        has_tags = bool((product.get("tags", "") or "").strip())

        # First variant identifiers (representative)
        barcode = variants[0].get("barcode", "") if variants else ""
        sku = variants[0].get("sku", "") if variants else ""

        # Inventory values from store
        total_inventory = inventory.get("total", 0)
        inventory_value = inventory.get("value", 0.0)
        full_price_value = inventory.get("full_price_value", 0.0)

        # Average price/cost across variants
        total_price = sum(v.get("price", 0.0) or 0.0 for v in variants)
        total_cost = sum(v.get("cost", 0.0) or 0.0 for v in variants)
        avg_price = total_price / len(variants) if variants else 0.0
        avg_cost = total_cost / len(variants) if variants else 0.0

        score = ProductScore(
            product_id=product_id,
            handle=product.get("handle", ""),
            title=product.get("title", ""),
            vendor=product.get("vendor", ""),
            product_type=product.get("product_type", ""),
            has_product_image=has_product_image,
            has_all_variant_images=variants_missing == 0,
            has_description=has_description,
            has_product_type=has_product_type,
            has_tags=has_tags,
            variant_count=len(variants),
            variants_missing_images=variants_missing,
            description_length=description_length,
            barcode=barcode,
            sku=sku,
            total_inventory=total_inventory,
            price=avg_price,
            cost=avg_cost,
            inventory_value=inventory_value,
            full_price_inventory_value=full_price_value,
        )
        score.calculate_gaps()
        return score
