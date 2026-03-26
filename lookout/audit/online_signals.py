"""Online opportunity signals via ShopifyQL.

Queries Shopify's analytics for per-product online metrics:
sessions, conversion rate, and online revenue.
These feed the opportunity-based audit priority scoring.

Sessions are keyed by landing_page_path (/products/{handle}),
sales are keyed by product_title. The auditor joins both to
ProductScore by title.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class OnlineSignals:
    """Online performance signals for a single product."""

    product_title: str
    sessions: int = 0
    conversion_rate: float = 0.0
    online_revenue: float = 0.0
    orders: int = 0
    quantity_ordered: int = 0

    @property
    def opportunity_gap(self) -> float:
        """High sessions + low conversion = content opportunity.

        Returns a 0-1 score where higher means more opportunity.
        Products with many sessions but low conversion are the
        highest opportunity for content improvement.
        """
        if self.sessions == 0:
            return 0.0
        return (1.0 - self.conversion_rate) * min(self.sessions / 100, 1.0)


async def _fetch_product_sessions(client, lookback_days: int = 90) -> dict[str, dict]:
    """Fetch session and conversion data per product handle from ShopifyQL.

    Uses landing_page_path grouped by /products/* pages, extracts handle
    from the URL path.
    """
    # ShopifyQL doesn't support LIKE — fetch all landing pages and filter in Python
    query = (
        f"FROM sessions "
        f"SHOW sessions, conversion_rate "
        f"GROUP BY landing_page_path "
        f"SINCE -{lookback_days}d "
        f"ORDER BY sessions DESC "
        f"LIMIT 1000"
    )
    result = await client.execute_shopifyql(query)

    if result.get("error"):
        logger.error("ShopifyQL sessions query failed: %s", result["error"])
        return {}

    # Key by handle extracted from /products/{handle}
    signals: dict[str, dict] = {}
    for row in result.get("rows", []):
        path = _get_val(row, result.get("columns", []), "landing_page_path")
        if not path or not path.startswith("/products/"):
            continue
        handle = path.split("/products/", 1)[1].split("?")[0].split("#")[0].strip("/")
        if not handle:
            continue
        signals[handle] = {
            "sessions": _parse_int(_get_val(row, result["columns"], "sessions")),
            "conversion_rate": _parse_float(_get_val(row, result["columns"], "conversion_rate")),
        }

    logger.info("Sessions data: %d product handles", len(signals))
    return signals


async def _fetch_product_online_sales(client, lookback_days: int = 90) -> dict[str, dict]:
    """Fetch online sales data per product title from ShopifyQL."""
    query = (
        f"FROM sales "
        f"SHOW total_sales, orders, quantity_ordered "
        f"WHERE sales_channel = 'Online Store' "
        f"GROUP BY product_title "
        f"SINCE -{lookback_days}d "
        f"ORDER BY total_sales DESC "
        f"LIMIT 500"
    )
    result = await client.execute_shopifyql(query)

    if result.get("error"):
        logger.error("ShopifyQL sales query failed: %s", result["error"])
        return {}

    # Key by product title
    signals: dict[str, dict] = {}
    for row in result.get("rows", []):
        title = _get_val(row, result.get("columns", []), "product_title")
        if not title:
            continue
        signals[title] = {
            "online_revenue": _parse_float(_get_val(row, result["columns"], "total_sales")),
            "orders": _parse_int(_get_val(row, result["columns"], "orders")),
            "quantity_ordered": _parse_int(_get_val(row, result["columns"], "quantity_ordered")),
        }

    logger.info("Online sales data: %d product titles", len(signals))
    return signals


async def fetch_online_signals(
    client,
    title_to_handle: dict[str, str] | None = None,
    lookback_days: int = 90,
) -> dict[str, OnlineSignals]:
    """Fetch all online signals, keyed by product title.

    Runs two ShopifyQL queries in parallel:
    - Sessions by landing_page_path (keyed by handle)
    - Online sales by product_title (keyed by title)

    Merges both into OnlineSignals objects keyed by product title,
    using title_to_handle mapping to join session data.

    Args:
        client: ShopifyQLClient instance.
        title_to_handle: Dict mapping product title → handle (from store).
        lookback_days: Number of days to look back.

    Returns:
        Dict mapping product_title → OnlineSignals.
    """
    title_to_handle = title_to_handle or {}
    handle_to_title = {h: t for t, h in title_to_handle.items()}

    sessions_by_handle, sales_by_title = await asyncio.gather(
        _fetch_product_sessions(client, lookback_days),
        _fetch_product_online_sales(client, lookback_days),
    )

    # Build signals keyed by title
    signals: dict[str, OnlineSignals] = {}

    # Start with sales data (already keyed by title)
    for title, sales in sales_by_title.items():
        signals[title] = OnlineSignals(
            product_title=title,
            online_revenue=sales.get("online_revenue", 0.0),
            orders=sales.get("orders", 0),
            quantity_ordered=sales.get("quantity_ordered", 0),
        )

    # Merge session data (keyed by handle → title)
    matched_sessions = 0
    for handle, sess in sessions_by_handle.items():
        title = handle_to_title.get(handle)
        if not title:
            continue
        matched_sessions += 1
        if title in signals:
            signals[title].sessions = sess.get("sessions", 0)
            signals[title].conversion_rate = sess.get("conversion_rate", 0.0)
        else:
            signals[title] = OnlineSignals(
                product_title=title,
                sessions=sess.get("sessions", 0),
                conversion_rate=sess.get("conversion_rate", 0.0),
            )

    logger.info(
        "Online signals: %d products total (%d with sessions, %d with sales, %d session-handle matches)",
        len(signals), matched_sessions, len(sales_by_title), matched_sessions,
    )
    return signals


def _get_val(row: dict | list, columns: list[str], col_name: str):
    """Extract a value from a ShopifyQL row (handles both dict and list rows)."""
    if isinstance(row, dict):
        return row.get(col_name)
    if isinstance(row, list):
        try:
            idx = columns.index(col_name)
            return row[idx]
        except (ValueError, IndexError):
            return None
    return None


def _parse_float(val) -> float:
    """Safely parse a float from ShopifyQL response values."""
    if val is None:
        return 0.0
    try:
        cleaned = str(val).replace("$", "").replace(",", "").replace("%", "").strip()
        return float(cleaned) if cleaned else 0.0
    except (ValueError, TypeError):
        return 0.0


def _parse_int(val) -> int:
    """Safely parse an int from ShopifyQL response values."""
    return int(_parse_float(val))
