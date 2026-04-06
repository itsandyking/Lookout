"""Format feedback analysis results for terminal and file output.

Two output modes:
- Terminal summary: concise pattern list (max 5), printed to stdout
- Full markdown report: detailed analysis written to feedback_analysis.md
"""

from __future__ import annotations

from pathlib import Path

from lookout.feedback.analyzer import PatternCluster, ThresholdProposal
from lookout.feedback.replay import ReplayDiff


# ---------------------------------------------------------------------------
# Reason labels — human-friendly names for internal failure reasons
# ---------------------------------------------------------------------------

_REASON_LABELS: dict[str, str] = {
    "reject_title_gate": "Title gate",
    "skip_low_confidence": "Low confidence",
    "reject_bot_blocked": "Bot blocked",
    "reject_foreign_product": "Foreign product",
    "reject_near_homonym": "Near homonym",
    "reject_demographic_mismatch": "Demographic mismatch",
    "reject_post_extraction": "Post-extraction check",
}

MAX_TERMINAL_PATTERNS = 5


def _reason_label(reason: str) -> str:
    return _REASON_LABELS.get(reason, reason.replace("_", " ").strip())


def _scope_tag(cluster: PatternCluster) -> str:
    """Build a parenthetical scope like '(all Petzl)' or '(ski boots)'."""
    parts = []
    if cluster.common_vendor:
        parts.append(f"all {cluster.common_vendor}")
    if cluster.common_type:
        parts.append(cluster.common_type)
    if parts:
        return f" ({', '.join(parts)})"
    return ""


def _diff_for_cluster(
    cluster: PatternCluster, diffs: list[ReplayDiff]
) -> ReplayDiff | None:
    """Find the ReplayDiff matching a cluster's proposal, if any."""
    if cluster.proposal is None:
        return None
    for d in diffs:
        if d.proposal.parameter == cluster.proposal.parameter:
            return d
    return None


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------

def format_terminal(
    clusters: list[PatternCluster],
    diffs: list[ReplayDiff],
    total: int,
    rejected: int,
    report_path: Path | None = None,
) -> str:
    """Render a concise terminal summary (max 5 patterns)."""
    lines: list[str] = []
    sep = "─"
    lines.append(f"── Feedback Analysis {sep * 34}")

    n_patterns = len(clusters)
    lines.append(
        f"{n_patterns} pattern{'s' if n_patterns != 1 else ''} detected "
        f"across {total} decisions ({rejected} rejected)"
    )
    lines.append("")

    shown = clusters[:MAX_TERMINAL_PATTERNS]
    for cluster in shown:
        label = _reason_label(cluster.failure_reason)
        scope = _scope_tag(cluster)
        lines.append(f"  {label}: {cluster.count} failures{scope}")

        diff = _diff_for_cluster(cluster, diffs)
        if diff is not None:
            rec = len(diff.recovered)
            reg = len(diff.regressed)
            p = diff.proposal
            lines.append(
                f"    → Lower {p.parameter} "
                f"{p.current_value} → {p.proposed_value}: "
                f"recovers {rec}, {reg} regressions"
            )
        elif not cluster.actionable:
            lines.append("    → No threshold fix — vendor site blocks scrapers"
                         if "bot_blocked" in cluster.failure_reason
                         else "    → No threshold fix")
        else:
            # Actionable but no diff yet (e.g., penalty stacking)
            if cluster.proposal:
                lines.append(f"    → {cluster.proposal.rationale}")
            else:
                lines.append(f"    → Needs manual investigation")

        lines.append("")

    if report_path is not None:
        lines.append(f"Full analysis: {report_path}")

    lines.append(sep * 52)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full markdown report
# ---------------------------------------------------------------------------

def format_report(
    clusters: list[PatternCluster],
    diffs: list[ReplayDiff],
    total: int,
    rejected: int,
) -> str:
    """Render a full markdown report."""
    accepted = total - rejected
    rate = accepted / max(total, 1) * 100
    sections: list[str] = []

    # Header
    sections.append("# Feedback Analysis Report\n")

    # Summary stats
    sections.append("## Summary\n")
    sections.append(f"| Metric | Value |")
    sections.append(f"|--------|-------|")
    sections.append(f"| Total decisions | {total} |")
    sections.append(f"| Accepted | {accepted} |")
    sections.append(f"| Rejected | {rejected} |")
    sections.append(f"| Approval rate | {rate:.1f}% |")
    sections.append("")

    # Actionable patterns
    actionable = [c for c in clusters if c.actionable]
    non_actionable = [c for c in clusters if not c.actionable]

    if actionable:
        sections.append("## Actionable Patterns\n")
        for cluster in actionable:
            sections.append(_format_cluster_md(cluster, diffs))

    # Non-actionable
    if non_actionable:
        sections.append("## No Action Needed\n")
        sections.append(
            "These patterns were detected but require no threshold changes.\n"
        )
        for cluster in non_actionable:
            sections.append(_format_cluster_md(cluster, diffs))

    return "\n".join(sections)


def _format_cluster_md(
    cluster: PatternCluster, diffs: list[ReplayDiff]
) -> str:
    """Format a single cluster as a markdown section."""
    lines: list[str] = []
    label = _reason_label(cluster.failure_reason)
    scope = _scope_tag(cluster)
    lines.append(f"### {label}: {cluster.count} failures{scope}\n")

    # Common attributes
    if cluster.common_vendor:
        lines.append(f"**Vendor:** {cluster.common_vendor}  ")
    if cluster.common_type:
        lines.append(f"**Product type:** {cluster.common_type}  ")

    # Affected products
    lines.append("\n**Affected products:**\n")
    for handle in cluster.affected_handles:
        lines.append(f"- `{handle}`")
    lines.append("")

    # Threshold boundary
    if cluster.threshold_boundary:
        lines.append("**Threshold boundary analysis:**\n")
        for key, val in cluster.threshold_boundary.items():
            lines.append(f"- {key}: {val}")
        lines.append("")

    # Proposal + replay diff
    diff = _diff_for_cluster(cluster, diffs)
    if cluster.proposal:
        p = cluster.proposal
        lines.append("**Proposed fix:**\n")
        lines.append(
            f"Change `{p.parameter}` from {p.current_value} to {p.proposed_value}  "
        )
        lines.append(f"Rationale: {p.rationale}\n")

        if diff:
            lines.append("**Replay results:**\n")
            lines.append(f"- Recovered: {len(diff.recovered)}")
            lines.append(f"- Regressed: {len(diff.regressed)}")
            lines.append(f"- Unchanged: {diff.unchanged}")
            lines.append("")

            if diff.recovered:
                lines.append("Recovered products:\n")
                for r in diff.recovered:
                    handle = r.get("handle", "unknown")
                    lines.append(f"- `{handle}`")
                lines.append("")

            if diff.regressed:
                lines.append("Regressed products:\n")
                for r in diff.regressed:
                    handle = r.get("handle", "unknown")
                    lines.append(f"- `{handle}`")
                lines.append("")
    elif not cluster.actionable:
        lines.append("**No threshold fix available.**\n")

    lines.append("---\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------

def write_report(report: str, run_dir: Path) -> Path:
    """Write the markdown report to the run directory."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "feedback_analysis.md"
    path.write_text(report)
    return path
