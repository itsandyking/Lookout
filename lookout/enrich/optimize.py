"""Karpathy Loop: iterative prompt optimization via quality scoring.

Modifies generate_body_html.prompt and measures the effect on a fixed
test set of products with cached ExtractedFacts. No scraping — only
LLM generation + deterministic scoring per iteration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lookout.enrich.llm import LLMClient
from lookout.enrich.models import ExtractedFacts
from lookout.enrich.scorer import QualityScore, score_quality
from lookout.feedback.collector import FeedbackEntry, load_all_feedback, feedback_summary

logger = logging.getLogger(__name__)


@dataclass
class IterationResult:
    """Result of a single optimization iteration."""

    iteration: int
    timestamp: str
    prompt_path: str
    avg_score: float
    max_score: float
    per_axis: dict[str, float]
    per_product: list[dict]
    prompt_diff: str = ""

    def to_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "timestamp": self.timestamp,
            "prompt_path": self.prompt_path,
            "avg_score": round(self.avg_score, 1),
            "max_score": round(self.max_score, 1),
            "per_axis": {k: round(v, 1) for k, v in self.per_axis.items()},
            "per_product": self.per_product,
            "prompt_diff": self.prompt_diff,
        }


def load_test_set(test_dir: Path) -> list[tuple[str, ExtractedFacts]]:
    """Load all products with cached extracted facts from the test set."""
    products = []
    for handle_dir in sorted(test_dir.iterdir()):
        if not handle_dir.is_dir():
            continue
        facts_path = handle_dir / "extracted_facts.json"
        if not facts_path.exists():
            continue
        try:
            data = json.loads(facts_path.read_text())
            facts = ExtractedFacts(**data)
            products.append((handle_dir.name, facts))
        except Exception as e:
            logger.warning("Skipping %s: %s", handle_dir.name, e)
    return products


async def regenerate_and_score(
    llm: LLMClient,
    products: list[tuple[str, ExtractedFacts]],
) -> list[tuple[str, str, QualityScore]]:
    """Regenerate body HTML for all products and score each one.

    Returns list of (handle, body_html, quality_score) tuples.
    """
    results = []

    for handle, facts in products:
        try:
            body_html = await llm.generate_body_html(
                facts=facts.model_dump(),
                handle=handle,
                vendor=facts.brand or "Unknown",
            )
            qs = score_quality(body_html, facts)
            qs.handle = handle
            results.append((handle, body_html, qs))
        except Exception as e:
            logger.error("Failed to generate for %s: %s", handle, e)

    return results


def compute_iteration_result(
    iteration: int,
    results: list[tuple[str, str, QualityScore]],
    prompt_path: str,
    prompt_diff: str = "",
    feedback_handles: set[str] | None = None,
) -> IterationResult:
    """Aggregate scores into an iteration result.

    Products whose handles appear in *feedback_handles* (rejected/edited by
    a human reviewer) are weighted 2x in the average score so the optimizer
    focuses on fixing the outputs users actually complained about.
    """
    feedback_handles = feedback_handles or set()

    # Per-axis averages (deterministic only — skip fidelity)
    axis_names = ["structural_compliance", "length_targets", "anti_hype", "coverage"]
    per_axis = {}
    for name in axis_names:
        vals = [s.axes[name].score for _, _, s in results if name in s.axes]
        per_axis[name] = sum(vals) / len(vals) if vals else 0

    # Deterministic total (exclude fidelity), with feedback weighting
    weighted_totals: list[float] = []
    weight_sum = 0.0
    per_product = []
    for handle, body_html, qs in results:
        det = sum(qs.axes[name].score for name in axis_names if name in qs.axes)
        w = 2.0 if handle in feedback_handles else 1.0
        weighted_totals.append(det * w)
        weight_sum += w
        per_product.append({
            "handle": handle,
            "score": det,
            "feedback_weighted": handle in feedback_handles,
            "axes": {name: qs.axes[name].score for name in axis_names if name in qs.axes},
        })

    avg_score = sum(weighted_totals) / weight_sum if weight_sum else 0
    max_score = max(
        (sum(qs.axes[name].score for name in axis_names if name in qs.axes) for _, _, qs in results),
        default=0,
    )

    return IterationResult(
        iteration=iteration,
        timestamp=datetime.now(timezone.utc).isoformat(),
        prompt_path=prompt_path,
        avg_score=avg_score,
        max_score=max_score,
        per_axis=per_axis,
        per_product=sorted(per_product, key=lambda x: x["score"], reverse=True),
        prompt_diff=prompt_diff,
    )


def load_feedback(feedback_dir: Path | None) -> list[FeedbackEntry]:
    """Load feedback entries from one or more directories.

    Accepts a single feedback dir or scans campaign/run_*/feedback dirs.
    """
    if feedback_dir is None:
        return []
    entries = load_all_feedback(feedback_dir)
    logger.info("Loaded %d feedback entries from %s", len(entries), feedback_dir)
    return entries


def _build_feedback_context(entries: list[FeedbackEntry]) -> str:
    """Build a text block summarizing user feedback for the meta-prompt."""
    if not entries:
        return ""

    summary = feedback_summary(entries)

    sections = [
        "## USER FEEDBACK (from product reviews)",
        f"Total reviews: {summary['total']} "
        f"(approved: {summary['approved']}, rejected: {summary['rejected']}, "
        f"edited: {summary['edited']})",
        f"Approval rate: {summary['approval_rate']:.0%}",
    ]

    # Rejection reasons breakdown
    if summary["rejection_reasons"]:
        sections.append("\nRejection reasons:")
        for reason, count in sorted(
            summary["rejection_reasons"].items(), key=lambda x: -x[1]
        ):
            sections.append(f"  - {reason}: {count} products")

    # Edited examples (gold standard corrections) — show up to 3
    edited = [e for e in entries if e.disposition == "edited" and e.generated_html and e.final_html]
    if edited:
        sections.append(f"\nEdited examples ({len(edited)} total, showing up to 3):")
        for entry in edited[:3]:
            # Truncate HTML to keep meta-prompt manageable
            gen_snippet = entry.generated_html[:500]
            final_snippet = entry.final_html[:500]
            sections.append(
                f"\n  Product: {entry.handle}\n"
                f"  GENERATED (rejected by reviewer):\n    {gen_snippet}\n"
                f"  HUMAN-CORRECTED (gold standard):\n    {final_snippet}"
            )

    # Rejected examples — show up to 3
    rejected = [e for e in entries if e.disposition == "rejected" and e.reason]
    if rejected:
        sections.append(f"\nRejected examples ({len(rejected)} total, showing up to 3):")
        for entry in rejected[:3]:
            gen_snippet = (entry.generated_html or "")[:300]
            sections.append(
                f"\n  Product: {entry.handle} — Reason: {entry.reason}\n"
                f"  Generated:\n    {gen_snippet}"
            )

    return "\n".join(sections)


async def run_optimization_loop(
    test_dir: Path,
    prompt_path: Path,
    log_dir: Path,
    max_iterations: int = 5,
    improvement_threshold: float = 1.0,
    feedback_dir: Path | None = None,
) -> list[IterationResult]:
    """Run the Karpathy Loop: modify prompt → regenerate → score → iterate.

    The loop uses an LLM meta-prompt to suggest prompt improvements based
    on scoring results, then tests the modified prompt against the full
    test set.

    Args:
        test_dir: Directory containing cached test set artifacts.
        prompt_path: Path to generate_body_html.prompt.
        log_dir: Directory to write iteration logs.
        max_iterations: Maximum number of optimization iterations.
        improvement_threshold: Stop if improvement drops below this.
        feedback_dir: Optional directory with user feedback JSON files.

    Returns:
        List of IterationResult objects.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # Load test set once
    products = load_test_set(test_dir)
    if not products:
        raise ValueError(f"No products found in {test_dir}")
    logger.info("Loaded %d products from test set", len(products))

    # Load user feedback
    feedback_entries = load_feedback(feedback_dir)
    feedback_context = _build_feedback_context(feedback_entries)

    # Identify rejected/edited handles for score weighting
    feedback_handles = {
        e.handle for e in feedback_entries
        if e.disposition in ("rejected", "edited")
    }
    if feedback_handles:
        test_handles = {h for h, _ in products}
        overlap = feedback_handles & test_handles
        logger.info(
            "Feedback: %d rejected/edited handles, %d overlap with test set",
            len(feedback_handles), len(overlap),
        )

    llm = LLMClient()
    history: list[IterationResult] = []

    # Iteration 0: baseline with current prompt
    logger.info("Iteration 0: scoring baseline prompt")
    results = await regenerate_and_score(llm, products)
    baseline = compute_iteration_result(
        0, results, str(prompt_path), feedback_handles=feedback_handles,
    )
    history.append(baseline)
    _save_iteration(log_dir, baseline, results, prompt_path.read_text())
    logger.info("Baseline: avg %.1f/70 (det.)", baseline.avg_score)

    for i in range(1, max_iterations + 1):
        # Ask the LLM to suggest a prompt improvement
        current_prompt = prompt_path.read_text()
        suggestion = await _suggest_improvement(
            llm, current_prompt, history, feedback_context,
        )

        if not suggestion.strip():
            logger.info("No further improvements suggested, stopping")
            break

        # Backup current prompt
        backup_path = log_dir / f"prompt_iter_{i - 1}.prompt"
        shutil.copy2(prompt_path, backup_path)

        # Apply the suggested prompt
        prompt_path.write_text(suggestion)

        # Clear the prompt cache so the LLM client picks up the new prompt
        llm._prompt_cache.clear()

        logger.info("Iteration %d: testing modified prompt", i)
        results = await regenerate_and_score(llm, products)
        result = compute_iteration_result(
            i, results, str(prompt_path),
            prompt_diff=f"See prompt_iter_{i}.prompt",
            feedback_handles=feedback_handles,
        )
        history.append(result)
        _save_iteration(log_dir, result, results, suggestion)

        improvement = result.avg_score - history[-2].avg_score
        logger.info(
            "Iteration %d: avg %.1f/70 (%+.1f from previous)",
            i, result.avg_score, improvement,
        )

        # If the new prompt is worse, revert
        if improvement < 0:
            logger.info("Score decreased, reverting prompt")
            prompt_path.write_text(current_prompt)
            llm._prompt_cache.clear()

        # Stop if improvement is below threshold
        if 0 <= improvement < improvement_threshold:
            logger.info("Improvement below threshold (%.1f < %.1f), stopping",
                        improvement, improvement_threshold)
            break

    # Summary
    best = max(history, key=lambda r: r.avg_score)
    logger.info(
        "Optimization complete. Best: iteration %d (%.1f/70). "
        "Improvement: %+.1f from baseline.",
        best.iteration, best.avg_score, best.avg_score - history[0].avg_score,
    )

    # Write summary log
    summary = {
        "iterations": len(history),
        "baseline_avg": history[0].avg_score,
        "best_avg": best.avg_score,
        "best_iteration": best.iteration,
        "improvement": round(best.avg_score - history[0].avg_score, 1),
        "history": [r.to_dict() for r in history],
    }
    (log_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    return history


async def _suggest_improvement(
    llm: LLMClient,
    current_prompt: str,
    history: list[IterationResult],
    feedback_context: str = "",
) -> str:
    """Use the LLM to suggest a prompt improvement based on scoring data and user feedback."""
    latest = history[-1]

    # Build the scoring context
    weak_products = [p for p in latest.per_product if p["score"] < latest.avg_score]
    weak_axes = sorted(latest.per_axis.items(), key=lambda x: x[1])

    # Mark feedback-weighted products in the weak list
    weak_lines = []
    for p in weak_products[:5]:
        tag = " [REJECTED/EDITED BY REVIEWER]" if p.get("feedback_weighted") else ""
        weak_lines.append(
            f"  {p['handle']}: {p['score']}/70 "
            f"(structure={p['axes'].get('structural_compliance', 0)}, "
            f"length={p['axes'].get('length_targets', 0)}, "
            f"hype={p['axes'].get('anti_hype', 0)}, "
            f"coverage={p['axes'].get('coverage', 0)}){tag}"
        )

    feedback_section = ""
    if feedback_context:
        feedback_section = f"""

{feedback_context}

NOTE: Products flagged by human reviewers are weighted 2x in scoring.
Prioritize fixing the patterns that caused rejections and edits."""

    meta_prompt = f"""You are optimizing a product description prompt for a Shopify store.

## CURRENT PROMPT
{current_prompt}

## SCORING RESULTS (iteration {latest.iteration})
Average deterministic score: {latest.avg_score:.1f}/70

Per-axis averages (lower = more room for improvement):
{chr(10).join(f"  {name}: {score:.1f}/{max_s}" for name, (score, max_s) in [
    ("structural_compliance", (latest.per_axis.get("structural_compliance", 0), 25)),
    ("length_targets", (latest.per_axis.get("length_targets", 0), 15)),
    ("anti_hype", (latest.per_axis.get("anti_hype", 0), 15)),
    ("coverage", (latest.per_axis.get("coverage", 0), 15)),
])}

Weakest products:
{chr(10).join(weak_lines)}
{feedback_section}
## SCORING CRITERIA
- **structural_compliance (0-25)**: Needs intro <p>, <h3>Features</h3> with <ul> (≤6 items), <h3>Specifications</h3> with <table>, semantic HTML
- **length_targets (0-15)**: Body 100-400 words, feature bullets ≤12 words each
- **anti_hype (0-15)**: No banned marketing words (amazing, incredible, revolutionary, etc.)
- **coverage (0-15)**: Use available facts — description blocks, feature bullets, specs, materials, care instructions

## ITERATION HISTORY
{chr(10).join(f"  Iter {r.iteration}: {r.avg_score:.1f}/70" for r in history)}

## YOUR TASK
Modify the prompt to improve the weakest axes. Rules:
1. Output ONLY the complete modified prompt — no commentary, no markdown code blocks
2. Keep the {{handle}}, {{vendor}}, and {{facts}} template variables exactly as-is
3. The prompt must still produce valid Shopify HTML
4. Focus changes on the weakest scoring axes
5. Small, targeted changes — don't rewrite the entire prompt
6. Consider: explicit word count guidance, mandatory section structure, coverage checklists
7. If user feedback is provided, address the specific rejection patterns and learn from human edits"""

    system = (
        "You are a prompt engineer optimizing an LLM prompt for product descriptions. "
        "Output only the modified prompt text, nothing else."
    )

    return await llm.provider.complete(meta_prompt, system, max_tokens=2000)


def _save_iteration(
    log_dir: Path,
    result: IterationResult,
    gen_results: list[tuple[str, str, QualityScore]],
    prompt_text: str,
) -> None:
    """Save iteration artifacts to disk."""
    iter_dir = log_dir / f"iter_{result.iteration}"
    iter_dir.mkdir(exist_ok=True)

    # Save the prompt used
    (iter_dir / "prompt.txt").write_text(prompt_text)

    # Save scoring result
    (iter_dir / "scores.json").write_text(
        json.dumps(result.to_dict(), indent=2)
    )

    # Save generated HTML per product
    outputs_dir = iter_dir / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    for handle, body_html, qs in gen_results:
        (outputs_dir / f"{handle}.html").write_text(body_html)
