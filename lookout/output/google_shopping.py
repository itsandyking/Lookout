"""Google Shopping metafield, SEO, and weight generators for Matrixify import.

Produces three files:
  1. matrixify_google_shopping.xlsx -- Google Shopping metafields + SEO meta
  2. matrixify_weights.xlsx -- Weight corrections for zero/default-weight variants
  3. weight_audit.csv -- Manual review report for dimensional/bulky products

Ported from CE's ``src/generate_google_shopping.py``, adapted to use
LookoutStore dict-based API.
"""

from __future__ import annotations

import csv
import re
from html.parser import HTMLParser

from openpyxl import Workbook

from lookout.store import LookoutStore
from lookout.taxonomy.mappings import (
    DIMENSIONAL_TYPES,
    EU_SIZING_VENDORS,
    EXCLUDED_VENDORS,
    LB_TO_GRAMS,
    MONDO_TYPES,
    PRICE_TIERS,
    PRODUCT_TYPE_TO_GOOGLE_CATEGORY,
    PRODUCT_TYPE_WEIGHT_DEFAULTS_LB,
    SEASON_TAG_MAP,
    TAG_TO_GOOGLE_AGE_GROUP,
    TAG_TO_GOOGLE_GENDER,
    TITLE_GENDER_KEYWORDS,
)

# ── Constants ────────────────────────────────────────────────────────────────

STORE_NAME = "The Mountain Air"

# Regex to detect non-size option values (volumes, weights, lengths, pack counts)
NON_SIZE_PATTERN = re.compile(
    r"^\d+\s*(oz|OZ|ml|mL|ML|L|l|g|kg|cm|mm|M|m|ft|pk|ct)\b"
    r"|^\d+(\.\d+)?\s*(oz|OZ|ml|mL|ML|L|l|g|kg|cm|mm|M|m|ft|pk|ct)\b"
    r"|^\d+\s*/\s*\d+\s*(oz|g)"
)


# ── HTML stripping ───────────────────────────────────────────────────────────


class _HTMLStripper(HTMLParser):
    """Simple HTML tag stripper that preserves text content."""

    def __init__(self) -> None:
        super().__init__()
        self.result = []

    def handle_data(self, data) -> None:
        self.result.append(data)

    def get_text(self) -> str:
        return "".join(self.result)


def strip_html(html_text) -> str:
    """Strip HTML tags and return clean text."""
    if not html_text:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(html_text)
    text = stripper.get_text()
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── Derivation functions ─────────────────────────────────────────────────────


def get_google_category(product_type) -> str:
    """Map product_type to Google Product Category path."""
    if not product_type:
        return ""
    return PRODUCT_TYPE_TO_GOOGLE_CATEGORY.get(product_type, "")


def parse_tags(tags_str) -> list[str]:
    """Parse comma-separated tags string into a list of stripped tags."""
    if not tags_str:
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def get_gender(tags_str, title) -> str:
    """Derive Google Shopping gender from tags, with title keyword fallback.

    Returns: "male", "female", "unisex", or ""
    """
    tags = parse_tags(tags_str)

    for tag in tags:
        tag_lower = tag.lower()
        if tag_lower in TAG_TO_GOOGLE_GENDER:
            return TAG_TO_GOOGLE_GENDER[tag_lower]

    # Title keyword fallback
    if title:
        title_str = title
        for keyword in TITLE_GENDER_KEYWORDS["kids"]:
            if re.search(keyword, title_str, re.IGNORECASE):
                return "unisex"
        for keyword in TITLE_GENDER_KEYWORDS["female"]:
            if re.search(keyword, title_str, re.IGNORECASE):
                return "female"
        for keyword in TITLE_GENDER_KEYWORDS["male"]:
            if re.search(keyword, title_str, re.IGNORECASE):
                return "male"

    return ""


def get_age_group(tags_str, title) -> str:
    """Derive Google Shopping age_group from tags.

    Returns: "kids" or "adult"
    """
    tags = parse_tags(tags_str)

    for tag in tags:
        tag_lower = tag.lower()
        if tag_lower in TAG_TO_GOOGLE_AGE_GROUP:
            return TAG_TO_GOOGLE_AGE_GROUP[tag_lower]

    # Title fallback for kids
    if title:
        for keyword in TITLE_GENDER_KEYWORDS["kids"]:
            if re.search(keyword, title, re.IGNORECASE):
                return "kids"

    return "adult"


def extract_color(option1_name, option1_value, option2_value=None) -> str:
    """Extract color from variant options.

    Color lives in option1 when option1_name is Color/Style.

    Returns: color string or ""
    """
    if not option1_name or not option1_value:
        return ""

    name_lower = option1_name.lower()
    val_lower = option1_value.lower()

    if name_lower in ("color", "style") and val_lower != "default title":
        return option1_value
    return ""


def is_non_size_value(value) -> bool:
    """Check if a value is not a real size (volumes, weights, lengths, etc.)."""
    if not value:
        return True
    return bool(NON_SIZE_PATTERN.match(value.strip()))


def extract_size(option1_name, option1_value, option2_value=None) -> str:
    """Extract size from variant options.

    Size is in option1 when option1_name is "Size", or in option2 when
    option1_name is "Color"/"Style" (i.e., the second axis is size).

    Returns: size string or ""
    """
    size_val = ""

    if option1_name:
        name_lower = option1_name.lower()
        if name_lower == "size" and option1_value:
            if option1_value.lower() != "default title":
                size_val = option1_value
        elif name_lower in ("color", "style") and option2_value:
            # option2 is likely size
            if option2_value.lower() != "default title":
                size_val = option2_value

    if not size_val:
        return ""

    # Filter out non-size values
    if is_non_size_value(size_val):
        return ""

    return size_val


def detect_size_system(vendor, product_type, size_value) -> str:
    """Detect the appropriate size system for a variant.

    Returns: "US", "EU", or "" (omit)
    """
    if not size_value:
        return ""

    # Vendor override for EU sizing
    if vendor in EU_SIZING_VENDORS:
        return "EU"

    # Mondo detection for ski boots
    if product_type in MONDO_TYPES:
        # Mondo sizes are numeric 20-33 with possible .5
        try:
            num = float(size_value)
            if 20 <= num <= 33:
                return ""  # Omit size_system for mondo
        except (ValueError, TypeError):
            pass

    # Default to US
    return "US"


def get_season_label(tags_str) -> str:
    """Extract season custom label from tags.

    Returns: "spring-summer", "fall-winter", or "year-round" (default)
    """
    tags = parse_tags(tags_str)
    for tag in tags:
        tag_lower = tag.lower()
        if tag_lower in SEASON_TAG_MAP:
            return SEASON_TAG_MAP[tag_lower]
    return "year-round"


def get_price_tier(price) -> str:
    """Classify price into tier bracket.

    Returns: "under-50", "50-100", "100-200", or "over-200"
    """
    if price is None:
        return "under-50"
    try:
        p = float(price)
    except (ValueError, TypeError):
        return "under-50"

    for threshold, label in PRICE_TIERS:
        if p < threshold:
            return label
    return "over-200"


def get_activity_label(tags_str) -> str:
    """Extract first activity tag value for custom label.

    Returns: activity name (e.g., "hiking") or "general"
    """
    tags = parse_tags(tags_str)
    for tag in tags:
        if tag.lower().startswith("activity:"):
            return tag.split(":", 1)[1].strip()
    return "general"


def build_seo_title(vendor, title) -> str:
    """Build SEO meta title: "{Vendor} {Title} | {STORE_NAME}".

    Truncates to 60 chars if needed, preserving vendor and store suffix.
    Avoids doubling vendor when title already starts with it.
    """
    if not vendor and not title:
        return ""

    vendor = (vendor or "").strip()
    title = (title or "").strip()
    suffix = f" | {STORE_NAME}"

    # Avoid doubling vendor name
    if vendor and title.lower().startswith(vendor.lower()):
        full = f"{title}{suffix}"
    elif vendor:
        full = f"{vendor} {title}{suffix}"
    else:
        full = f"{title}{suffix}"

    if len(full) <= 60:
        return full

    # Truncate: keep the suffix, trim the middle
    max_title_len = 60 - len(suffix)
    if vendor and not title.lower().startswith(vendor.lower()):
        prefix = f"{vendor} {title}"
    else:
        prefix = title
    return prefix[:max_title_len].rstrip() + suffix


def build_seo_description(vendor, title, body_html, product_type) -> str:
    """Build SEO meta description from body_html or template.

    For products with body_html: first ~155 chars of plain text.
    For products without: template description.

    Returns: description string <= 160 chars
    """
    if body_html and body_html.strip():
        text = strip_html(body_html)
        if text:
            if len(text) > 155:
                return text[:155].rstrip() + "..."
            return text

    # Template fallback
    type_phrase = _product_type_phrase(product_type)
    template = f"Shop the {vendor} {title} at {STORE_NAME} in San Luis Obispo."
    if type_phrase:
        template += f" {type_phrase}."
    template += " Free in-store pickup available."

    if len(template) > 160:
        return template[:157].rstrip() + "..."
    return template


# Product type to human-readable phrase for SEO template descriptions
_TYPE_PHRASES = {
    "Jacket": "Premium outdoor jacket",
    "Rain Jacket": "Waterproof rain jacket",
    "Vest": "Versatile outdoor vest",
    "Fleece": "Warm fleece layer",
    "Shirt": "Performance outdoor shirt",
    "T-Shirt": "Comfortable outdoor tee",
    "Tank Top": "Lightweight active tank",
    "Hoodie": "Comfortable outdoor hoodie",
    "Sweater": "Cozy outdoor sweater",
    "Pants": "Durable outdoor pants",
    "Snow Pants": "Insulated snow pants",
    "Shorts": "Performance outdoor shorts",
    "Backpack": "Quality hiking backpack",
    "Daypack": "Versatile daypack",
    "Tent": "Reliable camping tent",
    "Sleeping Bag": "Quality sleeping bag",
    "Ski": "High-performance skis",
    "Snowboard": "Quality snowboard",
    "Climbing Harness": "Reliable climbing harness",
    "Running Shoe": "Performance running shoe",
    "Trail Runner": "Performance trail running shoe",
    "Hiking Boot": "Durable hiking boot",
    "Hiking Shoe": "Versatile hiking shoe",
    "Casual Shoe": "Comfortable casual shoe",
    "Sunglasses": "Quality sunglasses",
    "Hat": "Outdoor hat",
    "Beanie": "Warm winter beanie",
    "Glove": "Quality outdoor glove",
    "Sock": "Performance outdoor sock",
    "Water Bottle": "Durable water bottle",
    "Roof Rack": "Quality vehicle roof rack",
    "Bike Rack": "Reliable vehicle bike rack",
}


def _product_type_phrase(product_type) -> str:
    """Get a human-readable phrase for a product type."""
    if not product_type:
        return ""
    return _TYPE_PHRASES.get(product_type, "")


def get_weight_grams(product_type) -> int | None:
    """Get estimated weight in grams from type-based defaults.

    Returns: weight in grams (int) or None if no default available
    """
    if not product_type:
        return None
    lb = PRODUCT_TYPE_WEIGHT_DEFAULTS_LB.get(product_type)
    if lb is None:
        return None
    return round(lb * LB_TO_GRAMS)


# ── Auto-size helper ─────────────────────────────────────────────────────────


def _auto_size(ws) -> None:
    """Auto-size columns for readability."""
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 60)


# ── Main generators ──────────────────────────────────────────────────────────


def generate_google_shopping(output_path, store: LookoutStore) -> dict:
    """Generate Matrixify XLSX with Google Shopping metafields + SEO meta.

    Args:
        output_path: Path to write XLSX
        store: LookoutStore instance

    Returns: dict with coverage stats.
    """
    products = store.list_products()

    wb = Workbook()
    ws = wb.active
    ws.title = "Products"

    headers = [
        "ID",
        "Handle",
        "Command",
        "Metafield: mm-google-shopping.google_product_category [string]",
        "Metafield: mm-google-shopping.gender [string]",
        "Metafield: mm-google-shopping.age_group [string]",
        "Metafield: mm-google-shopping.condition [string]",
        "Metafield: mm-google-shopping.color [string]",
        "Metafield: mm-google-shopping.size [string]",
        "Metafield: mm-google-shopping.size_system [string]",
        "Metafield: mm-google-shopping.custom_label_0 [string]",
        "Metafield: mm-google-shopping.custom_label_1 [string]",
        "Metafield: mm-google-shopping.custom_label_2 [string]",
        "SEO Title",
        "SEO Description",
    ]
    ws.append(headers)

    # Track stats
    stats = {
        "total_variants": 0,
        "total_products": set(),
        "has_category": 0,
        "has_gender": 0,
        "has_age_group": 0,
        "has_color": 0,
        "has_size": 0,
        "has_seo_title": 0,
        "has_seo_desc": 0,
        "categories_by_bucket": {},
    }

    # Track SEO titles already generated per handle (product-level, not variant)
    seen_seo = set()

    for product in products:
        vendor = product["vendor"]
        if vendor in EXCLUDED_VENDORS:
            continue

        handle = product["handle"] or ""
        title = product["title"] or ""
        product_type = product["product_type"] or ""
        tags = product["tags"] or ""
        body_html = product["body_html"] or ""

        variants = store.get_variants(product["id"])

        for variant in variants:
            price = variant["price"]
            option1_name = variant["option1_name"] or ""
            option1_value = variant["option1_value"] or ""
            option2_value = variant["option2_value"] or ""

            stats["total_variants"] += 1
            stats["total_products"].add(handle)

            # Google Product Category (product-level, same for all variants)
            category = get_google_category(product_type)
            if category:
                stats["has_category"] += 1
                bucket = category.split(" > ")[0]
                stats["categories_by_bucket"][bucket] = (
                    stats["categories_by_bucket"].get(bucket, 0) + 1
                )

            # Gender + age_group
            gender = get_gender(tags, title)
            age_group = get_age_group(tags, title)
            if gender:
                stats["has_gender"] += 1
            stats["has_age_group"] += 1  # always set

            # Color + size + size_system
            color = extract_color(option1_name, option1_value, option2_value)
            size = extract_size(option1_name, option1_value, option2_value)
            size_system = detect_size_system(vendor, product_type, size) if size else ""

            if color:
                stats["has_color"] += 1
            if size:
                stats["has_size"] += 1

            # Custom labels
            season = get_season_label(tags)
            price_tier = get_price_tier(price)
            activity = get_activity_label(tags)

            # SEO (product-level but included on each row for Matrixify)
            seo_title = ""
            seo_desc = ""
            if handle not in seen_seo:
                seen_seo.add(handle)
                seo_title = build_seo_title(vendor, title)
                seo_desc = build_seo_description(vendor, title, body_html, product_type)
                if seo_title:
                    stats["has_seo_title"] += 1
                if seo_desc:
                    stats["has_seo_desc"] += 1

            ws.append(
                [
                    product["id"],
                    handle,
                    "MERGE",
                    category,
                    gender,
                    age_group,
                    "new",
                    color,
                    size,
                    size_system,
                    season,
                    price_tier,
                    activity,
                    seo_title,
                    seo_desc,
                ]
            )

    _auto_size(ws)
    wb.save(output_path)

    stats["total_products"] = len(stats["total_products"])
    return stats


def generate_weights(output_path, store: LookoutStore) -> dict:
    """Generate Matrixify XLSX with weight corrections for variants.

    Only includes variants of product types that have a weight default.
    Uses type-based defaults since weight data is not in the DB.

    Args:
        output_path: Path to write XLSX
        store: LookoutStore instance

    Returns: dict with update stats.
    """
    products = store.list_products()

    wb = Workbook()
    ws = wb.active
    ws.title = "Products"

    headers = ["ID", "Variant ID", "Handle", "Command", "Variant Grams"]
    ws.append(headers)

    count = 0
    types_updated = {}

    for product in products:
        if product["vendor"] in EXCLUDED_VENDORS:
            continue

        product_type = product["product_type"]
        if not product_type:
            continue

        weight_g = get_weight_grams(product_type)
        if weight_g is None:
            continue

        variants = store.get_variants(product["id"])
        for variant in variants:
            ws.append(
                [
                    product["id"],
                    variant["id"],
                    product["handle"],
                    "MERGE",
                    weight_g,
                ]
            )
            count += 1
            types_updated[product_type] = types_updated.get(product_type, 0) + 1

    _auto_size(ws)
    wb.save(output_path)
    return {"variants_updated": count, "types": types_updated}


def generate_weight_audit(output_path, store: LookoutStore) -> dict:
    """Generate CSV audit report for dimensional/bulky products.

    Lists all products of dimensional types for manual weight review,
    sorted by price descending (highest COH first).

    Args:
        output_path: Path to write CSV
        store: LookoutStore instance

    Returns: dict with row count.
    """
    products = store.list_products()

    # Collect all rows, then sort by price descending
    audit_rows = []
    for product in products:
        if product["vendor"] in EXCLUDED_VENDORS:
            continue
        if product["product_type"] not in DIMENSIONAL_TYPES:
            continue

        variants = store.get_variants(product["id"])
        for variant in variants:
            weight_g = get_weight_grams(product["product_type"])
            audit_rows.append(
                {
                    "handle": product["handle"],
                    "title": product["title"],
                    "vendor": product["vendor"],
                    "product_type": product["product_type"],
                    "variant_sku": variant["sku"],
                    "suggested_weight_g": weight_g or "",
                    "price": variant["price"],
                }
            )

    # Sort by price descending
    audit_rows.sort(key=lambda r: float(r["price"] or 0), reverse=True)

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "handle",
                "title",
                "vendor",
                "product_type",
                "variant_sku",
                "suggested_weight_g",
                "price",
            ]
        )
        for row in audit_rows:
            writer.writerow(
                [
                    row["handle"],
                    row["title"],
                    row["vendor"],
                    row["product_type"],
                    row["variant_sku"],
                    row["suggested_weight_g"],
                    row["price"],
                ]
            )

    return {"rows": len(audit_rows)}
