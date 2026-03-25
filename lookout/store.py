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
        from tvr.db.vendor_store import VendorStore

        self._store = ShopifyStore(db_url) if db_url else ShopifyStore()
        self._vendor_store = VendorStore()

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

    def get_inventory(self, product_id: int) -> dict:
        """Get aggregated inventory data for a product."""
        return self._store.get_product_inventory(product_id)

    def get_sales_velocity(self, product_id: int, days: int = 28) -> dict:
        """Get sales velocity for a product over a period."""
        return self._store.get_product_sales_velocity(product_id, days=days)

    # --- Catalog data ---

    def find_catalog_image(self, barcode: str) -> str | None:
        """Find a catalog image URL by barcode."""
        return self._vendor_store.find_image_by_upc(barcode)

    def find_catalog_image_by_style(
        self, vendor: str, style: str, color: str
    ) -> str | None:
        """Find a catalog image URL by vendor style code and color."""
        return self._vendor_store.find_image_by_style_color(vendor, style, color)

    def find_catalog_description(self, product_id: int) -> str | None:
        """Find a catalog description for a product."""
        return self._vendor_store.find_description_by_product(product_id)

    # --- Collections ---

    def get_collection_products(self, handle: str) -> list[dict]:
        """Get products in a collection."""
        products = self._store.get_collection_products(handle)
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
