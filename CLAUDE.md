# CLAUDE.md — Lookout

## TVR Data Access

Lookout depends on TVR as a git dependency (`pip install the-variant-range`). All TVR access is channeled through `lookout/store.py`.

**TVR imports used:**
- `tvr.db.store.ShopifyStore` — Shopify product/variant data (Dolt `shopify` database)
- `tvr.db.vendor_store.VendorStore` — Vendor catalogs (Dolt `vendors` database)
- `tvr.db.models.Product`, `tvr.db.models.Variant` — SQLAlchemy ORM models
- `tvr.mcp_report.shopify_api.ShopifyQLClient` — ShopifyQL queries
- `tvr.mcp.auth.ShopifyAuth`, `tvr.mcp.api.ShopifyAdminAPI` — Shopify GraphQL API

**Dolt server:** Pi5 `100.122.28.91:3306` (Tailscale). Config loaded via `tvr.db.dolt_config.load_dolt_config()`.

For the full schema contract (stable tables, column types, data quality notes), see:
https://github.com/itsandyking/The-Variant-Range/blob/main/SCHEMA.md

## Bug Fixing

When fixing a bug, if your first two attempts don not resolve it, STOP. Explain the root cause, list 2-3 alternatives, and wait for direction.
