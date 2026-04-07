"""Catalog health text checks — description quality and coherence.

No vision, no network. Pure text analysis against product metadata.
"""

import re

# Patterns that indicate boilerplate / low-quality descriptions
_BOILERPLATE_PATTERNS = [
    r"buy\s+locally",
    r"visit\s+us",
    r"contact\s+(your\s+)?(local\s+)?dealer",
    r"description\s+coming\s+soon",
    r"check\s+with\s+your\s+local",
    r"available\s+at\s+your\s+(local|nearest)",
    r"see\s+in\s+store",
    r"call\s+for\s+(pricing|availability)",
]

_BOILERPLATE_RE = re.compile("|".join(_BOILERPLATE_PATTERNS), re.IGNORECASE)

# Minimum text length (after stripping HTML) to consider "adequate"
_MIN_DESCRIPTION_LENGTH = 50

# Words to ignore when checking title-description coherence
_IGNORE_WORDS = {
    "the", "a", "an", "and", "or", "of", "for", "in", "on", "to", "with",
    "men's", "mens", "women's", "womens", "unisex", "kid's", "kids",
    "youth", "junior", "adult",
}


def _strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&\w+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def check_description_quality(
    body_html: str | None,
    product_title: str | None = None,
) -> dict:
    """Check product description quality.

    Returns:
        {"quality": "ok"|"weak"|"empty", "reason": str}
    """
    if not body_html or not body_html.strip():
        return {"quality": "empty", "reason": "No description"}

    text = _strip_html(body_html)

    # Title repeated as entire description (check before length gate)
    if product_title:
        title_clean = _strip_html(product_title).strip()
        if title_clean and text.strip().lower() == title_clean.lower():
            return {"quality": "weak", "reason": "Title repeated as description"}

    if len(text) < _MIN_DESCRIPTION_LENGTH:
        return {"quality": "weak", "reason": f"Too short ({len(text)} chars)"}

    # Boilerplate check
    match = _BOILERPLATE_RE.search(text)
    if match:
        return {"quality": "weak", "reason": f"Boilerplate: '{match.group()}'"}

    return {"quality": "ok", "reason": ""}


def check_title_description_coherence(
    title: str,
    product_type: str,
    body_html: str,
) -> dict:
    """Check that description content relates to the product title/type.

    Returns:
        {"coherence": "ok"|"mismatch", "reason": str}
    """
    if not body_html or not body_html.strip():
        return {"coherence": "ok", "reason": ""}

    desc_text = _strip_html(body_html).lower()

    # Extract meaningful words from title and product_type
    title_words = set(title.lower().split()) - _IGNORE_WORDS if title else set()
    type_words = set(product_type.lower().split()) - _IGNORE_WORDS if product_type else set()

    # Check: does any meaningful title word or type word appear in description?
    all_check_words = title_words | type_words
    # Remove very short words and likely brand names (first word of title)
    all_check_words = {w for w in all_check_words if len(w) > 3}

    if not all_check_words:
        return {"coherence": "ok", "reason": ""}

    # Match if the check word appears in description, or its stem (first 4+ chars) does
    desc_words = set(re.findall(r"[a-z']+", desc_text))
    found = set()
    for w in all_check_words:
        # Exact match
        if w in desc_text:
            found.add(w)
            continue
        # Stem match: check word starts with a desc word, or desc word starts with check word
        stem = w[:max(4, len(w) - 1)]
        if any(d.startswith(stem) or w.startswith(d[:max(4, len(d) - 1)]) for d in desc_words if len(d) > 3):
            found.add(w)

    if found:
        return {"coherence": "ok", "reason": ""}

    return {
        "coherence": "mismatch",
        "reason": f"No title/type words found in description. "
                  f"Expected one of: {', '.join(sorted(all_check_words))}",
    }
