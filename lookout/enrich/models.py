"""
Data models for the Merchfill pipeline.

This module defines all Pydantic schemas used throughout the pipeline:
- Input CSV row models
- Vendor configuration models
- Extraction models (facts, images)
- Merchandising output models
- Shopify CSV row models
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ProcessingStatus(str, Enum):
    """Status of a product row processing."""

    UPDATED = "UPDATED"
    SKIPPED = "SKIPPED"
    NO_MATCH = "NO_MATCH"
    FAILED = "FAILED"
    SKIPPED_VENDOR_NOT_CONFIGURED = "SKIPPED_VENDOR_NOT_CONFIGURED"
    SKIPPED_NO_GAPS = "SKIPPED_NO_GAPS"


# -----------------------------------------------------------------------------
# Input Models
# -----------------------------------------------------------------------------


class VariantInfo(BaseModel):
    """Rich variant data from TVR for smarter enrichment."""

    variant_id: int = 0
    sku: str = ""
    barcode: str = ""
    color: str = ""  # option value when option name is Color/Style
    size: str = ""  # option value when option name is Size
    price: float = 0.0
    image_src: str = ""  # existing variant image (empty = needs assignment)
    catalog_image: str = ""  # image from vendor catalog (if available)

    model_config = {"populate_by_name": True}


class InputRow(BaseModel):
    """Represents a row from the merchandising priority CSV input."""

    product_handle: str = Field(..., alias="Product Handle")
    vendor: str = Field(..., alias="Vendor")
    has_image: bool = Field(..., alias="Has Image")
    has_variant_images: bool = Field(..., alias="Has Variant Images")
    has_description: bool = Field(..., alias="Has Description")
    has_product_type: bool = Field(True, alias="Has Product Type")
    has_tags: bool = Field(True, alias="Has Tags")
    gaps: str = Field("", alias="Gaps")
    admin_link: str | None = Field(None, alias="Admin Link")
    priority_score: float | None = Field(None, alias="Priority Score")
    suggestions: str | None = Field(None, alias="Suggestions")
    # Optional fields for better search matching
    title: str | None = Field(None, alias="Title")
    barcode: str | None = Field(None, alias="Barcode")
    sku: str | None = Field(None, alias="SKU")
    # Rich variant data (populated when running via internal audit, not from CSV)
    variant_data: list[VariantInfo] = Field(default_factory=list, exclude=True)

    model_config = {"populate_by_name": True}

    @property
    def all_barcodes(self) -> list[str]:
        """All barcodes across variants."""
        barcodes = [v.barcode for v in self.variant_data if v.barcode]
        if not barcodes and self.barcode:
            barcodes = [self.barcode]
        return barcodes

    @property
    def all_skus(self) -> list[str]:
        """All SKUs across variants."""
        skus = [v.sku for v in self.variant_data if v.sku]
        if not skus and self.sku:
            skus = [self.sku]
        return skus

    @property
    def known_colors(self) -> list[str]:
        """Color option values from variant data."""
        return list(dict.fromkeys(v.color for v in self.variant_data if v.color))

    @property
    def catalog_images_by_color(self) -> dict[str, str]:
        """Color→catalog image mapping from vendor catalog data."""
        return {
            v.color: v.catalog_image
            for v in self.variant_data
            if v.color and v.catalog_image
        }

    @field_validator(
        "has_image",
        "has_variant_images",
        "has_description",
        "has_product_type",
        "has_tags",
        mode="before",
    )
    @classmethod
    def parse_boolean(cls, v: Any) -> bool:
        """Parse various boolean representations from CSV."""
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            v_lower = v.lower().strip()
            if v_lower in ("true", "yes", "1", "y", "t"):
                return True
            if v_lower in ("false", "no", "0", "n", "f", ""):
                return False
        if isinstance(v, (int, float)):
            return bool(v)
        return False

    @property
    def needs_description(self) -> bool:
        """Check if description needs to be generated."""
        return not self.has_description

    @property
    def needs_images(self) -> bool:
        """Check if product images need to be added."""
        return not self.has_image

    @property
    def needs_variant_images(self) -> bool:
        """Check if variant images need to be assigned."""
        return not self.has_variant_images

    @property
    def has_any_gap(self) -> bool:
        """Check if any content gap exists."""
        return self.needs_description or self.needs_images or self.needs_variant_images


# -----------------------------------------------------------------------------
# Vendor Configuration Models
# -----------------------------------------------------------------------------


class PlaywrightConfig(BaseModel):
    """Configuration for Playwright-based scraping."""

    wait_for_selector: str | None = None
    wait_timeout_ms: int = 5000
    extra_wait_ms: int = 0


class SearchConfig(BaseModel):
    """Configuration for product URL search."""

    method: str = "site_search"
    query_template: str = "site:{domain} {query}"


class SelectorsConfig(BaseModel):
    """CSS selectors for content extraction."""

    product_name: str | None = None
    description: str | None = None
    features: str | None = None
    specs: str | None = None
    images: str | None = None
    price: str | None = None
    content_area: str | None = None


class VendorConfig(BaseModel):
    """Configuration for a single vendor."""

    domain: str
    is_shopify: bool = False
    fallback_domains: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)
    product_url_patterns: list[str] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)


class ConfidenceSettings(BaseModel):
    """Confidence threshold settings."""

    auto_proceed: int = 85
    warn_threshold: int = 70
    reject_threshold: int = 70


class RateLimitSettings(BaseModel):
    """Rate limiting settings."""

    min_delay_ms: int = 500
    max_delay_ms: int = 2000
    max_concurrent_per_domain: int = 2


class RetrySettings(BaseModel):
    """Retry settings."""

    max_attempts: int = 3
    backoff_base_ms: int = 1000
    backoff_max_ms: int = 30000


class TimeoutSettings(BaseModel):
    """Timeout settings."""

    request_timeout_ms: int = 30000
    page_load_timeout_ms: int = 60000


class GlobalSettings(BaseModel):
    """Global pipeline settings."""

    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
    rate_limiting: RateLimitSettings = Field(default_factory=RateLimitSettings)
    retries: RetrySettings = Field(default_factory=RetrySettings)
    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)


class VendorsConfig(BaseModel):
    """Full vendors configuration file model."""

    vendors: dict[str, VendorConfig] = Field(default_factory=dict)
    settings: GlobalSettings = Field(default_factory=GlobalSettings)


# -----------------------------------------------------------------------------
# Resolver Models
# -----------------------------------------------------------------------------


class URLCandidate(BaseModel):
    """A candidate URL from the resolver."""

    url: str
    confidence: int = Field(ge=0, le=100)
    reasoning: str = ""
    title: str = ""
    snippet: str = ""


class ResolverOutput(BaseModel):
    """Output from the URL resolver."""

    handle: str
    vendor: str
    query_used: str
    candidates: list[URLCandidate] = Field(default_factory=list)
    selected_url: str | None = None
    selected_confidence: int = 0
    warnings: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# -----------------------------------------------------------------------------
# Extraction Models
# -----------------------------------------------------------------------------


class ImageInfo(BaseModel):
    """Information about an extracted image."""

    url: str
    inferred_view: str | None = None
    source_hint: str = ""
    alt_text: str = ""
    width: int | None = None
    height: int | None = None


class VariantOption(BaseModel):
    """A product variant option."""

    option_name: str
    values: list[str] = Field(default_factory=list)


class ExtractedFacts(BaseModel):
    """
    Structured facts extracted from a vendor product page.

    This schema is used for the two-pass extraction:
    1. Deterministic parsing from HTML/JSON-LD
    2. LLM-assisted structuring (must not invent facts)
    """

    canonical_url: str
    product_name: str = ""
    brand: str = ""
    description_blocks: list[str] = Field(default_factory=list)
    feature_bullets: list[str] = Field(default_factory=list)
    specs: dict[str, str] = Field(default_factory=dict)
    materials: str = ""
    care: str = ""
    fit_dimensions: str = ""
    images: list[ImageInfo] = Field(default_factory=list)
    variants: list[VariantOption] = Field(default_factory=list)
    variant_image_candidates: dict[str, list[str]] = Field(default_factory=dict)
    json_ld_data: dict[str, Any] | None = None
    evidence_snippets: dict[str, str] = Field(default_factory=dict)
    extraction_warnings: list[str] = Field(default_factory=list)


class SourceText(BaseModel):
    """Extracted visible text blocks from a page."""

    visible_text_blocks: list[str] = Field(default_factory=list)
    bullet_lists: list[list[str]] = Field(default_factory=list)
    spec_tables: list[dict[str, str]] = Field(default_factory=list)
    json_ld_products: list[dict[str, Any]] = Field(default_factory=list)
    meta_description: str = ""
    page_title: str = ""


# -----------------------------------------------------------------------------
# Merchandising Output Models
# -----------------------------------------------------------------------------


class OutputImage(BaseModel):
    """An image to be included in Shopify output."""

    src: str
    position: int
    alt: str = ""


class MerchOutput(BaseModel):
    """
    Final merchandising output for a product.

    This is what gets written to merch_output.json and used
    to generate the Shopify CSV.
    """

    handle: str
    body_html: str | None = None
    images: list[OutputImage] = Field(default_factory=list)
    variant_image_map: dict[str, str | list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    gmc_flags: list[str] = Field(default_factory=list)
    confidence: int = Field(ge=0, le=100, default=0)
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# -----------------------------------------------------------------------------
# Shopify CSV Models
# -----------------------------------------------------------------------------


class ShopifyProductRow(BaseModel):
    """
    A row in the Shopify product import CSV.

    This represents the standard Shopify CSV format for product imports.
    Not all fields are required; we only populate what we're updating.
    """

    Handle: str
    Title: str = ""
    Body_HTML: str = Field("", alias="Body (HTML)")
    Vendor: str = ""
    Type: str = ""
    Tags: str = ""
    Published: str = ""
    Option1_Name: str = Field("", alias="Option1 Name")
    Option1_Value: str = Field("", alias="Option1 Value")
    Option2_Name: str = Field("", alias="Option2 Name")
    Option2_Value: str = Field("", alias="Option2 Value")
    Option3_Name: str = Field("", alias="Option3 Name")
    Option3_Value: str = Field("", alias="Option3 Value")
    Variant_SKU: str = Field("", alias="Variant SKU")
    Variant_Price: str = Field("", alias="Variant Price")
    Variant_Image: str = Field("", alias="Variant Image")
    Image_Src: str = Field("", alias="Image Src")
    Image_Position: str = Field("", alias="Image Position")
    Image_Alt_Text: str = Field("", alias="Image Alt Text")

    model_config = {"populate_by_name": True}


class VariantImageAssignment(BaseModel):
    """
    A variant image assignment record.

    Used for variant_image_assignments.csv output.
    Includes SKU and Variant ID for Ablestar/Matrixify per-variant matching.
    """

    Handle: str
    Variant_SKU: str = Field("", alias="Variant SKU")
    Variant_ID: str = Field("", alias="Variant ID")
    Option_Name: str = Field("", alias="Option Name")
    Option_Value: str = Field("", alias="Option Value")
    Variant_Image: str = Field("", alias="Variant Image")
    Confidence: int = 0
    Warning: str = ""

    model_config = {"populate_by_name": True}


# -----------------------------------------------------------------------------
# Run Report Models
# -----------------------------------------------------------------------------


class RunReportRow(BaseModel):
    """A row in the run report CSV."""

    handle: str
    vendor: str
    status: ProcessingStatus
    match_confidence: int = 0
    warnings: str = ""
    output_rows_count: int = 0
    error_message: str = ""
    processing_time_ms: int = 0


# -----------------------------------------------------------------------------
# Log Entry Model
# -----------------------------------------------------------------------------


class LogEntry(BaseModel):
    """A log entry for per-handle logging."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    level: str = "INFO"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class HandleLog(BaseModel):
    """Complete log for a single handle's processing."""

    handle: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    status: ProcessingStatus | None = None
    entries: list[LogEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    confidence: int = 0
