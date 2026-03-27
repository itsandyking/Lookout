"""Generate HTML review reports for enrichment runs."""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path

from lookout.apply.models import ApplyRun

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def generate_review_report(run: ApplyRun, output_path: Path) -> None:
    """Generate an HTML review report showing diffs and images.

    Flags empty proposals, shows image thumbnails, mobile-friendly layout.
    """
    products_html = []
    for change in run.changes:
        has_description = bool(change.new_body_html and change.new_body_html.strip())
        has_images = bool(change.new_images)

        # Skip products with nothing to review
        if not has_description and not has_images:
            continue

        current_desc = html.escape(change.current_body_html or "")
        proposed_desc = html.escape(change.new_body_html or "")

        # Description section
        if has_description:
            desc_html = _DESC_TEMPLATE.format(
                current=current_desc if current_desc else '<span class="empty-tag">No current description</span>',
                proposed=proposed_desc,
            )
        else:
            desc_html = '<div class="no-change">No description changes proposed</div>'

        # Images section
        if has_images:
            # Build image src lookup by position and URL
            img_by_src = {}
            thumbs = []
            for img in change.new_images[:12]:
                src = img.get("src", "")
                alt = html.escape(img.get("alt", ""))
                pos = img.get("position", "")
                img_by_src[src] = pos
                thumbs.append(f'<div class="thumb"><img src="{src}" alt="{alt}" loading="lazy" /><span class="pos">#{pos}</span></div>')
            remaining = len(change.new_images) - 12
            extra = f'<div class="thumb more">+{remaining} more</div>' if remaining > 0 else ""
            current_img_count = len(change.current_images) if change.current_images else 0

            # Variant assignment section
            vim = change.new_variant_image_map or {}
            assignments_html = ""
            if vim:
                rows = []
                for variant_key, img_src in vim.items():
                    if variant_key == "__all__":
                        label = "All variants"
                    else:
                        label = html.escape(variant_key)
                    # img_src can be a string or list
                    srcs = img_src if isinstance(img_src, list) else [img_src]
                    pos_labels = []
                    for s in srcs:
                        pos = img_by_src.get(s, "?")
                        pos_labels.append(f"#{pos}")
                    rows.append(f'<div class="assignment"><span class="variant-name">{label}</span>'
                                f'<span class="arrow">&rarr;</span>'
                                f'<span class="assigned-imgs">{", ".join(pos_labels)}</span></div>')
                assignments_html = '<div class="assignments">' + "\n".join(rows) + '</div>'

            images_html = _IMAGES_TEMPLATE.format(
                current_count=current_img_count,
                proposed_count=len(change.new_images),
                thumbnails="\n".join(thumbs) + extra,
                assignments=assignments_html,
            )
        else:
            images_html = ""

        # Confidence badge color
        conf = change.confidence
        if conf >= 80:
            conf_class = "conf-high"
        elif conf >= 60:
            conf_class = "conf-med"
        else:
            conf_class = "conf-low"

        products_html.append(
            _PRODUCT_TEMPLATE.format(
                handle=change.handle,
                title=html.escape(change.title),
                vendor=html.escape(change.vendor),
                confidence=conf,
                conf_class=conf_class,
                product_id=change.product_id,
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
      <span class="handle">{handle}</span>
    </div>
  </div>

  {description_section}
  {images_section}

  <div class="actions">
    <div class="action-buttons">
      <button type="button" class="btn btn-approve" onclick="setDisposition(this, 'approved')">Approve</button>
      <button type="button" class="btn btn-reject" onclick="setDisposition(this, 'rejected')">Reject</button>
      <button type="button" class="btn btn-skip" onclick="setDisposition(this, 'skip')">Skip</button>
    </div>
    <select class="rejection-reason" style="display:none" onchange="updateReason(this)">
      <option value="">Reason...</option>
      <optgroup label="Description">
        <option value="hallucinated">Hallucinated facts</option>
        <option value="bad_source_data">Bad source data (wrong page scraped)</option>
        <option value="stale_source">Stale/outdated source</option>
        <option value="bad_structure">Bad structure (formatting/flow)</option>
        <option value="incomplete">Incomplete (missing specs/features)</option>
        <option value="tone">Wrong tone</option>
        <option value="typos">Typos/grammar errors</option>
      </optgroup>
      <optgroup label="Images">
        <option value="wrong_image_match">Wrong image match (wrong color/style)</option>
        <option value="missing_image">Missing images</option>
        <option value="bad_image_quality">Bad image quality</option>
      </optgroup>
      <option value="other">Other</option>
    </select>
  </div>
</div>
"""

_DESC_TEMPLATE = """
<div class="section">
  <h4 class="section-label">Description</h4>
  <div class="comparison">
    <div class="side current">
      <div class="side-label">Current</div>
      <div class="content">{current}</div>
    </div>
    <div class="side proposed">
      <div class="side-label">Proposed</div>
      <div class="content">{proposed}</div>
    </div>
  </div>
</div>
"""

_IMAGES_TEMPLATE = """
<div class="section">
  <h4 class="section-label">Images <span class="img-count">{current_count} current &rarr; {proposed_count} proposed</span></h4>
  <div class="image-grid">
    {thumbnails}
  </div>
  {assignments}
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
  .summary strong {{ color: #333; }}
  .stat {{ display: inline-block; margin-right: 16px; }}
  .stat-val {{ font-weight: 600; }}

  .progress-bar {{
    height: 4px; background: #e0e0e0; border-radius: 2px;
    margin-top: 8px; overflow: hidden;
  }}
  .progress-fill {{ height: 100%; background: #4CAF50; transition: width 0.3s; }}

  /* Product card */
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

  .badge {{
    font-size: 0.75em; font-weight: 600; padding: 2px 8px;
    border-radius: 10px; white-space: nowrap;
  }}
  .conf-high {{ background: #e8f5e9; color: #2e7d32; }}
  .conf-med {{ background: #fff3e0; color: #e65100; }}
  .conf-low {{ background: #ffebee; color: #c62828; }}

  /* Sections */
  .section {{ padding: 0 16px 12px; }}
  .section-label {{
    font-size: 0.8em; font-weight: 600; color: #666;
    margin: 0 0 8px 0; text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .img-count {{ font-weight: 400; text-transform: none; letter-spacing: 0; }}

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
  .content {{ white-space: pre-wrap; word-break: break-word; }}
  .empty-tag {{
    color: #999; font-style: italic;
  }}
  .no-change {{
    padding: 0 16px 12px; color: #999; font-style: italic; font-size: 0.85em;
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
  .thumb img {{
    width: 100%; height: 100%; object-fit: cover;
    display: block;
  }}
  .thumb .pos {{
    position: absolute; bottom: 2px; right: 4px;
    font-size: 0.65em; color: #fff; background: rgba(0,0,0,0.5);
    padding: 1px 4px; border-radius: 3px;
  }}
  .thumb.more {{
    display: flex; align-items: center; justify-content: center;
    font-size: 0.85em; color: #666; font-weight: 600;
  }}

  /* Variant assignments */
  .assignments {{
    margin-top: 10px; padding: 8px 10px;
    background: #f8f9fa; border-radius: 6px; border: 1px solid #eee;
  }}
  .assignment {{
    display: flex; align-items: center; gap: 6px;
    padding: 4px 0; font-size: 0.8em;
    border-bottom: 1px solid #eee;
  }}
  .assignment:last-child {{ border-bottom: none; }}
  .variant-name {{ font-weight: 600; color: #333; min-width: 0; flex-shrink: 1; }}
  .arrow {{ color: #999; flex-shrink: 0; }}
  .assigned-imgs {{ color: #666; font-family: monospace; font-size: 0.9em; }}

  /* Actions */
  .actions {{ padding: 8px 16px 12px; }}
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

  .rejection-reason {{
    display: block; width: 100%; margin-top: 8px; padding: 8px;
    border: 1px solid #ddd; border-radius: 6px; font-size: 0.85em;
    background: #fff;
  }}

  /* Save button */
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
    -webkit-tap-highlight-color: transparent;
  }}
  #save-btn:active {{ background: #45a049; }}
  #save-btn.saved {{ background: #2196F3; }}
  #review-count {{ font-size: 0.85em; color: #666; white-space: nowrap; }}

  /* Bottom padding for fixed save bar */
  .bottom-spacer {{ height: 80px; }}

  /* Desktop: side-by-side descriptions */
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

function updateProgress() {{
  const count = Object.keys(dispositions).length;
  document.getElementById('review-count').textContent = count + ' / ' + total;
  document.getElementById('progress').style.width = (count / Math.max(total, 1) * 100) + '%';
  document.querySelector('#reviewed-stat .stat-val').textContent = count;
}}

function setDisposition(btn, status) {{
  const product = btn.closest('.product');
  const handle = product.dataset.handle;

  // Toggle off if clicking same button
  const wasActive = btn.classList.contains('active');
  product.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  product.classList.remove('reviewed-approved', 'reviewed-rejected', 'reviewed-skip');

  const reasonSelect = product.querySelector('.rejection-reason');

  if (wasActive) {{
    delete dispositions[handle];
    reasonSelect.style.display = 'none';
  }} else {{
    btn.classList.add('active');
    product.classList.add('reviewed-' + status);
    if (status === 'skip') {{
      delete dispositions[handle];
    }} else {{
      dispositions[handle] = {{ status: status }};
      if (status === 'rejected') {{
        reasonSelect.style.display = 'block';
        const reason = reasonSelect.value;
        if (reason) dispositions[handle].reason = reason;
      }} else {{
        reasonSelect.style.display = 'none';
      }}
    }}
  }}
  updateProgress();
}}

function updateReason(select) {{
  const handle = select.closest('.product').dataset.handle;
  if (dispositions[handle]) {{
    if (select.value) {{
      dispositions[handle].reason = select.value;
    }} else {{
      delete dispositions[handle].reason;
    }}
  }}
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
