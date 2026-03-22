"""Tests for vendor configuration loading."""

import tempfile
from pathlib import Path

import pytest
import yaml

from merchfill.utils.config import load_vendors_config


class TestVendorConfig:
    """Tests for vendor configuration."""

    def test_load_valid_config(self, tmp_path: Path):
        """Test loading a valid vendors.yaml."""
        config_content = {
            "vendors": {
                "TestVendor": {
                    "domain": "test.com",
                    "use_playwright": False,
                    "blocked_paths": ["/blog", "/support"],
                },
                "PlaywrightVendor": {
                    "domain": "dynamic.com",
                    "use_playwright": True,
                    "playwright_config": {
                        "wait_for_selector": ".product",
                        "wait_timeout_ms": 10000,
                    },
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
        assert config.vendors["TestVendor"].use_playwright is False
        assert "/blog" in config.vendors["TestVendor"].blocked_paths

        assert "PlaywrightVendor" in config.vendors
        assert config.vendors["PlaywrightVendor"].use_playwright is True
        assert config.vendors["PlaywrightVendor"].playwright_config.wait_for_selector == ".product"

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

    def test_playwright_config_defaults(self, tmp_path: Path):
        """Test Playwright config has default values."""
        config_content = {
            "vendors": {
                "TestVendor": {
                    "domain": "test.com",
                    "use_playwright": True,
                },
            },
        }

        config_path = tmp_path / "vendors.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config_content, f)

        config = load_vendors_config(config_path)

        # Check default Playwright config
        vendor = config.vendors["TestVendor"]
        assert vendor.playwright_config.wait_timeout_ms == 15000
        assert vendor.playwright_config.extra_wait_ms == 0
        assert vendor.playwright_config.wait_for_selector is None
