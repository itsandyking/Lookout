"""Content Audit routes — /audit/*"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.responses import Response

from tvr.web.app import get_result, store_result
from tvr.web.deps import get_store, get_templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
def audit_landing(request: Request) -> Response:
    templates = get_templates(request)
    store = get_store(request)
    vendors = store.list_vendors()

    return templates.TemplateResponse(
        request,
        "pages/audit/landing.html",
        {
            "active_page": "audit",
            "vendors": vendors,
        },
    )


@router.post("/run")
def audit_run(request: Request, vendor: str = Form("")) -> Response:
    from tvr.modules.content_auditor import ContentAuditor

    store = get_store(request)

    try:
        auditor = ContentAuditor(store)
        result = auditor.audit(vendor=vendor or None)
    except Exception as e:
        logger.exception("Error running content audit")
        templates = get_templates(request)
        return templates.TemplateResponse(
            request,
            "pages/audit/landing.html",
            {
                "active_page": "audit",
                "vendors": store.list_vendors(),
                "flash_message": f"Error running audit: {e}",
                "flash_type": "error",
            },
        )

    result_id = store_result(
        request.app.state,
        {
            "audit_result": result,
            "vendor": vendor or "",
        },
    )
    return RedirectResponse(f"/audit/{result_id}/results", status_code=303)


@router.get("/{result_id}/results")
def audit_results(request: Request, result_id: str) -> Response:
    templates = get_templates(request)
    data = get_result(request.app.state, result_id)
    if not data:
        return RedirectResponse("/audit/", status_code=303)

    result = data["audit_result"]
    summary = result.summary()
    priority = result.priority_items[:20]

    return templates.TemplateResponse(
        request,
        "pages/audit/results.html",
        {
            "active_page": "audit",
            "result_id": result_id,
            "summary": summary,
            "vendor": data.get("vendor", ""),
            "scores": result.scores,
            "priority": priority,
        },
    )


@router.get("/{result_id}/download")
def audit_download(request: Request, result_id: str) -> Response:
    data = get_result(request.app.state, result_id)
    if not data:
        return RedirectResponse("/audit/", status_code=303)

    result = data["audit_result"]
    csv_bytes = result.to_full_audit_csv()

    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="content_audit.csv"'},
    )
