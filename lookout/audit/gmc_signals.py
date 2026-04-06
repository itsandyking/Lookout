"""Google Merchant Center signals for audit priority scoring.

Fetches per-product performance (clicks, impressions, CTR) and
product status (disapprovals, issues) from the Merchant API.

Requires the `https://www.googleapis.com/auth/content` OAuth scope.
On first run, triggers browser-based re-authentication to add it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CREDENTIALS_DIR = Path.home() / ".tvr" / "google"
CREDENTIALS_PATH = CREDENTIALS_DIR / "credentials.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"

# All scopes needed (existing + merchant)
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/content",
]


@dataclass
class GMCSignals:
    """Google Merchant Center signals for a product."""

    offer_id: str
    title: str = ""
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    conversions: float = 0.0
    disapproved: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def discovery_gap(self) -> float:
        """High impressions + low CTR = listing content opportunity.

        Returns 0-1 where higher means more opportunity.
        """
        if self.impressions == 0:
            return 0.0
        return (1.0 - self.ctr) * min(self.impressions / 1000, 1.0)


def _get_credentials():
    """Get or refresh OAuth credentials with Merchant API scope."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    needs_reauth = False
    if creds and creds.valid:
        required = set(SCOPES)
        existing = set(creds.scopes or [])
        if not required.issubset(existing):
            missing = required - existing
            logger.info("Token missing scopes %s, re-authenticating", missing)
            needs_reauth = True
    elif creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            needs_reauth = True
    else:
        needs_reauth = True

    if needs_reauth:
        if not CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"OAuth credentials not found: {CREDENTIALS_PATH}\n"
                "Download from Google Cloud Console → APIs & Services → Credentials"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
        try:
            creds = flow.run_local_server(port=0)
        except Exception:
            logger.info("Local server failed, falling back to console flow")
            creds = flow.run_console()

        TOKEN_PATH.write_text(creds.to_json())
        TOKEN_PATH.chmod(0o600)
        logger.info("Token saved with Merchant API scope")

    return creds


DEFAULT_MERCHANT_ID = os.environ.get("GMC_MERCHANT_ID", "195688505")


def _get_merchant_id(service) -> str:
    """Get the Merchant Center account ID.

    Uses GMC_MERCHANT_ID env var or hardcoded default. Falls back to
    auto-detection via API if needed.
    """
    if DEFAULT_MERCHANT_ID:
        logger.info("Using Merchant Center account: %s (from config)", DEFAULT_MERCHANT_ID)
        return DEFAULT_MERCHANT_ID

    result = service.accounts().list(pageSize=1).execute()
    accounts = result.get("accounts", [])
    if not accounts:
        raise ValueError("No Merchant Center accounts found for this Google account")
    account_name = accounts[0].get("name", "")
    # name format: "accounts/123456789"
    merchant_id = account_name.split("/")[-1]
    logger.info("Using Merchant Center account: %s (auto-detected)", merchant_id)
    return merchant_id


def fetch_gmc_performance(
    lookback_days: int = 90,
    merchant_id: str | None = None,
) -> dict[str, GMCSignals]:
    """Fetch product performance from Google Merchant Center.

    Returns dict mapping offer_id → GMCSignals with clicks,
    impressions, CTR, and conversions.
    """
    from googleapiclient.discovery import build

    creds = _get_credentials()
    service = build("merchantapi", "reports_v1", credentials=creds)

    if not merchant_id:
        accounts_service = build("merchantapi", "accounts_v1", credentials=creds)
        merchant_id = _get_merchant_id(accounts_service)

    parent = f"accounts/{merchant_id}"

    # Map lookback to nearest GMC date literal
    if lookback_days > 30:
        logger.warning("GMC only supports up to 30 days lookback; using LAST_30_DAYS")
    if lookback_days <= 7:
        date_range = "LAST_7_DAYS"
    elif lookback_days <= 14:
        date_range = "LAST_14_DAYS"
    else:
        date_range = "LAST_30_DAYS"

    query = (
        "SELECT "
        "product_performance_view.offer_id, "
        "product_performance_view.title, "
        "product_performance_view.clicks, "
        "product_performance_view.impressions, "
        "product_performance_view.click_through_rate, "
        "product_performance_view.conversions "
        "FROM product_performance_view "
        f"WHERE date DURING {date_range}"
    )

    signals: dict[str, GMCSignals] = {}

    try:
        request = (
            service.accounts()
            .reports()
            .search(
                parent=parent,
                body={"query": query, "pageSize": 1000},
            )
        )

        while request:
            response = request.execute()
            for row in response.get("results", []):
                ppv = row.get("productPerformanceView", {})
                offer_id = ppv.get("offerId", "")
                if not offer_id:
                    continue

                clicks = int(ppv.get("clicks", 0) or 0)
                impressions = int(ppv.get("impressions", 0) or 0)

                if offer_id in signals:
                    # Aggregate across dates
                    signals[offer_id].clicks += clicks
                    signals[offer_id].impressions += impressions
                else:
                    signals[offer_id] = GMCSignals(
                        offer_id=offer_id,
                        title=ppv.get("title", ""),
                        clicks=clicks,
                        impressions=impressions,
                        ctr=float(ppv.get("clickThroughRate", 0) or 0),
                        conversions=float(ppv.get("conversions", 0) or 0),
                    )

            request = service.accounts().reports().search_next(request, response)

    except Exception as e:
        logger.error("GMC performance query failed: %s", e)
        return {}

    # Recalculate CTR from aggregated totals
    for sig in signals.values():
        sig.ctr = sig.clicks / sig.impressions if sig.impressions > 0 else 0.0

    logger.info("GMC performance: %d products with data", len(signals))
    return signals


def fetch_gmc_product_status(
    merchant_id: str | None = None,
) -> dict[str, GMCSignals]:
    """Fetch product disapproval status from Google Merchant Center.

    Returns dict mapping offer_id → GMCSignals with disapproval
    and issue information.
    """
    from googleapiclient.discovery import build

    creds = _get_credentials()
    service = build("merchantapi", "reports_v1", credentials=creds)

    if not merchant_id:
        accounts_service = build("merchantapi", "accounts_v1", credentials=creds)
        merchant_id = _get_merchant_id(accounts_service)

    parent = f"accounts/{merchant_id}"

    query = (
        "SELECT "
        "product_view.id, "
        "product_view.offer_id, "
        "product_view.title, "
        "product_view.aggregated_reporting_context_status, "
        "product_view.item_issues "
        "FROM product_view"
    )

    signals: dict[str, GMCSignals] = {}

    try:
        request = (
            service.accounts()
            .reports()
            .search(
                parent=parent,
                body={"query": query, "pageSize": 1000},
            )
        )

        while request:
            response = request.execute()
            for row in response.get("results", []):
                pv = row.get("productView", {})
                offer_id = pv.get("offerId", "")
                if not offer_id:
                    continue

                status = pv.get("aggregatedReportingContextStatus", "")
                issues = []
                for issue in pv.get("itemIssues", []):
                    severity = issue.get("severity", {}).get("severityPerReportingContext", [])
                    for ctx in severity:
                        if ctx.get("disapprovedCountries"):
                            issues.append(issue.get("resolution", "unknown"))

                signals[offer_id] = GMCSignals(
                    offer_id=offer_id,
                    title=pv.get("title", ""),
                    disapproved="DISAPPROVED" in status.upper() if status else False,
                    issues=issues,
                )

            request = service.accounts().reports().search_next(request, response)

    except Exception as e:
        logger.error("GMC product status query failed: %s", e)
        return {}

    disapproved_count = sum(1 for s in signals.values() if s.disapproved)
    logger.info(
        "GMC product status: %d products, %d disapproved",
        len(signals),
        disapproved_count,
    )
    return signals


def fetch_all_gmc_signals(
    lookback_days: int = 90,
    merchant_id: str | None = None,
) -> dict[str, GMCSignals]:
    """Fetch both performance and status, merged by offer_id."""
    performance = fetch_gmc_performance(lookback_days, merchant_id)
    status = fetch_gmc_product_status(merchant_id)

    # Merge status into performance
    for offer_id, status_sig in status.items():
        if offer_id in performance:
            performance[offer_id].disapproved = status_sig.disapproved
            performance[offer_id].issues = status_sig.issues
        else:
            performance[offer_id] = status_sig

    logger.info("GMC total: %d products with signals", len(performance))
    return performance
