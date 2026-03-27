"""Generate HTML review reports for enrichment runs."""

from __future__ import annotations

import html
import logging
from pathlib import Path

from lookout.apply.models import ApplyRun

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def generate_review_report(run: ApplyRun, output_path: Path) -> None:
    """Generate an HTML review report showing side-by-side diffs.

    The report includes current vs proposed description for each product,
    with approve/reject/edit controls that save to a dispositions JSON file.
    """
    template_path = TEMPLATE_DIR / "review.html"
    if template_path.exists():
        template = template_path.read_text()
    else:
        template = _FALLBACK_TEMPLATE

    products_html = []
    for change in run.changes:
        current = html.escape(change.current_body_html or "(empty)")
        proposed = html.escape(
            change.new_body_html or "(no change)"
        )
        products_html.append(
            _PRODUCT_TEMPLATE.format(
                handle=change.handle,
                title=html.escape(change.title),
                vendor=html.escape(change.vendor),
                confidence=change.confidence,
                current=current,
                proposed=proposed,
                product_id=change.product_id,
            )
        )

    output = template.format(
        run_id=run.run_id,
        product_count=len(run.changes),
        products="\n".join(products_html),
        dispositions_filename=f"{run.run_id}_dispositions.json",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output)
    logger.info("Review report written to %s", output_path)


_PRODUCT_TEMPLATE = """
<div class="product" data-handle="{handle}" data-product-id="{product_id}">
  <div class="product-header">
    <h3>{title}</h3>
    <span class="vendor">{vendor}</span>
    <span class="confidence">Confidence: {confidence}%</span>
    <span class="handle">{handle}</span>
  </div>
  <div class="comparison">
    <div class="side current">
      <h4>Current</h4>
      <div class="content">{current}</div>
    </div>
    <div class="side proposed">
      <h4>Proposed</h4>
      <div class="content">{proposed}</div>
    </div>
  </div>
  <div class="actions">
    <label><input type="radio" name="disposition-{handle}" value="approved" /> Approve</label>
    <label><input type="radio" name="disposition-{handle}" value="rejected" /> Reject</label>
    <select class="rejection-reason" style="display:none">
      <option value="">Select reason...</option>
      <option value="wrong_facts">Wrong facts</option>
      <option value="bad_structure">Bad structure</option>
      <option value="wrong_image">Wrong image</option>
      <option value="tone">Wrong tone</option>
      <option value="incomplete">Incomplete</option>
      <option value="other">Other</option>
    </select>
  </div>
</div>
"""

_FALLBACK_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<title>Enrichment Review: {run_id}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }}
  .product {{ border: 1px solid #ddd; border-radius: 8px; margin: 20px 0; padding: 20px; }}
  .product-header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }}
  .product-header h3 {{ margin: 0; }}
  .vendor {{ color: #666; }}
  .confidence {{ background: #e8f5e9; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }}
  .handle {{ color: #999; font-family: monospace; font-size: 0.85em; }}
  .comparison {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .side {{ border: 1px solid #eee; border-radius: 4px; padding: 12px; }}
  .side h4 {{ margin: 0 0 8px 0; color: #666; }}
  .current {{ background: #fff5f5; }}
  .proposed {{ background: #f5fff5; }}
  .content {{ white-space: pre-wrap; font-size: 0.9em; line-height: 1.5; }}
  .actions {{ margin-top: 12px; display: flex; gap: 16px; align-items: center; }}
  .actions label {{ cursor: pointer; }}
  .summary {{ background: #f0f0f0; padding: 16px; border-radius: 8px; margin-bottom: 20px; }}
  #save-btn {{ background: #4CAF50; color: white; border: none; padding: 12px 24px; font-size: 16px; border-radius: 4px; cursor: pointer; position: fixed; bottom: 20px; right: 20px; }}
  #save-btn:hover {{ background: #45a049; }}
</style>
</head>
<body>
<h1>Enrichment Review: {run_id}</h1>
<div class="summary">
  <strong>{product_count} products</strong> to review.
  Approve, reject (with reason), or skip each product.
  Then click Save to export your dispositions.
</div>

{products}

<button id="save-btn" onclick="saveDispositions()">Save Dispositions</button>

<script>
// Show rejection reason dropdown when "Reject" is selected
document.querySelectorAll('input[type=radio]').forEach(radio => {{
  radio.addEventListener('change', function() {{
    const product = this.closest('.product');
    const reasonSelect = product.querySelector('.rejection-reason');
    reasonSelect.style.display = this.value === 'rejected' ? 'inline-block' : 'none';
  }});
}});

function saveDispositions() {{
  const dispositions = {{}};
  document.querySelectorAll('.product').forEach(product => {{
    const handle = product.dataset.handle;
    const checked = product.querySelector('input[type=radio]:checked');
    if (!checked) return;
    const d = {{ status: checked.value }};
    if (checked.value === 'rejected') {{
      const reason = product.querySelector('.rejection-reason').value;
      if (reason) d.reason = reason;
    }}
    dispositions[handle] = d;
  }});

  const blob = new Blob([JSON.stringify(dispositions, null, 2)], {{ type: 'application/json' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = '{dispositions_filename}';
  a.click();
  URL.revokeObjectURL(url);
}}
</script>
</body>
</html>"""
