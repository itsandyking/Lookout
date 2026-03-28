"""Tests for vendor configuration loading."""

from pathlib import Path

import pytest
import yaml

from lookout.enrich.utils.config import load_vendors_config


class TestVendorConfig:
    """Tests for vendor configuration."""

    def test_load_valid_config(self, tmp_path: Path):
        """Test loading a valid vendors.yaml."""
        config_content = {
            "vendors": {
                "TestVendor": {
                    "domain": "test.com",
                    "blocked_paths": ["/blog", "/support"],
                },
                "AnotherVendor": {
                    "domain": "dynamic.com",
                    "product_url_patterns": ["/products/"],
                },
            },
            "settings": {
                "confidence": {
                    "auto_proceed": 85,
                    "warn_threshold": 70,
                },
            },
        }

        config_path = tmp_path / "vendors.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config_content, f)

        config = load_vendors_config(config_path)

        assert "TestVendor" in config.vendors
        assert config.vendors["TestVendor"].domain == "test.com"
        assert "/blog" in config.vendors["TestVendor"].blocked_paths

        assert "AnotherVendor" in config.vendors
        assert "/products/" in config.vendors["AnotherVendor"].product_url_patterns

        assert config.settings.confidence.auto_proceed == 85

    def test_load_missing_file(self):
        """Test loading a non-existent config file."""
        with pytest.raises(FileNotFoundError):
            load_vendors_config("/nonexistent/path/vendors.yaml")

    def test_vendor_not_found(self, tmp_path: Path):
        """Test accessing a vendor that doesn't exist."""
        config_content = {
            "vendors": {
                "Patagonia": {"domain": "patagonia.com"},
            },
        }

        config_path = tmp_path / "vendors.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config_content, f)

        config = load_vendors_config(config_path)

        assert "Patagonia" in config.vendors
        assert "UnknownVendor" not in config.vendors
        assert config.vendors.get("UnknownVendor") is None

    def test_default_settings(self, tmp_path: Path):
        """Test default settings are applied."""
        config_content = {
            "vendors": {
                "TestVendor": {"domain": "test.com"},
            },
        }

        config_path = tmp_path / "vendors.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config_content, f)

        config = load_vendors_config(config_path)

        # Check default values
        assert config.settings.confidence.auto_proceed == 85
        assert config.settings.confidence.warn_threshold == 70
        assert config.settings.rate_limiting.min_delay_ms == 500
        assert config.settings.retries.max_attempts == 3

    def test_search_config_defaults(self, tmp_path: Path):
        """Test search config has default values."""
        config_content = {
            "vendors": {
                "TestVendor": {
                    "domain": "test.com",
                },
            },
        }

        config_path = tmp_path / "vendors.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config_content, f)

        config = load_vendors_config(config_path)

        # Check default search config
        vendor = config.vendors["TestVendor"]
        assert vendor.search.method == "site_search"
        assert vendor.search.query_template == "site:{domain} {query}"
