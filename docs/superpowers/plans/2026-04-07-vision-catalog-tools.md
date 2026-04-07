# Vision Catalog Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build two standalone scripts — color family classification and catalog health audit — that use Gemma 4 E4B to analyze product images and text quality across the active catalog.

**Architecture:** Both scripts follow the same pattern as `scripts/audit_variant_images.py`: load products from TVR via direct SQL, process sequentially with `OllamaVisionClient`, write CSV reports with periodic flushing and resume support. No new modules — just two scripts and their test files.

**Tech Stack:** Python 3.11+, `OllamaVisionClient` (lookout/enrich/llm.py), TVR Dolt database, httpx, csv, pytest

**Spec:** `docs/superpowers/specs/2026-04-07-vision-catalog-tools-design.md`

---

### Task 1: Color Family Classification — Text Fallback Logic

**Files:**
- Create: `lookout/enrich/color_families.py`
- Create: `tests/test_color_families.py`

This is the pure-Python text fallback that maps creative color names to families without vision. Extracted into its own module so the script stays focused on orchestration.

- [ ] **Step 1: Write failing tests for text-based color family inference**

```python
# tests/test_color_families.py
"""Tests for color family text fallback classification."""

import pytest

from lookout.enrich.color_families import (
    COLOR_FAMILIES,
    infer_color_family,
)


class TestInferColorFamily:
    """Test text-based color name → family mapping."""

    def test_exact_family_name(self):
        assert infer_color_family("Black") == "Black"

    def test_case_insensitive(self):
        assert infer_color_family("black") == "Black"

    def test_family_as_substring(self):
        assert infer_color_family("Basin Green") == "Green"

    def test_creative_name_lookup(self):
        assert infer_color_family("Obsidian") == "Black"

    def test_creative_name_brine(self):
        assert infer_color_family("Brine") == "Green"

    def test_multi_word_creative(self):
        assert infer_color_family("Nouveau Green") == "Green"

    def test_navy_is_navy_not_blue(self):
        assert infer_color_family("Navy") == "Navy"

    def test_slash_color_multi(self):
        assert infer_color_family("Black/Poppy") == "Multi"

    def test_unknown_returns_none(self):
        assert infer_color_family("Zephyr") is None

    def test_empty_string(self):
        assert infer_color_family("") is None

    def test_default_title(self):
        assert infer_color_family("Default Title") is None

    def test_gold(self):
        assert infer_color_family("Antique Gold") == "Gold"

    def test_silver(self):
        assert infer_color_family("Brushed Silver") == "Silver"

    def test_cream_is_beige(self):
        assert infer_color_family("Cream") == "Beige"

    def test_charcoal_is_gray(self):
        assert infer_color_family("Charcoal") == "Gray"

    def test_coral_is_pink(self):
        assert infer_color_family("Coral") == "Pink"

    def test_burgundy_is_red(self):
        assert infer_color_family("Burgundy") == "Red"

    def test_tan_is_brown(self):
        assert infer_color_family("Tan") == "Brown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_color_families.py -v`
Expected: ImportError — module doesn't exist yet

- [ ] **Step 3: Implement color family inference**

```python
# lookout/enrich/color_families.py
"""Color family classification — text-based fallback.

Maps creative vendor color names (e.g., "Obsidian", "Brine") to a fixed
set of 16 color families for Shopify Search & Discovery swatch filtering.
"""

COLOR_FAMILIES = [
    "Black", "White", "Gray", "Navy", "Blue", "Green", "Red",
    "Pink", "Orange", "Yellow", "Brown", "Beige", "Purple",
    "Gold", "Silver", "Multi",
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
    "khaki": "Green",  # duplicate intentional — khaki is debatable
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
    "Black", "White", "Navy", "Gray", "Blue", "Green", "Red",
    "Pink", "Orange", "Yellow", "Brown", "Beige", "Purple",
    "Gold", "Silver",
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
    if "/" in name and len(name.split("/")) >= 2:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_color_families.py -v`
Expected: All 17 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/enrich/color_families.py tests/test_color_families.py
git commit -m "feat: add text-based color family inference with creative name lookup"
```

---

### Task 2: Color Family Classification — Script

**Files:**
- Create: `scripts/classify_color_families.py`

Standalone script that classifies every unique (color, image_url) pair via E4B vision, falling back to text inference when no image exists.

- [ ] **Step 1: Create the script**

```python
#!/usr/bin/env python3
"""Classify variant colors into color families for Search & Discovery filtering.

Groups variants by color option, deduplicates by image URL, sends one
vision call per unique image. Falls back to text-based inference when
no image exists.

Usage:
    cd ~/Lookout && uv run python scripts/classify_color_families.py
    cd ~/Lookout && uv run python scripts/classify_color_families.py --vendor Patagonia
    cd ~/Lookout && uv run python scripts/classify_color_families.py --resume
"""

import argparse
import asyncio
import base64
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlalchemy import text
from tvr.db.dolt_config import load_dolt_config
from tvr.db.store import ShopifyStore

from lookout.enrich.color_families import infer_color_family
from lookout.enrich.llm import OllamaVisionClient

_store = ShopifyStore(load_dolt_config().connection_string)
OUTPUT_DIR = Path(__file__).parent.parent / "output" / "color_families"
BATCH_SIZE = 10

COLOR_FAMILIES_MENU = (
    "Black, White, Gray, Navy, Blue, Green, Red, Pink, "
    "Orange, Yellow, Brown, Beige, Purple, Gold, Silver, Multi"
)
VALID_FAMILIES = {f.strip() for f in COLOR_FAMILIES_MENU.split(",")}


def load_color_groups(
    vendor: str | None = None,
) -> list[dict]:
    """Load unique (product, color, image_urls) groups.

    Returns one row per (product, color) with all distinct image URLs
    for that color collected into a semicolon-separated string.
    """
    query = """
        SELECT
            p.id as product_id,
            p.handle,
            p.title as product_title,
            p.vendor,
            v.option1_value as color,
            GROUP_CONCAT(DISTINCT SUBSTRING_INDEX(v.image_src, '?', 1)
                         SEPARATOR ';;') as image_urls
        FROM variants v
        JOIN products p ON p.id = v.product_id
        WHERE v.option1_name IN ('Color', 'color', 'Colour')
          AND v.option1_value != 'Default Title'
          AND p.status = 'active'
        GROUP BY p.id, v.option1_value
        ORDER BY p.vendor, p.title, v.option1_value
    """
    params: dict = {}
    if vendor:
        query = query.replace(
            "AND p.status = 'active'",
            "AND p.status = 'active' AND LOWER(p.vendor) = LOWER(:vendor)",
        )
        params["vendor"] = vendor

    with _store.session() as s:
        rows = s.execute(text(query), params).fetchall()

    result = []
    for r in rows:
        mapping = dict(r._mapping)
        raw_urls = mapping.get("image_urls") or ""
        # Filter out empty strings and None
        urls = [u for u in raw_urls.split(";;") if u and u.strip()]
        mapping["image_urls"] = urls
        result.append(mapping)
    return result


async def classify_image(
    vision: OllamaVisionClient,
    image_data: bytes,
) -> str | None:
    """Ask E4B which color family this product image belongs to."""
    b64 = base64.b64encode(image_data).decode()

    payload = {
        "model": vision.model,
        "prompt": (
            "What color family does this product image belong to?\n"
            f"Pick exactly one: {COLOR_FAMILIES_MENU}\n\n"
            "Respond with only the color family name."
        ),
        "images": [b64],
        "stream": False,
        "think": False,
        "options": {"num_predict": 10, "temperature": 0.1},
    }

    raw = await vision._post_vision(payload)
    # Clean and validate
    cleaned = raw.strip().strip(".")
    for family in VALID_FAMILIES:
        if cleaned.lower() == family.lower():
            return family
    return None


async def main():
    parser = argparse.ArgumentParser(
        description="Classify variant colors into color families"
    )
    parser.add_argument("--vendor", help="Classify only this vendor")
    parser.add_argument("--resume", action="store_true", help="Skip already-classified")
    parser.add_argument(
        "--output", default=str(OUTPUT_DIR), help="Output directory"
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    suffix = f"_{args.vendor.lower().replace(' ', '-')}" if args.vendor else ""
    output_csv = output_dir / f"color_family_report{suffix}_{timestamp}.csv"

    # Resume: collect already-classified (handle, color) pairs
    classified_keys: set[str] = set()
    if args.resume:
        for existing in output_dir.glob(f"color_family_report{suffix}_*.csv"):
            try:
                with open(existing) as f:
                    for row in csv.DictReader(f):
                        key = f"{row.get('handle', '')}|{row.get('color_name', '')}"
                        classified_keys.add(key)
                print(f"Resuming: {len(classified_keys)} color groups already classified")
            except Exception:
                pass

    groups = load_color_groups(vendor=args.vendor)
    if args.resume:
        groups = [
            g for g in groups
            if f"{g['handle']}|{g['color']}" not in classified_keys
        ]

    print("Color Family Classification")
    print(f"  Color groups to classify: {len(groups)}")
    print(f"  Output: {output_csv}")
    print()

    if not groups:
        print("Nothing to classify.")
        return

    vision = OllamaVisionClient(model="vision")
    http = httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Lookout/1.0)"},
    )

    fieldnames = [
        "vendor", "handle", "product_title", "color_name", "color_family",
        "source", "unique_image_count", "anomaly_flag", "image_urls",
    ]

    results: list[dict] = []
    stats = {"vision": 0, "text": 0, "unknown": 0, "error": 0, "anomaly": 0}
    t_start = time.time()

    try:
        for i, group in enumerate(groups):
            color = group["color"]
            urls = group["image_urls"]
            unique_count = len(urls)
            anomaly = unique_count > 1

            if anomaly:
                stats["anomaly"] += 1

            family = None
            source = "unknown"

            if urls:
                # Classify each unique image URL
                families_seen: list[str] = []
                for url in urls:
                    try:
                        resp = await http.get(url)
                        resp.raise_for_status()
                        result = await classify_image(vision, resp.content)
                        if result:
                            families_seen.append(result)
                    except Exception:
                        pass

                if families_seen:
                    # Use the most common classification
                    family = max(set(families_seen), key=families_seen.count)
                    source = "vision"
                    stats["vision"] += 1

                    # Note disagreement in anomaly cases
                    if anomaly and len(set(families_seen)) > 1:
                        anomaly_note = f"disagreement: {families_seen}"
                    else:
                        anomaly_note = ""

            # Text fallback if vision didn't resolve
            if family is None:
                family = infer_color_family(color)
                if family:
                    source = "text"
                    stats["text"] += 1
                else:
                    source = "unknown"
                    stats["unknown"] += 1

            row = {
                "vendor": group["vendor"],
                "handle": group["handle"],
                "product_title": group["product_title"],
                "color_name": color,
                "color_family": family or "",
                "source": source,
                "unique_image_count": unique_count,
                "anomaly_flag": "true" if anomaly else "",
                "image_urls": ";;".join(urls),
            }
            results.append(row)

            if anomaly:
                print(
                    f"  ! ANOMALY: {group['vendor']} / {group['product_title']} / "
                    f"{color} — {unique_count} distinct images"
                )

            # Progress + flush
            if (i + 1) % BATCH_SIZE == 0 or i == len(groups) - 1:
                elapsed = time.time() - t_start
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                remaining = (len(groups) - i - 1) / rate if rate > 0 else 0
                print(
                    f"  [{i+1}/{len(groups)}] "
                    f"vision={stats['vision']} text={stats['text']} "
                    f"unknown={stats['unknown']} anomalies={stats['anomaly']} "
                    f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)"
                )
    finally:
        await http.aclose()

    # Write final CSV
    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Summary
    elapsed = time.time() - t_start
    total = len(results)
    print(f"\n{'='*60}")
    print("  COLOR FAMILY CLASSIFICATION COMPLETE")
    print(f"{'='*60}")
    print(f"  Total color groups: {total}")
    print(f"  Time: {elapsed/60:.1f} minutes")
    print(f"  Vision:   {stats['vision']:5d} ({100*stats['vision']/total:.1f}%)")
    print(f"  Text:     {stats['text']:5d} ({100*stats['text']/total:.1f}%)")
    print(f"  Unknown:  {stats['unknown']:5d} ({100*stats['unknown']/total:.1f}%)")
    print(f"  Anomalies:{stats['anomaly']:5d}")
    print(f"\n  Results: {output_csv}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Smoke test with a single vendor (dry-run check — does it parse and start?)**

Run: `cd /Users/andyking/Lookout && uv run python scripts/classify_color_families.py --vendor "NONEXISTENT_VENDOR_12345"`
Expected: Prints "Nothing to classify." and exits cleanly (validates imports, DB connection, query execution)

- [ ] **Step 3: Commit**

```bash
git add scripts/classify_color_families.py
git commit -m "feat: add color family classification script with vision + text fallback"
```

---

### Task 3: Catalog Health Audit — Text Checks

**Files:**
- Create: `lookout/audit/health_checks.py`
- Create: `tests/test_health_checks.py`

Pure-Python text quality checks — no vision, no network. Testable in isolation.

- [ ] **Step 1: Write failing tests for text checks**

```python
# tests/test_health_checks.py
"""Tests for catalog health text checks."""

import pytest

from lookout.audit.health_checks import (
    check_description_quality,
    check_title_description_coherence,
)


class TestDescriptionQuality:
    """Test description quality flagging."""

    def test_empty_body(self):
        result = check_description_quality("")
        assert result["quality"] == "empty"

    def test_none_body(self):
        result = check_description_quality(None)
        assert result["quality"] == "empty"

    def test_short_body(self):
        result = check_description_quality("<p>Buy now</p>")
        assert result["quality"] == "weak"

    def test_boilerplate_buy_locally(self):
        result = check_description_quality(
            "<p>This is a great product. Buy locally at your nearest dealer for the best price.</p>"
        )
        assert result["quality"] == "weak"
        assert "buy locally" in result["reason"].lower()

    def test_boilerplate_contact_dealer(self):
        result = check_description_quality(
            "<p>For more information, contact dealer for pricing and availability.</p>"
        )
        assert result["quality"] == "weak"

    def test_boilerplate_description_coming(self):
        result = check_description_quality(
            "<p>Description coming soon.</p>"
        )
        assert result["quality"] == "weak"

    def test_title_repeated(self):
        result = check_description_quality(
            "<p>Men's Down Jacket</p>",
            product_title="Men's Down Jacket",
        )
        assert result["quality"] == "weak"
        assert "title repeated" in result["reason"].lower()

    def test_good_description(self):
        result = check_description_quality(
            "<p>The Alpine Down Jacket features 800-fill goose down insulation "
            "with a water-resistant shell. Zippered hand pockets and an "
            "adjustable hood keep you warm in cold conditions.</p>"
        )
        assert result["quality"] == "ok"

    def test_html_tags_stripped(self):
        """Quality check should work on text content, not raw HTML."""
        result = check_description_quality(
            "<div><ul><li>Feature 1</li><li>Feature 2</li><li>Feature 3</li>"
            "<li>Feature 4</li><li>Feature 5</li></ul></div>"
        )
        assert result["quality"] == "ok"


class TestTitleDescriptionCoherence:
    """Test title-description coherence checking."""

    def test_coherent(self):
        result = check_title_description_coherence(
            title="Men's Down Jacket",
            product_type="Jackets",
            body_html="<p>This insulated down jacket keeps you warm.</p>",
        )
        assert result["coherence"] == "ok"

    def test_mismatch(self):
        result = check_title_description_coherence(
            title="Women's Down Jacket",
            product_type="Jackets",
            body_html="<p>These hiking boots feature Vibram soles and waterproof leather.</p>",
        )
        assert result["coherence"] == "mismatch"

    def test_empty_body_is_ok(self):
        """Empty body is caught by description quality, not coherence."""
        result = check_title_description_coherence(
            title="Men's Down Jacket",
            product_type="Jackets",
            body_html="",
        )
        assert result["coherence"] == "ok"

    def test_type_word_match(self):
        """Product type word in description is sufficient."""
        result = check_title_description_coherence(
            title="Patagonia Nano Puff",
            product_type="Jackets",
            body_html="<p>Lightweight synthetic jacket with PrimaLoft insulation.</p>",
        )
        assert result["coherence"] == "ok"

    def test_ignores_common_words(self):
        """Words like 'men's', 'women's', brand names shouldn't drive mismatch."""
        result = check_title_description_coherence(
            title="Patagonia Men's Better Sweater",
            product_type="Fleece",
            body_html="<p>Classic fleece pullover with a sweater-knit face.</p>",
        )
        assert result["coherence"] == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_health_checks.py -v`
Expected: ImportError — module doesn't exist yet

- [ ] **Step 3: Implement text checks**

```python
# lookout/audit/health_checks.py
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

    if len(text) < _MIN_DESCRIPTION_LENGTH:
        return {"quality": "weak", "reason": f"Too short ({len(text)} chars)"}

    # Boilerplate check
    match = _BOILERPLATE_RE.search(text)
    if match:
        return {"quality": "weak", "reason": f"Boilerplate: '{match.group()}'"}

    # Title repeated as entire description
    if product_title:
        title_clean = _strip_html(product_title).strip()
        if title_clean and text.strip().lower() == title_clean.lower():
            return {"quality": "weak", "reason": "Title repeated as description"}

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
    title_words = set(title.lower().split()) - _IGNORE_WORDS
    type_words = set(product_type.lower().split()) - _IGNORE_WORDS if product_type else set()

    # Check: does any meaningful title word or type word appear in description?
    all_check_words = title_words | type_words
    # Remove very short words and likely brand names (first word of title)
    all_check_words = {w for w in all_check_words if len(w) > 3}

    if not all_check_words:
        return {"coherence": "ok", "reason": ""}

    found = {w for w in all_check_words if w in desc_text}

    if found:
        return {"coherence": "ok", "reason": ""}

    return {
        "coherence": "mismatch",
        "reason": f"No title/type words found in description. "
                  f"Expected one of: {', '.join(sorted(all_check_words))}",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_health_checks.py -v`
Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lookout/audit/health_checks.py tests/test_health_checks.py
git commit -m "feat: add catalog health text checks — description quality and coherence"
```

---

### Task 4: Catalog Health Audit — Script

**Files:**
- Create: `scripts/catalog_health_audit.py`

Standalone script combining vision image checks with text quality checks in a single pass.

- [ ] **Step 1: Create the script**

```python
#!/usr/bin/env python3
"""Catalog health audit — image validation + description quality checks.

Single-pass audit of active products. Vision checks validate hero images
against product title/type. Text checks flag weak descriptions and
title-description mismatches.

Usage:
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py --vendor Patagonia
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py --skip-vision
    cd ~/Lookout && uv run python scripts/catalog_health_audit.py --resume
"""

import argparse
import asyncio
import base64
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlalchemy import text
from tvr.db.dolt_config import load_dolt_config
from tvr.db.store import ShopifyStore

from lookout.audit.health_checks import (
    check_description_quality,
    check_title_description_coherence,
)
from lookout.enrich.llm import OllamaVisionClient

_store = ShopifyStore(load_dolt_config().connection_string)
OUTPUT_DIR = Path(__file__).parent.parent / "output" / "catalog_health"
BATCH_SIZE = 10


def load_products(vendor: str | None = None) -> list[dict]:
    """Load active products with hero image URL and body HTML."""
    query = """
        SELECT
            p.id as product_id,
            p.handle,
            p.title as product_title,
            p.vendor,
            p.product_type,
            p.body_html,
            (SELECT pi.src FROM product_images pi
             WHERE pi.product_id = p.id
             ORDER BY pi.position ASC LIMIT 1) as hero_image_url
        FROM products p
        WHERE p.status = 'active'
    """
    params: dict = {}
    if vendor:
        query += " AND LOWER(p.vendor) = LOWER(:vendor)"
        params["vendor"] = vendor

    query += " ORDER BY p.vendor, p.title"

    with _store.session() as s:
        rows = s.execute(text(query), params).fetchall()
    return [dict(r._mapping) for r in rows]


async def check_image(
    vision: OllamaVisionClient,
    image_data: bytes,
    product_title: str,
    product_type: str,
) -> dict:
    """Ask E4B to validate the hero image against the product title/type.

    Returns:
        {"match": "yes"|"no", "image_type": "product"|"lifestyle"|"placeholder"|"other"}
    """
    b64 = base64.b64encode(image_data).decode()

    type_hint = f" (type: {product_type})" if product_type else ""
    prompt = (
        f'Look at this product image. The product is listed as: '
        f'"{product_title}"{type_hint}.\n\n'
        f'1. Does the image show this type of product? Answer YES or NO.\n'
        f'2. What kind of image is this? Answer one of: '
        f'PRODUCT, LIFESTYLE, PLACEHOLDER, OTHER.\n\n'
        f'Respond in exactly this format:\n'
        f'MATCH: YES or NO\n'
        f'TYPE: PRODUCT or LIFESTYLE or PLACEHOLDER or OTHER'
    )

    payload = {
        "model": vision.model,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "think": False,
        "options": {"num_predict": 20, "temperature": 0.1},
    }

    raw = await vision._post_vision(payload)

    # Parse response
    lines = raw.strip().upper().split("\n")
    match_val = "yes"
    type_val = "product"

    for line in lines:
        line = line.strip()
        if line.startswith("MATCH:"):
            val = line.replace("MATCH:", "").strip()
            match_val = "no" if "NO" in val else "yes"
        elif line.startswith("TYPE:"):
            val = line.replace("TYPE:", "").strip()
            for t in ("PRODUCT", "LIFESTYLE", "PLACEHOLDER", "OTHER"):
                if t in val:
                    type_val = t.lower()
                    break

    return {"match": match_val, "image_type": type_val}


def _image_verdict(vision_result: dict | None, has_image: bool) -> str:
    """Convert vision result to a verdict string."""
    if not has_image:
        return "no_image"
    if vision_result is None:
        return "error"
    if vision_result["match"] == "no":
        return "mismatch"
    return vision_result["image_type"]  # product, lifestyle, placeholder, other


def _overall_severity(image_verdict: str, desc_quality: str, coherence: str) -> str:
    """Determine overall severity from individual check results."""
    if image_verdict == "mismatch" or coherence == "mismatch":
        return "FAIL"
    if image_verdict in ("lifestyle", "placeholder", "no_image"):
        return "WARN"
    if desc_quality in ("weak", "empty"):
        return "WARN"
    return "OK"


async def main():
    parser = argparse.ArgumentParser(description="Catalog health audit")
    parser.add_argument("--vendor", help="Audit only this vendor")
    parser.add_argument("--resume", action="store_true", help="Skip already-audited")
    parser.add_argument(
        "--skip-vision", action="store_true", help="Run text checks only"
    )
    parser.add_argument(
        "--output", default=str(OUTPUT_DIR), help="Output directory"
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    suffix = f"_{args.vendor.lower().replace(' ', '-')}" if args.vendor else ""
    output_csv = output_dir / f"catalog_health_report{suffix}_{timestamp}.csv"

    # Resume: collect already-audited handles
    audited_handles: set[str] = set()
    if args.resume:
        for existing in output_dir.glob(f"catalog_health_report{suffix}_*.csv"):
            try:
                with open(existing) as f:
                    for row in csv.DictReader(f):
                        audited_handles.add(row.get("handle", ""))
                print(f"Resuming: {len(audited_handles)} products already audited")
            except Exception:
                pass

    products = load_products(vendor=args.vendor)
    if args.resume:
        products = [p for p in products if p["handle"] not in audited_handles]

    mode = "text-only" if args.skip_vision else "full (vision + text)"
    print("Catalog Health Audit")
    print(f"  Mode: {mode}")
    print(f"  Products to audit: {len(products)}")
    if not args.skip_vision:
        print(f"  Estimated time: {len(products) * 1.5 / 60:.0f} minutes")
    print(f"  Output: {output_csv}")
    print()

    if not products:
        print("Nothing to audit.")
        return

    vision = None if args.skip_vision else OllamaVisionClient(model="vision")
    http = None
    if not args.skip_vision:
        http = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Lookout/1.0)"},
        )

    fieldnames = [
        "vendor", "handle", "product_title", "product_type",
        "image_verdict", "description_quality", "title_desc_coherence",
        "overall_severity", "notes",
    ]

    results: list[dict] = []
    severity_counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    t_start = time.time()

    try:
        for i, product in enumerate(products):
            notes_parts: list[str] = []

            # --- Vision checks ---
            image_verdict = "skipped"
            hero_url = product.get("hero_image_url")

            if not args.skip_vision:
                if not hero_url:
                    image_verdict = "no_image"
                else:
                    try:
                        resp = await http.get(hero_url)
                        resp.raise_for_status()
                        vision_result = await check_image(
                            vision,
                            resp.content,
                            product["product_title"],
                            product.get("product_type", ""),
                        )
                        image_verdict = _image_verdict(vision_result, True)
                        if image_verdict == "mismatch":
                            notes_parts.append("Hero image doesn't match product")
                        elif image_verdict == "lifestyle":
                            notes_parts.append("Hero image is a lifestyle shot")
                        elif image_verdict == "placeholder":
                            notes_parts.append("Hero image is a placeholder")
                    except Exception as e:
                        image_verdict = "error"
                        notes_parts.append(f"Vision error: {e}")

            # --- Text checks ---
            desc_result = check_description_quality(
                product.get("body_html"),
                product_title=product.get("product_title"),
            )
            desc_quality = desc_result["quality"]
            if desc_result["reason"]:
                notes_parts.append(desc_result["reason"])

            coherence_result = check_title_description_coherence(
                title=product.get("product_title", ""),
                product_type=product.get("product_type", ""),
                body_html=product.get("body_html") or "",
            )
            coherence = coherence_result["coherence"]
            if coherence_result["reason"]:
                notes_parts.append(coherence_result["reason"])

            # --- Overall ---
            severity = _overall_severity(image_verdict, desc_quality, coherence)
            severity_counts[severity] += 1

            row = {
                "vendor": product["vendor"],
                "handle": product["handle"],
                "product_title": product["product_title"],
                "product_type": product.get("product_type", ""),
                "image_verdict": image_verdict,
                "description_quality": desc_quality,
                "title_desc_coherence": coherence,
                "overall_severity": severity,
                "notes": "; ".join(notes_parts),
            }
            results.append(row)

            if severity == "FAIL":
                print(
                    f"  ✗ FAIL: {product['vendor']} / {product['product_title']} — "
                    f"{'; '.join(notes_parts)}"
                )

            # Progress + flush
            if (i + 1) % BATCH_SIZE == 0 or i == len(products) - 1:
                elapsed = time.time() - t_start
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                remaining = (len(products) - i - 1) / rate if rate > 0 else 0
                print(
                    f"  [{i+1}/{len(products)}] "
                    f"OK={severity_counts['OK']} WARN={severity_counts['WARN']} "
                    f"FAIL={severity_counts['FAIL']} "
                    f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)"
                )
    finally:
        if http:
            await http.aclose()

    # Write final CSV
    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Summary
    elapsed = time.time() - t_start
    total = len(results)
    print(f"\n{'='*60}")
    print("  CATALOG HEALTH AUDIT COMPLETE")
    print(f"{'='*60}")
    print(f"  Total products: {total}")
    print(f"  Time: {elapsed/60:.1f} minutes")
    print(f"  OK:   {severity_counts['OK']:5d} ({100*severity_counts['OK']/total:.1f}%)")
    print(f"  WARN: {severity_counts['WARN']:5d} ({100*severity_counts['WARN']/total:.1f}%)")
    print(f"  FAIL: {severity_counts['FAIL']:5d} ({100*severity_counts['FAIL']/total:.1f}%)")
    print(f"\n  Results: {output_csv}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Smoke test with a nonexistent vendor**

Run: `cd /Users/andyking/Lookout && uv run python scripts/catalog_health_audit.py --vendor "NONEXISTENT_VENDOR_12345"`
Expected: Prints "Nothing to audit." and exits cleanly

- [ ] **Step 3: Smoke test text-only mode with a real vendor**

Run: `cd /Users/andyking/Lookout && uv run python scripts/catalog_health_audit.py --skip-vision --vendor Patagonia`
Expected: Runs quickly (no vision calls), produces CSV with description_quality and title_desc_coherence columns populated, image_verdict shows "skipped"

- [ ] **Step 4: Commit**

```bash
git add scripts/catalog_health_audit.py
git commit -m "feat: add catalog health audit script with vision + text checks"
```

---

### Task 5: Integration Smoke Tests

**Files:**
- Create: `tests/test_color_family_script.py`
- Create: `tests/test_health_audit_script.py`

Lightweight tests that verify the scripts' helper functions work with mocked DB/vision. Not end-to-end — just the processing logic.

- [ ] **Step 1: Write color family script tests**

```python
# tests/test_color_family_script.py
"""Tests for classify_color_families script logic."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

# Import the classify function directly
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from classify_color_families import classify_image
from lookout.enrich.llm import OllamaVisionClient


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestClassifyImage:
    def test_valid_family(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="Green")):
            result = run(classify_image(vision, b"fake_image"))
            assert result == "Green"

    def test_invalid_response_returns_none(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="Chartreuse")):
            result = run(classify_image(vision, b"fake_image"))
            assert result is None

    def test_case_insensitive(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="navy")):
            result = run(classify_image(vision, b"fake_image"))
            assert result == "Navy"

    def test_trailing_period_stripped(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="Blue.")):
            # _post_vision already strips trailing period, but classify_image also strips
            result = run(classify_image(vision, b"fake_image"))
            assert result == "Blue"
```

- [ ] **Step 2: Write health audit script tests**

```python
# tests/test_health_audit_script.py
"""Tests for catalog_health_audit script logic."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from catalog_health_audit import check_image, _image_verdict, _overall_severity
from lookout.enrich.llm import OllamaVisionClient


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestCheckImage:
    def test_matching_product(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(
            vision, "_post_vision",
            AsyncMock(return_value="MATCH: YES\nTYPE: PRODUCT"),
        ):
            result = run(check_image(vision, b"img", "Down Jacket", "Jackets"))
            assert result["match"] == "yes"
            assert result["image_type"] == "product"

    def test_mismatched_product(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(
            vision, "_post_vision",
            AsyncMock(return_value="MATCH: NO\nTYPE: PRODUCT"),
        ):
            result = run(check_image(vision, b"img", "Down Jacket", "Jackets"))
            assert result["match"] == "no"

    def test_lifestyle_image(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(
            vision, "_post_vision",
            AsyncMock(return_value="MATCH: YES\nTYPE: LIFESTYLE"),
        ):
            result = run(check_image(vision, b"img", "Down Jacket", "Jackets"))
            assert result["image_type"] == "lifestyle"


class TestImageVerdict:
    def test_no_image(self):
        assert _image_verdict(None, False) == "no_image"

    def test_error(self):
        assert _image_verdict(None, True) == "error"

    def test_mismatch(self):
        assert _image_verdict({"match": "no", "image_type": "product"}, True) == "mismatch"

    def test_product_match(self):
        assert _image_verdict({"match": "yes", "image_type": "product"}, True) == "product"


class TestOverallSeverity:
    def test_all_ok(self):
        assert _overall_severity("product", "ok", "ok") == "OK"

    def test_image_mismatch_is_fail(self):
        assert _overall_severity("mismatch", "ok", "ok") == "FAIL"

    def test_coherence_mismatch_is_fail(self):
        assert _overall_severity("product", "ok", "mismatch") == "FAIL"

    def test_weak_description_is_warn(self):
        assert _overall_severity("product", "weak", "ok") == "WARN"

    def test_lifestyle_is_warn(self):
        assert _overall_severity("lifestyle", "ok", "ok") == "WARN"

    def test_no_image_is_warn(self):
        assert _overall_severity("no_image", "ok", "ok") == "WARN"
```

- [ ] **Step 3: Run all tests**

Run: `cd /Users/andyking/Lookout && uv run pytest tests/test_color_families.py tests/test_color_family_script.py tests/test_health_checks.py tests/test_health_audit_script.py -v`
Expected: All tests PASS

- [ ] **Step 4: Run full test suite to check for regressions**

Run: `cd /Users/andyking/Lookout && uv run pytest --tb=short -q`
Expected: All existing tests still pass

- [ ] **Step 5: Commit**

```bash
git add tests/test_color_family_script.py tests/test_health_audit_script.py
git commit -m "test: add smoke tests for color family and catalog health scripts"
```
