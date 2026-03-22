"""Module 7: Merchandising Ranking.

Manages product sort order within Shopify collections for website merchandising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from tvr.core.config import (
    LOCATIONS,
    LOW_INVENTORY_THRESHOLD,
    MERCH_WEIGHTS,
    NEW_ARRIVAL_DAYS,
)
from tvr.db.models import (
    Collection,
    CollectionProduct,
    InventoryItem,
    InventoryLevel,
    Order,
    OrderLineItem,
    Product,
    Variant,
)
from tvr.db.store import ShopifyStore


@dataclass
class RankedProduct:
    """A product with its merchandising score and rank."""

    product_id: int
    handle: str
    title: str
    vendor: str

    # Score components
    velocity_score: float = 0.0
    margin_score: float = 0.0
    inventory_health_score: float = 0.0
    new_arrival_boost: float = 0.0
    low_inventory_penalty: float = 0.0

    # Overrides
    pinned_position: int | None = None
    boost: float = 0.0
    buried: bool = False

    # Final
    total_score: float = 0.0
    rank: int = 0

    # Raw metrics (for display)
    weekly_units: float = 0.0
    margin_pct: float = 0.0
    total_inventory: int = 0
    weeks_of_supply: float = 0.0
    days_since_creation: int = 0


@dataclass
class RankingResult:
    """Result of ranking products in a collection."""

    collection_name: str
    products: list[RankedProduct]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ranked(self) -> list[RankedProduct]:
        """Products sorted by final rank."""
        return sorted(self.products, key=lambda p: p.rank)

    def summary_markdown(self) -> str:
        lines = [
            f"# Merchandising Ranking: {self.collection_name}",
            f"Generated: {self.generated_at.strftime('%Y-%m-%d %H:%M')}",
            "",
            "| Rank | Product | Velocity | Margin | Inv | Score | Notes |",
            "|-----:|---------|----------|--------|-----|------:|-------|",
        ]
        for p in self.ranked[:50]:
            notes = []
            if p.pinned_position is not None:
                notes.append(f"pinned #{p.pinned_position}")
            if p.boost > 0:
                notes.append("boosted")
            if p.buried:
                notes.append("buried")
            if p.days_since_creation <= NEW_ARRIVAL_DAYS:
                notes.append("new")
            if p.total_inventory < LOW_INVENTORY_THRESHOLD:
                notes.append("low stock")

            lines.append(
                f"| {p.rank} | {p.title[:40]} | "
                f"{p.weekly_units:.1f}/wk | {p.margin_pct:.0f}% | "
                f"{p.total_inventory} | {p.total_score:.2f} | {', '.join(notes)} |"
            )
        return "\n".join(lines)


class Merchandiser:
    """Calculate and manage product rankings within collections."""

    def __init__(self, store: ShopifyStore) -> None:
        self.store = store

    def rank_collection(
        self,
        collection_handle: str | None = None,
        vendor: str | None = None,
        product_type: str | None = None,
        overrides: dict[str, dict] | None = None,
        reference_date: datetime | None = None,
        limit: int = 200,
    ) -> RankingResult:
        """Rank products for a collection or filtered product set.

        Args:
            collection_handle: Collection to rank (by handle)
            vendor: Filter by vendor instead of collection
            product_type: Filter by product type
            overrides: Dict of handle -> {"pin": position, "boost": float, "bury": bool}
            reference_date: Reference date for calculations
            limit: Max products to rank
        """
        if reference_date is None:
            reference_date = datetime.now(UTC).replace(tzinfo=None)

        overrides = overrides or {}

        # Get products
        products = self._get_products(collection_handle, vendor, product_type, limit)

        # Score each product
        ranked = []
        for product in products:
            scored = self._score_product(product, reference_date)

            # Apply overrides
            handle_overrides = overrides.get(product.handle, {})
            if "pin" in handle_overrides:
                scored.pinned_position = handle_overrides["pin"]
            if "boost" in handle_overrides:
                scored.boost = handle_overrides["boost"]
            if handle_overrides.get("bury"):
                scored.buried = True

            ranked.append(scored)

        # Calculate final scores
        self._calculate_final_scores(ranked)

        # Assign ranks
        self._assign_ranks(ranked)

        collection_name = collection_handle or vendor or product_type or "All Products"
        return RankingResult(
            collection_name=collection_name,
            products=ranked,
            generated_at=reference_date,
        )

    def _get_products(
        self,
        collection_handle: str | None,
        vendor: str | None,
        product_type: str | None,
        limit: int,
    ) -> list[Product]:
        """Get products to rank."""
        with self.store.session() as s:
            if collection_handle:
                collection = (
                    s.query(Collection).filter(Collection.handle == collection_handle).first()
                )
                if collection:
                    product_ids = (
                        s.query(CollectionProduct.product_id)
                        .filter(CollectionProduct.collection_id == collection.id)
                        .all()
                    )
                    pids = [pid for (pid,) in product_ids]
                    return (
                        s.query(Product)
                        .filter(Product.id.in_(pids))
                        .filter(Product.status == "active")
                        .limit(limit)
                        .all()
                    )

            query = s.query(Product).filter(Product.status == "active")
            query = query.filter(Product.vendor != "The Switchback")

            if vendor:
                query = query.filter(Product.vendor == vendor)
            if product_type:
                query = query.filter(Product.product_type == product_type)

            return query.limit(limit).all()

    def _score_product(self, product: Product, reference_date: datetime) -> RankedProduct:
        """Score a single product on all ranking factors."""
        tma_location_id = LOCATIONS["The Mountain Air"]["id"]

        with self.store.session() as s:
            variants = s.query(Variant).filter(Variant.product_id == product.id).all()

            # Sales velocity (last 28 days) — match by SKU since order data lacks product_id
            cutoff_28d = reference_date - timedelta(days=28)
            variant_skus = [v.sku for v in variants if v.sku]
            if variant_skus:
                total_units = (
                    s.query(func.coalesce(func.sum(OrderLineItem.quantity), 0))
                    .join(Order)
                    .filter(OrderLineItem.sku.in_(variant_skus))
                    .filter(Order.created_at >= cutoff_28d)
                    .scalar()
                )
            else:
                total_units = 0
            weekly_units = float(total_units) / 4.0

            # Inventory
            total_inv = 0
            total_cost = 0.0
            total_price = 0.0
            variant_count = 0

            for v in variants:
                inv = (
                    s.query(func.coalesce(func.sum(InventoryLevel.available), 0))
                    .join(InventoryItem)
                    .filter(InventoryItem.variant_id == v.id)
                    .filter(InventoryLevel.location_id == tma_location_id)
                    .scalar()
                )
                total_inv += inv
                if v.cost and v.cost > 0:
                    total_cost += v.cost
                    variant_count += 1
                if v.price and v.price > 0:
                    total_price += v.price

            avg_cost = total_cost / variant_count if variant_count > 0 else 0
            avg_price = total_price / variant_count if variant_count > 0 else 0
            margin_pct = ((avg_price - avg_cost) / avg_price * 100) if avg_price > 0 else 0

            # Weeks of supply
            wos = total_inv / weekly_units if weekly_units > 0 else 999

            # Days since creation
            days_old = (reference_date - product.created_at).days if product.created_at else 999

        return RankedProduct(
            product_id=product.id,
            handle=product.handle,
            title=product.title,
            vendor=product.vendor,
            weekly_units=round(weekly_units, 2),
            margin_pct=round(margin_pct, 1),
            total_inventory=total_inv,
            weeks_of_supply=round(wos, 1),
            days_since_creation=days_old,
        )

    def _calculate_final_scores(self, products: list[RankedProduct]) -> None:
        """Normalize and combine scores."""
        if not products:
            return

        # Get max values for normalization
        max_velocity = max((p.weekly_units for p in products), default=1) or 1
        max_margin = max((p.margin_pct for p in products), default=1) or 1

        weights = MERCH_WEIGHTS

        for p in products:
            # Velocity score (0-1, higher = better)
            p.velocity_score = p.weekly_units / max_velocity

            # Margin score (0-1, higher = better)
            p.margin_score = max(0, min(1, p.margin_pct / max_margin))

            # Inventory health (0-1, sweet spot is 3-12 WOS)
            if 3 <= p.weeks_of_supply <= 12:
                p.inventory_health_score = 1.0
            elif p.weeks_of_supply < 3:
                p.inventory_health_score = 0.3  # Low stock
            elif p.weeks_of_supply <= 20:
                p.inventory_health_score = 0.6
            else:
                p.inventory_health_score = 0.2  # Overstocked

            # New arrival boost
            if p.days_since_creation <= NEW_ARRIVAL_DAYS:
                p.new_arrival_boost = 1.0 - (p.days_since_creation / NEW_ARRIVAL_DAYS)
            else:
                p.new_arrival_boost = 0.0

            # Low inventory penalty
            if p.total_inventory < LOW_INVENTORY_THRESHOLD:
                p.low_inventory_penalty = -1.0
            else:
                p.low_inventory_penalty = 0.0

            # Weighted total
            p.total_score = (
                p.velocity_score * weights["sales_velocity"]
                + p.margin_score * weights["margin"]
                + p.inventory_health_score * weights["inventory_health"]
                + p.new_arrival_boost * weights["new_arrival_boost"]
                + p.low_inventory_penalty * weights["low_inventory_penalty"]
                + p.boost
            )

            if p.buried:
                p.total_score = -999

    def _assign_ranks(self, products: list[RankedProduct]) -> None:
        """Assign final ranks, respecting pinned positions."""
        # Separate pinned and unpinned
        pinned = {p.pinned_position: p for p in products if p.pinned_position is not None}
        unpinned = sorted(
            [p for p in products if p.pinned_position is None],
            key=lambda p: p.total_score,
            reverse=True,
        )

        # Assign ranks
        rank = 1
        unpinned_idx = 0

        total = len(products)
        for pos in range(1, total + 1):
            if pos in pinned:
                pinned[pos].rank = pos
            elif unpinned_idx < len(unpinned):
                unpinned[unpinned_idx].rank = pos
                unpinned_idx += 1
            rank += 1

        # Handle any remaining unpinned (if pins go beyond the list)
        while unpinned_idx < len(unpinned):
            unpinned[unpinned_idx].rank = rank
            rank += 1
            unpinned_idx += 1
