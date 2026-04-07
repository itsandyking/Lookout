"""Tests for classify_color_families script logic."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from classify_color_families import classify_image
from lookout.enrich.llm import OllamaVisionClient


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestClassifyImage:
    def test_valid_family(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="Green")):
            result = run(classify_image(vision, b"fake_image"))
            assert result == "Green"

    def test_invalid_response_returns_none(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="Chartreuse")):
            result = run(classify_image(vision, b"fake_image"))
            assert result is None

    def test_case_insensitive(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="navy")):
            result = run(classify_image(vision, b"fake_image"))
            assert result == "Navy"

    def test_trailing_period_stripped(self):
        vision = OllamaVisionClient(model="vision")
        with patch.object(vision, "_post_vision", AsyncMock(return_value="Blue.")):
            # _post_vision already strips trailing period, but classify_image also strips
            result = run(classify_image(vision, b"fake_image"))
            assert result == "Blue"
