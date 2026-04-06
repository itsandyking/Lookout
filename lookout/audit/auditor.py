"""Content Auditor.

Audits Shopify product content for gaps -- missing images, short descriptions,
missing product type, missing tags -- and prioritizes them by inventory value
for merchandising improvement.
"""

from __future__ import annotations

import logging
import re

from lookout.audit.gmc_signals import GMCSignals
from lookout.audit.models import MIN_DESCRIPTION_LENGTH, AuditResult, ProductScore
from lookout.audit.online_signals import OnlineSignals
from lookout.store import LookoutStore
from lookout.taxonomy.mappings import EXCLUDED_VENDORS

logger = logging.getLogger(__name__)


def _extract_product_id(
    offer_id: str,
    cache: dict[int, int],
    store: LookoutStore,
) -> int | None:
    """Extract the Shopify product_id from a GMC offer_id.

    Shopify's Google channel uses the format ``shopify_US_{variant_id}``.
    We look up the variant in the store to find its parent product_id,
    caching results for efficiency.

    Returns the product_id (int) or None if the offer_id can't be resolved.
    """
    parts = offer_id.split("_")

    # Format: shopify_XX_{variant_id}  (3 parts, standard Shopify Google channel)
    if len(parts) == 3 and parts[0].lower() == "shopify":
        try:
            vid = int(parts[2])
        except (ValueError, IndexError):
            return None
        if vid in cache:
            return cache[vid]
        # Look up variant → product in the store
        variant = store.get_variant_by_id(vid)
        if variant:
            pid = variant["product_id"]
            cache[vid] = pid
            return pid
        logger.debug("GMC offer_id %s: variant %d not found in store", offer_id, vid)
        return None

    # Legacy/alternative format: shopify_XX_{product_id}_{variant_id} (4 parts)
    if len(parts) >= 4 and parts[0].lower() == "shopify":
        try:
            return int(parts[2])
        except (ValueError, IndexError):
            return None

    # Bare numeric ID (plain variant_id or product_id) — skip, ambiguous
    return None


class ContentAuditor:
    """Audits Shopify product content for gaps.

    Uses LookoutStore to fetch product, variant, and inventory data as plain
    dicts and evaluates each product for:
    - Product-level image (inferred from variant images)
    - Variant-level images (variant["image_src"])
    - Description length (HTML stripped, >= MIN_DESCRIPTION_LENGTH chars)
    - Product type
    - Tags

    When online signals are provided, products with high sessions but low
    conversion get boosted priority (content opportunity signal).

    Returns an AuditResult with per-product scores and priority ranking.
    """

    def __init__(
        self,
        store: LookoutStore,
        min_description_length: int = MIN_DESCRIPTION_LENGTH,
        exclude_house_brands: bool = True,
        online_signals: dict[str, OnlineSignals] | None = None,
        gmc_signals: dict[str, GMCSignals] | None = None,
    ) -> None:
        self.store = store
        self.min_description_length = min_description_length
        self.exclude_house_brands = exclude_house_brands
        self.online_signals = online_signals or {}
        self.gmc_signals = gmc_signals or {}

        # Build product_id → aggregated GMC signals lookup.
        # Shopify's Google channel submits offer_ids as
        # "shopify_US_{variant_id}" (3 parts).  We need a variant→product
        # reverse map so we can aggregate per-variant GMC data up to the
        # product level.
        self._variant_to_product: dict[int, int] = {}
        self._gmc_by_product: dict[int, GMCSignals] = {}
        for offer_id, sig in self.gmc_signals.items():
            pid = _extract_product_id(offer_id, self._variant_to_product, self.store)
            if pid is None:
                continue
            if pid in self._gmc_by_product:
                existing = self._gmc_by_product[pid]
                existing.clicks += sig.clicks
                existing.impressions += sig.impressions
                if sig.disapproved:
                    existing.disapproved = True
                existing.issues.extend(sig.issues)
            else:
                self._gmc_by_product[pid] = GMCSignals(
                    offer_id=offer_id,
                    title=sig.title,
                    clicks=sig.clicks,
                    impressions=sig.impressions,
                    disapproved=sig.disapproved,
                    issues=list(sig.issues),
                )
        # Recalculate CTR after aggregation
        for sig in self._gmc_by_product.values():
            sig.ctr = sig.clicks / sig.impressions if sig.impressions > 0 else 0.0

    def audit(self, vendor: str | None = None) -> AuditResult:
        """Run content audit on all active products (or filtered by vendor)."""
        products = self.store.list_products(vendor=vendor, status="active")
        if self.exclude_house_brands:
            products = [p for p in products if p.get("vendor", "") not in EXCLUDED_VENDORS]
        scores: list[ProductScore] = []

        for product in products:
            score = self._score_product(product)
            # Enrich with online signals if available (matched by title)
            title = product.get("title", "")
            needs_recalc = False
            if title in self.online_signals:
                sig = self.online_signals[title]
                score.online_sessions = sig.sessions
                score.online_conversion_rate = sig.conversion_rate
                score.online_revenue = sig.online_revenue
                score.online_orders = sig.orders
                score.opportunity_gap = sig.opportunity_gap
                needs_recalc = True

            # Enrich with GMC signals if available (matched by product_id)
            gmc_sig = self._gmc_by_product.get(score.product_id)
            if gmc_sig:
                score.gmc_clicks = gmc_sig.clicks
                score.gmc_impressions = gmc_sig.impressions
                score.gmc_ctr = gmc_sig.ctr
                score.gmc_disapproved = gmc_sig.disapproved
                score.gmc_issues = gmc_sig.issues
                score.discovery_gap = gmc_sig.discovery_gap
                needs_recalc = True

            if needs_recalc:
                score.calculate_gaps()

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
            _variants_raw=variants,
            total_inventory=total_inventory,
            price=avg_price,
            cost=avg_cost,
            inventory_value=inventory_value,
            full_price_inventory_value=full_price_value,
        )
        score.calculate_gaps()
        return score
