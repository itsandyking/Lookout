"""Weight optimization: tune PriorityWeights against an expert ranking.

Serializes audit snapshots, loads expert rankings, and runs Nelder-Mead
(or random search as fallback) to maximize Spearman rank correlation.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import asdict
from pathlib import Path

from lookout.audit.models import ProductScore
from lookout.audit.priority_fn import rank_scores
from lookout.audit.weight_config import BOUNDS, PriorityWeights, _CONTINUOUS_PARAMS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------

# Fields to exclude when serializing (non-serializable or internal)
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
        # Filter to only known ProductScore fields
        from dataclasses import fields as dc_fields

        known = {f.name for f in dc_fields(ProductScore)}
        filtered = {k: v for k, v in d.items() if k in known}
        scores.append(ProductScore(**filtered))
    return scores


# ---------------------------------------------------------------------------
# Expert ranking I/O
# ---------------------------------------------------------------------------


def load_expert_ranking(path: Path) -> list[str]:
    """Load an expert ranking file: one handle per line, skip blank/comments."""
    handles = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        handles.append(line)
    return handles


# ---------------------------------------------------------------------------
# Spearman rank correlation (pure Python)
# ---------------------------------------------------------------------------


def _spearman(x_ranks: list[float], y_ranks: list[float]) -> float:
    """Compute Spearman rank correlation coefficient."""
    n = len(x_ranks)
    if n < 2:
        return 0.0
    d_squared = sum((x - y) ** 2 for x, y in zip(x_ranks, y_ranks))
    return 1 - (6 * d_squared) / (n * (n**2 - 1))


def compute_spearman(
    scores: list[ProductScore],
    weights: PriorityWeights,
    expert_handles: list[str],
) -> float:
    """Compute Spearman correlation between formula ranking and expert ranking.

    Only handles present in both the formula output and the expert list are
    compared. Returns 0.0 if fewer than 2 handles overlap.
    """
    formula_ranking = rank_scores(scores, weights)

    # Build rank maps (1-indexed)
    formula_rank = {h: i + 1 for i, h in enumerate(formula_ranking)}
    expert_rank = {h: i + 1 for i, h in enumerate(expert_handles)}

    # Intersect
    common = [h for h in expert_handles if h in formula_rank]
    if len(common) < 2:
        return 0.0

    x_ranks = [float(formula_rank[h]) for h in common]
    y_ranks = [float(expert_rank[h]) for h in common]

    return _spearman(x_ranks, y_ranks)


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


def _objective(
    arr: list[float],
    scores: list[ProductScore],
    expert_handles: list[str],
    inv_transform: str,
) -> float:
    """Negative Spearman correlation (for minimization)."""
    weights = PriorityWeights.from_array(list(arr), inventory_transform=inv_transform)
    return -compute_spearman(scores, weights, expert_handles)


def run_weight_optimization(
    scores: list[ProductScore],
    expert_handles: list[str],
    log_dir: Path,
    max_iterations: int = 200,
    n_restarts: int = 5,
) -> dict:
    """Optimize weights to maximize Spearman correlation with expert ranking.

    Tries scipy Nelder-Mead if available, otherwise falls back to random
    search. Runs ``n_restarts`` independent starting points and keeps the
    global best.

    Returns dict with best_weights, best_correlation, baseline_correlation,
    and history.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # Baseline correlation with default weights
    default_weights = PriorityWeights()
    baseline_corr = compute_spearman(scores, default_weights, expert_handles)
    logger.info("Baseline Spearman correlation: %.4f", baseline_corr)

    global_best_corr = baseline_corr
    global_best_weights = default_weights
    history: list[dict] = []

    # Try each inventory transform
    inv_transforms = ["linear", "log", "sqrt"]

    use_scipy = False
    try:
        from scipy.optimize import minimize  # noqa: F401

        use_scipy = True
        logger.info("Using scipy Nelder-Mead optimizer")
    except ImportError:
        logger.info("scipy not available, using random search")

    iteration = 0

    for inv_transform in inv_transforms:
        for restart in range(n_restarts):
            if use_scipy:
                from scipy.optimize import minimize

                x0 = _random_weights_array()
                bounds_list = [BOUNDS[name] for name in _CONTINUOUS_PARAMS]

                result = minimize(
                    _objective,
                    x0,
                    args=(scores, expert_handles, inv_transform),
                    method="Nelder-Mead",
                    options={"maxiter": max_iterations, "xatol": 1e-4, "fatol": 1e-4},
                )

                candidate = PriorityWeights.from_array(
                    list(result.x), inventory_transform=inv_transform
                )
                corr = compute_spearman(scores, candidate, expert_handles)

            else:
                # Random search fallback
                best_local_corr = -2.0
                candidate = default_weights
                for _ in range(max_iterations):
                    arr = _random_weights_array()
                    w = PriorityWeights.from_array(arr, inventory_transform=inv_transform)
                    c = compute_spearman(scores, w, expert_handles)
                    if c > best_local_corr:
                        best_local_corr = c
                        candidate = w
                corr = best_local_corr

            entry = {
                "iteration": iteration,
                "restart": restart,
                "inventory_transform": inv_transform,
                "correlation": round(corr, 6),
                "weights": candidate.to_dict(),
            }
            history.append(entry)

            if corr > global_best_corr:
                global_best_corr = corr
                global_best_weights = candidate
                # Log improvement
                iter_path = log_dir / f"iter_{iteration}.json"
                iter_path.write_text(json.dumps(entry, indent=2))
                logger.info(
                    "New best: %.4f (restart %d, transform=%s)",
                    corr,
                    restart,
                    inv_transform,
                )

            iteration += 1

    # Write summary
    summary = {
        "best_weights": global_best_weights.to_dict(),
        "best_correlation": round(global_best_corr, 6),
        "baseline_correlation": round(baseline_corr, 6),
        "improvement": round(global_best_corr - baseline_corr, 6),
        "total_iterations": iteration,
        "optimizer": "scipy-nelder-mead" if use_scipy else "random-search",
        "history": history,
    }
    (log_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    logger.info(
        "Optimization complete. Best: %.4f (baseline: %.4f, improvement: %+.4f)",
        global_best_corr,
        baseline_corr,
        global_best_corr - baseline_corr,
    )

    return {
        "best_weights": global_best_weights,
        "best_correlation": global_best_corr,
        "baseline_correlation": baseline_corr,
        "history": history,
    }
