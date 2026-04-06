"""Rule-based product type and tag inference from extracted facts.

No LLM — uses keyword matching against product name, description,
and vendor to infer product type and tags.
"""

import re

# Product type inference rules: keyword patterns → product type
# Checked in order — first match wins
TYPE_RULES = [
    # Footwear
    (r"\b(ski boot|ski-boot)\b", "Ski Boot"),
    (r"\b(snowboard boot)\b", "Snowboard Boot"),
    (r"\b(hiking boot)\b", "Hiking Boot"),
    (r"\b(approach shoe)\b", "Approach Shoe"),
    (r"\b(trail run|trail shoe|trail runner)\b", "Trail Runner"),
    (r"\b(running shoe)\b", "Running Shoe"),
    (r"\bboot\b", "Boot"),
    (r"\bshoe\b", "Shoe"),
    # Skis/Boards
    (r"\b(splitboard)\b", "Splitboard"),
    (r"\b(snowboard)\b", "Snowboard"),
    (r"\b(ski)\b(?!.*boot)(?!.*pole)(?!.*goggle)(?!.*helmet)", "Ski"),
    (r"\b(ski pole|pole)\b", "Ski Pole"),
    # Outerwear
    (r"\b(rain jacket|rain shell|hardshell)\b", "Rain Jacket"),
    (r"\b(down jacket|puffy|puffer|insulated jacket|nano puff)\b", "Insulated Jacket"),
    (r"\b(fleece)\b(?!.*glove)", "Fleece"),
    (r"\b(softshell)\b", "Softshell"),
    (r"\b(vest)\b", "Vest"),
    (r"\b(jacket|shell|parka|anorak)\b", "Jacket"),
    # Pants/Shorts
    (r"\b(ski pant|snow pant|snowboard pant)\b", "Snow Pants"),
    (r"\b(pant|trouser)\b", "Pants"),
    (r"\b(short)\b", "Shorts"),
    # Tops
    (r"\b(hoodie|hoody)\b", "Hoodie"),
    (r"\b(sweater)\b", "Sweater"),
    (r"\b(t-shirt|tee|tshirt)\b", "T-Shirt"),
    (r"\b(tank top|tank)\b", "Tank Top"),
    (r"\b(shirt|button-up|button up)\b", "Shirt"),
    # Accessories
    (r"\b(goggles?)\b", "Goggle"),
    (r"\b(sunglass|sunglasses)\b", "Sunglasses"),
    (r"\b(helmet)\b", "Helmet"),
    (r"\b(glove|mitt|mitten)\b", "Glove"),
    (r"\b(beanie|toque)\b", "Beanie"),
    (r"\b(hat|cap|trucker)\b", "Hat"),
    (r"\b(sock)\b", "Sock"),
    (r"\b(belt)\b", "Belt"),
    (r"\b(neck gaiter|neckwarmer|balaclava|buff)\b", "Neck Gaiter"),
    # Packs
    (r"\b(backpack|daypack|rucksack)\b", "Backpack"),
    (r"\b(duffel|duffle)\b", "Duffel"),
    # Climbing
    (r"\b(harness)\b", "Climbing Harness"),
    (r"\b(carabiner)\b", "Carabiner"),
    (r"\b(rope)\b(?!.*jump)", "Rope"),
    # Camping
    (r"\b(tent)\b", "Tent"),
    (r"\b(sleeping bag|sleep bag)\b", "Sleeping Bag"),
    (r"\b(sleeping pad|sleep pad)\b", "Sleeping Pad"),
    (r"\b(water bottle|bottle)\b", "Water Bottle"),
]

# Tag inference rules
GENDER_PATTERNS = {
    "mens": [r"\bmen'?s?\b", r"\bmale\b", r"\bguys?\b"],
    "womens": [r"\bwomen'?s?\b", r"\bfemale\b", r"\bladies\b"],
    "kids": [r"\bkids?\b", r"\bjunior\b", r"\byouth\b", r"\bboys?\b", r"\bgirls?\b"],
    "unisex": [r"\bunisex\b"],
}

ACTIVITY_PATTERNS = {
    "skiing": [r"\bski\b", r"\balpine\b", r"\bbackcountry\b", r"\bfreeride\b"],
    "snowboarding": [r"\bsnowboard\b"],
    "hiking": [r"\bhik(?:e|ing)\b", r"\btrail\b", r"\btrek\b"],
    "climbing": [r"\bclimb\b", r"\bboulder\b", r"\bcrag\b", r"\balpine\b"],
    "running": [r"\brun(?:ning)?\b", r"\btrail run\b"],
    "camping": [r"\bcamp(?:ing)?\b"],
    "casual": [r"\bcasual\b", r"\beveryday\b", r"\blifestyle\b"],
}


def infer_product_type(product_name: str, description: str = "", vendor: str = "") -> str | None:
    """Infer product type from product name and description.

    Returns product type string or None if no match.
    """
    text = f"{product_name} {description}".lower()

    for pattern, product_type in TYPE_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return product_type

    return None


def infer_tags(
    product_name: str,
    description: str = "",
    vendor: str = "",
    existing_tags: str = "",
) -> list[str]:
    """Infer tags from product name and description.

    Returns list of new tags to add (doesn't duplicate existing).
    """
    text = f"{product_name} {description}".lower()
    existing = {t.strip().lower() for t in existing_tags.split(",") if t.strip()}
    new_tags: list[str] = []

    # Gender
    for tag, patterns in GENDER_PATTERNS.items():
        if tag not in existing:
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    new_tags.append(tag)
                    break

    # Activity
    for tag, patterns in ACTIVITY_PATTERNS.items():
        if tag not in existing:
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    new_tags.append(tag)
                    break

    return new_tags
