"""
Web routes for the Merchfill Web UI.

Includes:
- Main pages (index, run detail)
- htmx partial endpoints
- File downloads
- CSV validation
"""

import csv
import io
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .job_queue import enqueue_run
from .schemas import (
    CreateRunRequest,
    ItemResult,
    RunConfig,
    RunStatus,
    ValidationError,
    ValidationResult,
)
from .storage import (
    cancel_run,
    create_artifacts_zip,
    create_run,
    get_output_file_path,
    list_runs,
    load_run_meta,
    read_item_artifacts,
    read_run_report,
    run_exists,
)

# Router
router = APIRouter()

# Templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Get vendors.yaml path
def get_vendors_yaml_path() -> Path:
    """Get the path to vendors.yaml from environment or default."""
    return Path(os.environ.get("VENDORS_YAML_PATH", "./vendors.yaml"))


# -----------------------------------------------------------------------------
# Main Pages
# -----------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """
    Home page showing new run form and recent runs.
    """
    recent_runs = list_runs(limit=20)
    vendors_exists = get_vendors_yaml_path().exists()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "recent_runs": recent_runs,
            "vendors_exists": vendors_exists,
        },
    )


@router.post("/runs")
async def create_new_run(
    request: Request,
    file: Annotated[UploadFile, File(description="Input CSV file")],
    concurrency: Annotated[int, Form()] = 5,
    max_rows: Annotated[int | None, Form()] = None,
    force: Annotated[bool, Form()] = False,
    dry_run: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    """
    Create a new pipeline run from uploaded CSV.
    """
    # Read file content
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # Quick validation
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []

        required = {"Product Handle", "Vendor", "Has Image", "Has Variant Images", "Has Description"}
        missing = required - set(headers)
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required columns: {', '.join(missing)}",
            )
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid CSV encoding. Please use UTF-8.")

    # Create run config
    config = RunConfig(
        concurrency=concurrency,
        max_rows=max_rows if max_rows and max_rows > 0 else None,
        force=force,
        dry_run=dry_run,
    )

    # Create run
    vendors_path = get_vendors_yaml_path()
    meta = create_run(content, vendors_path, config)

    # Enqueue job
    job_id = enqueue_run(meta.run_id)
    if not job_id:
        raise HTTPException(status_code=500, detail="Failed to enqueue job")

    # Redirect to run detail
    return RedirectResponse(url=f"/runs/{meta.run_id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str) -> HTMLResponse:
    """
    Run detail/dashboard page.
    """
    meta = load_run_meta(run_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Run not found")

    # Check if outputs exist
    has_shopify_csv = get_output_file_path(run_id, "shopify_update.csv") is not None
    has_variant_csv = get_output_file_path(run_id, "variant_image_assignments.csv") is not None
    has_report_csv = get_output_file_path(run_id, "run_report.csv") is not None

    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "run": meta,
            "has_shopify_csv": has_shopify_csv,
            "has_variant_csv": has_variant_csv,
            "has_report_csv": has_report_csv,
        },
    )


# -----------------------------------------------------------------------------
# htmx Partial Endpoints
# -----------------------------------------------------------------------------


@router.get("/runs/{run_id}/progress", response_class=HTMLResponse)
async def run_progress(request: Request, run_id: str) -> HTMLResponse:
    """
    Progress partial for htmx polling.
    """
    meta = load_run_meta(run_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Run not found")

    # Calculate progress percentage
    progress_pct = 0
    if meta.stats.total > 0:
        progress_pct = int((meta.stats.completed / meta.stats.total) * 100)

    return templates.TemplateResponse(
        "partials/run_progress.html",
        {
            "request": request,
            "run": meta,
            "progress_pct": progress_pct,
        },
    )


@router.get("/runs/{run_id}/results", response_class=HTMLResponse)
async def run_results(
    request: Request,
    run_id: str,
    status_filter: str | None = None,
    low_confidence: bool = False,
    has_warnings: bool = False,
) -> HTMLResponse:
    """
    Results table partial for htmx polling.
    """
    meta = load_run_meta(run_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Run not found")

    # Read results
    results = read_run_report(run_id)

    # Apply filters
    if status_filter:
        results = [r for r in results if r.status == status_filter]
    if low_confidence:
        results = [r for r in results if r.match_confidence < 85]
    if has_warnings:
        results = [r for r in results if r.warnings]

    return templates.TemplateResponse(
        "partials/results_table.html",
        {
            "request": request,
            "run_id": run_id,
            "results": results,
            "status_filter": status_filter,
            "low_confidence": low_confidence,
            "has_warnings": has_warnings,
            "run_status": meta.status,
        },
    )


@router.get("/runs/{run_id}/items/{handle}", response_class=HTMLResponse)
async def item_detail(request: Request, run_id: str, handle: str) -> HTMLResponse:
    """
    Item detail partial showing artifacts and preview.
    """
    if not run_exists(run_id):
        raise HTTPException(status_code=404, detail="Run not found")

    artifacts = read_item_artifacts(run_id, handle)
    if not artifacts:
        raise HTTPException(status_code=404, detail="Item artifacts not found")

    # Get result info from run report
    results = read_run_report(run_id)
    result = next((r for r in results if r.handle == handle), None)

    return templates.TemplateResponse(
        "partials/item_detail.html",
        {
            "request": request,
            "run_id": run_id,
            "handle": handle,
            "artifacts": artifacts,
            "result": result,
        },
    )


@router.get("/runs/{run_id}/run-row", response_class=HTMLResponse)
async def run_row(request: Request, run_id: str) -> HTMLResponse:
    """
    Single run row partial for htmx updates.
    """
    meta = load_run_meta(run_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Run not found")

    from .schemas import RunSummary

    summary = RunSummary(
        run_id=meta.run_id,
        created_at=meta.created_at,
        status=meta.status,
        total=meta.stats.total,
        completed=meta.stats.completed,
        updated=meta.stats.updated,
        failed=meta.stats.failed,
    )

    return templates.TemplateResponse(
        "partials/run_row.html",
        {
            "request": request,
            "run": summary,
        },
    )


# -----------------------------------------------------------------------------
# Actions
# -----------------------------------------------------------------------------


@router.post("/runs/{run_id}/cancel")
async def cancel_run_endpoint(run_id: str) -> dict:
    """
    Cancel a running job.
    """
    success = cancel_run(run_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel run")

    return {"success": True}


# -----------------------------------------------------------------------------
# Downloads
# -----------------------------------------------------------------------------


@router.get("/runs/{run_id}/download/shopify_update.csv")
async def download_shopify_csv(run_id: str) -> FileResponse:
    """Download the Shopify update CSV."""
    path = get_output_file_path(run_id, "shopify_update.csv")
    if not path:
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path,
        media_type="text/csv",
        filename=f"shopify_update_{run_id}.csv",
    )


@router.get("/runs/{run_id}/download/variant_image_assignments.csv")
async def download_variant_csv(run_id: str) -> FileResponse:
    """Download the variant image assignments CSV."""
    path = get_output_file_path(run_id, "variant_image_assignments.csv")
    if not path:
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path,
        media_type="text/csv",
        filename=f"variant_image_assignments_{run_id}.csv",
    )


@router.get("/runs/{run_id}/download/run_report.csv")
async def download_report_csv(run_id: str) -> FileResponse:
    """Download the run report CSV."""
    path = get_output_file_path(run_id, "run_report.csv")
    if not path:
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path,
        media_type="text/csv",
        filename=f"run_report_{run_id}.csv",
    )


@router.get("/runs/{run_id}/download/artifacts.zip")
async def download_artifacts_zip(run_id: str) -> FileResponse:
    """Download all artifacts as a zip file."""
    path = create_artifacts_zip(run_id)
    if not path:
        raise HTTPException(status_code=404, detail="No artifacts found")

    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"artifacts_{run_id}.zip",
    )


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


@router.post("/validate", response_class=HTMLResponse)
async def validate_csv(
    request: Request,
    file: Annotated[UploadFile, File(description="CSV file to validate")],
) -> HTMLResponse:
    """
    Validate a CSV file before upload.
    Returns an HTML partial with validation results.
    """
    content = await file.read()

    result = ValidationResult(valid=True)

    if not content:
        result.valid = False
        result.errors.append(ValidationError(message="Empty file"))
        return templates.TemplateResponse(
            "partials/validation_result.html",
            {"request": request, "result": result},
        )

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        result.valid = False
        result.errors.append(
            ValidationError(message="Invalid encoding. Please use UTF-8.")
        )
        return templates.TemplateResponse(
            "partials/validation_result.html",
            {"request": request, "result": result},
        )

    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []

        # Check required columns
        required = {"Product Handle", "Vendor", "Has Image", "Has Variant Images", "Has Description"}
        missing = required - set(headers)
        if missing:
            result.valid = False
            for col in missing:
                result.errors.append(
                    ValidationError(column=col, message=f"Missing required column: {col}")
                )

        # Count rows and check for issues
        row_count = 0
        vendors = set()

        for i, row in enumerate(reader, start=2):
            row_count += 1
            handle = row.get("Product Handle", "").strip()
            vendor = row.get("Vendor", "").strip()

            if not handle:
                result.valid = False
                result.errors.append(
                    ValidationError(row=i, column="Product Handle", message="Empty handle")
                )

            if vendor:
                vendors.add(vendor)

        result.row_count = row_count

        # Warn about unknown vendors
        if vendors:
            result.warnings.append(f"Found {len(vendors)} unique vendors: {', '.join(sorted(vendors))}")

    except csv.Error as e:
        result.valid = False
        result.errors.append(ValidationError(message=f"CSV parsing error: {e}"))

    return templates.TemplateResponse(
        "partials/validation_result.html",
        {"request": request, "result": result},
    )
