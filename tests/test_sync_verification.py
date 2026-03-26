"""Verify TVR database is in sync with live Shopify API.

These tests require:
- TVR database populated at ~/The-Variant-Range/tvr/db/shopify.db
- Shopify API credentials at ~/.tvr/shopify/config.json

Skip automatically if either is unavailable.
"""

import json
from pathlib import Path

import pytest

# Skip entire module if prerequisites aren't met
DB_PATH = Path.home() / "The-Variant-Range" / "tvr" / "db" / "shopify.db"
CREDS_PATH = Path.home() / ".tvr" / "shopify" / "config.json"

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not CREDS_PATH.exists(),
    reason="TVR database or Shopify credentials not available",
)


@pytest.fixture(scope="module")
def shopify_client():
    """Create a ShopifyClient from saved credentials."""
    from tvr.services.shopify_client import ShopifyClient, ShopifyCredentials

    with open(CREDS_PATH) as f:
        config = json.load(f)

    creds = ShopifyCredentials(
        store_url=config["store_url"],
        access_token=config["access_token"],
    )
    return ShopifyClient(creds)


@pytest.fixture(scope="module")
def store():
    """Create a LookoutStore connected to the real database."""
    from lookout.store import LookoutStore

    return LookoutStore(db_url=str(DB_PATH))


def test_total_product_count_matches(shopify_client, store):
    """Total active product count in DB should match Shopify API."""
    api_count = shopify_client.get_product_count(status="active")
    db_count = len(store.list_products())
    drift_pct = abs(api_count - db_count) / api_count * 100 if api_count > 0 else 0

    assert drift_pct < 1, (
        f"Product count drift: DB={db_count}, API={api_count} ({drift_pct:.1f}%). "
        f"Run a TVR sync to update."
    )


def test_vendor_counts_match(shopify_client, store):
    """Spot-check vendor counts match between DB and API."""
    vendors_to_check = ["Patagonia", "Black Diamond", "Smith Optics", "Vuori"]
    mismatches = []

    for vendor in vendors_to_check:
        api_count = shopify_client.get_product_count(vendor=vendor, status="active")
        db_count = len(store.list_products(vendor=vendor))

        if db_count != api_count:
            mismatches.append(f"{vendor}: DB={db_count}, API={api_count}")

    assert not mismatches, f"Vendor count mismatches: {'; '.join(mismatches)}"


def test_db_has_products(store):
    """Database should have a reasonable number of products."""
    products = store.list_products()
    assert len(products) > 1000, f"Only {len(products)} products — expected 10,000+"


def test_db_has_vendors(store):
    """Database should have vendors we know about."""
    vendors = store.list_vendors()
    expected = {"Patagonia", "Black Diamond", "Smith Optics"}
    missing = expected - set(vendors)
    assert not missing, f"Missing vendors: {missing}"


def test_products_have_variants(store):
    """Products should have variants — spot check a few."""
    products = store.list_products(vendor="Patagonia", limit=5)
    assert len(products) > 0, "No Patagonia products found"

    for product in products[:3]:
        variants = store.get_variants(product["id"])
        assert len(variants) > 0, f"Product {product['handle']} has no variants"
