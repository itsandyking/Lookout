"""Tests for the audit module (models + auditor)."""

from __future__ import annotations

import csv
import io
from unittest.mock import MagicMock

from lookout.audit.auditor import ContentAuditor
from lookout.audit.models import AuditResult, ProductScore

# ---------------------------------------------------------------------------
# ProductScore tests
# ---------------------------------------------------------------------------


def test_product_score_gap_calculation():
    """Missing image + missing description = gap_count 2.0."""
    score = ProductScore(
        product_id=1,
        handle="test-product",
        title="Test Product",
        vendor="TestVendor",
        product_type="Jacket",
        has_product_image=False,
        has_description=False,
        description_length=0,
        variant_count=2,
        variants_missing_images=0,
    )
    score.calculate_gaps()
    assert score.gap_count == 2.0
    assert "Missing product image" in score.gaps
    assert "Missing description" in score.gaps


def test_product_score_no_gaps():
    """Complete product has gap_count 0."""
    score = ProductScore(
        product_id=2,
        handle="complete-product",
        title="Complete Product",
        vendor="TestVendor",
        product_type="Boots",
        has_product_image=True,
        has_all_variant_images=True,
        has_description=True,
        has_product_type=True,
        has_tags=True,
    )
    score.calculate_gaps()
    assert score.gap_count == 0.0
    assert score.is_complete
    assert score.gaps == []


# ---------------------------------------------------------------------------
# AuditResult tests
# ---------------------------------------------------------------------------


def test_audit_result_priority_sorting():
    """Higher inventory_value * more gaps sorts first."""
    low = ProductScore(
        product_id=1,
        handle="low",
        title="Low",
        vendor="V",
        product_type="T",
        has_product_image=False,
        has_description=True,
        inventory_value=100.0,
    )
    low.calculate_gaps()

    high = ProductScore(
        product_id=2,
        handle="high",
        title="High",
        vendor="V",
        product_type="T",
        has_product_image=False,
        has_description=False,
        inventory_value=500.0,
    )
    high.calculate_gaps()

    result = AuditResult(scores=[low, high])
    priorities = result.priority_items
    assert len(priorities) == 2
    assert priorities[0].handle == "high"
    assert priorities[1].handle == "low"


def test_audit_result_summary():
    """Summary reports total_products and products_with_gaps."""
    complete = ProductScore(
        product_id=1,
        handle="ok",
        title="OK",
        vendor="V",
        product_type="T",
    )
    complete.calculate_gaps()

    incomplete = ProductScore(
        product_id=2,
        handle="bad",
        title="Bad",
        vendor="V",
        product_type="T",
        has_product_image=False,
    )
    incomplete.calculate_gaps()

    result = AuditResult(scores=[complete, incomplete])
    summary = result.summary()
    assert summary["total_products"] == 2
    assert summary["products_with_gaps"] == 1
    assert summary["products_complete"] == 1


def test_audit_result_to_priority_csv():
    """CSV has canonical columns and is parseable."""
    score = ProductScore(
        product_id=1,
        handle="test-handle",
        title="Test Title",
        vendor="TestVendor",
        product_type="Boots",
        has_product_image=False,
        has_description=False,
        barcode="123456789",
        inventory_value=200.0,
    )
    score.calculate_gaps()

    result = AuditResult(scores=[score])
    csv_bytes = result.to_priority_csv()
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    rows = list(reader)

    expected_columns = {
        "Product Handle",
        "Vendor",
        "Title",
        "Barcode",
        "SKU",
        "Has Image",
        "Has Variant Images",
        "Has Description",
        "Has Product Type",
        "Has Tags",
        "Gaps",
        "Suggestions",
        "Priority Score",
        "Sessions",
        "Conversion Rate",
        "Online Revenue",
        "Opportunity Gap",
        "GMC Clicks",
        "GMC Impressions",
        "GMC CTR",
        "GMC Disapproved",
        "Discovery Gap",
        "Admin Link",
    }
    assert expected_columns == set(reader.fieldnames)
    assert len(rows) == 1
    assert rows[0]["Product Handle"] == "test-handle"
    assert rows[0]["Vendor"] == "TestVendor"


# ---------------------------------------------------------------------------
# ContentAuditor tests
# ---------------------------------------------------------------------------


def _make_mock_store(products, variants_by_pid, inventory_by_pid):
    """Build a mock LookoutStore returning the given data."""
    store = MagicMock()
    store.list_products.return_value = products
    store.get_variants.side_effect = lambda pid: variants_by_pid.get(pid, [])
    store.get_inventory.side_effect = lambda pid: inventory_by_pid.get(
        pid, {"total": 0, "value": 0.0, "full_price_value": 0.0, "by_location": {}}
    )
    return store


def test_content_auditor_finds_gaps():
    """ContentAuditor finds gaps using mocked LookoutStore."""
    products = [
        {
            "id": 10,
            "handle": "gap-product",
            "title": "Gap Product",
            "body_html": "",
            "vendor": "Acme",
            "product_type": "",
            "tags": "",
            "status": "active",
            "created_at": None,
        }
    ]
    variants = {
        10: [
            {
                "id": 100,
                "product_id": 10,
                "sku": "SKU1",
                "barcode": "111",
                "price": 50.0,
                "compare_at_price": None,
                "cost": 25.0,
                "image_src": "",
                "option1_name": "",
                "option1_value": "",
                "option2_name": "",
                "option2_value": "",
                "option3_name": "",
                "option3_value": "",
                "position": 1,
            }
        ]
    }
    inventory = {
        10: {"total": 5, "value": 125.0, "full_price_value": 125.0, "by_location": {}}
    }

    store = _make_mock_store(products, variants, inventory)
    auditor = ContentAuditor(store)
    result = auditor.audit()

    assert len(result.scores) == 1
    score = result.scores[0]
    assert score.gap_count > 0
    assert not score.has_description
    assert not score.has_product_type
    assert not score.has_tags


def test_content_auditor_vendor_filter():
    """ContentAuditor passes vendor filter through to store."""
    store = _make_mock_store([], {}, {})
    auditor = ContentAuditor(store)
    auditor.audit(vendor="Burton")

    store.list_products.assert_called_once_with(vendor="Burton", status="active")


def test_content_auditor_excludes_house_brands_by_default():
    """House brands are excluded from audit by default."""
    from lookout.taxonomy.mappings import EXCLUDED_VENDORS

    house_brand = EXCLUDED_VENDORS[0]
    products = [
        {
            "id": 20,
            "handle": "house-product",
            "title": "House Product",
            "body_html": "",
            "vendor": house_brand,
            "product_type": "",
            "tags": "",
            "status": "active",
            "created_at": None,
        },
        {
            "id": 21,
            "handle": "vendor-product",
            "title": "Vendor Product",
            "body_html": "",
            "vendor": "Patagonia",
            "product_type": "",
            "tags": "",
            "status": "active",
            "created_at": None,
        },
    ]
    variants = {
        20: [{"id": 200, "product_id": 20, "sku": "", "barcode": "", "price": 0, "compare_at_price": None, "cost": 0, "image_src": "", "option1_name": "", "option1_value": "", "option2_name": "", "option2_value": "", "option3_name": "", "option3_value": "", "position": 1}],
        21: [{"id": 210, "product_id": 21, "sku": "", "barcode": "", "price": 0, "compare_at_price": None, "cost": 0, "image_src": "", "option1_name": "", "option1_value": "", "option2_name": "", "option2_value": "", "option3_name": "", "option3_value": "", "position": 1}],
    }
    inventory = {
        20: {"total": 0, "value": 0.0, "full_price_value": 0.0, "by_location": {}},
        21: {"total": 0, "value": 0.0, "full_price_value": 0.0, "by_location": {}},
    }

    store = _make_mock_store(products, variants, inventory)
    auditor = ContentAuditor(store)
    result = auditor.audit()

    assert len(result.scores) == 1
    assert result.scores[0].handle == "vendor-product"


def test_content_auditor_include_house_brands():
    """House brands are included when exclude_house_brands=False."""
    from lookout.taxonomy.mappings import EXCLUDED_VENDORS

    house_brand = EXCLUDED_VENDORS[0]
    products = [
        {
            "id": 30,
            "handle": "house-product",
            "title": "House Product",
            "body_html": "",
            "vendor": house_brand,
            "product_type": "",
            "tags": "",
            "status": "active",
            "created_at": None,
        },
    ]
    variants = {
        30: [{"id": 300, "product_id": 30, "sku": "", "barcode": "", "price": 0, "compare_at_price": None, "cost": 0, "image_src": "", "option1_name": "", "option1_value": "", "option2_name": "", "option2_value": "", "option3_name": "", "option3_value": "", "position": 1}],
    }
    inventory = {
        30: {"total": 0, "value": 0.0, "full_price_value": 0.0, "by_location": {}},
    }

    store = _make_mock_store(products, variants, inventory)
    auditor = ContentAuditor(store, exclude_house_brands=False)
    result = auditor.audit()

    assert len(result.scores) == 1
    assert result.scores[0].handle == "house-product"


# ---------------------------------------------------------------------------
# GMC offer_id format tests
# ---------------------------------------------------------------------------


def test_extract_product_id_shopify_us_3part():
    """Standard Shopify Google channel format: shopify_US_{variant_id}."""
    from lookout.audit.auditor import _extract_product_id

    store = MagicMock()
    store.get_variant_by_id.return_value = {"product_id": 9999, "id": 55555}
    cache: dict[int, int] = {}

    pid = _extract_product_id("shopify_US_55555", cache, store)
    assert pid == 9999
    store.get_variant_by_id.assert_called_once_with(55555)
    # Second call should use cache, not hit store again
    store.get_variant_by_id.reset_mock()
    pid2 = _extract_product_id("shopify_US_55555", cache, store)
    assert pid2 == 9999
    store.get_variant_by_id.assert_not_called()


def test_extract_product_id_4part_legacy():
    """Legacy 4-part format: shopify_US_{product_id}_{variant_id}."""
    from lookout.audit.auditor import _extract_product_id

    store = MagicMock()
    cache: dict[int, int] = {}

    pid = _extract_product_id("shopify_US_9999_55555", cache, store)
    assert pid == 9999
    # Should not need store lookup for 4-part format
    store.get_variant_by_id.assert_not_called()


def test_extract_product_id_case_insensitive():
    """The 'shopify' prefix should match case-insensitively."""
    from lookout.audit.auditor import _extract_product_id

    store = MagicMock()
    store.get_variant_by_id.return_value = {"product_id": 1234, "id": 5678}
    cache: dict[int, int] = {}

    pid = _extract_product_id("Shopify_us_5678", cache, store)
    assert pid == 1234


def test_extract_product_id_unknown_format():
    """Unrecognised offer_id formats return None."""
    from lookout.audit.auditor import _extract_product_id

    store = MagicMock()
    cache: dict[int, int] = {}

    assert _extract_product_id("random_string", cache, store) is None
    assert _extract_product_id("12345", cache, store) is None
    assert _extract_product_id("", cache, store) is None


def test_extract_product_id_variant_not_in_store():
    """When variant_id isn't in the store, return None."""
    from lookout.audit.auditor import _extract_product_id

    store = MagicMock()
    store.get_variant_by_id.return_value = None
    cache: dict[int, int] = {}

    pid = _extract_product_id("shopify_US_99999", cache, store)
    assert pid is None


def test_auditor_aggregates_gmc_by_product():
    """ContentAuditor correctly aggregates GMC signals from 3-part offer_ids."""
    from lookout.audit.gmc_signals import GMCSignals

    products = [
        {
            "id": 10,
            "handle": "test-product",
            "title": "Test Product",
            "body_html": "<p>A description that is long enough to pass.</p>" * 3,
            "vendor": "Acme",
            "product_type": "Jacket",
            "tags": "outdoor",
            "status": "active",
            "created_at": None,
        }
    ]
    variants = {
        10: [
            {"id": 100, "product_id": 10, "sku": "SKU1", "barcode": "111",
             "price": 50.0, "compare_at_price": None, "cost": 25.0, "image_src": "img.jpg",
             "option1_name": "Color", "option1_value": "Red",
             "option2_name": "", "option2_value": "",
             "option3_name": "", "option3_value": "", "position": 1},
            {"id": 200, "product_id": 10, "sku": "SKU2", "barcode": "222",
             "price": 50.0, "compare_at_price": None, "cost": 25.0, "image_src": "img2.jpg",
             "option1_name": "Color", "option1_value": "Blue",
             "option2_name": "", "option2_value": "",
             "option3_name": "", "option3_value": "", "position": 2},
        ]
    }
    inventory = {
        10: {"total": 5, "value": 125.0, "full_price_value": 125.0, "by_location": {}}
    }

    store = _make_mock_store(products, variants, inventory)
    # Mock get_variant_by_id to return the variant dicts
    def _get_variant_by_id(vid):
        for v_list in variants.values():
            for v in v_list:
                if v["id"] == vid:
                    return v
        return None
    store.get_variant_by_id = _get_variant_by_id

    gmc = {
        "shopify_US_100": GMCSignals(offer_id="shopify_US_100", clicks=10, impressions=500),
        "shopify_US_200": GMCSignals(offer_id="shopify_US_200", clicks=5, impressions=300),
    }

    auditor = ContentAuditor(store, gmc_signals=gmc)
    # The two variant signals should be aggregated to product_id 10
    assert 10 in auditor._gmc_by_product
    agg = auditor._gmc_by_product[10]
    assert agg.clicks == 15
    assert agg.impressions == 800
