"""Generate an enrichment opportunity dashboard from audit data."""

from __future__ import annotations

import csv
import html
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_dashboard(audit_csv: Path, output_path: Path) -> None:
    """Generate an HTML dashboard showing products ranked by enrichment payoff."""
    rows = []
    with open(audit_csv) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # Collect vendors for filter
    vendors = sorted(set(r.get("Vendor", "") for r in rows if r.get("Vendor")))

    # Build product cards as JSON for client-side filtering/sorting
    products = []
    for r in rows:
        gaps = r.get("Gaps", "")
        gap_list = [g.strip() for g in gaps.split(",") if g.strip()] if gaps else []

        products.append({
            "handle": r.get("Product Handle", ""),
            "vendor": r.get("Vendor", ""),
            "title": r.get("Title", ""),
            "priority": float(r.get("Priority Score", 0) or 0),
            "has_image": r.get("Has Image", "").lower() == "true",
            "has_variant_images": r.get("Has Variant Images", "").lower() == "true",
            "has_description": r.get("Has Description", "").lower() == "true",
            "has_product_type": r.get("Has Product Type", "").lower() == "true",
            "has_tags": r.get("Has Tags", "").lower() == "true",
            "gaps": gap_list,
            "suggestions": r.get("Suggestions", ""),
            "admin_link": r.get("Admin Link", ""),
            "sessions": r.get("Sessions", ""),
            "revenue": r.get("Online Revenue", ""),
        })

    output = _TEMPLATE.format(
        product_count=len(products),
        vendor_count=len(vendors),
        products_json=json.dumps(products),
        vendor_options="".join(
            f'<option value="{html.escape(v)}">{html.escape(v)}</option>' for v in vendors
        ),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output)
    logger.info("Dashboard written to %s", output_path)


_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enrichment Opportunities</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}

  .header {{
    background: #fff; border-bottom: 1px solid #e0e0e0; padding: 16px 24px;
    position: sticky; top: 0; z-index: 10;
  }}
  .header h1 {{ font-size: 1.3em; margin-bottom: 8px; }}
  .header-stats {{ display: flex; gap: 16px; font-size: 0.85em; color: #666; margin-bottom: 12px; }}
  .stat {{ font-weight: 600; color: #333; }}

  .filters {{
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  }}
  .filters select, .filters input {{
    padding: 6px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 0.85em;
  }}
  .filters select {{ min-width: 150px; }}
  .filters input {{ min-width: 200px; }}
  .filters label {{ font-size: 0.8em; color: #666; display: flex; align-items: center; gap: 4px; }}

  .sort-group {{ display: flex; gap: 4px; }}
  .sort-btn {{
    padding: 4px 10px; border: 1px solid #ddd; border-radius: 4px;
    background: #fff; font-size: 0.8em; cursor: pointer;
  }}
  .sort-btn.active {{ background: #1976d2; color: #fff; border-color: #1976d2; }}

  .container {{ max-width: 1100px; margin: 0 auto; padding: 16px; }}

  .results-info {{ font-size: 0.85em; color: #666; margin-bottom: 12px; }}

  .product-list {{ display: flex; flex-direction: column; gap: 8px; }}

  .product-row {{
    background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
    padding: 12px 16px; display: flex; align-items: center; gap: 16px;
    transition: border-color 0.15s;
  }}
  .product-row:hover {{ border-color: #999; }}

  .product-rank {{
    font-size: 1.2em; font-weight: 700; color: #999; min-width: 36px; text-align: center;
  }}

  .product-info {{ flex: 1; min-width: 0; }}
  .product-title {{
    font-weight: 600; font-size: 0.95em; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }}
  .product-meta {{
    font-size: 0.8em; color: #666; margin-top: 2px;
    display: flex; gap: 8px; flex-wrap: wrap;
  }}
  .vendor-badge {{
    background: #e8eaf6; color: #3f51b5; padding: 1px 6px;
    border-radius: 3px; font-size: 0.75em; font-weight: 600;
  }}

  .gap-pills {{ display: flex; gap: 4px; flex-wrap: wrap; }}
  .gap-pill {{
    font-size: 0.7em; padding: 2px 8px; border-radius: 10px;
    border: 1px solid #ddd; color: #666;
  }}
  .gap-pill.image {{ border-color: #e57373; color: #c62828; background: #ffebee; }}
  .gap-pill.variant {{ border-color: #ffb74d; color: #e65100; background: #fff3e0; }}
  .gap-pill.desc {{ border-color: #64b5f6; color: #1565c0; background: #e3f2fd; }}
  .gap-pill.type {{ border-color: #aaa; }}
  .gap-pill.tags {{ border-color: #aaa; }}

  .product-score {{
    min-width: 60px; text-align: right;
  }}
  .score-val {{
    font-size: 1.1em; font-weight: 700;
  }}
  .score-label {{ font-size: 0.65em; color: #999; }}

  .product-actions {{ display: flex; gap: 6px; }}
  .action-link {{
    font-size: 0.75em; padding: 4px 8px; border-radius: 4px;
    border: 1px solid #ddd; text-decoration: none; color: #666;
    transition: all 0.15s;
  }}
  .action-link:hover {{ background: #f5f5f5; border-color: #999; }}

  .select-col {{ min-width: 28px; }}
  .select-col input {{ cursor: pointer; }}

  .batch-bar {{
    position: sticky; bottom: 0; background: #fff; border-top: 1px solid #e0e0e0;
    padding: 12px 24px; display: none; align-items: center; gap: 12px;
    box-shadow: 0 -2px 8px rgba(0,0,0,0.1);
  }}
  .batch-bar.visible {{ display: flex; }}
  .batch-bar .count {{ font-weight: 600; }}
  .batch-bar button {{
    padding: 8px 20px; border-radius: 6px; border: none;
    background: #4CAF50; color: #fff; font-weight: 600; cursor: pointer;
    font-size: 0.9em;
  }}
  .batch-bar button:hover {{ background: #45a049; }}
  .batch-bar .export-btn {{ background: #1976d2; }}
  .batch-bar .export-btn:hover {{ background: #1565c0; }}
</style>
</head>
<body>

<div class="header">
  <h1>Enrichment Opportunities</h1>
  <div class="header-stats">
    <span><span class="stat">{product_count}</span> products with gaps</span>
    <span><span class="stat">{vendor_count}</span> vendors</span>
  </div>
  <div class="filters">
    <select id="vendor-filter" onchange="applyFilters()">
      <option value="">All Vendors</option>
      {vendor_options}
    </select>
    <input type="text" id="search-input" placeholder="Search products..." oninput="applyFilters()" />
    <label><input type="checkbox" id="filter-images" onchange="applyFilters()" /> Missing Images</label>
    <label><input type="checkbox" id="filter-desc" onchange="applyFilters()" /> Missing Description</label>
    <label><input type="checkbox" id="filter-variants" onchange="applyFilters()" /> Missing Variant Images</label>
    <div class="sort-group">
      <button class="sort-btn active" data-sort="priority" onclick="setSort(this)">Priority</button>
      <button class="sort-btn" data-sort="title" onclick="setSort(this)">Title</button>
      <button class="sort-btn" data-sort="vendor" onclick="setSort(this)">Vendor</button>
    </div>
  </div>
</div>

<div class="container">
  <div class="results-info" id="results-info"></div>
  <div class="product-list" id="product-list"></div>
</div>

<div class="batch-bar" id="batch-bar">
  <span><span class="count" id="selected-count">0</span> selected</span>
  <button class="export-btn" onclick="exportSelected()">Export CSV for Pipeline</button>
</div>

<script>
const allProducts = {products_json};
let currentSort = 'priority';
let selected = new Set();

function gapClass(gap) {{
  if (gap.includes('image') && !gap.includes('variant')) return 'image';
  if (gap.includes('variant')) return 'variant';
  if (gap.includes('description')) return 'desc';
  if (gap.includes('type')) return 'type';
  if (gap.includes('tag')) return 'tags';
  return '';
}}

function shortGap(gap) {{
  return gap.replace('Missing ', '').replace('product ', '').replace(' (minimum 100 characters)', '');
}}

function renderProducts(products) {{
  const list = document.getElementById('product-list');
  const info = document.getElementById('results-info');
  info.textContent = products.length + ' products shown';

  if (products.length === 0) {{
    list.innerHTML = '<div style="text-align:center;color:#999;padding:40px;">No products match filters</div>';
    return;
  }}

  list.innerHTML = products.slice(0, 200).map((p, i) => `
    <div class="product-row" data-handle="${{p.handle}}">
      <div class="select-col">
        <input type="checkbox" ${{selected.has(p.handle) ? 'checked' : ''}}
               onchange="toggleSelect('${{p.handle}}', this.checked)" />
      </div>
      <div class="product-rank">${{i + 1}}</div>
      <div class="product-info">
        <div class="product-title">${{p.title}}</div>
        <div class="product-meta">
          <span class="vendor-badge">${{p.vendor}}</span>
          <span>${{p.handle}}</span>
        </div>
        <div class="gap-pills" style="margin-top:4px">
          ${{p.gaps.map(g => `<span class="gap-pill ${{gapClass(g)}}">${{shortGap(g)}}</span>`).join('')}}
        </div>
      </div>
      <div class="product-score">
        <div class="score-val">${{Math.round(p.priority).toLocaleString()}}</div>
        <div class="score-label">priority</div>
      </div>
      <div class="product-actions">
        ${{p.admin_link ? `<a class="action-link" href="${{p.admin_link}}" target="_blank">Shopify</a>` : ''}}
      </div>
    </div>
  `).join('');

  if (products.length > 200) {{
    list.innerHTML += `<div style="text-align:center;color:#999;padding:20px;">Showing top 200 of ${{products.length}}</div>`;
  }}
}}

function applyFilters() {{
  const vendor = document.getElementById('vendor-filter').value;
  const search = document.getElementById('search-input').value.toLowerCase();
  const needImages = document.getElementById('filter-images').checked;
  const needDesc = document.getElementById('filter-desc').checked;
  const needVariants = document.getElementById('filter-variants').checked;

  let filtered = allProducts.filter(p => {{
    if (vendor && p.vendor !== vendor) return false;
    if (search && !p.title.toLowerCase().includes(search) && !p.handle.includes(search)) return false;
    if (needImages && p.has_image) return false;
    if (needDesc && p.has_description) return false;
    if (needVariants && p.has_variant_images) return false;
    return true;
  }});

  if (currentSort === 'priority') filtered.sort((a, b) => b.priority - a.priority);
  else if (currentSort === 'title') filtered.sort((a, b) => a.title.localeCompare(b.title));
  else if (currentSort === 'vendor') filtered.sort((a, b) => a.vendor.localeCompare(b.vendor) || b.priority - a.priority);

  renderProducts(filtered);
}}

function setSort(btn) {{
  document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  currentSort = btn.dataset.sort;
  applyFilters();
}}

function toggleSelect(handle, checked) {{
  if (checked) selected.add(handle); else selected.delete(handle);
  document.getElementById('selected-count').textContent = selected.size;
  document.getElementById('batch-bar').classList.toggle('visible', selected.size > 0);
}}

function exportSelected() {{
  const selectedProducts = allProducts.filter(p => selected.has(p.handle));
  const headers = ['Product Handle', 'Vendor', 'Title', 'Barcode', 'SKU', 'Has Image', 'Has Variant Images', 'Has Description', 'Has Product Type', 'Has Tags', 'Gaps', 'Suggestions', 'Priority Score', 'Admin Link'];
  let csv = headers.join(',') + '\\n';
  selectedProducts.forEach(p => {{
    csv += [p.handle, p.vendor, `"${{p.title}}"`, '', '', p.has_image, p.has_variant_images, p.has_description, p.has_product_type, p.has_tags, `"${{p.gaps.join(', ')}}"`, `"${{p.suggestions}}"`, p.priority, p.admin_link].join(',') + '\\n';
  }});
  const blob = new Blob([csv], {{ type: 'text/csv' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'enrichment_batch_' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
  URL.revokeObjectURL(url);
}}

applyFilters();
</script>
</body>
</html>
"""
