"""Tests for data models."""


from lookout.enrich.models import InputRow, ProcessingStatus


class TestInputRow:
    """Tests for InputRow model."""

    def test_parse_boolean_true_values(self):
        """Test parsing various true boolean representations."""
        test_cases = [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("yes", True),
            ("Yes", True),
            ("1", True),
            ("y", True),
            ("t", True),
            (True, True),
            (1, True),
        ]

        for input_val, expected in test_cases:
            row = InputRow(
                **{
                    "Product Handle": "test-handle",
                    "Vendor": "TestVendor",
                    "Has Image": input_val,
                    "Has Variant Images": "true",
                    "Has Description": "true",
                }
            )
            assert row.has_image == expected, f"Failed for input: {input_val}"

    def test_parse_boolean_false_values(self):
        """Test parsing various false boolean representations."""
        test_cases = [
            ("false", False),
            ("False", False),
            ("FALSE", False),
            ("no", False),
            ("No", False),
            ("0", False),
            ("n", False),
            ("f", False),
            ("", False),
            (False, False),
            (0, False),
        ]

        for input_val, expected in test_cases:
            row = InputRow(
                **{
                    "Product Handle": "test-handle",
                    "Vendor": "TestVendor",
                    "Has Image": input_val,
                    "Has Variant Images": "true",
                    "Has Description": "true",
                }
            )
            assert row.has_image == expected, f"Failed for input: {input_val}"

    def test_needs_properties(self):
        """Test the needs_* computed properties."""
        row = InputRow(
            **{
                "Product Handle": "test-handle",
                "Vendor": "TestVendor",
                "Has Image": "false",
                "Has Variant Images": "true",
                "Has Description": "false",
            }
        )

        assert row.needs_description is True
        assert row.needs_images is True
        assert row.needs_variant_images is False
        assert row.has_any_gap is True

    def test_no_gaps(self):
        """Test when product has no gaps."""
        row = InputRow(
            **{
                "Product Handle": "test-handle",
                "Vendor": "TestVendor",
                "Has Image": "true",
                "Has Variant Images": "true",
                "Has Description": "true",
            }
        )

        assert row.needs_description is False
        assert row.needs_images is False
        assert row.needs_variant_images is False
        assert row.has_any_gap is False

    def test_optional_fields(self):
        """Test optional fields with default values."""
        row = InputRow(
            **{
                "Product Handle": "test-handle",
                "Vendor": "TestVendor",
                "Has Image": "true",
                "Has Variant Images": "true",
                "Has Description": "true",
            }
        )

        assert row.gaps == ""
        assert row.admin_link is None
        assert row.priority_score is None
        assert row.suggestions is None
        assert row.has_product_type is True  # Default
        assert row.has_tags is True  # Default


class TestProcessingStatus:
    """Tests for ProcessingStatus enum."""

    def test_status_values(self):
        """Test all status values exist."""
        assert ProcessingStatus.UPDATED.value == "UPDATED"
        assert ProcessingStatus.SKIPPED.value == "SKIPPED"
        assert ProcessingStatus.NO_MATCH.value == "NO_MATCH"
        assert ProcessingStatus.FAILED.value == "FAILED"
        assert ProcessingStatus.SKIPPED_VENDOR_NOT_CONFIGURED.value == "SKIPPED_VENDOR_NOT_CONFIGURED"
        assert ProcessingStatus.SKIPPED_NO_GAPS.value == "SKIPPED_NO_GAPS"
