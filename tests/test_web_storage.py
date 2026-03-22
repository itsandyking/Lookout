"""Tests for web storage module."""

import json
import os
import tempfile
from pathlib import Path

import pytest

# Set test runs directory before importing storage
os.environ["MERCHRUNS_DIR"] = tempfile.mkdtemp()

from lookout.enrich_web.schemas import RunConfig, RunStatus
from lookout.enrich_web.storage import (
    append_event,
    cancel_run,
    create_run,
    generate_run_id,
    get_run_dir,
    get_runs_dir,
    list_runs,
    load_run_meta,
    read_events,
    read_run_report,
    save_run_meta,
    update_run_status,
)


@pytest.fixture
def sample_csv_content() -> bytes:
    """Sample CSV content for testing."""
    return b"""Product Handle,Vendor,Has Image,Has Variant Images,Has Description
test-product-1,TestVendor,false,false,false
test-product-2,TestVendor,true,false,true
"""


@pytest.fixture
def vendors_yaml(tmp_path: Path) -> Path:
    """Create a temporary vendors.yaml."""
    vendors_file = tmp_path / "vendors.yaml"
    vendors_file.write_text("""
vendors:
  TestVendor:
    domain: test.com
    use_playwright: false
""")
    return vendors_file


class TestRunIdGeneration:
    """Tests for run ID generation."""

    def test_generate_run_id_format(self):
        """Test that run IDs have correct format."""
        run_id = generate_run_id()

        assert run_id.startswith("run_")
        parts = run_id.split("_")
        assert len(parts) == 4  # run, date, time, shortid

    def test_generate_run_id_unique(self):
        """Test that run IDs are unique."""
        ids = [generate_run_id() for _ in range(100)]
        assert len(ids) == len(set(ids))


class TestRunCreation:
    """Tests for run creation."""

    def test_create_run(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test creating a new run."""
        config = RunConfig(concurrency=3, max_rows=10)

        meta = create_run(sample_csv_content, vendors_yaml, config)

        assert meta.run_id.startswith("run_")
        assert meta.status == RunStatus.QUEUED
        assert meta.config.concurrency == 3
        assert meta.config.max_rows == 10

        # Check files were created
        run_dir = get_run_dir(meta.run_id)
        assert (run_dir / "input.csv").exists()
        assert (run_dir / "meta.json").exists()
        assert (run_dir / "vendors.yaml").exists()
        assert (run_dir / "events.log").exists()
        assert (run_dir / "outputs").is_dir()
        assert (run_dir / "artifacts").is_dir()

    def test_create_run_default_config(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test creating a run with default config."""
        config = RunConfig()

        meta = create_run(sample_csv_content, vendors_yaml, config)

        assert meta.config.concurrency == 5
        assert meta.config.max_rows is None
        assert meta.config.force is False
        assert meta.config.dry_run is False


class TestRunMeta:
    """Tests for run metadata operations."""

    def test_save_and_load_meta(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test saving and loading run metadata."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        # Load it back
        loaded = load_run_meta(meta.run_id)

        assert loaded is not None
        assert loaded.run_id == meta.run_id
        assert loaded.status == meta.status
        assert loaded.config.concurrency == meta.config.concurrency

    def test_update_run_status(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test updating run status."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        update_run_status(meta.run_id, RunStatus.RUNNING)

        loaded = load_run_meta(meta.run_id)
        assert loaded.status == RunStatus.RUNNING

    def test_load_nonexistent_run(self):
        """Test loading a run that doesn't exist."""
        result = load_run_meta("nonexistent_run_id")
        assert result is None


class TestEvents:
    """Tests for event logging."""

    def test_append_and_read_events(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test appending and reading events."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        # Append events
        append_event(meta.run_id, {"type": "RUN_STARTED", "total": 10})
        append_event(meta.run_id, {"type": "ITEM_DONE", "handle": "test-1"})
        append_event(meta.run_id, {"type": "ITEM_DONE", "handle": "test-2"})

        # Read events
        events = read_events(meta.run_id)

        assert len(events) == 3
        assert events[0]["type"] == "RUN_STARTED"
        assert events[1]["handle"] == "test-1"

    def test_read_events_with_offset(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test reading events with offset."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        for i in range(5):
            append_event(meta.run_id, {"type": "ITEM_DONE", "index": i})

        events = read_events(meta.run_id, since_line=2)

        assert len(events) == 3
        assert events[0]["index"] == 2


class TestListRuns:
    """Tests for listing runs."""

    def test_list_runs(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test listing runs."""
        config = RunConfig()

        # Create multiple runs
        for _ in range(3):
            create_run(sample_csv_content, vendors_yaml, config)

        runs = list_runs(limit=10)

        assert len(runs) >= 3


class TestCancelRun:
    """Tests for run cancellation."""

    def test_cancel_queued_run(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test cancelling a queued run."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        result = cancel_run(meta.run_id)

        assert result is True

        loaded = load_run_meta(meta.run_id)
        assert loaded.status == RunStatus.CANCELED

        # Check cancel flag exists
        run_dir = get_run_dir(meta.run_id)
        assert (run_dir / "cancel.flag").exists()

    def test_cancel_done_run(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test cancelling a completed run (should fail)."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        # Mark as done
        update_run_status(meta.run_id, RunStatus.DONE)

        result = cancel_run(meta.run_id)

        assert result is False


class TestRunReport:
    """Tests for reading run reports."""

    def test_read_empty_report(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test reading report when none exists."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        results = read_run_report(meta.run_id)

        assert results == []

    def test_read_run_report(self, sample_csv_content: bytes, vendors_yaml: Path):
        """Test reading a run report CSV."""
        config = RunConfig()
        meta = create_run(sample_csv_content, vendors_yaml, config)

        # Create a fake run report
        run_dir = get_run_dir(meta.run_id)
        report_content = """handle,vendor,status,match_confidence,warnings,output_rows_count,error_message,processing_time_ms
test-1,TestVendor,UPDATED,85,,2,,150
test-2,TestVendor,FAILED,0,,0,Error occurred,50
"""
        (run_dir / "outputs" / "run_report.csv").write_text(report_content)

        results = read_run_report(meta.run_id)

        assert len(results) == 2
        assert results[0].handle == "test-1"
        assert results[0].status == "UPDATED"
        assert results[0].match_confidence == 85
        assert results[1].status == "FAILED"
        assert results[1].error_message == "Error occurred"
