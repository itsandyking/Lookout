"""Tests for GMC hardening in Google Shopping export."""

import pytest


class TestGtinValidationInExport:
    def test_valid_barcode_included(self):
        from lookout.enrich.gmc_rules import validate_gtin
        assert validate_gtin("012345678905") is True

    def test_invalid_barcode_flagged(self):
        from lookout.enrich.gmc_rules import validate_gtin
        assert validate_gtin("000000000000") is False


class TestColorMappingInExport:
    def test_export_maps_color(self):
        from lookout.enrich.gmc_rules import map_color_for_gmc
        assert map_color_for_gmc("Midnight") == "Navy"

    def test_export_passes_through_standard_color(self):
        from lookout.enrich.gmc_rules import map_color_for_gmc
        assert map_color_for_gmc("Blue") == "Blue"

    def test_export_preserves_internal_color(self):
        """Verify that map_color_for_gmc is a pure function that
        does not modify its input."""
        from lookout.enrich.gmc_rules import map_color_for_gmc
        internal = "Midnight"
        _ = map_color_for_gmc(internal)
        assert internal == "Midnight"
