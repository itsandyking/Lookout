"""Lookout's interface to TVR data.

This is the ONLY module that imports from tvr. All other Lookout modules
receive plain dicts, never SQLAlchemy models.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class LookoutStore:
    """Wraps TVR's ShopifyStore and VendorStore for Lookout's needs."""

    def __init__(self, db_url: str | None = None) -> None:
        from tvr.db.store import ShopifyStore

        self._store = ShopifyStore(db_url) if db_url else ShopifyStore()

    # --- Product data ---

    def list_vendors(self) -> list[str]:
        return self._store.list_vendors()

    def list_product_types(self) -> list[str]:
        return self._store.list_product_types()

    def list_collections(self) -> list[dict]:
        return self._store.list_collections()

    def list_products(
        self,
        vendor: str | None = None,
        product_type: str | None = None,
        status: str = "active",
    ) -> list[dict]:
        products = self._store.list_products(
            vendor=vendor, product_type=product_type, status=status
        )
        return [self._product_to_dict(p) for p in products]

    def get_product(self, handle: str) -> dict | None:
        results = self._store.search_products(handle, limit=10)
        for p in results:
            if p.handle == handle:
                return self._product_to_dict(p)
        return None

    # --- Variant data ---

    def get_variants(self, product_id: int) -> list[dict]:
        variants = self._store.get_variants_by_product(product_id)
        return [self._variant_to_dict(v) for v in variants]

    def get_variant_by_barcode(self, barcode: str) -> dict | None:
        v = self._store.get_variant_by_barcode(barcode)
        return self._variant_to_dict(v) if v else None

    # --- Inventory + sales ---
    # TODO: Move to TVR ShopifyStore

    def get_inventory(self, product_id: int) -> dict:
        """Get aggregated inventory data for a product."""
        with self._store.session() as s:
            from tvr.db.models import InventoryItem, InventoryLevel, Location, Variant

            locations = {loc.id: loc.name for loc in s.query(Location).all()}
            variants = s.query(Variant).filter(Variant.product_id == product_id).all()

            total = 0
            value = 0.0
            full_price_value = 0.0
            by_location: dict[str, int] = {}

            for v in variants:
                levels = (
                    s.query(InventoryLevel)
                    .join(InventoryItem)
                    .filter(InventoryItem.variant_id == v.id)
                    .all()
                )
                for level in levels:
                    qty = max(0, level.available or 0)
                    total += qty
                    cost = v.cost or 0.0
                    value += qty * cost
                    if not v.compare_at_price:
                        full_price_value += qty * cost
                    loc_name = locations.get(
                        level.location_id, f"loc_{level.location_id}"
                    )
                    by_location[loc_name] = by_location.get(loc_name, 0) + qty

            return {
                "total": total,
                "value": round(value, 2),
                "full_price_value": round(full_price_value, 2),
                "by_location": by_location,
            }

    # TODO: Move to TVR ShopifyStore
    def get_sales_velocity(self, product_id: int, days: int = 28) -> dict:
        """Get sales velocity for a product over a period."""
        with self._store.session() as s:
            from datetime import UTC, datetime, timedelta

            from sqlalchemy import func
            from tvr.db.models import Order, OrderLineItem, Variant

            cutoff = datetime.now(UTC) - timedelta(days=days)
            variant_skus = [
                v.sku
                for v in s.query(Variant)
                .filter(Variant.product_id == product_id)
                .all()
                if v.sku
            ]

            if not variant_skus:
                return {"units": 0, "weekly_avg": 0.0}

            total_units = (
                s.query(func.coalesce(func.sum(OrderLineItem.quantity), 0))
                .join(Order)
                .filter(OrderLineItem.sku.in_(variant_skus))
                .filter(Order.created_at >= cutoff)
                .scalar()
            )

            weeks = days / 7.0
            return {
                "units": int(total_units),
                "weekly_avg": round(float(total_units) / weeks, 2),
            }

    # --- Catalog data ---
    # TODO: Move to TVR VendorStore

    def find_catalog_image(self, barcode: str) -> str | None:
        """Find a catalog image URL by barcode."""
        with self._store.session() as s:
            from tvr.db.models_vendor import CatalogItem

            item = (
                s.query(CatalogItem)
                .filter(
                    CatalogItem.upc == barcode,
                    CatalogItem.image_url.isnot(None),
                    CatalogItem.image_url != "",
                )
                .first()
            )
            return item.image_url if item else None

    # TODO: Move to TVR VendorStore
    def find_catalog_image_by_style(
        self, vendor: str, style: str, color: str
    ) -> str | None:
        """Find a catalog image URL by vendor style code and color."""
        with self._store.session() as s:
            from tvr.db.models_vendor import CatalogItem

            items = (
                s.query(CatalogItem)
                .filter(
                    CatalogItem.vendor == vendor,
                    CatalogItem.style == style,
                    CatalogItem.image_url.isnot(None),
                    CatalogItem.image_url != "",
                )
                .all()
            )

            color_lower = color.lower().strip()
            for item in items:
                if (
                    item.color_name
                    and item.color_name.lower().strip() == color_lower
                ):
                    return item.image_url

            for item in items:
                if not item.color_name:
                    continue
                catalog_color = item.color_name.lower().strip()
                if color_lower in catalog_color or catalog_color in color_lower:
                    return item.image_url

            return None

    # TODO: Move to TVR VendorStore
    def find_catalog_description(self, product_id: int) -> str | None:
        """Find a catalog description for a product."""
        with self._store.session() as s:
            from tvr.db.models_vendor import CatalogItem, VendorStyleMap

            mapping = (
                s.query(VendorStyleMap)
                .filter(VendorStyleMap.product_id == product_id)
                .first()
            )
            if mapping:
                item = (
                    s.query(CatalogItem)
                    .filter(
                        CatalogItem.vendor == mapping.vendor,
                        CatalogItem.style == mapping.style_code,
                        CatalogItem.description.isnot(None),
                        CatalogItem.description != "",
                    )
                    .first()
                )
                if item:
                    return item.description.strip()
            return None

    # --- Collections ---
    # TODO: Move to TVR ShopifyStore

    def get_collection_products(self, handle: str) -> list[dict]:
        """Get products in a collection."""
        with self._store.session() as s:
            from tvr.db.models import Collection, CollectionProduct, Product

            collection = (
                s.query(Collection).filter(Collection.handle == handle).first()
            )
            if not collection:
                return []

            product_ids = (
                s.query(CollectionProduct.product_id)
                .filter(CollectionProduct.collection_id == collection.id)
                .all()
            )
            pids = [pid for (pid,) in product_ids]
            if not pids:
                return []
            products = (
                s.query(Product)
                .filter(Product.id.in_(pids), Product.status == "active")
                .all()
            )
            return [self._product_to_dict(p) for p in products]

    # --- Conversion helpers ---

    @staticmethod
    def _product_to_dict(p: Any) -> dict:
        return {
            "id": p.id,
            "handle": p.handle,
            "title": p.title or "",
            "body_html": p.body_html or "",
            "vendor": p.vendor or "",
            "product_type": p.product_type or "",
            "tags": p.tags or "",
            "status": p.status or "",
            "created_at": p.created_at,
        }

    @staticmethod
    def _variant_to_dict(v: Any) -> dict:
        return {
            "id": v.id,
            "product_id": v.product_id,
            "sku": v.sku or "",
            "barcode": v.barcode or "",
            "price": v.price or 0.0,
            "compare_at_price": v.compare_at_price,
            "cost": v.cost or 0.0,
            "option1_name": v.option1_name or "",
            "option1_value": v.option1_value or "",
            "option2_name": v.option2_name or "",
            "option2_value": v.option2_value or "",
            "option3_name": v.option3_name or "",
            "option3_value": v.option3_value or "",
            "image_src": v.image_src or "",
            "position": v.position,
        }
