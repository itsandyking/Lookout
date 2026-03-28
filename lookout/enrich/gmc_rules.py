"""Google Merchant Center compliance rules.

Rule logic extracted from Google Shoptimizer (Apache-2.0) and adapted
for Lookout's data models. Pure Python, no external dependencies.

CRITICAL: Color mapping is EXPORT-ONLY. Internal color names are never
modified anywhere in the system. map_color_for_gmc() is called only
by lookout/output/google_shopping.py when writing the GMC color attribute.
"""

from __future__ import annotations

import re

# ── GTIN Validation ──────────────────────────────────────────────────

VALID_GTIN_LENGTHS = (8, 12, 13, 14)


def validate_gtin(gtin: str) -> bool:
    """Validate a GTIN (UPC/EAN/JAN) check digit.
    Supports 8, 12, 13, and 14 digit GTINs.
    All-zeros barcodes are rejected as placeholder/null values."""
    if not gtin or not gtin.isdigit():
        return False
    if len(gtin) not in VALID_GTIN_LENGTHS:
        return False
    # Reject all-zeros placeholder barcodes
    if all(c == "0" for c in gtin):
        return False
    digits = [int(d) for d in gtin]
    check = digits[-1]
    payload = digits[:-1]
    total = 0
    for i, d in enumerate(reversed(payload)):
        total += d * (3 if i % 2 == 0 else 1)
    expected = (10 - (total % 10)) % 10
    return check == expected


# ── Title Validation ─────────────────────────────────────────────────

MAX_TITLE_LENGTH = 150


def validate_title(title: str) -> list[str]:
    """Check a product title for GMC violations."""
    violations = []
    if not title or not title.strip():
        violations.append("Title is empty")
        return violations
    if len(title) > MAX_TITLE_LENGTH:
        violations.append(f"Title exceeds {MAX_TITLE_LENGTH} characters ({len(title)})")
    if title == title.upper() and len(title) > 10:
        violations.append("Title is all caps (GMC may flag as spammy)")
    if re.search(r"[!]{2,}|[?]{2,}", title):
        violations.append("Title contains excessive punctuation")
    return violations


def structure_title(
    brand: str,
    product_type: str,
    attributes: dict[str, str] | None = None,
) -> str:
    """Build a GMC-optimal title from components.
    Format: [Brand] [Product Type] [Key Attributes]"""
    parts = []
    if brand:
        parts.append(brand)
    if product_type:
        parts.append(product_type)
    if attributes:
        for key in ("gender", "color", "size"):
            val = attributes.get(key, "")
            if val:
                parts.append(f"- {val}")
    title = " ".join(parts)
    if len(title) > MAX_TITLE_LENGTH:
        title = title[: MAX_TITLE_LENGTH - 3].rsplit(" ", 1)[0] + "..."
    return title


# ── Prohibited Terms ─────────────────────────────────────────────────

PROHIBITED_PATTERNS = [
    (r"\bfree shipping\b", "Promotional: 'free shipping'"),
    (r"\bbuy now\b", "Promotional: 'buy now'"),
    (r"\bon sale\b", "Promotional: 'on sale'"),
    (r"\blimited time\b", "Promotional: 'limited time'"),
    (r"\bwhile supplies last\b", "Promotional: 'while supplies last'"),
    (r"\bbest\b", "Superlative: 'best'"),
    (r"\bcheapest\b", "Superlative: 'cheapest'"),
    (r"\b(?:incredible|amazing|unbelievable)\b", "Superlative: exaggerated claim"),
    (r"\b(?:premium|superior|ultimate)\b", "Marketing superlative"),
    (r"\$\d+", "Price mention in description"),
    (r"\bguarantee\b", "Unsubstantiated guarantee claim"),
    (r"\b#\d+\s*(?:rated|selling|choice)\b", "Ranking claim"),
]

_COMPILED_PROHIBITED = [(re.compile(p, re.IGNORECASE), msg) for p, msg in PROHIBITED_PATTERNS]


def check_prohibited_terms(text: str) -> list[str]:
    """Flag promotional language, superlatives, and claims GMC rejects."""
    violations = []
    for pattern, message in _COMPILED_PROHIBITED:
        if pattern.search(text):
            violations.append(message)
    return violations


# ── Color Mapping (EXPORT-ONLY) ──────────────────────────────────────

GMC_COLOR_MAP: dict[str, str] = {
    "Midnight": "Navy", "Midnight Navy": "Navy", "Deep Navy": "Navy",
    "Abyss": "Navy", "Dark Navy": "Navy",
    "Deep Forest": "Green", "Forest": "Green", "Pine": "Green",
    "Sage": "Green", "Olive": "Green", "Moss": "Green",
    "Hemlock": "Green", "Nouveau Green": "Green",
    "Slate": "Gray", "Forge Grey": "Gray", "Smolder": "Gray",
    "Carbon": "Gray", "Graphite": "Gray", "Ash": "Gray",
    "Stone": "Gray", "Plume Grey": "Gray",
    "Mocha": "Brown", "Espresso": "Brown", "Earth": "Brown",
    "Coriander": "Brown", "Dark Walnut": "Brown",
    "Sumac Red": "Red", "Barn Red": "Red", "Paintbrush Red": "Red",
    "Touring Red": "Red", "Coral": "Pink", "Quartz Coral": "Pink",
    "Storm Blue": "Blue", "Tidepool Blue": "Blue", "Anacapa Blue": "Blue",
    "Wavy Blue": "Blue", "Lagom Blue": "Blue",
    "Birch White": "White", "Natural": "White",
    "Oatmeal": "Beige", "Pumice": "Beige",
    "Shrub": "Yellow", "Phosphorus": "Yellow",
    "Mango": "Orange", "Pufferfish Gold": "Gold",
}

_COLOR_MAP_LOWER = {k.lower(): v for k, v in GMC_COLOR_MAP.items()}


def map_color_for_gmc(color: str) -> str:
    """Map a display color name to a GMC-recognized color value.
    EXPORT-ONLY. Unmapped colors pass through unchanged."""
    if not color:
        return ""
    return _COLOR_MAP_LOWER.get(color.lower(), color)


# ── Required Attributes ──────────────────────────────────────────────

REQUIRED_FIELDS = {
    "title": "Product title",
    "body_html": "Product description",
    "image": "Product image",
    "price": "Product price",
}


def check_required_attributes(product: dict) -> list[str]:
    """Flag missing required GMC attributes."""
    missing = []
    for field, label in REQUIRED_FIELDS.items():
        val = product.get(field)
        if not val or (isinstance(val, str) and not val.strip()):
            missing.append(f"Missing required attribute: {label}")
    return missing
