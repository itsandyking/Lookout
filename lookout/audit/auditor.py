"""Module: Content Auditor.

Audits Shopify product content for gaps — missing images, short descriptions,
missing product type, missing tags — and prioritizes them by inventory value
for merchandising improvement.

Ported from Product-Reconciliation's MerchandisingAnalyzer.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from sqlalchemy import func
from tvr.db.models import (
    Image,
    InventoryItem,
    InventoryLevel,
    Product,
    Variant,
)
from tvr.db.store import ShopifyStore

# Minimum description length (plain text, HTML stripped) to be "complete".
MIN_DESCRIPTION_LENGTH = 100


@dataclass
class ProductScore:
    """Content audit score for a single product."""

    product_id: int
    product_handle: str
    product_title: str
    vendor: str
    product_type: str

    # Gap flags
    has_product_image: bool = True
    has_all_variant_images: bool = True
    has_description: bool = True
    has_product_type: bool = True
    has_tags: bool = True

    # Variant image detail
    variant_count: int = 0
    variants_missing_images: int = 0

    # Description detail
    description_length: int = 0

    # Inventory / value
    total_inventory: int = 0
    price: float = 0.0
    cost: float = 0.0
    inventory_value: float = 0.0  # cost * quantity
    full_price_inventory_value: float = 0.0  # value of variants NOT on sale

    # Computed
    gap_count: float = 0.0
    gaps: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    # Priority
    priority_score: float = 0.0

    def calculate_gaps(self) -> None:
        """Populate gap_count, gaps, suggestions, and priority_score.

        Gap weighting (matches PR's MerchandisingAnalyzer):
        - Missing product image: 1
        - Missing variant images: (variants_missing / variant_count), i.e. fractional
        - Missing description: 1
        - Missing product type: 0.5
        - Missing tags: 0.5

        Priority formula:
            inventory_value * gap_count * priority_multiplier
        where priority_multiplier ranges from 1x to 2x based on the share of
        full-price inventory value.
        """
        self.gap_count = 0.0
        self.gaps = []
        self.suggestions = []

        if not self.has_product_image:
            self.gap_count += 1
            self.gaps.append("Missing product image")
            self.suggestions.append("Add at least one product-level image")

        if not self.has_all_variant_images:
            weight = self.variants_missing_images / max(self.variant_count, 1)
            self.gap_count += weight
            self.gaps.append(
                f"Missing variant images ({self.variants_missing_images}/{self.variant_count})"
            )
            self.suggestions.append("Add images for each color variant")

        if not self.has_description:
            self.gap_count += 1
            if self.description_length == 0:
                self.gaps.append("Missing description")
            else:
                self.gaps.append(f"Short description ({self.description_length} chars)")
            self.suggestions.append(
                f"Add description (minimum {MIN_DESCRIPTION_LENGTH} characters)"
            )

        if not self.has_product_type:
            self.gap_count += 0.5
            self.gaps.append("Missing product type")
            self.suggestions.append("Set product type for better categorization")

        if not self.has_tags:
            self.gap_count += 0.5
            self.gaps.append("No tags")
            self.suggestions.append("Add relevant tags for search and filtering")

        # Priority score: inventory_value * gap_count * multiplier
        if self.full_price_inventory_value > 0 and self.inventory_value > 0:
            full_price_ratio = self.full_price_inventory_value / self.inventory_value
            priority_multiplier = 1 + full_price_ratio  # 1x to 2x
        else:
            priority_multiplier = 1.0

        self.priority_score = self.inventory_value * self.gap_count * priority_multiplier

    @property
    def completeness_percent(self) -> float:
        """Merchandising completeness (5 checks)."""
        total_checks = 5
        complete = sum(
            [
                self.has_product_image,
                self.has_all_variant_images,
                self.has_description,
                self.has_product_type,
                self.has_tags,
            ]
        )
        return (complete / total_checks) * 100

    @property
    def is_complete(self) -> bool:
        return self.gap_count == 0


class ContentAuditResult:
    """Result of a content audit."""

    def __init__(self, scores: list[ProductScore], vendor: str = "") -> None:
        self.scores = scores
        self.vendor = vendor

    @property
    def priority_items(self) -> list[ProductScore]:
        """Items with gaps, sorted by priority score descending."""
        return sorted(
            [s for s in self.scores if s.gap_count > 0],
            key=lambda s: s.priority_score,
            reverse=True,
        )

    @property
    def all_items(self) -> list[ProductScore]:
        return self.scores

    def summary(self) -> dict:
        total = len(self.scores)
        with_gaps = sum(1 for s in self.scores if s.gap_count > 0)
        return {
            "total_products": total,
            "products_with_gaps": with_gaps,
            "products_complete": total - with_gaps,
            "completion_pct": round((total - with_gaps) / total * 100, 1) if total > 0 else 100.0,
            # Gap breakdown
            "missing_images": sum(1 for s in self.scores if not s.has_product_image),
            "missing_variant_images": sum(1 for s in self.scores if not s.has_all_variant_images),
            "missing_description": sum(1 for s in self.scores if not s.has_description),
            "missing_product_type": sum(1 for s in self.scores if not s.has_product_type),
            "missing_tags": sum(1 for s in self.scores if not s.has_tags),
            # Value at risk
            "total_inventory_value": round(
                sum(s.inventory_value for s in self.scores if s.gap_count > 0), 2
            ),
        }

    # ── CSV export ─────────────────────────────────────────────────

    @staticmethod
    def _write_csv(headers: list[str], rows: list[dict]) -> bytes:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    def to_priority_csv(self) -> bytes:
        """Export priority items (only products with gaps), sorted by priority."""
        headers = [
            "Product Handle",
            "Title",
            "Vendor",
            "Gap Count",
            "Priority Score",
            "Gaps",
            "Suggestions",
            "Total Inventory",
            "Inventory Value",
            "Completeness %",
        ]
        rows = [
            {
                "Product Handle": s.product_handle,
                "Title": s.product_title,
                "Vendor": s.vendor,
                "Gap Count": round(s.gap_count, 1),
                "Priority Score": round(s.priority_score, 2),
                "Gaps": ", ".join(s.gaps),
                "Suggestions": "; ".join(s.suggestions),
                "Total Inventory": s.total_inventory,
                "Inventory Value": round(s.inventory_value, 2),
                "Completeness %": round(s.completeness_percent, 1),
            }
            for s in self.priority_items
        ]
        return self._write_csv(headers, rows)

    def to_full_audit_csv(self) -> bytes:
        """Export all products with audit details."""
        headers = [
            "Product Handle",
            "Title",
            "Vendor",
            "Product Type",
            "Has Image",
            "Has Variant Images",
            "Has Description",
            "Has Product Type",
            "Has Tags",
            "Variant Count",
            "Variants Missing Images",
            "Description Length",
            "Gap Count",
            "Priority Score",
            "Completeness %",
            "Total Inventory",
            "Inventory Value",
            "Full Price Inventory Value",
            "Gaps",
            "Suggestions",
        ]
        rows = [
            {
                "Product Handle": s.product_handle,
                "Title": s.product_title,
                "Vendor": s.vendor,
                "Product Type": s.product_type,
                "Has Image": s.has_product_image,
                "Has Variant Images": s.has_all_variant_images,
                "Has Description": s.has_description,
                "Has Product Type": s.has_product_type,
                "Has Tags": s.has_tags,
                "Variant Count": s.variant_count,
                "Variants Missing Images": s.variants_missing_images,
                "Description Length": s.description_length,
                "Gap Count": round(s.gap_count, 1),
                "Priority Score": round(s.priority_score, 2),
                "Completeness %": round(s.completeness_percent, 1),
                "Total Inventory": s.total_inventory,
                "Inventory Value": round(s.inventory_value, 2),
                "Full Price Inventory Value": round(s.full_price_inventory_value, 2),
                "Gaps": ", ".join(s.gaps),
                "Suggestions": "; ".join(s.suggestions),
            }
            for s in self.scores
        ]
        return self._write_csv(headers, rows)


class ContentAuditor:
    """Audits Shopify product content for gaps.

    Takes a ShopifyStore and evaluates each product for:
    - Product-level image
    - Variant-level images (via Variant.image_src)
    - Description length (HTML stripped, >= MIN_DESCRIPTION_LENGTH chars)
    - Product type
    - Tags

    Returns a ContentAuditResult with per-product scores and priority ranking.
    """

    def __init__(
        self, store: ShopifyStore, min_description_length: int = MIN_DESCRIPTION_LENGTH
    ) -> None:
        self.store = store
        self.min_description_length = min_description_length

    def audit(self, vendor: str | None = None) -> ContentAuditResult:
        """Run content audit on all active products (or filtered by vendor)."""
        scores: list[ProductScore] = []

        with self.store.session() as s:
            query = s.query(Product).filter(Product.status == "active")
            if vendor:
                query = query.filter(Product.vendor == vendor)

            products = query.all()

            for product in products:
                score = self._score_product(s, product)
                scores.append(score)

        return ContentAuditResult(scores=scores, vendor=vendor or "")

    def _score_product(self, session, product: Product) -> ProductScore:
        """Score a single product for content gaps."""
        score = ProductScore(
            product_id=product.id,
            product_handle=product.handle,
            product_title=product.title or "",
            vendor=product.vendor or "",
            product_type=product.product_type or "",
        )

        # --- Image checks ---
        # Product-level images (from the images table)
        image_count = (
            session.query(func.count(Image.id)).filter(Image.product_id == product.id).scalar()
        )
        score.has_product_image = image_count > 0

        # Variant-level images (from variant.image_src)
        variants = session.query(Variant).filter(Variant.product_id == product.id).all()
        score.variant_count = len(variants)

        variants_missing = 0
        for v in variants:
            has_variant_img = bool(v.image_src and v.image_src.strip())
            if not has_variant_img and not score.has_product_image:
                # No variant image AND no product image => missing
                variants_missing += 1
            elif not has_variant_img and score.has_product_image:
                # No variant-specific image but product image exists.
                # In Shopify, the product image covers variants without their own.
                # We still count this as covered (matches PR logic where
                # all images apply if no variant associations exist).
                pass

        score.variants_missing_images = variants_missing
        score.has_all_variant_images = variants_missing == 0

        # --- Description check ---
        body_html = product.body_html or ""
        clean_text = re.sub(r"<[^>]+>", "", body_html).strip()
        score.description_length = len(clean_text)
        score.has_description = score.description_length >= self.min_description_length

        # --- Product type check ---
        score.has_product_type = bool((product.product_type or "").strip())

        # --- Tags check ---
        score.has_tags = bool((product.tags or "").strip())

        # --- Inventory & value ---
        total_inventory = 0
        inventory_value = 0.0
        full_price_inventory_value = 0.0
        total_price = 0.0
        total_cost = 0.0

        for v in variants:
            inv_qty = (
                session.query(func.coalesce(func.sum(InventoryLevel.available), 0))
                .join(InventoryItem, InventoryLevel.inventory_item_id == InventoryItem.id)
                .filter(InventoryItem.variant_id == v.id)
                .scalar()
            )
            qty = max(0, int(inv_qty))
            total_inventory += qty

            cost = v.cost or 0.0
            price = v.price or 0.0
            variant_value = qty * cost
            inventory_value += variant_value

            total_price += price
            total_cost += cost

            # Track full-price variants (no compare_at_price => not on sale)
            if not v.compare_at_price:
                full_price_inventory_value += variant_value

        score.total_inventory = total_inventory
        score.inventory_value = inventory_value
        score.full_price_inventory_value = full_price_inventory_value
        score.price = total_price / len(variants) if variants else 0.0
        score.cost = total_cost / len(variants) if variants else 0.0

        # --- Calculate gaps & priority ---
        score.calculate_gaps()

        return score
