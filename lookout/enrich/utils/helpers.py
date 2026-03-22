"""
Helper utilities for the merchfill pipeline.
"""

import re
from pathlib import Path
from urllib.parse import urlparse


def ensure_dir(path: str | Path) -> Path:
    """
    Ensure a directory exists, creating it if necessary.

    Args:
        path: Path to the directory.

    Returns:
        Path object for the directory.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """
    Sanitize a string for use as a filename.

    Args:
        name: The string to sanitize.
        max_length: Maximum length of the result.

    Returns:
        A safe filename string.
    """
    # Replace problematic characters with underscore
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Collapse multiple underscores
    safe = re.sub(r"_+", "_", safe)
    # Strip leading/trailing underscores and dots
    safe = safe.strip("_.")
    # Truncate if needed
    if len(safe) > max_length:
        safe = safe[:max_length].rstrip("_.")
    return safe or "unnamed"


def handle_to_query(handle: str) -> str:
    """
    Convert a Shopify product handle to a search query.

    Handles are typically lowercase with hyphens between words.

    Args:
        handle: The product handle (e.g., "mens-nano-puff-jacket")

    Returns:
        A search query string (e.g., "mens nano puff jacket")
    """
    # Replace hyphens with spaces
    query = handle.replace("-", " ")
    # Remove extra whitespace
    query = " ".join(query.split())
    return query


def normalize_url(url: str) -> str:
    """
    Normalize a URL by removing fragments and trailing slashes.

    Args:
        url: The URL to normalize.

    Returns:
        Normalized URL string.
    """
    parsed = urlparse(url)
    # Remove fragment
    normalized = parsed._replace(fragment="")
    result = normalized.geturl()
    # Remove trailing slash (but not for root)
    if result.endswith("/") and parsed.path != "/":
        result = result.rstrip("/")
    return result


def is_product_url(url: str, blocked_paths: list[str], product_patterns: list[str]) -> bool:
    """
    Check if a URL appears to be a product page.

    Args:
        url: The URL to check.
        blocked_paths: List of path prefixes to block.
        product_patterns: List of path patterns that indicate product pages.

    Returns:
        True if the URL appears to be a product page.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()

    # Check blocked paths
    for blocked in blocked_paths:
        if path.startswith(blocked.lower()):
            return False

    # If we have product patterns, check if URL matches any
    if product_patterns:
        for pattern in product_patterns:
            if pattern.lower() in path:
                return True
        # If patterns are defined but none match, be cautious
        # Allow URLs that don't match blocked paths
        return True

    # No patterns defined - allow anything not blocked
    return True


def extract_domain(url: str) -> str:
    """
    Extract the domain from a URL.

    Args:
        url: The URL to extract from.

    Returns:
        The domain (e.g., "example.com")
    """
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path.split("/")[0]
    # Remove www. prefix
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def parse_csv_boolean(value: str | bool | int | None) -> bool:
    """
    Parse a boolean value from CSV data.

    Handles various representations: true/false, yes/no, 1/0, etc.

    Args:
        value: The value to parse.

    Returns:
        Boolean interpretation of the value.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v_lower = value.lower().strip()
        if v_lower in ("true", "yes", "1", "y", "t"):
            return True
        if v_lower in ("false", "no", "0", "n", "f", ""):
            return False
    return False


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """
    Truncate text to a maximum length.

    Args:
        text: The text to truncate.
        max_length: Maximum length including suffix.
        suffix: Suffix to append when truncating.

    Returns:
        Truncated text.
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def clean_html_text(text: str) -> str:
    """
    Clean text extracted from HTML.

    Removes excessive whitespace and normalizes line breaks.

    Args:
        text: The text to clean.

    Returns:
        Cleaned text.
    """
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)
    # Strip leading/trailing whitespace
    text = text.strip()
    return text
