"""Generate HTML review reports for enrichment runs."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path

from lookout.apply.models import ApplyRun

logger = logging.getLogger(__name__)


def _clean_description(raw: str) -> str:
    """Clean scraper junk from descriptions for display."""
    # Remove rating/review noise like "4.5(35)35 total reviews"
    text = re.sub(r'\d+\.\d+\(\d+\)\d+\s*total reviews?', '', raw)
    # Remove standalone rating patterns
    text = re.sub(r'\b\d+\.\d+\s*out of \d+\s*stars?\b', '', text)
    return text.strip()


def generate_review_report(run: ApplyRun, output_path: Path) -> None:
    """Generate an HTML review report with rendered descriptions, variant info, and images."""
    products_html = []
    for change in run.changes:
        has_description = bool(change.new_body_html and change.new_body_html.strip())
        has_images = bool(change.new_images)

        if not has_description and not has_images:
            continue

        # Render HTML descriptions (don't escape — show the actual formatted output)
        current_desc = _clean_description(change.current_body_html or "")
        proposed_desc = _clean_description(change.new_body_html or "")

        if has_description:
            current_display = current_desc if current_desc else '<span class="empty-tag">No current description</span>'
            desc_html = _DESC_TEMPLATE.format(
                current=current_display,
                proposed=proposed_desc,
            )
        else:
            desc_html = '<div class="no-change">No description changes proposed</div>'

        # Build image data and variant-to-image mapping
        vim = change.new_variant_image_map or {}
        img_by_src = {}
        if has_images:
            for img in change.new_images[:12]:
                img_by_src[img.get("src", "")] = img.get("position", "")

        # Build variant → image positions mapping for interactive highlighting
        variant_img_positions = {}  # variant_label → [position, ...]
        for variant_key, img_src in vim.items():
            srcs = img_src if isinstance(img_src, list) else [img_src]
            positions = [str(img_by_src.get(s, "")) for s in srcs if img_by_src.get(s)]
            if variant_key == "__all__":
                # All images go to all variants
                positions = [str(img_by_src.get(s, "")) for s in img_by_src]
            variant_img_positions[variant_key] = positions

        # Build src → image data lookup
        img_data_by_src = {}
        if has_images:
            for img in change.new_images:
                img_data_by_src[img.get("src", "")] = img

        # Variant assignment table: Color → thumbnail
        if change.variant_labels and (has_images or vim):
            # Deduplicate variant labels by color (strip size info)
            seen_colors = {}
            for v in change.variant_labels:
                color = v.split(" / ")[0] if " / " in v else v
                if color not in seen_colors:
                    seen_colors[color] = v

            assignment_rows = []
            for color, full_label in seen_colors.items():
                # Find assigned image src(s) for this variant
                assigned_srcs = []
                if color in vim:
                    raw = vim[color]
                    assigned_srcs = raw if isinstance(raw, list) else [raw]
                elif full_label in vim:
                    raw = vim[full_label]
                    assigned_srcs = raw if isinstance(raw, list) else [raw]
                elif "__all__" in vim:
                    raw = vim["__all__"]
                    assigned_srcs = raw if isinstance(raw, list) else [raw]

                if assigned_srcs:
                    thumb_cells = []
                    for src in assigned_srcs[:3]:
                        # Show the image directly from the assigned URL
                        # (may not be in the main image list)
                        pos = img_by_src.get(src, "")
                        pos_label = f'<span class="assign-pos">#{pos}</span>' if pos else ""
                        thumb_cells.append(
                            f'<div class="assign-thumb">'
                            f'<img src="{src}" loading="lazy" />'
                            f'{pos_label}</div>'
                        )
                    remaining_count = len(assigned_srcs) - 3
                    if remaining_count > 0:
                        thumb_cells.append(f'<span class="assign-more">+{remaining_count}</span>')
                    img_cell = "".join(thumb_cells)
                    is_all = "__all__" in vim and color not in vim and full_label not in vim
                    if is_all:
                        img_cell += '<span class="assign-shared">shared</span>'
                else:
                    img_cell = '<span class="no-assign">No image assigned</span>'

                positions = variant_img_positions.get(color, variant_img_positions.get(full_label, variant_img_positions.get("__all__", [])))
                pos_data = html.escape(json.dumps(positions))

                assignment_rows.append(
                    f'<tr class="assign-row" onclick="highlightVariantImages(this)" data-images=\'{pos_data}\'>'
                    f'<td class="assign-label">{html.escape(color)}</td>'
                    f'<td class="assign-images">{img_cell}</td></tr>'
                )

            variants_html = (
                f'<div class="section" data-section-type="variant_images">'
                f'<div class="section-head">'
                f'<h4 class="section-label">Variant Image Assignments</h4>'
                f'<div class="section-action" data-section="variant_images">'
                f'<button type="button" class="sbtn sbtn-approve" onclick="setSectionDisposition(this, \'approved\')">&#10003;</button>'
                f'<button type="button" class="sbtn sbtn-reject" onclick="setSectionDisposition(this, \'rejected\')">&#10007;</button>'
                f'</div></div>'
                f'<table class="assign-table">{"".join(assignment_rows)}</table>'
                f'<div class="section-reasons" style="display:none">'
                f'<button type="button" class="pill" data-reason="wrong_image_match">Wrong variant match</button>'
                f'<button type="button" class="pill" data-reason="missing_variant_image">Missing variant image</button>'
                f'</div>'
                f'</div>'
            )
        elif change.variant_labels:
            pills = " ".join(
                f'<span class="variant-pill">{html.escape(v)}</span>'
                for v in change.variant_labels
            )
            variants_html = f'<div class="section"><h4 class="section-label">Variants ({len(change.variant_labels)})</h4><div class="variant-pills">{pills}</div></div>'
        else:
            variants_html = ""

        # Images section (full grid)
        if has_images:
            thumbs = []
            for img in change.new_images[:12]:
                src = img.get("src", "")
                alt_text = html.escape(img.get("alt", ""))
                pos = img.get("position", "")
                thumbs.append(
                    f'<div class="thumb" data-pos="{pos}">'
                    f'<img src="{src}" alt="{alt_text}" loading="lazy" />'
                    f'<span class="pos">#{pos}</span></div>'
                )
            remaining = len(change.new_images) - 12
            extra = f'<div class="thumb more">+{remaining} more</div>' if remaining > 0 else ""
            current_img_count = len(change.current_images) if change.current_images else 0
            proposed_count = len(change.new_images)

            if current_img_count == 0:
                change_summary = f"adding {proposed_count} images (none currently)"
            else:
                # Check for overlapping URLs
                current_srcs = {img.get("src", "") for img in (change.current_images or [])}
                new_srcs = {img.get("src", "") for img in change.new_images}
                kept = len(current_srcs & new_srcs)
                added = len(new_srcs - current_srcs)
                removed = len(current_srcs - new_srcs)
                parts = []
                if kept:
                    parts.append(f"{kept} kept")
                if added:
                    parts.append(f"{added} added")
                if removed:
                    parts.append(f"{removed} replaced")
                change_summary = ", ".join(parts) if parts else f"{current_img_count} → {proposed_count}"

            images_html = _IMAGES_TEMPLATE.format(
                change_summary=change_summary,
                thumbnails="\n".join(thumbs) + extra,
            )
        else:
            images_html = ""

        conf = change.confidence
        conf_class = "conf-high" if conf >= 80 else ("conf-med" if conf >= 60 else "conf-low")

        # Inventory and payoff info
        inv_parts = []
        if change.inventory_count > 0:
            inv_parts.append(f'{change.inventory_count} units')
        if change.inventory_value > 0:
            inv_parts.append(f'${change.inventory_value:,.0f} on hand')
        inventory_html = f'<span class="inv-badge">{" / ".join(inv_parts)}</span>' if inv_parts else ""

        # Missing fields flags
        flag_labels = {
            "product_type": "No product type",
            "tags": "No tags",
        }
        flags = [f'<span class="flag-pill">{flag_labels.get(f, f)}</span>' for f in change.missing_fields]
        flags_html = " ".join(flags)

        products_html.append(
            _PRODUCT_TEMPLATE.format(
                handle=change.handle,
                title=html.escape(change.title),
                vendor=html.escape(change.vendor),
                confidence=conf,
                conf_class=conf_class,
                product_id=change.product_id,
                inventory_info=inventory_html,
                missing_flags=flags_html,
                variants_section=variants_html,
                description_section=desc_html,
                images_section=images_html,
                has_description="true" if has_description else "false",
                has_images="true" if has_images else "false",
            )
        )

    output = _TEMPLATE.format(
        run_id=run.run_id,
        product_count=len(products_html),
        total_count=len(run.changes),
        skipped_count=len(run.changes) - len(products_html),
        products="\n".join(products_html),
        dispositions_filename=f"{run.run_id}_dispositions.json",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output)
    logger.info("Review report written to %s", output_path)


_PRODUCT_TEMPLATE = """
<div class="product" data-handle="{handle}" data-product-id="{product_id}"
     data-has-description="{has_description}" data-has-images="{has_images}">
  <div class="product-header">
    <div class="header-top">
      <h3>{title}</h3>
      <span class="badge {conf_class}">{confidence}%</span>
    </div>
    <div class="header-meta">
      <span class="vendor">{vendor}</span>
      {inventory_info}
      <span class="handle">{handle}</span>
    </div>
    <div class="header-flags">{missing_flags}</div>
  </div>

  {images_section}
  {variants_section}
  {description_section}

  <div class="actions">
    <div class="bulk-actions">
      <button type="button" class="btn btn-approve-all" onclick="approveAll(this)">Approve All Sections</button>
      <button type="button" class="btn btn-reject-all" onclick="rejectAll(this)">Reject All Sections</button>
    </div>
    <div class="action-buttons">
      <button type="button" class="btn btn-skip" onclick="setDisposition(this, 'skip')">Skip</button>
    </div>
  </div>
</div>
"""

_DESC_TEMPLATE = """
<div class="section" data-section-type="description">
  <div class="section-head">
    <h4 class="section-label">Description</h4>
    <div class="section-action" data-section="description">
      <button type="button" class="sbtn sbtn-approve" onclick="setSectionDisposition(this, 'approved')">&#10003;</button>
      <button type="button" class="sbtn sbtn-reject" onclick="setSectionDisposition(this, 'rejected')">&#10007;</button>
    </div>
  </div>
  <div class="comparison">
    <div class="side current">
      <div class="side-label">Current</div>
      <div class="content rendered-html">{current}</div>
    </div>
    <div class="side proposed">
      <div class="side-label">Proposed</div>
      <div class="content rendered-html selectable-text">{proposed}</div>
    </div>
  </div>
  <div class="section-reasons" style="display:none">
    <button type="button" class="pill" data-reason="hallucinated">Hallucinated</button>
    <button type="button" class="pill" data-reason="bad_source_data">Bad source data</button>
    <button type="button" class="pill" data-reason="stale_source">Stale/outdated</button>
    <button type="button" class="pill" data-reason="bad_structure">Bad structure</button>
    <button type="button" class="pill" data-reason="incomplete">Incomplete</button>
    <button type="button" class="pill" data-reason="tone">Wrong tone</button>
    <button type="button" class="pill" data-reason="typos">Typos/grammar</button>
  </div>
  <div class="highlights-list" style="display:none">
    <div class="highlights-label">Highlighted issues:</div>
    <div class="highlights-items"></div>
  </div>
</div>
"""

_IMAGES_TEMPLATE = """
<div class="section" data-section-type="images">
  <div class="section-head">
    <h4 class="section-label">Images <span class="img-count">{change_summary}</span></h4>
    <div class="section-action" data-section="images">
      <button type="button" class="sbtn sbtn-approve" onclick="setSectionDisposition(this, 'approved')">&#10003;</button>
      <button type="button" class="sbtn sbtn-reject" onclick="setSectionDisposition(this, 'rejected')">&#10007;</button>
    </div>
  </div>
  <div class="image-grid">
    {thumbnails}
  </div>
  <div class="section-reasons" style="display:none">
    <button type="button" class="pill" data-reason="wrong_image_match">Wrong image match</button>
    <button type="button" class="pill" data-reason="missing_image">Missing images</button>
    <button type="button" class="pill" data-reason="bad_image_quality">Bad image quality</button>
  </div>
</div>
"""

_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review: {run_id}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, system-ui, sans-serif;
    margin: 0; padding: 16px;
    background: #fafafa;
    color: #1a1a1a;
    -webkit-text-size-adjust: 100%;
  }}

  h1 {{ font-size: 1.3em; margin: 0 0 12px 0; }}

  .summary {{
    background: #fff; padding: 12px 16px; border-radius: 8px;
    margin-bottom: 16px; border: 1px solid #e0e0e0;
    font-size: 0.9em;
  }}
  .stat {{ display: inline-block; margin-right: 16px; }}
  .stat-val {{ font-weight: 600; }}
  .progress-bar {{
    height: 4px; background: #e0e0e0; border-radius: 2px;
    margin-top: 8px; overflow: hidden;
  }}
  .progress-fill {{ height: 100%; background: #4CAF50; transition: width 0.3s; }}

  .product {{
    background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
    margin-bottom: 16px; overflow: hidden;
    transition: border-color 0.2s;
  }}
  .product.reviewed-approved {{ border-color: #4CAF50; border-width: 2px; }}
  .product.reviewed-rejected {{ border-color: #f44336; border-width: 2px; }}
  .product.reviewed-skip {{ border-color: #9e9e9e; opacity: 0.6; }}

  .product-header {{ padding: 12px 16px 8px; }}
  .header-top {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
  .header-top h3 {{ margin: 0; font-size: 1em; flex: 1; }}
  .header-meta {{ display: flex; gap: 8px; margin-top: 4px; font-size: 0.8em; }}
  .vendor {{ color: #666; }}
  .handle {{ color: #999; font-family: monospace; }}
  .inv-badge {{
    font-size: 0.8em; color: #2e7d32; font-weight: 600;
    background: #e8f5e9; padding: 1px 6px; border-radius: 4px;
  }}
  .header-flags {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }}
  .header-flags:empty {{ display: none; }}
  .flag-pill {{
    font-size: 0.7em; padding: 2px 8px; border-radius: 10px;
    background: #fff3e0; color: #e65100; border: 1px solid #ffcc80;
  }}

  .badge {{
    font-size: 0.75em; font-weight: 600; padding: 2px 8px;
    border-radius: 10px; white-space: nowrap;
  }}
  .conf-high {{ background: #e8f5e9; color: #2e7d32; }}
  .conf-med {{ background: #fff3e0; color: #e65100; }}
  .conf-low {{ background: #ffebee; color: #c62828; }}

  .section {{ padding: 0 16px 12px; }}
  .section-label {{
    font-size: 0.8em; font-weight: 600; color: #666;
    margin: 0 0 8px 0; text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .img-count {{ font-weight: 400; text-transform: none; letter-spacing: 0; }}

  /* Variant pills */
  .variant-pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .variant-pill {{
    font-size: 0.75em; padding: 4px 10px; border-radius: 14px;
    background: #e3f2fd; color: #1565c0; border: 1px solid #bbdefb;
    cursor: pointer; transition: all 0.15s;
    -webkit-tap-highlight-color: transparent;
    display: inline-flex; align-items: center; gap: 4px;
  }}
  .variant-pill:active {{ transform: scale(0.95); }}
  .variant-pill.active {{ background: #1565c0; color: #fff; border-color: #0d47a1; }}
  .pill-img-count {{
    background: rgba(0,0,0,0.1); padding: 0 5px; border-radius: 8px;
    font-size: 0.9em;
  }}
  .variant-pill.active .pill-img-count {{ background: rgba(255,255,255,0.25); }}

  /* Variant assignment table */
  .assign-table {{
    width: 100%; border-collapse: collapse;
    border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden;
  }}
  .assign-row {{
    cursor: pointer; transition: background 0.15s;
    -webkit-tap-highlight-color: transparent;
  }}
  .assign-row:active {{ background: #e3f2fd; }}
  .assign-row.active {{ background: #e3f2fd; }}
  .assign-row + .assign-row {{ border-top: 1px solid #eee; }}
  .assign-label {{
    padding: 8px 12px; font-weight: 600; font-size: 0.85em;
    color: #333; white-space: nowrap; vertical-align: middle;
    width: 1%; /* shrink to content */
  }}
  .assign-images {{
    padding: 6px 8px; display: flex; gap: 6px;
    align-items: center; flex-wrap: wrap;
  }}
  .assign-thumb {{
    position: relative; width: 48px; height: 48px;
    border-radius: 4px; overflow: hidden; border: 1px solid #ddd;
    flex-shrink: 0;
  }}
  .assign-thumb img {{
    width: 100%; height: 100%; object-fit: cover; display: block;
  }}
  .assign-pos {{
    position: absolute; bottom: 1px; right: 2px;
    font-size: 0.6em; color: #fff; background: rgba(0,0,0,0.5);
    padding: 0 3px; border-radius: 2px;
  }}
  .assign-more {{
    font-size: 0.8em; color: #666; font-weight: 600;
  }}
  .no-assign {{
    font-size: 0.8em; color: #999; font-style: italic;
  }}
  .assign-shared {{
    font-size: 0.65em; color: #999; font-style: italic;
    margin-left: 4px;
  }}

  /* Description comparison */
  .comparison {{ display: flex; flex-direction: column; gap: 8px; }}
  .side {{
    border: 1px solid #eee; border-radius: 6px; padding: 10px;
    font-size: 0.85em; line-height: 1.5;
  }}
  .side-label {{
    font-size: 0.7em; font-weight: 600; text-transform: uppercase;
    color: #999; margin-bottom: 4px;
  }}
  .current {{ background: #fafafa; }}
  .proposed {{ background: #f0faf0; }}

  /* Render HTML descriptions properly */
  .rendered-html {{ word-break: break-word; }}
  .rendered-html ul, .rendered-html ol {{ padding-left: 20px; margin: 4px 0; }}
  .rendered-html li {{ margin: 2px 0; }}
  .rendered-html p {{ margin: 4px 0; }}
  .rendered-html h1, .rendered-html h2, .rendered-html h3, .rendered-html h4 {{
    margin: 8px 0 4px; font-size: 1em;
  }}
  .empty-tag {{ color: #999; font-style: italic; }}
  .no-change {{ padding: 0 16px 12px; color: #999; font-style: italic; font-size: 0.85em; }}

  /* Text selection highlighting */
  .selectable-text ::selection {{ background: #ffcdd2; }}
  .highlight-mark {{
    background: #ffcdd2; border-radius: 2px; padding: 0 1px;
    cursor: pointer;
  }}

  /* Image grid */
  .image-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
    gap: 8px;
  }}
  .thumb {{
    position: relative; aspect-ratio: 1; border-radius: 6px;
    overflow: hidden; border: 1px solid #eee; background: #f5f5f5;
  }}
  .thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .thumb.dim {{ opacity: 0.25; }}
  .thumb.highlighted {{ border: 2px solid #1565c0; box-shadow: 0 0 0 2px rgba(21,101,192,0.3); }}
  .thumb .pos {{
    position: absolute; bottom: 2px; right: 4px;
    font-size: 0.65em; color: #fff; background: rgba(0,0,0,0.5);
    padding: 1px 4px; border-radius: 3px;
  }}
  .thumb.more {{
    display: flex; align-items: center; justify-content: center;
    font-size: 0.85em; color: #666; font-weight: 600;
  }}

  /* No variant — all images indicator */
  .all-images-note {{
    font-size: 0.75em; color: #999; font-style: italic;
    margin-top: 4px;
  }}

  /* Actions */
  .actions {{ padding: 8px 16px 12px; }}

  /* Section header with inline approve/reject */
  .section-head {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 8px;
  }}
  .section-head .section-label {{ margin: 0; }}
  .section-action {{
    display: flex; align-items: center; gap: 4px;
  }}
  .sbtn {{
    width: 32px; height: 32px; border: 1px solid #ddd; border-radius: 4px;
    background: #fff; font-size: 1em; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.15s; color: #999;
    -webkit-tap-highlight-color: transparent;
  }}
  .sbtn:active {{ transform: scale(0.9); }}
  .sbtn.active-approve {{ background: #e8f5e9; border-color: #4CAF50; color: #2e7d32; }}
  .sbtn.active-reject {{ background: #ffebee; border-color: #f44336; color: #c62828; }}
  .section.section-approved {{ border-left: 3px solid #4CAF50; padding-left: 13px; }}
  .section.section-rejected {{ border-left: 3px solid #f44336; padding-left: 13px; opacity: 0.7; }}

  .action-buttons {{ display: flex; gap: 8px; }}
  .btn {{
    flex: 1; padding: 10px 0; border: 2px solid #ddd; border-radius: 6px;
    background: #fff; font-size: 0.85em; font-weight: 600;
    cursor: pointer; transition: all 0.15s; color: #666;
    -webkit-tap-highlight-color: transparent;
  }}
  .btn:active {{ transform: scale(0.97); }}
  .btn-approve.active {{ background: #e8f5e9; border-color: #4CAF50; color: #2e7d32; }}
  .btn-reject.active {{ background: #ffebee; border-color: #f44336; color: #c62828; }}
  .btn-skip.active {{ background: #f5f5f5; border-color: #9e9e9e; color: #666; }}

  .bulk-actions {{
    display: flex; gap: 8px; margin-bottom: 12px;
  }}
  .btn-approve-all, .btn-reject-all {{
    padding: 6px 14px; border-radius: 6px; border: 1px solid #ddd;
    background: #fff; font-size: 0.8em; font-weight: 600;
    cursor: pointer; transition: all 0.15s;
  }}
  .btn-approve-all:hover {{ background: #e8f5e9; border-color: #4CAF50; color: #2e7d32; }}
  .btn-reject-all:hover {{ background: #ffebee; border-color: #f44336; color: #c62828; }}

  /* Section-local rejection reason pills */
  .section-reasons {{
    display: none; flex-wrap: wrap; gap: 6px;
    margin-top: 8px; padding-top: 8px; border-top: 1px solid #eee;
  }}
  .pill {{
    font-size: 0.8em; padding: 5px 12px; border-radius: 16px;
    border: 1px solid #ddd; background: #fff; color: #666;
    cursor: pointer; transition: all 0.15s;
    -webkit-tap-highlight-color: transparent;
  }}
  .pill:active {{ transform: scale(0.95); }}
  .pill.selected {{
    background: #ffebee; border-color: #ef9a9a; color: #c62828;
  }}

  /* Highlights */
  .highlights-list {{ margin-top: 8px; }}
  .highlights-label {{ font-size: 0.7em; text-transform: uppercase; color: #999; margin-bottom: 4px; }}
  .highlights-items {{ display: flex; flex-direction: column; gap: 4px; }}
  .highlight-item {{
    font-size: 0.8em; padding: 4px 8px; background: #fff5f5;
    border-left: 3px solid #ef9a9a; border-radius: 0 4px 4px 0;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .highlight-item .remove {{
    color: #999; cursor: pointer; font-size: 0.9em; padding: 0 4px;
  }}

  /* Save bar */
  #save-bar {{
    position: fixed; bottom: 0; left: 0; right: 0;
    background: #fff; border-top: 1px solid #e0e0e0;
    padding: 12px 16px; display: flex; gap: 12px; align-items: center;
    z-index: 100;
  }}
  #save-btn {{
    flex: 1; background: #4CAF50; color: white; border: none;
    padding: 14px; font-size: 1em; font-weight: 600;
    border-radius: 8px; cursor: pointer;
  }}
  #save-btn:active {{ background: #45a049; }}
  #save-btn.saved {{ background: #2196F3; }}
  #review-count {{ font-size: 0.85em; color: #666; white-space: nowrap; }}
  .bottom-spacer {{ height: 80px; }}

  @media (min-width: 768px) {{
    body {{ max-width: 1000px; margin: 0 auto; padding: 20px; }}
    .comparison {{ flex-direction: row; }}
    .comparison .side {{ flex: 1; }}
    .image-grid {{ grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); }}
  }}
</style>
</head>
<body>

<h1>Review: {run_id}</h1>
<div class="summary">
  <span class="stat"><span class="stat-val">{product_count}</span> to review</span>
  <span class="stat" id="reviewed-stat"><span class="stat-val">0</span> reviewed</span>
  <span class="stat" id="skipped-info">{skipped_count} skipped (no changes)</span>
  <div class="progress-bar"><div class="progress-fill" id="progress" style="width:0%"></div></div>
</div>

{products}

<div class="bottom-spacer"></div>

<div id="save-bar">
  <span id="review-count">0 / {product_count}</span>
  <button id="save-btn" onclick="saveDispositions()">Save</button>
</div>

<script>
const total = {product_count};
let dispositions = {{}};

function highlightVariantImages(el) {{
  const product = el.closest('.product');
  const wasActive = el.classList.contains('active');

  // Clear all highlights in this product
  product.querySelectorAll('.variant-pill, .assign-row').forEach(p => p.classList.remove('active'));
  product.querySelectorAll('.thumb').forEach(t => {{
    t.classList.remove('highlighted', 'dim');
  }});

  if (wasActive) return; // Toggle off

  el.classList.add('active');
  const positions = JSON.parse(el.dataset.images || '[]');

  if (positions.length > 0) {{
    product.querySelectorAll('.thumb[data-pos]').forEach(t => {{
      if (positions.includes(t.dataset.pos)) {{
        t.classList.add('highlighted');
      }} else {{
        t.classList.add('dim');
      }}
    }});
  }}
}}

function setSectionDisposition(btn, status) {{
  const sectionAction = btn.closest('.section-action');
  const section = sectionAction.dataset.section;
  const product = btn.closest('.product');
  const handle = product.dataset.handle;

  // Toggle off if same button
  const wasActive = btn.classList.contains('active-approve') || btn.classList.contains('active-reject');
  sectionAction.querySelectorAll('.sbtn').forEach(b => b.classList.remove('active-approve', 'active-reject'));

  if (!dispositions[handle]) dispositions[handle] = {{ status: 'mixed' }};
  if (!dispositions[handle].sections) dispositions[handle].sections = {{}};

  const sectionEl = sectionAction.closest('.section');

  if (wasActive && dispositions[handle].sections[section] === status) {{
    delete dispositions[handle].sections[section];
    if (sectionEl) sectionEl.classList.remove('section-approved', 'section-rejected');
  }} else {{
    btn.classList.add(status === 'approved' ? 'active-approve' : 'active-reject');
    dispositions[handle].sections[section] = status;
    if (sectionEl) {{
      sectionEl.classList.remove('section-approved', 'section-rejected');
      sectionEl.classList.add(status === 'approved' ? 'section-approved' : 'section-rejected');
    }}
  }}

  // Show/hide section-local reason pills and highlights
  if (sectionEl) {{
    const reasons = sectionEl.querySelector('.section-reasons');
    const highlights = sectionEl.querySelector('.highlights-list');
    const isRejected = dispositions[handle].sections[section] === 'rejected';
    if (reasons) reasons.style.display = isRejected ? 'flex' : 'none';
    if (highlights) highlights.style.display = isRejected ? 'block' : 'none';
  }}

  // Determine overall status from sections
  const sectionStatuses = Object.values(dispositions[handle].sections);
  if (sectionStatuses.length === 0) {{
    delete dispositions[handle];
  }} else {{
    product.classList.remove('reviewed-approved', 'reviewed-rejected', 'reviewed-skip');
    if (sectionStatuses.every(s => s === 'approved')) {{
      dispositions[handle].status = 'approved';
      product.classList.add('reviewed-approved');
    }} else if (sectionStatuses.some(s => s === 'rejected')) {{
      dispositions[handle].status = 'rejected';
      product.classList.add('reviewed-rejected');
    }} else {{
      dispositions[handle].status = 'mixed';
    }}
  }}

  updateProgress();
}}

function approveAll(btn) {{
  const product = btn.closest('.product');
  product.querySelectorAll('.sbtn-approve').forEach(b => setSectionDisposition(b, 'approved'));
}}

function rejectAll(btn) {{
  const product = btn.closest('.product');
  product.querySelectorAll('.sbtn-reject').forEach(b => setSectionDisposition(b, 'rejected'));
}}

function updateProgress() {{
  const count = Object.keys(dispositions).length;
  document.getElementById('review-count').textContent = count + ' / ' + total;
  document.getElementById('progress').style.width = (count / Math.max(total, 1) * 100) + '%';
  document.querySelector('#reviewed-stat .stat-val').textContent = count;
}}

function setDisposition(btn, status) {{
  const product = btn.closest('.product');
  const handle = product.dataset.handle;

  const wasActive = btn.classList.contains('active');
  product.querySelectorAll('.action-buttons .btn').forEach(b => b.classList.remove('active'));
  product.classList.remove('reviewed-approved', 'reviewed-rejected', 'reviewed-skip');

  const reasonPills = product.querySelector('.reason-pills');
  const highlightsList = product.querySelector('.highlights-list');

  if (wasActive) {{
    delete dispositions[handle];
    reasonPills.style.display = 'none';
    highlightsList.style.display = 'none';
  }} else {{
    btn.classList.add('active');
    product.classList.add('reviewed-' + status);
    if (status === 'skip') {{
      delete dispositions[handle];
      reasonPills.style.display = 'none';
      highlightsList.style.display = 'none';
    }} else {{
      dispositions[handle] = {{ status: status }};
      if (status === 'rejected') {{
        reasonPills.style.display = 'block';
        highlightsList.style.display = 'block';
        // Restore any previously selected reasons
        const existing = dispositions[handle].reasons || [];
        product.querySelectorAll('.pill').forEach(p => {{
          p.classList.toggle('selected', existing.includes(p.dataset.reason));
        }});
      }} else {{
        reasonPills.style.display = 'none';
        highlightsList.style.display = 'none';
      }}
    }}
  }}
  updateProgress();
}}

// Pill multi-select — saves reasons per section
document.querySelectorAll('.pill').forEach(pill => {{
  pill.addEventListener('click', function() {{
    this.classList.toggle('selected');
    const product = this.closest('.product');
    const handle = product.dataset.handle;
    const sectionEl = this.closest('.section');
    const section = sectionEl ? sectionEl.dataset.sectionType : 'general';
    if (!dispositions[handle]) return;

    // Gather selected reasons within this section
    const container = this.closest('.section-reasons');
    const selected = [...container.querySelectorAll('.pill.selected')].map(p => p.dataset.reason);

    if (!dispositions[handle].section_reasons) dispositions[handle].section_reasons = {{}};
    if (selected.length > 0) {{
      dispositions[handle].section_reasons[section] = selected;
    }} else {{
      delete dispositions[handle].section_reasons[section];
    }}

    // Also flatten to top-level reasons for backward compat
    const allReasons = Object.values(dispositions[handle].section_reasons).flat();
    if (allReasons.length > 0) {{
      dispositions[handle].reasons = [...new Set(allReasons)];
    }} else {{
      delete dispositions[handle].reasons;
    }}
  }});
}});

// Text highlight on selection in proposed descriptions
document.addEventListener('mouseup', handleTextSelect);
document.addEventListener('touchend', handleTextSelect);

function handleTextSelect() {{
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.toString().trim()) return;

  const proposed = sel.anchorNode?.parentElement?.closest('.selectable-text');
  if (!proposed) return;

  const text = sel.toString().trim();
  if (text.length < 3) return;

  const product = proposed.closest('.product');
  const handle = product.dataset.handle;

  // Only allow highlights when description is rejected
  if (!dispositions[handle] || !dispositions[handle].sections || dispositions[handle].sections.description !== 'rejected') return;

  // Add highlight
  if (!dispositions[handle].highlights) dispositions[handle].highlights = [];
  if (!dispositions[handle].highlights.includes(text)) {{
    dispositions[handle].highlights.push(text);
  }}

  // Wrap selected text in highlight mark
  try {{
    const range = sel.getRangeAt(0);
    const mark = document.createElement('mark');
    mark.className = 'highlight-mark';
    mark.onclick = function(e) {{
      e.stopPropagation();
      unhighlight(this);
    }};
    range.surroundContents(mark);
  }} catch(e) {{ /* cross-element selection */ }}

  sel.removeAllRanges();
  renderHighlights(product, handle);
}}

function unhighlight(mark) {{
  const product = mark.closest('.product');
  const handle = product.dataset.handle;
  const text = mark.textContent;

  // Remove from dispositions
  if (dispositions[handle] && dispositions[handle].highlights) {{
    const idx = dispositions[handle].highlights.indexOf(text);
    if (idx > -1) dispositions[handle].highlights.splice(idx, 1);
    if (dispositions[handle].highlights.length === 0) delete dispositions[handle].highlights;
  }}

  // Unwrap the mark element
  const parent = mark.parentNode;
  while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
  parent.removeChild(mark);
  parent.normalize();

  renderHighlights(product, handle);
}}

function renderHighlights(product, handle) {{
  const container = product.querySelector('.highlights-items');
  const highlights = (dispositions[handle] && dispositions[handle].highlights) || [];
  container.innerHTML = highlights.map((h, i) =>
    `<div class="highlight-item">
      <span>"${{h.length > 50 ? h.slice(0, 50) + '...' : h}}"</span>
      <span class="remove" onclick="removeHighlight('${{handle}}', ${{i}}, this)">&times;</span>
    </div>`
  ).join('');
}}

function removeHighlight(handle, index, el) {{
  if (dispositions[handle] && dispositions[handle].highlights) {{
    dispositions[handle].highlights.splice(index, 1);
    if (dispositions[handle].highlights.length === 0) delete dispositions[handle].highlights;
  }}
  const product = el.closest('.product');
  renderHighlights(product, handle);
}}

function saveDispositions() {{
  const count = Object.keys(dispositions).length;
  if (count === 0) {{ alert('No products reviewed yet.'); return; }}

  if (location.protocol.startsWith('http')) {{
    fetch('/dispositions', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(dispositions, null, 2),
    }})
    .then(r => r.json())
    .then(data => {{
      const btn = document.getElementById('save-btn');
      btn.textContent = 'Saved ' + data.saved;
      btn.classList.add('saved');
      setTimeout(() => {{ btn.textContent = 'Save'; btn.classList.remove('saved'); }}, 3000);
    }})
    .catch(err => alert('Save failed: ' + err));
  }} else {{
    const blob = new Blob([JSON.stringify(dispositions, null, 2)], {{ type: 'application/json' }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '{dispositions_filename}';
    a.click();
    URL.revokeObjectURL(url);
  }}
}}
</script>
</body>
</html>"""
