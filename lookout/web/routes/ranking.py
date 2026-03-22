"""Merchandiser routes — /merchandise/*"""

import csv
import io

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.responses import Response
from tvr.web.app import get_result, store_result
from tvr.web.deps import get_store, get_templates

router = APIRouter()


@router.get("/")
def merchandise_form(request: Request) -> Response:
    templates = get_templates(request)
    store = get_store(request)
    vendors = store.list_vendors()
    product_types = store.list_product_types()
    collections = store.list_collections()

    return templates.TemplateResponse(
        request,
        "pages/merchandiser/form.html",
        {
            "active_page": "merchandiser",
            "vendors": vendors,
            "product_types": product_types,
            "collections": collections,
        },
    )


@router.get("/results")
def merchandise_results(
    request: Request,
    collection: str = "",
    vendor: str = "",
    product_type: str = "",
    limit: int = 50,
) -> Response:
    from lookout.ranking.collection_ranker import Merchandiser

    templates = get_templates(request)
    store = get_store(request)
    merch = Merchandiser(store)

    result = merch.rank_collection(
        collection_handle=collection or None,
        vendor=vendor or None,
        product_type=product_type or None,
        limit=limit,
    )

    result_id = store_result(request.app.state, {"rankings": result})

    return templates.TemplateResponse(
        request,
        "pages/merchandiser/rankings.html",
        {
            "active_page": "merchandiser",
            "result": result,
            "result_id": result_id,
            "products": result.ranked,
        },
    )


@router.post("/{result_id}/override")
def merchandise_override(
    request: Request, result_id: str, handle: str = Form(...), action: str = Form(...)
) -> Response:
    """Apply pin/boost/bury override and re-rank."""
    from lookout.ranking.collection_ranker import Merchandiser

    templates = get_templates(request)
    data = get_result(request.app.state, result_id)
    if not data:
        return RedirectResponse("/merchandise/", status_code=303)

    result = data["rankings"]
    overrides = {}
    for p in result.ranked:
        if p.pinned_position:
            overrides[p.handle] = {"pin": p.pinned_position}
        if p.boost:
            overrides.setdefault(p.handle, {})["boost"] = p.boost
        if p.buried:
            overrides.setdefault(p.handle, {})["bury"] = True

    # Apply the new override
    if action == "pin":
        overrides.setdefault(handle, {})["pin"] = 1
    elif action == "boost":
        overrides.setdefault(handle, {})["boost"] = 0.3
    elif action == "bury":
        overrides.setdefault(handle, {})["bury"] = True
    elif action == "clear":
        overrides.pop(handle, None)

    store = get_store(request)
    merch = Merchandiser(store)
    new_result = merch.rank_collection(
        collection_handle=result.collection_name
        if result.collection_name != "All Products"
        else None,
        overrides=overrides or None,
        limit=len(result.products),
    )

    # Update stored result
    data["rankings"] = new_result

    return templates.TemplateResponse(
        request,
        "pages/merchandiser/_rankings_table.html",
        {
            "result_id": result_id,
            "products": new_result.ranked,
        },
    )


@router.get("/{result_id}/download")
def merchandise_download(request: Request, result_id: str) -> Response:
    data = get_result(request.app.state, result_id)
    if not data:
        return RedirectResponse("/merchandise/", status_code=303)

    result = data["rankings"]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Rank",
            "Handle",
            "Title",
            "Vendor",
            "Score",
            "Velocity",
            "Margin",
            "Inventory Health",
            "Weekly Units",
            "Margin %",
            "Inventory",
            "WOS",
        ]
    )

    for p in result.ranked:
        writer.writerow(
            [
                p.rank,
                p.handle,
                p.title,
                p.vendor,
                f"{p.total_score:.3f}",
                f"{p.velocity_score:.3f}",
                f"{p.margin_score:.3f}",
                f"{p.inventory_health_score:.3f}",
                f"{p.weekly_units:.1f}",
                f"{p.margin_pct:.1f}",
                p.total_inventory,
                f"{p.weeks_of_supply:.1f}",
            ]
        )

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="rankings_{result.collection_name}.csv"'
        },
    )
