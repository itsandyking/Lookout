from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from lookout.ranking.ranker import CollectionRanker, RankingResult


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.list_products.return_value = [
        {"id": 1, "handle": "fast-seller", "title": "Fast Seller", "vendor": "Patagonia",
         "product_type": "Jacket", "tags": "", "body_html": "", "status": "active",
         "created_at": datetime(2026, 3, 1, tzinfo=UTC)},
        {"id": 2, "handle": "slow-mover", "title": "Slow Mover", "vendor": "Burton",
         "product_type": "Jacket", "tags": "", "body_html": "", "status": "active",
         "created_at": datetime(2025, 6, 1, tzinfo=UTC)},
    ]
    store.get_variants.side_effect = lambda pid: [
        {"id": pid * 100, "product_id": pid, "sku": f"SKU-{pid}", "price": 200.0,
         "cost": 100.0, "compare_at_price": None, "barcode": "", "position": 1,
         "option1_name": "", "option1_value": "", "option2_name": "", "option2_value": "",
         "option3_name": "", "option3_value": "", "image_src": ""},
    ]
    store.get_inventory.side_effect = lambda pid: {
        "total": 10 if pid == 1 else 50,
        "value": 1000.0 if pid == 1 else 5000.0,
        "full_price_value": 1000.0,
        "by_location": {},
    }
    store.get_sales_velocity.side_effect = lambda pid, days=28: {
        "units": 8 if pid == 1 else 1,
        "weekly_avg": 2.0 if pid == 1 else 0.25,
    }
    store.get_collection_products.return_value = []
    return store


def test_rank_by_vendor(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank(vendor="Patagonia")
    assert isinstance(result, RankingResult)
    assert len(result.products) == 2


def test_fast_seller_ranks_higher(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank()
    ranked = result.ranked
    assert ranked[0].handle == "fast-seller"


def test_pin_override(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank(overrides={"slow-mover": {"pin": 1}})
    ranked = result.ranked
    assert ranked[0].handle == "slow-mover"


def test_bury_override(mock_store):
    ranker = CollectionRanker(mock_store)
    result = ranker.rank(overrides={"fast-seller": {"bury": True}})
    ranked = result.ranked
    assert ranked[-1].handle == "fast-seller"
