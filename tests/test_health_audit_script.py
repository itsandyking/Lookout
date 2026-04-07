"""Tests for catalog_health_audit script logic."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from catalog_health_audit import check_image, _image_verdict, _overall_severity
from lookout.enrich.llm import OllamaVisionClient


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestCheckImage:
    def test_matching_product(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(
            vision, "_post_vision",
            AsyncMock(return_value="MATCH: YES\nTYPE: PRODUCT"),
        ):
            result = run(check_image(vision, b"img", "Down Jacket", "Jackets"))
            assert result["match"] == "yes"
            assert result["image_type"] == "product"

    def test_mismatched_product(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(
            vision, "_post_vision",
            AsyncMock(return_value="MATCH: NO\nTYPE: PRODUCT"),
        ):
            result = run(check_image(vision, b"img", "Down Jacket", "Jackets"))
            assert result["match"] == "no"

    def test_lifestyle_image(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(
            vision, "_post_vision",
            AsyncMock(return_value="MATCH: YES\nTYPE: LIFESTYLE"),
        ):
            result = run(check_image(vision, b"img", "Down Jacket", "Jackets"))
            assert result["image_type"] == "lifestyle"


class TestImageVerdict:
    def test_no_image(self):
        assert _image_verdict(None, False) == "no_image"

    def test_error(self):
        assert _image_verdict(None, True) == "error"

    def test_mismatch(self):
        assert _image_verdict({"match": "no", "image_type": "product"}, True) == "mismatch"

    def test_product_match(self):
        assert _image_verdict({"match": "yes", "image_type": "product"}, True) == "product"


class TestOverallSeverity:
    def test_all_ok(self):
        assert _overall_severity("product", "ok", "ok") == "OK"

    def test_image_mismatch_is_fail(self):
        assert _overall_severity("mismatch", "ok", "ok") == "FAIL"

    def test_coherence_mismatch_is_fail(self):
        assert _overall_severity("product", "ok", "mismatch") == "FAIL"

    def test_weak_description_is_warn(self):
        assert _overall_severity("product", "weak", "ok") == "WARN"

    def test_lifestyle_is_warn(self):
        assert _overall_severity("lifestyle", "ok", "ok") == "WARN"

    def test_no_image_is_warn(self):
        assert _overall_severity("no_image", "ok", "ok") == "WARN"
