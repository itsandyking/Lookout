"""Alt text generation for Shopify product images.

Pattern:
  - Color/Style option: "{Vendor} {Title} - {option1_value}"
  - Size/other/Default Title: "{Vendor} {Title}"
"""

from openpyxl import Workbook

COLOR_OPTION_NAMES = {"color", "style"}

EXCLUDED_VENDORS = {
    "The Switchback",
    "The Mountain Air",
    "The Mountain Air Back Shop",
    "The Mountain Air Deposits",
}


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


def generate_alt_text_xlsx(output_path, store) -> dict:
    """Generate Matrixify XLSX with alt text for product images.

    Args:
        output_path: Path to write XLSX
        store: ShopifyStore instance

    Returns:
        dict with 'products' and 'images' counts.
    """
    with store.session() as s:
        from tvr.db.models import Product, Variant

        rows = (
            s.query(
                Product.id,
                Product.handle,
                Product.title,
                Product.vendor,
                Variant.option1_name,
                Variant.option1_value,
                Variant.image_src,
            )
            .join(Variant, Variant.product_id == Product.id)
            .filter(
                Product.status == "active",
                Variant.image_src.isnot(None),
                Variant.image_src != "",
                Product.vendor.notin_(EXCLUDED_VENDORS),
            )
            .order_by(Product.handle, Variant.option1_value)
            .all()
        )

    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(["ID", "Handle", "Command", "Image Src", "Image Command", "Image Alt Text"])

    product_ids = set()
    seen_images = set()
    image_count = 0
    for product_id, handle, title, vendor, opt1_name, opt1_value, image_src in rows:
        key = (product_id, image_src)
        if key in seen_images:
            continue
        seen_images.add(key)
        alt = build_alt_text(vendor or "", title or "", opt1_name, opt1_value)
        product_ids.add(product_id)
        image_count += 1
        ws.append([product_id, handle, "MERGE", image_src, "MERGE", alt])

    wb.save(output_path)
    return {"products": len(product_ids), "images": image_count}
