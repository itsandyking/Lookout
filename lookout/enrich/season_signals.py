"""Season signal detection for the enrichment pipeline.

Detects whether a vendor's product page likely shows the same season/year
as the Shopify catalog product.  This is a FLAGGING system — mismatches
surface as warnings, never block enrichment.

Key signals:
- Color overlap (excluding stable neutrals like black/white/grey)
- URL year hints (e.g. /2026/ or /fw25/)
- SKU/style cross-validation (future)

Ski/snowboard convention: "2025" = winter 24-25, "2026" = winter 25-26.
"""

from __future__ import annotations

import re

from .colors import normalize_color

# Colors that persist across seasons and shouldn't count toward overlap
STABLE_NEUTRALS = frozenset(
    {
        "black",
        "white",
        "grey",
        "gray",
        "charcoal",
        "navy",
    }
)

# Patterns that look like years in URLs
_YEAR_RE = re.compile(
    r"(?:^|[/_-])"  # boundary
    r"(20[2-3]\d)"  # full 4-digit year
    r"(?:$|[/_-])"
)
_SHORT_YEAR_RE = re.compile(
    r"(?:^|[/_-])"
    r"(?:fw|ss|sp|su|fa|aw)"  # season prefix
    r"(\d{2})"  # 2-digit year
    r"(?:$|[/_-])",
    re.IGNORECASE,
)


def _is_neutral(color_normalized: str) -> bool:
    """Return True if a normalized color token is a stable neutral."""
    # Check if any neutral word appears as a standalone token
    tokens = color_normalized.split()
    return any(t in STABLE_NEUTRALS for t in tokens)


def score_color_overlap(
    catalog_colors: list[str],
    vendor_colors: list[str],
) -> dict:
    """Score the color overlap between catalog and vendor, excluding neutrals.

    Returns dict with overlap score, sets of shared/exclusive colors,
    and which neutrals were excluded.
    """
    cat_norm = {normalize_color(c) for c in catalog_colors if c.strip()}
    ven_norm = {normalize_color(c) for c in vendor_colors if c.strip()}

    # Separate neutrals
    cat_neutrals = {c for c in cat_norm if _is_neutral(c)}
    ven_neutrals = {c for c in ven_norm if _is_neutral(c)}
    neutrals_excluded = sorted(cat_neutrals | ven_neutrals)

    # Work with non-neutral colors only
    cat_active = cat_norm - cat_neutrals
    ven_active = ven_norm - ven_neutrals

    shared = sorted(cat_active & ven_active)
    catalog_only = sorted(cat_active - ven_active)
    vendor_only = sorted(ven_active - cat_active)

    union_size = len(cat_active | ven_active)
    overlap = len(shared) / union_size if union_size else 1.0  # both empty = fine

    return {
        "overlap": round(overlap, 3),
        "shared": shared,
        "catalog_only": catalog_only,
        "vendor_only": vendor_only,
        "neutrals_excluded": neutrals_excluded,
    }


def detect_url_year_hints(url: str) -> list[str]:
    """Extract year-like patterns from a URL.

    Returns deduplicated list of 4-digit year strings found.
    """
    years: list[str] = []

    for m in _YEAR_RE.finditer(url):
        years.append(m.group(1))

    for m in _SHORT_YEAR_RE.finditer(url):
        short = m.group(1)
        full = f"20{short}"
        years.append(full)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for y in years:
        if y not in seen:
            seen.add(y)
            unique.append(y)
    return unique


def check_season_match(
    catalog_product: dict,
    vendor_facts: dict,
    url: str,
) -> dict:
    """Orchestrate season signal checks.

    Args:
        catalog_product: Must contain 'colors' (list[str]) and optionally
            'tags' (list[str]), 'title' (str).
        vendor_facts: Must contain 'colors' (list[str]), may contain
            'product_name' (str).
        url: The vendor product page URL.

    Returns:
        Season signal summary with color overlap, year hints, flags, and
        a human-readable confidence note.
    """
    flags: list[str] = []

    # --- Color overlap ---
    cat_colors = catalog_product.get("colors", [])
    ven_colors = vendor_facts.get("colors", [])
    color_result = score_color_overlap(cat_colors, ven_colors)

    # Flag when there are enough non-neutral colors to compare and overlap is low
    has_enough_colors = (
        len(color_result["shared"])
        + len(color_result["catalog_only"])
        + len(color_result["vendor_only"])
    ) >= 2
    if has_enough_colors and color_result["overlap"] < 0.3:
        flags.append("LOW_COLOR_OVERLAP")

    # --- URL year hints ---
    year_hints = detect_url_year_hints(url)

    # Check against catalog tags for year mismatch
    catalog_tags = catalog_product.get("tags", [])
    catalog_years: set[str] = set()
    for tag in catalog_tags:
        tag_lower = tag.lower().strip()
        # Look for year: or season:year patterns
        for token in re.split(r"[:\-_/\s]", tag_lower):
            if re.fullmatch(r"20[2-3]\d", token):
                catalog_years.add(token)

    if year_hints and catalog_years:
        if not set(year_hints) & catalog_years:
            flags.append("URL_YEAR_MISMATCH")

    # Check if vendor URL year is newer than catalog year
    if year_hints and catalog_years:
        max_url_year = max(int(y) for y in year_hints)
        max_cat_year = max(int(y) for y in catalog_years)
        if max_url_year > max_cat_year:
            flags.append("POSSIBLE_NEWER_MODEL")

    # --- Confidence note ---
    if not flags:
        note = "No season mismatch signals detected."
    elif "LOW_COLOR_OVERLAP" in flags and "URL_YEAR_MISMATCH" in flags:
        note = (
            "Low color overlap AND URL year mismatch — likely different season. "
            "Review before using vendor data."
        )
    elif "LOW_COLOR_OVERLAP" in flags:
        note = (
            "Low non-neutral color overlap with vendor page. "
            "May indicate a different season or colorway refresh."
        )
    elif "POSSIBLE_NEWER_MODEL" in flags:
        note = "Vendor page appears to show a newer model year than catalog."
    elif "URL_YEAR_MISMATCH" in flags:
        note = "URL contains a year that doesn't match catalog tags."
    else:
        note = f"Season signals: {', '.join(flags)}"

    return {
        "color_overlap": color_result["overlap"],
        "color_detail": color_result,
        "year_hints": year_hints,
        "flags": flags,
        "confidence_note": note,
    }
