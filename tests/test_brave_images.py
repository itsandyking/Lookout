"""Tests for Brave Image Search fallback."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lookout.enrich.models import BraveImagesSettings, GlobalSettings, ImageInfo


def run(coro):
    """Helper to run async tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestBraveImagesSettings:
    def test_defaults(self):
        s = BraveImagesSettings()
        assert s.enabled is True
        assert s.ollama_host == "http://localhost:11434"
        assert s.ollama_model == "gemma4:e4b"
        assert s.max_candidates_per_color == 3
        assert s.min_image_dimensions == 400
        assert s.verify_timeout == 30
        assert s.brave_count == 50
        assert s.max_evaluate == 15

    def test_in_global_settings(self):
        gs = GlobalSettings()
        assert isinstance(gs.brave_images, BraveImagesSettings)


class TestImageInfoSource:
    def test_default_source_empty(self):
        img = ImageInfo(url="https://example.com/img.jpg")
        assert img.source == ""

    def test_source_set(self):
        img = ImageInfo(url="https://example.com/img.jpg", source="brave_image_search")
        assert img.source == "brave_image_search"
