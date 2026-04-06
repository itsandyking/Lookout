"""Color name normalization for matching variant colors across sources.

Shopify, vendor sites, and catalog data use different formats for the
same color: "Matte Black / Polarized Gray" vs "Matte Black | Polarized Gray"
vs "Matte Black - Polarized Gray". This module normalizes for matching
while preserving the original values.
"""

import re


def normalize_color(color: str) -> str:
    """Normalize a color name for comparison/matching.

    Strips separators, extra whitespace, and lowercases.
    "Matte Black / Polarized Gray" → "matte black polarized gray"
    "Matte Black | Polarized Gray" → "matte black polarized gray"
    "Black w/Black" → "black w black"
    """
    # Replace common separators with space
    normalized = re.sub(r"\s*[/|]\s*", " ", color)
    # Replace " - " separator (but keep hyphens within words like "X-Dye")
    normalized = re.sub(r"\s+-\s+", " ", normalized)
    # Replace "w/" with space
    normalized = re.sub(r"\bw/", "w ", normalized)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def colors_match(color_a: str, color_b: str) -> bool:
    """Check if two color names refer to the same color.

    Uses normalized comparison, plus substring matching for cases where
    one source has a shorter name than the other.
    """
    norm_a = normalize_color(color_a)
    norm_b = normalize_color(color_b)

    # Exact normalized match
    if norm_a == norm_b:
        return True

    # One contains the other (e.g., "Matte Black" matches
    # "Matte Black Polarized Gray" when matching is loose)
    # Only if the shorter one is at least 60% of the longer
    shorter, longer = sorted([norm_a, norm_b], key=len)
    if shorter and shorter in longer and len(shorter) / len(longer) > 0.5:
        return True

    return False


def find_matching_color(target: str, candidates: dict[str, str]) -> str | None:
    """Find a matching color in a dict of color→value mappings.

    Args:
        target: The color to look for.
        candidates: Dict with color names as keys.

    Returns:
        The matching key from candidates, or None.
    """
    # Try exact match first
    if target in candidates:
        return target

    # Try normalized match
    norm_target = normalize_color(target)
    for candidate_color in candidates:
        if normalize_color(candidate_color) == norm_target:
            return candidate_color

    # Try fuzzy/substring match
    for candidate_color in candidates:
        if colors_match(target, candidate_color):
            return candidate_color

    return None


def deduplicate_color_images(
    color_images: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Merge color image entries that refer to the same color.

    When catalog data uses "Matte Black / Polarized Gray" and scraped data
    uses "Matte Black | Polarized Gray", merge them into one entry.

    Keeps the first key encountered as the canonical name.
    """
    merged: dict[str, list[str]] = {}
    norm_to_key: dict[str, str] = {}

    for color, urls in color_images.items():
        norm = normalize_color(color)

        if norm in norm_to_key:
            # Merge into existing entry
            canonical = norm_to_key[norm]
            for url in urls:
                if url not in merged[canonical]:
                    merged[canonical].append(url)
        else:
            norm_to_key[norm] = color
            merged[color] = list(urls)

    return merged
