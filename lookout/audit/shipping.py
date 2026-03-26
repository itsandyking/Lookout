"""Shipping audit: weight accuracy + shipping-to-price ratio analysis.

Identifies products where:
1. Shopify weight is zero or missing (broken checkout quotes)
2. Shopify weight diverges from type-based estimate (wrong quotes)
3. Estimated shipping cost is a high % of product price (conversion blocker)
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from lookout.output.google_shopping import get_weight_grams
from lookout.store import LookoutStore
from lookout.taxonomy.mappings import DIMENSIONAL_TYPES, EXCLUDED_VENDORS, LB_TO_GRAMS

logger = logging.getLogger(__name__)

# UPS Ground rough rate tiers (continental US, 2026 general rates)
# Zone 5 average for a mid-country shipment from CA
# These are estimates — actual rates vary by zone, dims, surcharges
UPS_GROUND_RATE_TIERS_LB = [
    (1, 9.50),
    (2, 10.50),
    (3, 11.50),
    (5, 13.50),
    (10, 18.00),
    (20, 25.00),
    (30, 33.00),
    (50, 50.00),
    (70, 70.00),
    (150, 120.00),
]


def _estimate_shipping_cost(weight_lb: float) -> float:
    """Estimate UPS Ground shipping cost from weight.

    Uses rough rate tiers for Zone 5. Returns the estimated
    carrier cost (before the 20% markup).
    """
    if weight_lb <= 0:
        return 0.0
    for max_lb, rate in UPS_GROUND_RATE_TIERS_LB:
        if weight_lb <= max_lb:
            return rate
    return UPS_GROUND_RATE_TIERS_LB[-1][1]


@dataclass
class ShippingIssue:
    """A shipping-related issue for a product."""

    handle: str
    title: str
    vendor: str
    product_type: str
    issue_type: str  # zero_weight, weight_mismatch, high_shipping_ratio
    severity: str  # critical, warning, info
    details: str
    shopify_weight_g: float = 0
    estimated_weight_g: float = 0
    price: float = 0
    estimated_shipping: float = 0
    shipping_to_price_pct: float = 0
    variant_count: int = 0
    product_id: int = 0


def run_shipping_audit(store: LookoutStore) -> list[ShippingIssue]:
    """Run shipping weight and cost audit across all products.

    Checks:
    1. Zero-weight shippable variants (critical — broken checkout)
    2. Weight mismatch between Shopify and type estimate (warning)
    3. High shipping-to-price ratio (info — conversion risk)
    """
    products = store.list_products(status="active")
    issues: list[ShippingIssue] = []

    for product in products:
        if product["vendor"] in EXCLUDED_VENDORS:
            continue

        product_type = product.get("product_type", "")
        variants = store.get_variants(product["id"])
        if not variants:
            continue

        # Get type-based weight estimate
        estimated_g = get_weight_grams(product_type)

        for variant in variants:
            if not variant.get("requires_shipping", True):
                continue

            shopify_g = variant.get("grams", 0) or 0
            price = variant.get("price", 0) or 0

            # 1. Zero weight on shippable variant
            if shopify_g == 0:
                issues.append(ShippingIssue(
                    handle=product["handle"],
                    title=product["title"],
                    vendor=product["vendor"],
                    product_type=product_type,
                    issue_type="zero_weight",
                    severity="critical",
                    details=f"Shippable variant has 0g weight — checkout will show wrong shipping rates",
                    shopify_weight_g=0,
                    estimated_weight_g=estimated_g or 0,
                    price=price,
                    variant_count=len(variants),
                    product_id=product["id"],
                ))
                # Only flag once per product for zero weight
                break

            # 2. Weight mismatch (if we have an estimate)
            if estimated_g and shopify_g > 0:
                ratio = shopify_g / estimated_g if estimated_g > 0 else 0
                if ratio < 0.5 or ratio > 2.0:
                    direction = "lighter" if ratio < 0.5 else "heavier"
                    issues.append(ShippingIssue(
                        handle=product["handle"],
                        title=product["title"],
                        vendor=product["vendor"],
                        product_type=product_type,
                        issue_type="weight_mismatch",
                        severity="warning",
                        details=(
                            f"Shopify weight ({shopify_g}g) is {ratio:.1f}x the "
                            f"expected weight ({estimated_g}g) for {product_type} — "
                            f"{'undercharging shipping' if direction == 'heavier' else 'overcharging, may hurt conversion'}"
                        ),
                        shopify_weight_g=shopify_g,
                        estimated_weight_g=estimated_g,
                        price=price,
                        variant_count=len(variants),
                        product_id=product["id"],
                    ))
                    break

        # 3. Shipping-to-price ratio (use first variant as representative)
        rep_variant = variants[0]
        price = rep_variant.get("price", 0) or 0
        weight_g = rep_variant.get("grams", 0) or 0

        if price > 0 and weight_g > 0:
            weight_lb = weight_g / LB_TO_GRAMS
            carrier_cost = _estimate_shipping_cost(weight_lb)
            customer_cost = carrier_cost * 1.20  # 20% markup
            ratio_pct = (customer_cost / price) * 100

            if ratio_pct > 25:
                issues.append(ShippingIssue(
                    handle=product["handle"],
                    title=product["title"],
                    vendor=product["vendor"],
                    product_type=product_type,
                    issue_type="high_shipping_ratio",
                    severity="warning" if ratio_pct > 40 else "info",
                    details=(
                        f"Shipping ~${customer_cost:.0f} is {ratio_pct:.0f}% of "
                        f"${price:.0f} price — potential conversion blocker"
                    ),
                    shopify_weight_g=weight_g,
                    price=price,
                    estimated_shipping=customer_cost,
                    shipping_to_price_pct=ratio_pct,
                    variant_count=len(variants),
                    product_id=product["id"],
                ))

    # Sort: critical first, then by shipping_to_price_pct descending
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (severity_order.get(i.severity, 9), -i.shipping_to_price_pct))

    return issues


def export_shipping_audit_csv(issues: list[ShippingIssue], output_path: Path) -> None:
    """Export shipping audit to CSV."""
    headers = [
        "Handle", "Title", "Vendor", "Product Type", "Issue Type", "Severity",
        "Details", "Shopify Weight (g)", "Estimated Weight (g)", "Price",
        "Est. Shipping", "Shipping/Price %",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for i in issues:
            writer.writerow({
                "Handle": i.handle,
                "Title": i.title,
                "Vendor": i.vendor,
                "Product Type": i.product_type,
                "Issue Type": i.issue_type,
                "Severity": i.severity,
                "Details": i.details,
                "Shopify Weight (g)": i.shopify_weight_g or "",
                "Estimated Weight (g)": i.estimated_weight_g or "",
                "Price": f"${i.price:.2f}" if i.price else "",
                "Est. Shipping": f"${i.estimated_shipping:.2f}" if i.estimated_shipping else "",
                "Shipping/Price %": f"{i.shipping_to_price_pct:.0f}%" if i.shipping_to_price_pct else "",
            })
