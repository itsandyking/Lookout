"""Integration test: audit finds gaps, enrich pipeline processes them."""
import csv
import io

from unittest.mock import MagicMock

from lookout.audit.auditor import ContentAuditor
from lookout.enrich.models import InputRow


def test_audit_csv_is_valid_enrich_input():
    """Verify audit CSV output can be parsed as enrich pipeline InputRow."""
    mock_store = MagicMock()
    mock_store.list_products.return_value = [
        {
            "id": 1, "handle": "nano-puff", "title": "Nano Puff",
            "vendor": "Patagonia", "product_type": "Jacket",
            "body_html": "", "tags": "mens", "status": "active",
            "created_at": None,
        },
    ]
    mock_store.get_variants.return_value = [
        {
            "id": 101, "product_id": 1, "sku": "PAT-NP-M",
            "barcode": "123456789", "price": 229.0, "compare_at_price": None,
            "cost": 100.0, "option1_name": "Color", "option1_value": "Black",
            "option2_name": "Size", "option2_value": "M",
            "option3_name": "", "option3_value": "",
            "image_src": "", "position": 1,
        },
    ]
    mock_store.get_inventory.return_value = {
        "total": 5, "value": 500.0, "full_price_value": 500.0, "by_location": {},
    }

    # Run audit
    auditor = ContentAuditor(mock_store)
    result = auditor.audit()

    # Export to CSV
    csv_bytes = result.to_priority_csv()
    csv_text = csv_bytes.decode("utf-8")

    # Parse as enrich input
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert len(rows) == 1

    # Verify it can be parsed as InputRow
    row = rows[0]
    input_row = InputRow.model_validate(row)
    assert input_row.product_handle == "nano-puff"
    assert input_row.vendor == "Patagonia"
    assert input_row.needs_description  # body_html was empty
    assert input_row.has_any_gap
