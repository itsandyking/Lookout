"""Tests for LookoutStore — mocks TVR's ShopifyStore."""
from unittest.mock import MagicMock, patch

import pytest

from lookout.store import LookoutStore


@pytest.fixture
def mock_tvr_store():
    """Create a mock ShopifyStore."""
    store = MagicMock()
    store.list_vendors.return_value = ["Patagonia", "Altra", "Burton"]
    store.list_product_types.return_value = ["Jacket", "Shoe", "Backpack"]
    store.list_collections.return_value = [
        {"id": 1, "title": "Winter Jackets", "handle": "winter-jackets", "product_count": 15},
    ]
    return store


@pytest.fixture
def lookout_store(mock_tvr_store):
    """Create a LookoutStore with mocked TVR."""
    with patch("tvr.db.store.ShopifyStore", return_value=mock_tvr_store):
        return LookoutStore()


def test_list_vendors(lookout_store):
    vendors = lookout_store.list_vendors()
    assert "Patagonia" in vendors
    assert isinstance(vendors, list)


def test_list_product_types(lookout_store):
    types = lookout_store.list_product_types()
    assert "Jacket" in types


def test_list_collections(lookout_store):
    collections = lookout_store.list_collections()
    assert len(collections) == 1
    assert collections[0]["handle"] == "winter-jackets"


def test_get_product_not_found(lookout_store, mock_tvr_store):
    mock_tvr_store.search_products.return_value = []
    result = lookout_store.get_product("nonexistent-handle")
    assert result is None


def test_list_vendors_returns_strings(lookout_store):
    vendors = lookout_store.list_vendors()
    assert all(isinstance(v, str) for v in vendors)
