"""Tests for GMC hardening in Google Shopping export."""

import pytest


class TestGtinValidationInExport:
    def test_valid_barcode_included(self):
        from lookout.enrich.gmc_rules import validate_gtin
        assert validate_gtin("012345678905") is True

    def test_invalid_barcode_flagged(self):
        from lookout.enrich.gmc_rules import validate_gtin
        assert validate_gtin("000000000000") is False
