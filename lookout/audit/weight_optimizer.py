"""Weight optimization: tune PriorityWeights for coverage efficiency.

Instead of optimizing against a manual expert ranking, this optimizer
maximizes a composite coverage efficiency metric that measures how well
the top-N priority products cover:

1. Variant leverage — total variants fixed per product fix
2. Vendor clustering — fewer distinct vendors = more batch-fixable
3. Inventory value coverage — total value addressed
4. Online traffic alignment — products with real sessions/impressions
"""

from __future__ import annotations

import json
import logging
import random
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from lookout.audit.models import ProductScore
from lookout.audit.priority_fn import rank_scores
from lookout.audit.weight_config import _CONTINUOUS_PARAMS, BOUNDS, PriorityWeights

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------

_EXCLUDE_FIELDS = {"_variants_raw"}


def save_snapshot(scores: list[ProductScore], path: Path) -> None:
    """Serialize a list of ProductScore to JSON."""
    data = []
    for s in scores:
        d = asdict(s)
        for key in _EXCLUDE_FIELDS:
            d.pop(key, None)
        data.append(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    logger.info("Saved snapshot with %d products to %s", len(data), path)


def load_snapshot(path: Path) -> list[ProductScore]:
    """Deserialize a snapshot JSON back to ProductScore objects."""
    data = json.loads(path.read_text())
    scores = []
    for d in data:
        from dataclasses import fields as dc_fields

        known = {f.name for f in dc_fields(ProductScore)}
        filtered = {k: v for k, v in d.items() if k in known}
        scores.append(ProductScore(**filtered))
    return scores


# ---------------------------------------------------------------------------
# Coverage efficiency metric
# ---------------------------------------------------------------------------


def coverage_efficiency(
    scores: list[ProductScore],
    weights: PriorityWeights,
    top_n: int = 50,
) -> float:
    """Compute coverage efficiency for a set of weights.

    Ranks all products using the given weights, takes the top_n, and
    measures how much merchandising value that batch covers.

    Components (each normalized to 0-1, then combined):

    1. Variant leverage (weight 0.30):
       Total variants with missing images in top-N / total across all products.
       More variants fixed per batch = better.

    2. Vendor concentration (weight 0.20):
       1 - (distinct vendors in top-N / top-N count).
       Fewer vendors = more batch-fixable.

    3. Inventory value coverage (weight 0.25):
       Sum of inventory_value in top-N / total inventory value of all gap products.

    4. Online traffic alignment (weight 0.25):
       Sum of sessions + impressions in top-N / total sessions + impressions.
       Products with real traffic should be prioritized.

    Returns a score from 0 to 1 (higher = better coverage efficiency).
    """
    ranking = rank_scores(scores, weights)

    if not ranking:
        return 0.0

    # Build lookup
    by_handle = {s.handle: s for s in scores}
    # Use ranking handles to determine gap products (rank_scores already filters)
    all_ranked = set(rank_scores(scores, weights))
    gap_products = [s for s in scores if s.handle in all_ranked]

    if not gap_products:
        return 0.0

    # Take top-N from ranking
    top_handles = ranking[:top_n]
    top_scores = [by_handle[h] for h in top_handles if h in by_handle]

    if not top_scores:
        return 0.0

    # --- 1. Variant leverage (0.30) ---
    total_missing_variants = sum(s.variants_missing_images for s in gap_products)
    top_missing_variants = sum(s.variants_missing_images for s in top_scores)
    variant_coverage = (
        top_missing_variants / total_missing_variants if total_missing_variants > 0 else 0.0
    )

    # --- 2. Vendor concentration (0.20) ---
    top_vendors = set(s.vendor for s in top_scores)
    vendor_concentration = 1.0 - (len(top_vendors) / max(len(top_scores), 1))

    # --- 3. Inventory value coverage (0.25) ---
    total_inv = sum(s.inventory_value for s in gap_products)
    top_inv = sum(s.inventory_value for s in top_scores)
    inv_coverage = top_inv / total_inv if total_inv > 0 else 0.0

    # --- 4. Online traffic alignment (0.25) ---
    total_traffic = sum(s.online_sessions + s.gmc_impressions for s in gap_products)
    top_traffic = sum(s.online_sessions + s.gmc_impressions for s in top_scores)
    traffic_coverage = top_traffic / total_traffic if total_traffic > 0 else 0.0

    # Weighted combination
    score = (
        0.30 * variant_coverage
        + 0.20 * vendor_concentration
        + 0.25 * inv_coverage
        + 0.25 * traffic_coverage
    )

    return score


def coverage_efficiency_breakdown(
    scores: list[ProductScore],
    weights: PriorityWeights,
    top_n: int = 50,
) -> dict:
    """Like coverage_efficiency but returns per-component breakdown."""
    ranking = rank_scores(scores, weights)
    by_handle = {s.handle: s for s in scores}
    all_ranked = set(ranking)
    gap_products = [s for s in scores if s.handle in all_ranked]

    top_handles = ranking[:top_n]
    top_scores = [by_handle[h] for h in top_handles if h in by_handle]

    if not gap_products or not top_scores:
        return {
            "variant_leverage": 0.0,
            "vendor_concentration": 0.0,
            "inventory_coverage": 0.0,
            "traffic_alignment": 0.0,
            "composite": 0.0,
            "top_n": top_n,
            "top_vendors": [],
            "top_variant_count": 0,
        }

    total_missing = sum(s.variants_missing_images for s in gap_products)
    top_missing = sum(s.variants_missing_images for s in top_scores)
    variant_leverage = top_missing / total_missing if total_missing > 0 else 0.0

    top_vendors_set = set(s.vendor for s in top_scores)
    vendor_concentration = 1.0 - (len(top_vendors_set) / max(len(top_scores), 1))

    total_inv = sum(s.inventory_value for s in gap_products)
    top_inv = sum(s.inventory_value for s in top_scores)
    inv_coverage = top_inv / total_inv if total_inv > 0 else 0.0

    total_traffic = sum(s.online_sessions + s.gmc_impressions for s in gap_products)
    top_traffic = sum(s.online_sessions + s.gmc_impressions for s in top_scores)
    traffic_alignment = top_traffic / total_traffic if total_traffic > 0 else 0.0

    composite = (
        0.30 * variant_leverage
        + 0.20 * vendor_concentration
        + 0.25 * inv_coverage
        + 0.25 * traffic_alignment
    )

    # Top vendors by product count
    vendor_counts = Counter(s.vendor for s in top_scores)

    return {
        "variant_leverage": round(variant_leverage, 4),
        "vendor_concentration": round(vendor_concentration, 4),
        "inventory_coverage": round(inv_coverage, 4),
        "traffic_alignment": round(traffic_alignment, 4),
        "composite": round(composite, 4),
        "top_n": len(top_scores),
        "top_vendors": vendor_counts.most_common(10),
        "top_variant_count": top_missing,
        "top_inventory_value": round(top_inv, 2),
    }


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------


def _random_weights_array() -> list[float]:
    """Generate random weight values within BOUNDS."""
    arr = []
    for name in _CONTINUOUS_PARAMS:
        lo, hi = BOUNDS[name]
        arr.append(random.uniform(lo, hi))
    return arr


def run_weight_optimization(
    scores: list[ProductScore],
    log_dir: Path,
    top_n: int = 50,
    max_iterations: int = 200,
    n_restarts: int = 5,
) -> dict:
    """Optimize weights to maximize coverage efficiency.

    Tries scipy Nelder-Mead if available, otherwise falls back to random
    search. Runs n_restarts per inventory transform (linear/log/sqrt).

    Returns dict with best_weights, best_efficiency, baseline_efficiency,
    breakdown, and history.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # Baseline
    default_weights = PriorityWeights()
    baseline_eff = coverage_efficiency(scores, default_weights, top_n)
    baseline_breakdown = coverage_efficiency_breakdown(scores, default_weights, top_n)
    logger.info("Baseline coverage efficiency: %.4f", baseline_eff)

    global_best_eff = baseline_eff
    global_best_weights = default_weights
    history: list[dict] = []

    inv_transforms = ["linear", "log", "sqrt"]

    use_scipy = False
    try:
        from scipy.optimize import minimize

        use_scipy = True
        logger.info("Using scipy Nelder-Mead optimizer")
    except ImportError:
        logger.info("scipy not available, using random search")

    iteration = 0

    for inv_transform in inv_transforms:
        for restart in range(n_restarts):

            def objective(arr, _inv_transform=inv_transform):
                w = PriorityWeights.from_array(list(arr), inventory_transform=_inv_transform)
                return -coverage_efficiency(scores, w, top_n)

            if use_scipy:
                from scipy.optimize import minimize

                x0 = _random_weights_array()
                result = minimize(
                    objective,
                    x0,
                    method="Nelder-Mead",
                    options={"maxiter": max_iterations, "xatol": 1e-4, "fatol": 1e-4},
                )
                candidate = PriorityWeights.from_array(
                    list(result.x), inventory_transform=inv_transform
                )
                eff = coverage_efficiency(scores, candidate, top_n)
            else:
                best_local = -1.0
                candidate = default_weights
                for _ in range(max_iterations):
                    arr = _random_weights_array()
                    w = PriorityWeights.from_array(arr, inventory_transform=inv_transform)
                    e = coverage_efficiency(scores, w, top_n)
                    if e > best_local:
                        best_local = e
                        candidate = w
                eff = best_local

            entry = {
                "iteration": iteration,
                "restart": restart,
                "inventory_transform": inv_transform,
                "efficiency": round(eff, 6),
                "weights": candidate.to_dict(),
            }
            history.append(entry)

            if eff > global_best_eff:
                global_best_eff = eff
                global_best_weights = candidate
                iter_path = log_dir / f"iter_{iteration}.json"
                iter_path.write_text(json.dumps(entry, indent=2))
                logger.info(
                    "New best: %.4f (restart %d, transform=%s)",
                    eff,
                    restart,
                    inv_transform,
                )

            iteration += 1

    best_breakdown = coverage_efficiency_breakdown(scores, global_best_weights, top_n)

    summary = {
        "best_weights": global_best_weights.to_dict(),
        "best_efficiency": round(global_best_eff, 6),
        "baseline_efficiency": round(baseline_eff, 6),
        "improvement": round(global_best_eff - baseline_eff, 6),
        "baseline_breakdown": baseline_breakdown,
        "best_breakdown": best_breakdown,
        "total_iterations": iteration,
        "optimizer": "scipy-nelder-mead" if use_scipy else "random-search",
        "top_n": top_n,
        "history": history,
    }
    (log_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    logger.info(
        "Optimization complete. Best: %.4f (baseline: %.4f, improvement: %+.4f)",
        global_best_eff,
        baseline_eff,
        global_best_eff - baseline_eff,
    )

    return {
        "best_weights": global_best_weights,
        "best_efficiency": global_best_eff,
        "baseline_efficiency": baseline_eff,
        "baseline_breakdown": baseline_breakdown,
        "best_breakdown": best_breakdown,
        "history": history,
    }
