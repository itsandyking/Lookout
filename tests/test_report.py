"""Tests for feedback analysis report formatting."""

from pathlib import Path

from lookout.feedback.analyzer import PatternCluster, ThresholdProposal
from lookout.feedback.replay import ReplayDiff
from lookout.feedback.report import (
    format_terminal,
    format_report,
    write_report,
    MAX_TERMINAL_PATTERNS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proposal(param="title_gate.word_overlap", cur=0.30, new=0.22):
    return ThresholdProposal(
        parameter=param,
        current_value=cur,
        proposed_value=new,
        rationale=f"5 failures cluster near {cur}",
    )


def _cluster(reason="reject_title_gate", count=5, vendor="Petzl",
             ptype=None, actionable=True, proposal=None, handles=None):
    return PatternCluster(
        failure_reason=reason,
        count=count,
        common_vendor=vendor,
        common_type=ptype,
        affected_handles=handles or [f"handle-{i}" for i in range(count)],
        threshold_boundary={"min": 0.25, "max": 0.29} if actionable else None,
        proposal=proposal,
        actionable=actionable,
    )


def _diff(proposal=None, recovered=3, regressed=0, unchanged=10):
    p = proposal or _proposal()
    return ReplayDiff(
        proposal=p,
        recovered=[{"handle": f"rec-{i}"} for i in range(recovered)],
        regressed=[{"handle": f"reg-{i}"} for i in range(regressed)],
        unchanged=unchanged,
    )


# ---------------------------------------------------------------------------
# Terminal summary tests
# ---------------------------------------------------------------------------

class TestFormatTerminal:
    def test_basic_structure(self):
        clusters = [_cluster(proposal=_proposal())]
        diffs = [_diff()]
        out = format_terminal(clusters, diffs, total=47, rejected=12)

        assert "Feedback Analysis" in out
        assert "1 pattern detected" in out
        assert "47 decisions" in out
        assert "12 rejected" in out
        # Separator lines at top and bottom
        assert out.startswith("──")
        assert out.rstrip().endswith("─" * 52)

    def test_max_five_patterns(self):
        clusters = [_cluster(reason=f"reason_{i}", count=i + 1, vendor=None)
                     for i in range(8)]
        out = format_terminal(clusters, [], total=100, rejected=30)

        assert "8 patterns detected" in out
        # Only first 5 should appear as detail lines (underscores become spaces)
        assert "reason 0" in out
        assert "reason 4" in out
        assert "reason 5" not in out

    def test_correct_counts(self):
        p = _proposal()
        clusters = [_cluster(count=5, proposal=p)]
        diffs = [_diff(proposal=p, recovered=4, regressed=0)]
        out = format_terminal(clusters, diffs, total=47, rejected=12)

        assert "5 failures" in out
        assert "recovers 4" in out
        assert "0 regressions" in out

    def test_non_actionable_bot_blocked(self):
        cluster = _cluster(
            reason="reject_bot_blocked", count=3, vendor="Arc'teryx",
            actionable=False, proposal=None,
        )
        out = format_terminal([cluster], [], total=20, rejected=5)

        assert "No threshold fix" in out
        assert "blocks scrapers" in out

    def test_non_actionable_generic(self):
        cluster = _cluster(
            reason="reject_demographic_mismatch", count=2, vendor=None,
            actionable=False, proposal=None,
        )
        out = format_terminal([cluster], [], total=20, rejected=5)

        assert "No threshold fix" in out
        # Should NOT mention scrapers for non-bot reasons
        assert "blocks scrapers" not in out

    def test_report_path_shown(self, tmp_path):
        rp = tmp_path / "feedback_analysis.md"
        out = format_terminal([], [], total=0, rejected=0, report_path=rp)
        assert str(rp) in out

    def test_scope_vendor_and_type(self):
        cluster = _cluster(vendor="Petzl", ptype="headlamps")
        out = format_terminal([cluster], [], total=10, rejected=3)
        assert "all Petzl" in out
        assert "headlamps" in out


# ---------------------------------------------------------------------------
# Full markdown report tests
# ---------------------------------------------------------------------------

class TestFormatReport:
    def test_has_all_sections(self):
        p = _proposal()
        clusters = [
            _cluster(proposal=p, actionable=True),
            _cluster(reason="reject_bot_blocked", count=3, vendor="Arc'teryx",
                     actionable=False),
        ]
        diffs = [_diff(proposal=p)]
        report = format_report(clusters, diffs, total=47, rejected=12)

        assert "# Feedback Analysis Report" in report
        assert "## Summary" in report
        assert "## Actionable Patterns" in report
        assert "## No Action Needed" in report

    def test_summary_stats(self):
        report = format_report([], [], total=100, rejected=25)

        assert "Total decisions | 100" in report
        assert "Accepted | 75" in report
        assert "Rejected | 25" in report
        assert "75.0%" in report

    def test_affected_products_listed(self):
        cluster = _cluster(handles=["cool-helmet", "warm-jacket"])
        report = format_report([cluster], [], total=10, rejected=2)

        assert "`cool-helmet`" in report
        assert "`warm-jacket`" in report

    def test_replay_diff_in_report(self):
        p = _proposal()
        clusters = [_cluster(proposal=p)]
        diffs = [_diff(proposal=p, recovered=4, regressed=1, unchanged=8)]
        report = format_report(clusters, diffs, total=20, rejected=5)

        assert "Recovered: 4" in report
        assert "Regressed: 1" in report
        assert "Unchanged: 8" in report

    def test_non_actionable_section(self):
        cluster = _cluster(
            reason="reject_bot_blocked", count=3, vendor="Arc'teryx",
            actionable=False,
        )
        report = format_report([cluster], [], total=20, rejected=5)

        assert "No Action Needed" in report
        assert "No threshold fix available" in report


# ---------------------------------------------------------------------------
# File output tests
# ---------------------------------------------------------------------------

class TestWriteReport:
    def test_creates_file(self, tmp_path):
        run_dir = tmp_path / "run-20260405"
        content = "# Test report\nSome content."
        path = write_report(content, run_dir)

        assert path.exists()
        assert path.name == "feedback_analysis.md"
        assert path.read_text() == content

    def test_creates_parent_dirs(self, tmp_path):
        run_dir = tmp_path / "deep" / "nested" / "run"
        path = write_report("content", run_dir)

        assert path.exists()
        assert path.parent == run_dir
