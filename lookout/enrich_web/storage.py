"""
Run storage management for the web UI.

Handles:
- Creating run directories
- Reading/writing meta.json
- Reading run reports and artifacts
- Listing runs
"""

import csv
import json
import os
import secrets
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Generator

from .schemas import (
    ItemArtifacts,
    ItemResult,
    RunConfig,
    RunMeta,
    RunStats,
    RunStatus,
    RunSummary,
)

# Default runs directory
DEFAULT_RUNS_DIR = Path("./runs")


def get_runs_dir() -> Path:
    """Get the runs directory from environment or default."""
    runs_dir = Path(os.environ.get("MERCHRUNS_DIR", DEFAULT_RUNS_DIR))
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir


def generate_run_id() -> str:
    """Generate a unique run ID."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    short_id = secrets.token_hex(3)
    return f"run_{timestamp}_{short_id}"


def get_run_dir(run_id: str) -> Path:
    """Get the directory path for a run."""
    return get_runs_dir() / run_id


def create_run(
    input_csv_content: bytes,
    vendors_yaml_path: Path,
    config: RunConfig,
) -> RunMeta:
    """
    Create a new run directory with initial files.

    Args:
        input_csv_content: Content of the uploaded CSV file.
        vendors_yaml_path: Path to vendors.yaml to snapshot.
        config: Run configuration.

    Returns:
        RunMeta for the new run.
    """
    run_id = generate_run_id()
    run_dir = get_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (run_dir / "outputs").mkdir(exist_ok=True)
    (run_dir / "artifacts").mkdir(exist_ok=True)

    # Save input CSV
    input_path = run_dir / "input.csv"
    input_path.write_bytes(input_csv_content)

    # Snapshot vendors.yaml
    if vendors_yaml_path.exists():
        shutil.copy(vendors_yaml_path, run_dir / "vendors.yaml")

    # Create meta.json
    meta = RunMeta(
        run_id=run_id,
        created_at=datetime.utcnow(),
        status=RunStatus.QUEUED,
        config=config,
        stats=RunStats(),
    )
    save_run_meta(run_id, meta)

    # Create empty events.log
    (run_dir / "events.log").touch()

    return meta


def save_run_meta(run_id: str, meta: RunMeta) -> None:
    """Save run metadata to meta.json."""
    run_dir = get_run_dir(run_id)
    meta_path = run_dir / "meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta.model_dump(mode="json"), f, indent=2, default=str)


def load_run_meta(run_id: str) -> RunMeta | None:
    """Load run metadata from meta.json."""
    run_dir = get_run_dir(run_id)
    meta_path = run_dir / "meta.json"

    if not meta_path.exists():
        return None

    try:
        with open(meta_path) as f:
            data = json.load(f)
        return RunMeta.model_validate(data)
    except Exception:
        return None


def update_run_status(run_id: str, status: RunStatus, error_message: str | None = None) -> None:
    """Update the status of a run."""
    meta = load_run_meta(run_id)
    if meta:
        meta.status = status
        if error_message:
            meta.error_message = error_message
        save_run_meta(run_id, meta)


def update_run_stats(run_id: str, stats: RunStats) -> None:
    """Update the stats of a run."""
    meta = load_run_meta(run_id)
    if meta:
        meta.stats = stats
        meta.last_event_at = datetime.utcnow()
        save_run_meta(run_id, meta)


def append_event(run_id: str, event: dict) -> None:
    """Append an event to the events.log file."""
    run_dir = get_run_dir(run_id)
    events_path = run_dir / "events.log"

    with open(events_path, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def read_events(run_id: str, since_line: int = 0) -> list[dict]:
    """Read events from events.log, optionally starting from a line number."""
    run_dir = get_run_dir(run_id)
    events_path = run_dir / "events.log"

    if not events_path.exists():
        return []

    events = []
    with open(events_path) as f:
        for i, line in enumerate(f):
            if i >= since_line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    return events


def list_runs(limit: int = 20) -> list[RunSummary]:
    """List recent runs, sorted by creation time descending."""
    runs_dir = get_runs_dir()
    summaries = []

    # Get all run directories
    run_dirs = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith("run_")],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )[:limit]

    for run_dir in run_dirs:
        meta = load_run_meta(run_dir.name)
        if meta:
            summaries.append(
                RunSummary(
                    run_id=meta.run_id,
                    created_at=meta.created_at,
                    status=meta.status,
                    total=meta.stats.total,
                    completed=meta.stats.completed,
                    updated=meta.stats.updated,
                    failed=meta.stats.failed,
                )
            )

    return summaries


def run_exists(run_id: str) -> bool:
    """Check if a run exists."""
    return get_run_dir(run_id).exists()


def read_run_report(run_id: str) -> list[ItemResult]:
    """Read the run report CSV as a list of ItemResult."""
    run_dir = get_run_dir(run_id)
    report_path = run_dir / "outputs" / "run_report.csv"

    if not report_path.exists():
        return []

    results = []
    with open(report_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                results.append(
                    ItemResult(
                        handle=row.get("handle", ""),
                        vendor=row.get("vendor", ""),
                        status=row.get("status", ""),
                        match_confidence=int(row.get("match_confidence", 0) or 0),
                        warnings=row.get("warnings", ""),
                        output_rows_count=int(row.get("output_rows_count", 0) or 0),
                        error_message=row.get("error_message", ""),
                        processing_time_ms=int(row.get("processing_time_ms", 0) or 0),
                    )
                )
            except (ValueError, KeyError):
                continue

    return results


def read_item_artifacts(run_id: str, handle: str) -> ItemArtifacts | None:
    """Read artifact JSONs for a specific item."""
    run_dir = get_run_dir(run_id)
    # Sanitize handle for directory name (same logic as pipeline)
    import re
    safe_handle = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", handle)
    safe_handle = re.sub(r"_+", "_", safe_handle).strip("_.")

    artifacts_dir = run_dir / "artifacts" / safe_handle

    if not artifacts_dir.exists():
        return None

    artifacts = ItemArtifacts(handle=handle)

    # Load resolver.json
    resolver_path = artifacts_dir / "resolver.json"
    if resolver_path.exists():
        try:
            with open(resolver_path) as f:
                artifacts.resolver = json.load(f)
        except json.JSONDecodeError:
            pass

    # Load extracted_facts.json
    facts_path = artifacts_dir / "extracted_facts.json"
    if facts_path.exists():
        try:
            with open(facts_path) as f:
                artifacts.extracted_facts = json.load(f)
        except json.JSONDecodeError:
            pass

    # Load merch_output.json
    merch_path = artifacts_dir / "merch_output.json"
    if merch_path.exists():
        try:
            with open(merch_path) as f:
                artifacts.merch_output = json.load(f)
        except json.JSONDecodeError:
            pass

    # Load log.json
    log_path = artifacts_dir / "log.json"
    if log_path.exists():
        try:
            with open(log_path) as f:
                artifacts.log = json.load(f)
        except json.JSONDecodeError:
            pass

    # Check for source.html
    artifacts.has_source_html = (artifacts_dir / "source.html").exists()

    return artifacts


def get_output_file_path(run_id: str, filename: str) -> Path | None:
    """Get the path to an output file if it exists."""
    run_dir = get_run_dir(run_id)
    file_path = run_dir / "outputs" / filename

    if file_path.exists():
        return file_path

    return None


def create_artifacts_zip(run_id: str) -> Path | None:
    """Create a zip file of all artifacts for a run."""
    run_dir = get_run_dir(run_id)
    artifacts_dir = run_dir / "artifacts"
    zip_path = run_dir / "outputs" / "artifacts.zip"

    if not artifacts_dir.exists():
        return None

    # Create zip file
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(artifacts_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(artifacts_dir)
                zf.write(file_path, arcname)

    return zip_path


def cancel_run(run_id: str) -> bool:
    """
    Request cancellation of a run by creating cancel.flag.

    Returns True if the flag was created, False if run doesn't exist
    or is not in a cancellable state.
    """
    meta = load_run_meta(run_id)
    if not meta:
        return False

    if meta.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
        return False

    run_dir = get_run_dir(run_id)
    cancel_flag = run_dir / "cancel.flag"
    cancel_flag.touch()

    # Update status
    meta.status = RunStatus.CANCELED
    save_run_meta(run_id, meta)

    return True


def get_cancel_flag_path(run_id: str) -> Path:
    """Get the path to the cancel flag for a run."""
    return get_run_dir(run_id) / "cancel.flag"


def get_input_csv_path(run_id: str) -> Path | None:
    """Get the path to the input CSV for a run."""
    path = get_run_dir(run_id) / "input.csv"
    return path if path.exists() else None


def get_vendors_yaml_path(run_id: str) -> Path | None:
    """Get the path to the vendors.yaml snapshot for a run."""
    path = get_run_dir(run_id) / "vendors.yaml"
    return path if path.exists() else None


def get_outputs_dir(run_id: str) -> Path:
    """Get the outputs directory for a run."""
    return get_run_dir(run_id) / "outputs"
