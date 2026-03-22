"""Matrixify integration — import Shopify state, enrich images, export for upload.

Three main classes:
- MatrixifyImporter: Backfill variant images/positions from a Matrixify XLSX export
- ImageEnricher: Fill image gaps using catalog data (barcode + style-map matching)
- MatrixifyExporter: Export enrichments as Matrixify-compatible CSV for Shopify upload
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from tvr.core.sizes import sort_key
from tvr.db.models import Product, Variant
from tvr.db.models_vendor import CatalogItem, VendorStyleMap
from tvr.db.store import ShopifyStore

# ---------------------------------------------------------------------------
# MatrixifyImporter
# ---------------------------------------------------------------------------


@dataclass
class MatrixifyImportResult:
    """Result of importing a Matrixify XLSX export."""

    variants_updated: int = 0
    variants_skipped: int = 0  # not found in DB
    images_set: int = 0
    positions_set: int = 0
    products_seen: int = 0


class MatrixifyImporter:
    """Import variant image assignments and positions from a Matrixify XLSX export."""

    def __init__(self, store: ShopifyStore) -> None:
        self.store = store

    def import_file(self, path: str | Path) -> MatrixifyImportResult:
        """Read a Matrixify Products XLSX and update variant image_src/position."""
        import openpyxl

        path = Path(path)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb["Products"] if "Products" in wb.sheetnames else wb.active

        # Read headers from row 1
        headers = []
        for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
            headers = [str(h) if h else "" for h in row]

        col = {h: i for i, h in enumerate(headers)}
        result = MatrixifyImportResult()
        product_ids_seen = set()

        with self.store.session() as session:
            for row in ws.iter_rows(min_row=2, values_only=True):
                values = list(row)
                if "Variant ID" not in col:
                    continue
                if not values or not values[col["Variant ID"]]:
                    continue

                variant_id = values[col["Variant ID"]]
                try:
                    variant_id = int(variant_id)
                except (ValueError, TypeError):
                    result.variants_skipped += 1
                    continue

                variant = session.query(Variant).filter(Variant.id == variant_id).first()

                if not variant:
                    result.variants_skipped += 1
                    continue

                updated = False

                # Image
                image_url = values[col.get("Variant Image", -1)] if "Variant Image" in col else None
                if image_url and str(image_url).strip():
                    variant.image_src = str(image_url).strip()
                    result.images_set += 1
                    updated = True

                # Position
                position = (
                    values[col.get("Variant Position", -1)] if "Variant Position" in col else None
                )
                if position is not None:
                    try:
                        variant.position = int(position)
                        result.positions_set += 1
                        updated = True
                    except (ValueError, TypeError):
                        pass

                if updated:
                    result.variants_updated += 1

                # Track products
                product_id = values[col.get("ID", 0)]
                if product_id:
                    product_ids_seen.add(product_id)

            session.commit()

        result.products_seen = len(product_ids_seen)
        wb.close()
        return result


# ---------------------------------------------------------------------------
# ImageEnricher
# ---------------------------------------------------------------------------


@dataclass
class ImageEnrichmentResult:
    """Result of the image enrichment pipeline."""

    propagated_from_shopify: int = 0  # Step A — same-color propagation
    matched_from_catalog: int = 0  # Step B — barcode lookup
    matched_from_style_map: int = 0  # Step C — style+color match
    still_missing: int = 0  # Could not find image
    products_processed: int = 0
    description_enrichments: list[dict] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)
    # [{variant_id, product_id, product_handle, image_url, source, source_id}, ...]


class ImageEnricher:
    """Enrich variant images using existing Shopify data and catalog database.

    Three-step pipeline per color group:
    A. Propagate images from variants that already have them (same color)
    B. Look up variant barcodes in catalog_items for image_url
    C. Fall back to style-map + color-name matching against catalog
    """

    def __init__(self, store: ShopifyStore) -> None:
        self.store = store

    def enrich(
        self,
        session: Session,
        vendor: str | None = None,
        dry_run: bool = False,
    ) -> ImageEnrichmentResult:
        """Run the full enrichment pipeline.

        Args:
            session: SQLAlchemy session
            vendor: Optional vendor filter
            dry_run: If True, don't write to DB, just collect assignments
        """
        result = ImageEnrichmentResult()

        # Get all products (optionally filtered by vendor)
        product_query = session.query(Product).filter(Product.status == "active")
        if vendor:
            product_query = product_query.filter(Product.vendor == vendor)
        products = product_query.all()

        for product in products:
            variants = session.query(Variant).filter(Variant.product_id == product.id).all()
            if not variants:
                continue

            result.products_processed += 1
            self._enrich_product(session, product, variants, result, dry_run)

            # Description enrichment (Phase 5)
            if not product.body_html or not product.body_html.strip():
                desc = self._find_description(session, product, variants)
                if desc:
                    result.description_enrichments.append(
                        {
                            "product_id": product.id,
                            "product_handle": product.handle,
                            "body_html": desc,
                        }
                    )

        if not dry_run:
            session.commit()

        return result

    def _enrich_product(
        self,
        session: Session,
        product: Product,
        variants: list[Variant],
        result: ImageEnrichmentResult,
        dry_run: bool,
    ) -> None:
        """Enrich a single product's variants with images."""
        # Determine if this product uses Color as Option1
        has_color_option = any(
            v.option1_name and v.option1_name.lower() == "color" for v in variants
        )

        if has_color_option:
            # Group variants by color (Option1 Value)
            color_groups: dict[str, list[Variant]] = {}
            for v in variants:
                color = v.option1_value or "Unknown"
                color_groups.setdefault(color, []).append(v)

            for color, group in color_groups.items():
                self._enrich_color_group(session, product, group, color, result, dry_run)
        else:
            # No color grouping — treat each variant independently
            for v in variants:
                if v.image_src:
                    continue  # Already has image
                self._enrich_single_variant(session, product, v, result, dry_run)

    def _enrich_color_group(
        self,
        session: Session,
        product: Product,
        variants: list[Variant],
        color: str,
        result: ImageEnrichmentResult,
        dry_run: bool,
    ) -> None:
        """Enrich a color group of variants."""
        # Split into has-image vs needs-image
        with_image = [v for v in variants if v.image_src]
        without_image = [v for v in variants if not v.image_src]

        if not without_image:
            return  # All variants already have images

        # Step A: Same-color propagation
        if with_image:
            source_variant = with_image[0]
            image_url = source_variant.image_src
            for v in without_image:
                self._assign_image(
                    session,
                    v,
                    product,
                    image_url,
                    source="shopify_propagation",
                    source_id=source_variant.id,
                    result=result,
                    dry_run=dry_run,
                )
                result.propagated_from_shopify += 1
            return

        # Step B: Catalog barcode lookup
        image_url = None
        source_catalog_id = None
        for v in variants:
            if not v.barcode:
                continue
            catalog_item = (
                session.query(CatalogItem)
                .filter(
                    CatalogItem.upc == v.barcode,
                    CatalogItem.image_url.isnot(None),
                    CatalogItem.image_url != "",
                )
                .first()
            )
            if catalog_item:
                image_url = catalog_item.image_url
                source_catalog_id = catalog_item.id
                break

        if image_url:
            for v in without_image:
                self._assign_image(
                    session,
                    v,
                    product,
                    image_url,
                    source="catalog_barcode",
                    source_id=source_catalog_id,
                    result=result,
                    dry_run=dry_run,
                )
                result.matched_from_catalog += 1
            return

        # Step C: Style-map + color match
        image_url = self._find_by_style_color(session, product, color)
        if image_url:
            for v in without_image:
                self._assign_image(
                    session,
                    v,
                    product,
                    image_url,
                    source="catalog_style_color",
                    source_id=None,
                    result=result,
                    dry_run=dry_run,
                )
                result.matched_from_style_map += 1
            return

        # No image found
        result.still_missing += len(without_image)

    def _enrich_single_variant(
        self,
        session: Session,
        product: Product,
        variant: Variant,
        result: ImageEnrichmentResult,
        dry_run: bool,
    ) -> None:
        """Enrich a single variant (no color grouping)."""
        # Step B: Catalog barcode lookup
        if variant.barcode:
            catalog_item = (
                session.query(CatalogItem)
                .filter(
                    CatalogItem.upc == variant.barcode,
                    CatalogItem.image_url.isnot(None),
                    CatalogItem.image_url != "",
                )
                .first()
            )
            if catalog_item:
                self._assign_image(
                    session,
                    variant,
                    product,
                    catalog_item.image_url,
                    source="catalog_barcode",
                    source_id=catalog_item.id,
                    result=result,
                    dry_run=dry_run,
                )
                result.matched_from_catalog += 1
                return

        # No image found
        result.still_missing += 1

    def _find_by_style_color(
        self,
        session: Session,
        product: Product,
        color: str,
    ) -> str | None:
        """Find a catalog image URL via style-map + color-name matching."""
        # Find VendorStyleMap entry for this product
        mapping = (
            session.query(VendorStyleMap)
            .filter(
                VendorStyleMap.product_id == product.id,
            )
            .first()
        )
        if not mapping:
            return None

        # Find catalog items for this (vendor, style_code)
        catalog_items = (
            session.query(CatalogItem)
            .filter(
                CatalogItem.vendor == mapping.vendor,
                CatalogItem.style == mapping.style_code,
                CatalogItem.image_url.isnot(None),
                CatalogItem.image_url != "",
            )
            .all()
        )
        if not catalog_items:
            return None

        # Try exact color match first
        color_lower = color.lower().strip()
        for item in catalog_items:
            if item.color_name and item.color_name.lower().strip() == color_lower:
                return item.image_url

        # Try fuzzy color matching (substring in either direction)
        for item in catalog_items:
            if not item.color_name:
                continue
            catalog_color = item.color_name.lower().strip()
            if color_lower in catalog_color or catalog_color in color_lower:
                return item.image_url

        # Try color code matching (e.g., "BLK" in variant color "Black")
        for item in catalog_items:
            if not item.color_code:
                continue
            code_lower = item.color_code.lower().strip()
            if code_lower in color_lower or color_lower in code_lower:
                return item.image_url

        # Last resort: just use any image from this style (better than nothing)
        # Don't do this — it could assign the wrong color's image
        return None

    def _find_description(
        self,
        session: Session,
        product: Product,
        variants: list[Variant],
    ) -> str | None:
        """Find a catalog description for this product."""
        # Try via style map first
        mapping = (
            session.query(VendorStyleMap)
            .filter(
                VendorStyleMap.product_id == product.id,
            )
            .first()
        )
        if mapping:
            catalog_item = (
                session.query(CatalogItem)
                .filter(
                    CatalogItem.vendor == mapping.vendor,
                    CatalogItem.style == mapping.style_code,
                    CatalogItem.description.isnot(None),
                    CatalogItem.description != "",
                )
                .first()
            )
            if catalog_item:
                return catalog_item.description.strip()

        # Try via barcode
        for v in variants:
            if not v.barcode:
                continue
            catalog_item = (
                session.query(CatalogItem)
                .filter(
                    CatalogItem.upc == v.barcode,
                    CatalogItem.description.isnot(None),
                    CatalogItem.description != "",
                )
                .first()
            )
            if catalog_item:
                return catalog_item.description.strip()

        return None

    def _assign_image(
        self,
        session: Session,
        variant: Variant,
        product: Product,
        image_url: str,
        source: str,
        source_id: int | None,
        result: ImageEnrichmentResult,
        dry_run: bool,
    ) -> None:
        """Assign an image URL to a variant and record the assignment."""
        if not dry_run:
            variant.image_src = image_url

        result.assignments.append(
            {
                "variant_id": variant.id,
                "product_id": product.id,
                "product_handle": product.handle,
                "image_url": image_url,
                "source": source,
                "source_id": source_id,
            }
        )


# ---------------------------------------------------------------------------
# MatrixifyExporter
# ---------------------------------------------------------------------------


class MatrixifyExporter:
    """Export enrichments as Matrixify-compatible CSV for Shopify upload."""

    @staticmethod
    def export_enriched_images(assignments: list[dict]) -> str:
        """Export image enrichment assignments as Matrixify CSV.

        Columns: ID, Handle, Command, Variant ID, Variant Command, Variant Image

        - Command: MERGE on first row per product, blank on subsequent
        - Variant Command: MERGE on all rows
        """
        output = io.StringIO()
        fieldnames = [
            "ID",
            "Handle",
            "Command",
            "Variant ID",
            "Variant Command",
            "Variant Image",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        # Group by product to set Command correctly
        by_product: dict[int, list[dict]] = {}
        for a in assignments:
            by_product.setdefault(a["product_id"], []).append(a)

        for _product_id, product_assignments in by_product.items():
            for i, a in enumerate(product_assignments):
                writer.writerow(
                    {
                        "ID": a["product_id"],
                        "Handle": a["product_handle"],
                        "Command": "MERGE" if i == 0 else "",
                        "Variant ID": a["variant_id"],
                        "Variant Command": "MERGE",
                        "Variant Image": a["image_url"],
                    }
                )

        return output.getvalue()

    @staticmethod
    def export_descriptions(enrichments: list[dict]) -> str:
        """Export description enrichments as Matrixify CSV.

        Columns: ID, Handle, Command, Body HTML
        """
        output = io.StringIO()
        fieldnames = ["ID", "Handle", "Command", "Body HTML"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for e in enrichments:
            writer.writerow(
                {
                    "ID": e["product_id"],
                    "Handle": e["product_handle"],
                    "Command": "MERGE",
                    "Body HTML": e["body_html"],
                }
            )

        return output.getvalue()


# ---------------------------------------------------------------------------
# Variant Sort Order Assessment
# ---------------------------------------------------------------------------


@dataclass
class SortViolation:
    """A single variant sort violation within a product."""

    product_handle: str
    product_title: str
    expected_order: list[str]  # What the order should be
    actual_order: list[str]  # What it actually is


@dataclass
class SortAssessmentResult:
    """Result of variant sort order assessment."""

    correctly_sorted: int = 0
    violations: list[SortViolation] = field(default_factory=list)
    no_position_data: int = 0
    total_products: int = 0


def assess_variant_sort_order(session: Session, vendor: str | None = None) -> SortAssessmentResult:
    """Assess variant sort order against Color-first convention.

    Convention: All sizes of one color before next color, sizes in canonical order.
    """
    result = SortAssessmentResult()

    product_query = session.query(Product).filter(Product.status == "active")
    if vendor:
        product_query = product_query.filter(Product.vendor == vendor)
    products = product_query.all()

    for product in products:
        variants = session.query(Variant).filter(Variant.product_id == product.id).all()
        if len(variants) <= 1:
            continue

        result.total_products += 1

        # Check if any variants have position data
        has_positions = any(v.position is not None for v in variants)
        if not has_positions:
            result.no_position_data += 1
            continue

        # Check if product has Color option
        has_color = any(v.option1_name and v.option1_name.lower() == "color" for v in variants)
        if not has_color:
            # Non-color products: check size ordering
            sorted_by_pos = sorted(variants, key=lambda v: v.position or 0)
            actual_sizes = [_variant_label(v) for v in sorted_by_pos]
            expected_sizes = sorted(actual_sizes, key=lambda s: sort_key(s))

            if actual_sizes == expected_sizes:
                result.correctly_sorted += 1
            else:
                result.violations.append(
                    SortViolation(
                        product_handle=product.handle,
                        product_title=product.title,
                        expected_order=expected_sizes,
                        actual_order=actual_sizes,
                    )
                )
            continue

        # Color-first ordering: group by color, sort colors alphabetically,
        # within each color sort sizes canonically
        sorted_by_pos = sorted(variants, key=lambda v: v.position or 0)
        actual_order = [_variant_label(v) for v in sorted_by_pos]

        # Build expected order: Color-first, sizes within each color
        color_groups: dict[str, list[Variant]] = {}
        for v in variants:
            color = v.option1_value or "Unknown"
            color_groups.setdefault(color, []).append(v)

        # Determine color order from current positions (first appearance)
        color_order = []
        seen_colors = set()
        for v in sorted_by_pos:
            color = v.option1_value or "Unknown"
            if color not in seen_colors:
                color_order.append(color)
                seen_colors.add(color)

        # Build expected: within each color block, sizes should be in canonical order
        expected_order = []
        for color in color_order:
            group = color_groups[color]
            sorted_group = sorted(
                group, key=lambda v: sort_key(v.option2_value or v.option1_value or "")
            )
            expected_order.extend([_variant_label(v) for v in sorted_group])

        if actual_order == expected_order:
            result.correctly_sorted += 1
        else:
            result.violations.append(
                SortViolation(
                    product_handle=product.handle,
                    product_title=product.title,
                    expected_order=expected_order,
                    actual_order=actual_order,
                )
            )

    return result


def _variant_label(v: Variant) -> str:
    """Create a label for a variant for sort display."""
    parts = []
    if v.option1_value:
        parts.append(v.option1_value)
    if v.option2_value:
        parts.append(v.option2_value)
    if v.option3_value:
        parts.append(v.option3_value)
    return " / ".join(parts) if parts else v.sku or str(v.id)
