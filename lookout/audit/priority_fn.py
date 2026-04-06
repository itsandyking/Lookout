"""Pure priority computation using configurable weights.

Replicates the logic in ProductScore.calculate_gaps() but parameterised
by a PriorityWeights instance so the formula can be tuned.
"""

from __future__ import annotations

import math

from lookout.audit.models import ProductScore
from lookout.audit.weight_config import PriorityWeights


def compute_priority(score: ProductScore, weights: PriorityWeights) -> float:
    """Compute priority score for a single product using the given weights.

    With default PriorityWeights this produces identical results to
    ProductScore.calculate_gaps().
    """
    # --- Gap count (weighted) ---
    gap_count = 0.0
    if not score.has_product_image:
        gap_count += weights.gap_image
    if not score.has_all_variant_images:
        gap_count += weights.gap_variant_images * (
            score.variants_missing_images / max(score.variant_count, 1)
        )
    if not score.has_description:
        gap_count += weights.gap_description
    if not score.has_product_type:
        gap_count += weights.gap_type
    if not score.has_tags:
        gap_count += weights.gap_tags

    if gap_count == 0:
        return 0.0

    # --- Inventory transform ---
    inv = score.inventory_value
    if weights.inventory_transform == "log":
        inv = math.log1p(inv)
    elif weights.inventory_transform == "sqrt":
        inv = math.sqrt(inv)
    # else "linear" — keep as-is

    # --- Price multiplier ---
    if score.full_price_inventory_value > 0 and score.inventory_value > 0:
        full_price_ratio = score.full_price_inventory_value / score.inventory_value
        price_multiplier = 1 + full_price_ratio
    else:
        price_multiplier = 1.0

    base_score = inv * gap_count * price_multiplier

    # --- Online / GMC boost ---
    boost = 1.0

    if score.online_sessions > 0:
        session_weight = math.log1p(score.online_sessions) / math.log1p(weights.session_scale)
        boost += score.opportunity_gap * session_weight

    if score.gmc_impressions > 0:
        impression_weight = math.log1p(score.gmc_impressions) / math.log1p(weights.impression_scale)
        boost += score.discovery_gap * impression_weight

    if score.gmc_disapproved:
        boost += weights.disapproval_boost

    return base_score * boost


def rank_scores(scores: list[ProductScore], weights: PriorityWeights) -> list[str]:
    """Rank products by priority, returning handles in descending order.

    Only products with at least one gap (gap_count > 0 under the given
    weights) are included.
    """
    scored = []
    for s in scores:
        p = compute_priority(s, weights)
        if p > 0:
            scored.append((s.handle, p))

    scored.sort(key=lambda t: t[1], reverse=True)
    return [handle for handle, _ in scored]
