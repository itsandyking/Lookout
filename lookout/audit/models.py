"""Audit data models: ProductScore and AuditResult.

Extracted from the original auditor module. No TVR imports.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

# Minimum description length (plain text, HTML stripped) to be "complete".
MIN_DESCRIPTION_LENGTH = 100


@dataclass
class ProductScore:
    """Content audit score for a single product."""

    product_id: int
    handle: str
    title: str
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

    # First barcode (representative)
    barcode: str = ""

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

        Gap weighting:
        - Missing product image: 1
        - Missing variant images: (variants_missing / variant_count), fractional
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

    @property
    def admin_link(self) -> str:
        return f"https://admin.shopify.com/store/the-mountain-air/products/{self.product_id}"


class AuditResult:
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
            "missing_variant_images": sum(
                1 for s in self.scores if not s.has_all_variant_images
            ),
            "missing_description": sum(1 for s in self.scores if not s.has_description),
            "missing_product_type": sum(1 for s in self.scores if not s.has_product_type),
            "missing_tags": sum(1 for s in self.scores if not s.has_tags),
            # Value at risk
            "total_inventory_value": round(
                sum(s.inventory_value for s in self.scores if s.gap_count > 0), 2
            ),
        }

    # -- CSV export ------------------------------------------------------------

    _PRIORITY_COLUMNS = [
        "Product Handle",
        "Vendor",
        "Title",
        "Barcode",
        "Has Image",
        "Has Variant Images",
        "Has Description",
        "Has Product Type",
        "Has Tags",
        "Gaps",
        "Suggestions",
        "Priority Score",
        "Admin Link",
    ]

    @staticmethod
    def _write_csv(headers: list[str], rows: list[dict]) -> bytes:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    def to_priority_csv(self) -> bytes:
        """Export priority items (only products with gaps), sorted by priority."""
        rows = [
            {
                "Product Handle": s.handle,
                "Vendor": s.vendor,
                "Title": s.title,
                "Barcode": s.barcode,
                "Has Image": s.has_product_image,
                "Has Variant Images": s.has_all_variant_images,
                "Has Description": s.has_description,
                "Has Product Type": s.has_product_type,
                "Has Tags": s.has_tags,
                "Gaps": ", ".join(s.gaps),
                "Suggestions": "; ".join(s.suggestions),
                "Priority Score": round(s.priority_score, 2),
                "Admin Link": s.admin_link,
            }
            for s in self.priority_items
        ]
        return self._write_csv(self._PRIORITY_COLUMNS, rows)

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
                "Product Handle": s.handle,
                "Title": s.title,
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
