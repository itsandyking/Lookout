# Feedback Analysis: Pattern Detection & Threshold Proposals

**Date:** 2026-04-05
**Issue:** Lookout-mnq — Feedback Loop for Improving System

## Problem

The enrichment pipeline has ~30 hardcoded scoring thresholds across resolver, title gate, and post-extraction validation. When products fail matching, the failure reasons are logged to `match_decisions.jsonl` and feedback dispositions are captured via `enrich apply`. But this data sits unused — failures that cluster around specific thresholds or vendors require manual investigation to notice.

## Solution

An analyzer that runs automatically after feedback submission, clusters failures by root cause, identifies threshold boundaries causing false rejections, and proposes specific threshold changes with replay-based impact assessment. Human reviews and decides — no auto-tuning.

## Architecture

Three new modules, one integration point:

### `lookout/feedback/analyzer.py` — Pattern Detection

Loads decision logs + feedback dispositions. Two-pass analysis:

**Pass 1 — Group by failure reason.** Each `candidates_tried` entry has an `outcome` field (`skip_low_confidence`, `reject_title_gate`, `reject_bot_blocked`, etc.). Count occurrences per reason. Only surface clusters with 3+ occurrences.

**Pass 2 — Find commonalities within each cluster:**
- Same vendor — "All 5 title gate failures are Petzl"
- Same product type — "All 4 foreign product rejections are ski boots"
- Same threshold boundary — For each numeric threshold in the registry, check if the cluster's values fall within 80% of the cutoff (e.g., word_overlap values of 0.25-0.29 are within 80% of the 0.30 cutoff). If so, propose moving the threshold to 10% below the cluster minimum (e.g., 0.22).
- Same penalty stacking — Sum all penalties applied to each decision. If the total penalty exceeds -50 but no single penalty is dominant (>60% of total), flag as "penalty stacking" with the combination listed.

Output: list of `PatternCluster` objects, each containing the failure reason, affected decisions, common attributes, and (if applicable) a proposed threshold change.

**Key types:**

```python
@dataclass
class ThresholdProposal:
    parameter: str          # e.g., "title_gate.word_overlap"
    current_value: float    # e.g., 0.30
    proposed_value: float   # e.g., 0.22
    rationale: str          # e.g., "5 failures cluster at 0.25-0.29"

@dataclass
class PatternCluster:
    failure_reason: str         # e.g., "reject_title_gate"
    count: int                  # e.g., 5
    common_vendor: str | None   # e.g., "Petzl" or None if mixed
    common_type: str | None     # e.g., "ski boots" or None
    affected_handles: list[str]
    threshold_boundary: dict | None  # min/max values near cutoff
    proposal: ThresholdProposal | None
    actionable: bool            # False for bot-blocked, etc.

def analyze(decisions_path: Path, feedback_dir: Path | None = None) -> list[PatternCluster]
```

### `lookout/feedback/replay.py` — Impact Assessment

Thin wrapper around existing `rescore_candidates`. Takes a `ThresholdProposal` and replays affected decisions to show what would change.

```python
@dataclass
class ReplayDiff:
    proposal: ThresholdProposal
    recovered: list[dict]     # was rejected, now passes
    regressed: list[dict]     # was accepted, now rejected
    unchanged: int

def replay_proposal(
    proposal: ThresholdProposal,
    decisions_path: Path,
) -> ReplayDiff
```

**How it works:**
1. Loads all decisions from `match_decisions.jsonl`
2. For resolver-stage thresholds: calls `rescore_candidates` with modified value
3. For title gate thresholds: re-runs `check_title_gate` with saved inputs
4. For post-extraction thresholds: re-runs `check_post_extraction` with saved signals
5. Compares old vs new outcome for each decision

Scope: current run's decisions only. Multi-run aggregation deferred.

### `lookout/feedback/report.py` — Output Formatting

Formats analysis results in two modes:

**Terminal summary** (max 5 patterns, printed to stdout):
```
── Feedback Analysis ──────────────────────────────
3 patterns detected across 47 decisions (12 rejected)

  Title gate: 5 failures (all Petzl)
    → Lower word_overlap 0.30 → 0.22: recovers 4, 0 regressions

  Foreign product: 4 failures (ski boots)
    → Near-homonym threshold too tight? 3 were false near-homonym hits

  Bot blocked: 3 failures (Arc'teryx)
    → No threshold fix — vendor site blocks scrapers

Full analysis: output/run-20260405/feedback_analysis.md
────────────────────────────────────────────────────
```

**Full markdown report** (`feedback_analysis.md` in run directory):
- Summary stats: total decisions, accepted, rejected, approval rate
- Each pattern cluster: failure reason, affected products, common attributes, threshold boundary analysis, proposed fix with full replay diff
- "No action needed" section for correctly-working patterns (e.g., demographic mismatch correctly rejecting wrong-gender products)

```python
def format_terminal(clusters: list[PatternCluster], diffs: list[ReplayDiff], total: int, rejected: int) -> str
def format_report(clusters: list[PatternCluster], diffs: list[ReplayDiff], total: int, rejected: int) -> str
def write_report(report: str, run_dir: Path) -> Path
```

### Integration: `enrich apply` hook

In `lookout/cli.py`, after `collect_feedback()` and `save_feedback()` complete:

1. Call `analyze()` with the run's `match_decisions.jsonl` and feedback directory
2. For each cluster with a `proposal`, call `replay_proposal()` to get the diff
3. Print terminal summary via `format_terminal()`
4. Write full report via `write_report()`

This adds ~10 lines to the existing `enrich apply` command. No new CLI commands needed.

## Threshold Parameter Registry

The analyzer needs to know which thresholds exist and where they live. A simple registry dict in `analyzer.py`:

```python
THRESHOLD_REGISTRY = {
    "title_gate.word_overlap": {
        "current": 0.3,
        "file": "match_validator.py",
        "line_context": "word_overlap < 0.3",
        "direction": "lower_to_recover",  # lowering recovers more products
    },
    "title_gate.title_similarity": {
        "current": 0.4,
        "file": "match_validator.py",
        "line_context": "title_sim < 0.4",
        "direction": "lower_to_recover",
    },
    "resolver.strong_overlap": {
        "current": 0.6,
        "file": "resolver.py",
        "line_context": "overlap_ratio >= 0.6",
        "direction": "lower_to_recover",
    },
    "resolver.strong_seq": {
        "current": 0.5,
        "file": "resolver.py",
        "line_context": "seq_ratio >= 0.5",
        "direction": "lower_to_recover",
    },
    "resolver.weak_overlap": {
        "current": 0.2,
        "file": "resolver.py",
        "line_context": "overlap_ratio < 0.2",
        "direction": "raise_to_recover",
    },
    "resolver.weak_seq": {
        "current": 0.3,
        "file": "resolver.py",
        "line_context": "seq_ratio < 0.3",
        "direction": "raise_to_recover",
    },
    "resolver.moderate_overlap": {
        "current": 0.3,
        "file": "resolver.py",
        "line_context": "overlap_ratio < 0.3",
        "direction": "raise_to_recover",
    },
    "resolver.foreign_product_penalty": {
        "current": -35,
        "file": "resolver.py",
        "direction": "raise_to_recover",  # less negative = recover
    },
    "resolver.near_homonym_penalty": {
        "current": -40,
        "file": "resolver.py",
        "direction": "raise_to_recover",
    },
    "resolver.demographic_penalty": {
        "current": -15,
        "file": "resolver.py",
        "direction": "raise_to_recover",
    },
    "resolver.critical_mismatch_penalty": {
        "current": -30,
        "file": "resolver.py",
        "direction": "raise_to_recover",
    },
    "post_extraction.pass_threshold": {
        "current": 50,
        "file": "match_validator.py",
        "direction": "lower_to_recover",
    },
    "post_extraction.title_weight": {
        "current": 40,
        "file": "match_validator.py",
        "direction": "lower_to_recover",
    },
    "pipeline.candidate_skip": {
        "current": 50,
        "file": "pipeline.py",
        "direction": "lower_to_recover",
    },
}
```

This is a static registry — not auto-discovered from code. When we add or change thresholds in code, we update the registry manually. Simple and explicit.

## Testing

1. **Analyzer unit tests** — Synthetic decision logs with known failure patterns. Assert correct cluster detection, vendor identification, and threshold boundary detection.

2. **Replay unit tests** — Known decisions + threshold change → assert correct diff (N recovered, 0 regressed).

3. **Report unit tests** — Assert terminal summary format and markdown report structure.

4. **Integration test** — Mock analyzer in `enrich apply` flow, assert terminal output printed and markdown file written.

## What This Does NOT Do

- **No auto-tuning** — Proposes changes, human decides
- **No vendor-level overrides** — Global thresholds only (vendor overrides deferred)
- **No multi-run aggregation** — Analyzes current run only (--all-runs deferred)
- **No LLM-powered diagnosis** — Template-driven diagnostics from structured failure reasons
- **No code modification** — Reports only; threshold changes are manual
