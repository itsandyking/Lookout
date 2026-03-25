"""Alt text generation for Shopify product images.

Pattern:
  - Color/Style option: "{Vendor} {Title} - {option1_value}"
  - Size/other/Default Title: "{Vendor} {Title}"
"""

from __future__ import annotations

from openpyxl import Workbook

from lookout.store import LookoutStore
from lookout.taxonomy.mappings import EXCLUDED_VENDORS

COLOR_OPTION_NAMES = {"color", "style"}


def build_alt_text(vendor, title, option1_name, option1_value) -> str:
    """Build WCAG-compliant alt text for a product image."""
    base = f"{vendor} {title}".strip()
    if (
        option1_name
        and option1_value
        and option1_name.lower() in COLOR_OPTION_NAMES
        and option1_value.lower() != "default title"
    ):
        return f"{base} - {option1_value}"
    return base


def generate_alt_text_xlsx(output_path, store: LookoutStore) -> dict:
    """Generate Matrixify XLSX with alt text for product images.

    Args:
        output_path: Path to write XLSX
        store: LookoutStore instance

    Returns:
        dict with 'products' and 'images' counts.
    """
    products = store.list_products()

    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(["ID", "Handle", "Command", "Image Src", "Image Command", "Image Alt Text"])

    product_ids = set()
    seen_images = set()
    image_count = 0

    for product in products:
        if product["vendor"] in EXCLUDED_VENDORS:
            continue

        variants = store.get_variants(product["id"])
        # Sort variants by option1_value for consistent output
        variants.sort(key=lambda v: v.get("option1_value", ""))

        for variant in variants:
            image_src = variant["image_src"]
            if not image_src:
                continue

            key = (product["id"], image_src)
            if key in seen_images:
                continue
            seen_images.add(key)

            alt = build_alt_text(
                product["vendor"],
                product["title"],
                variant["option1_name"],
                variant["option1_value"],
            )
            product_ids.add(product["id"])
            image_count += 1
            ws.append([product["id"], product["handle"], "MERGE", image_src, "MERGE", alt])

    wb.save(output_path)
    return {"products": len(product_ids), "images": image_count}
