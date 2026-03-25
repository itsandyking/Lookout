"""Tests for helper utilities."""


from lookout.enrich.utils.helpers import (
    handle_to_query,
    is_product_url,
    normalize_url,
    parse_csv_boolean,
    sanitize_filename,
)


class TestHandleToQuery:
    """Tests for handle_to_query function."""

    def test_basic_handle(self):
        """Test converting a basic handle."""
        assert handle_to_query("mens-nano-puff-jacket") == "mens nano puff jacket"

    def test_handle_with_numbers(self):
        """Test handle with numbers."""
        assert handle_to_query("altra-lone-peak-7") == "altra lone peak 7"

    def test_single_word(self):
        """Test single word handle."""
        assert handle_to_query("jacket") == "jacket"

    def test_empty_handle(self):
        """Test empty handle."""
        assert handle_to_query("") == ""


class TestNormalizeUrl:
    """Tests for normalize_url function."""

    def test_remove_fragment(self):
        """Test removing URL fragments."""
        url = "https://example.com/page#section"
        assert normalize_url(url) == "https://example.com/page"

    def test_remove_trailing_slash(self):
        """Test removing trailing slashes."""
        url = "https://example.com/page/"
        assert normalize_url(url) == "https://example.com/page"

    def test_keep_root_slash(self):
        """Test keeping root path slash."""
        url = "https://example.com/"
        assert normalize_url(url) == "https://example.com/"

    def test_no_changes_needed(self):
        """Test URL that needs no changes."""
        url = "https://example.com/page"
        assert normalize_url(url) == "https://example.com/page"


class TestIsProductUrl:
    """Tests for is_product_url function."""

    def test_product_url(self):
        """Test valid product URL."""
        assert is_product_url(
            "https://example.com/product/jacket",
            blocked_paths=["/blog", "/support"],
            product_patterns=["/product/"],
        )

    def test_blocked_path(self):
        """Test blocked path URL."""
        assert not is_product_url(
            "https://example.com/blog/article",
            blocked_paths=["/blog", "/support"],
            product_patterns=["/product/"],
        )

    def test_no_patterns(self):
        """Test with no product patterns defined."""
        # Should allow anything not blocked
        assert is_product_url(
            "https://example.com/some-page",
            blocked_paths=["/blog"],
            product_patterns=[],
        )


class TestSanitizeFilename:
    """Tests for sanitize_filename function."""

    def test_basic_sanitization(self):
        """Test basic filename sanitization."""
        assert sanitize_filename("hello world") == "hello world"

    def test_special_characters(self):
        """Test removing special characters."""
        result = sanitize_filename('file<>:"/\\|?*name')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result

    def test_max_length(self):
        """Test max length truncation."""
        long_name = "a" * 200
        result = sanitize_filename(long_name, max_length=50)
        assert len(result) <= 50

    def test_empty_result(self):
        """Test handling of result that would be empty."""
        result = sanitize_filename("???")
        assert result == "unnamed"


class TestParseCsvBoolean:
    """Tests for parse_csv_boolean function."""

    def test_true_values(self):
        """Test parsing true values."""
        for val in ["true", "True", "TRUE", "yes", "1", "y", "t", True, 1]:
            assert parse_csv_boolean(val) is True

    def test_false_values(self):
        """Test parsing false values."""
        for val in ["false", "False", "FALSE", "no", "0", "n", "f", "", False, 0]:
            assert parse_csv_boolean(val) is False

    def test_none(self):
        """Test parsing None."""
        assert parse_csv_boolean(None) is False
