"""Color family classification — text-based fallback.

Maps creative vendor color names (e.g., "Obsidian", "Brine") to a fixed
set of 16 color families for Shopify Search & Discovery swatch filtering.
"""

COLOR_FAMILIES = [
    "Black",
    "White",
    "Gray",
    "Navy",
    "Blue",
    "Green",
    "Red",
    "Pink",
    "Orange",
    "Yellow",
    "Brown",
    "Beige",
    "Purple",
    "Gold",
    "Silver",
    "Multi",
]

# Creative color names that don't contain their family as a substring.
# Keys are lowercase. Add entries as new unmapped names are discovered.
_CREATIVE_LOOKUP: dict[str, str] = {
    # Black family
    "obsidian": "Black",
    "onyx": "Black",
    "jet": "Black",
    "midnight": "Black",
    "ink": "Black",
    # White family
    "ivory": "White",
    "snow": "White",
    "pearl": "White",
    "chalk": "White",
    # Gray family
    "charcoal": "Gray",
    "slate": "Gray",
    "ash": "Gray",
    "graphite": "Gray",
    "pewter": "Gray",
    "steel": "Gray",
    "heather": "Gray",
    # Navy family
    "indigo": "Navy",
    # Blue family
    "cobalt": "Blue",
    "azure": "Blue",
    "sky": "Blue",
    "denim": "Blue",
    "teal": "Blue",
    "cerulean": "Blue",
    # Green family
    "olive": "Green",
    "sage": "Green",
    "moss": "Green",
    "forest": "Green",
    "brine": "Green",
    "pine": "Green",
    "fern": "Green",
    "emerald": "Green",
    "jade": "Green",
    "khaki": "Green",
    # Red family
    "crimson": "Red",
    "scarlet": "Red",
    "ruby": "Red",
    "burgundy": "Red",
    "maroon": "Red",
    "wine": "Red",
    "merlot": "Red",
    "cardinal": "Red",
    # Pink family
    "coral": "Pink",
    "salmon": "Pink",
    "rose": "Pink",
    "blush": "Pink",
    "fuchsia": "Pink",
    "magenta": "Pink",
    "flamingo": "Pink",
    # Orange family
    "rust": "Orange",
    "copper": "Orange",
    "amber": "Orange",
    "tangerine": "Orange",
    "peach": "Orange",
    "paprika": "Orange",
    # Yellow family
    "mustard": "Yellow",
    "lemon": "Yellow",
    "canary": "Yellow",
    "maize": "Yellow",
    # Brown family
    "chocolate": "Brown",
    "coffee": "Brown",
    "mocha": "Brown",
    "espresso": "Brown",
    "walnut": "Brown",
    "chestnut": "Brown",
    "caramel": "Brown",
    "tan": "Brown",
    "taupe": "Brown",
    "sienna": "Brown",
    "cocoa": "Brown",
    # Beige family
    "cream": "Beige",
    "sand": "Beige",
    "oat": "Beige",
    "wheat": "Beige",
    "linen": "Beige",
    "ecru": "Beige",
    "bone": "Beige",
    "vanilla": "Beige",
    "natural": "Beige",
    # Purple family
    "plum": "Purple",
    "violet": "Purple",
    "lavender": "Purple",
    "lilac": "Purple",
    "mauve": "Purple",
    "eggplant": "Purple",
    "amethyst": "Purple",
    # Gold family
    "brass": "Gold",
    "bronze": "Gold",
    # Silver family
    "chrome": "Silver",
    "platinum": "Silver",
    "nickel": "Silver",
}

# Families to check via substring, ordered so more specific matches
# come first (e.g., "Navy" before "Blue" so "Navy Blue" → Navy).
_FAMILY_PRIORITY = [
    "Black",
    "White",
    "Navy",
    "Gray",
    "Blue",
    "Green",
    "Red",
    "Pink",
    "Orange",
    "Yellow",
    "Brown",
    "Beige",
    "Purple",
    "Gold",
    "Silver",
]


def infer_color_family(color_name: str) -> str | None:
    """Infer color family from a color name string.

    Strategy:
    1. Check for slash/multi-color names → "Multi"
    2. Exact match against family names (case-insensitive)
    3. Creative name lookup table
    4. Family name as substring of color name
    5. Individual words against creative lookup
    6. None if nothing matches

    Returns:
        One of COLOR_FAMILIES or None.
    """
    if not color_name or color_name == "Default Title":
        return None

    name = color_name.strip()

    # Multi-color: slash-separated names
    if "/" in name:
        parts = [p.strip() for p in name.split("/")]
        if len(parts) >= 2 and all(len(p) > 0 for p in parts):
            return "Multi"

    lower = name.lower()

    # Exact family match
    for family in COLOR_FAMILIES:
        if lower == family.lower():
            return family

    # Creative lookup (full name)
    if lower in _CREATIVE_LOOKUP:
        return _CREATIVE_LOOKUP[lower]

    # Family name as substring (priority order)
    for family in _FAMILY_PRIORITY:
        if family.lower() in lower:
            return family

    # Word-by-word creative lookup
    words = lower.replace("-", " ").split()
    for word in words:
        if word in _CREATIVE_LOOKUP:
            return _CREATIVE_LOOKUP[word]

    return None
