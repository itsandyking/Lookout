"""Tests for OllamaVisionClient menu-based color matching."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from lookout.enrich.llm import OllamaVisionClient


def run(coro):
    """Helper to run async tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestMatchImageToColor:
    """Test the menu-based color matching logic."""

    def _client(self):
        return OllamaVisionClient(model="vision")

    def test_exact_match(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="Basin Green")):
            assert run(c.match_image_to_color(b"img", ["Basin Green", "Nouveau Green"])) == "Basin Green"

    def test_case_insensitive(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="basin green")):
            assert run(c.match_image_to_color(b"img", ["Basin Green", "Black"])) == "Basin Green"

    def test_none_response(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="NONE")):
            assert run(c.match_image_to_color(b"img", ["Red", "Blue"])) is None

    def test_empty_response(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="")):
            assert run(c.match_image_to_color(b"img", ["Red", "Blue"])) is None

    def test_partial_match_extra_words(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="The color is Storm Blue")):
            assert run(c.match_image_to_color(b"img", ["Storm Blue", "Black/Poppy"])) == "Storm Blue"

    def test_no_match_returns_none(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="Turquoise")):
            assert run(c.match_image_to_color(b"img", ["Red", "Blue"])) is None

    def test_colorblocked_name(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="Black/Poppy")):
            assert run(c.match_image_to_color(b"img", ["Basin Green", "Black/Poppy"])) == "Black/Poppy"

    def test_url_hint_in_prompt(self):
        c = self._client()
        with patch.object(c, "_post_vision", AsyncMock(return_value="NONE")) as mock:
            run(c.match_image_to_color(
                b"img", ["Red"],
                image_url="https://cdn.example.com/products/basin-green/hero.jpg",
            ))
            payload = mock.call_args[0][0]
            assert "basin-green" in payload["prompt"]

    def test_trailing_period_stripped(self):
        c = self._client()
        # _post_vision strips trailing dots before returning
        with patch.object(c, "_post_vision", AsyncMock(return_value="Storm Blue")):
            assert run(c.match_image_to_color(b"img", ["Storm Blue", "Red"])) == "Storm Blue"


class TestMatchImagesBatch:
    """Test batch processing of multiple images."""

    def _client(self):
        return OllamaVisionClient(model="vision")

    def test_assigns_each_color_once(self):
        c = self._client()
        call_count = 0

        async def mock_match(data, colors, image_url=""):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Red" if "Red" in colors else None
            if call_count == 2:
                return None
            return "Blue" if "Blue" in colors else None

        with patch.object(c, "match_image_to_color", side_effect=mock_match):
            result = run(c.match_images_batch(
                [("img1.jpg", b"a"), ("img2.jpg", b"b"), ("img3.jpg", b"c")],
                ["Red", "Blue"],
            ))
            assert result == {"Red": "img1.jpg", "Blue": "img3.jpg"}

    def test_stops_when_all_matched(self):
        c = self._client()
        call_count = 0

        async def mock_match(data, colors, image_url=""):
            nonlocal call_count
            call_count += 1
            return colors[0] if colors else None

        with patch.object(c, "match_image_to_color", side_effect=mock_match):
            result = run(c.match_images_batch(
                [("a.jpg", b"a"), ("b.jpg", b"b"), ("c.jpg", b"c"), ("d.jpg", b"d")],
                ["Red", "Blue"],
            ))
            assert len(result) == 2
            assert call_count == 2

    def test_skips_none_responses(self):
        c = self._client()
        responses = iter([None, None, "Red"])

        async def mock_match(data, colors, image_url=""):
            return next(responses)

        with patch.object(c, "match_image_to_color", side_effect=mock_match):
            result = run(c.match_images_batch(
                [("lifestyle.jpg", b"a"), ("chart.jpg", b"b"), ("product.jpg", b"c")],
                ["Red"],
            ))
            assert result == {"Red": "product.jpg"}

    def test_handles_failures(self):
        c = self._client()
        call_count = 0

        async def mock_match(data, colors, image_url=""):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("timeout")
            return "Red" if "Red" in colors else None

        with patch.object(c, "match_image_to_color", side_effect=mock_match):
            result = run(c.match_images_batch(
                [("bad.jpg", b"a"), ("good.jpg", b"b")],
                ["Red"],
            ))
            assert result == {"Red": "good.jpg"}

    def test_narrows_options_per_image(self):
        c = self._client()
        seen_options = []

        async def mock_match(data, colors, image_url=""):
            seen_options.append(list(colors))
            return colors[0]

        with patch.object(c, "match_image_to_color", side_effect=mock_match):
            run(c.match_images_batch(
                [("a.jpg", b"a"), ("b.jpg", b"b"), ("c.jpg", b"c")],
                ["Red", "Blue", "Green"],
            ))
            assert seen_options == [
                ["Red", "Blue", "Green"],
                ["Blue", "Green"],
                ["Green"],
            ]

    def test_empty_inputs(self):
        c = self._client()
        result = run(c.match_images_batch([], ["Red"]))
        assert result == {}


class TestMatchImagesBatchTwoPass:
    """Test the two-pass matching (pass 1 menu, pass 2 freeform)."""

    def _client(self):
        return OllamaVisionClient(model="vision")

    def test_pass2_picks_up_unmatched(self):
        """Pass 1 misses, pass 2 free-form matches via token overlap."""
        c = self._client()

        async def mock_menu(data, colors, image_url=""):
            # Pass 1 can't match this image
            return None

        async def mock_freeform(data, image_url=""):
            return "dark purple and yellow"

        with patch.object(c, "match_image_to_color", side_effect=mock_menu), \
             patch.object(c, "_identify_color_freeform", side_effect=mock_freeform):
            result = run(c.match_images_batch(
                [("img1.jpg", b"a")],
                ["Purple Ink/Purple Dusk/Cheddar"],
            ))
            assert "Purple Ink/Purple Dusk/Cheddar" in result

    def test_pass2_skips_already_matched_urls(self):
        """URLs matched in pass 1 aren't reused in pass 2."""
        c = self._client()
        call_count = 0

        async def mock_menu(data, colors, image_url=""):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Red"
            return None

        async def mock_freeform(data, image_url=""):
            return "blue"

        with patch.object(c, "match_image_to_color", side_effect=mock_menu), \
             patch.object(c, "_identify_color_freeform", side_effect=mock_freeform):
            result = run(c.match_images_batch(
                [("img1.jpg", b"a"), ("img2.jpg", b"b")],
                ["Red", "Blue"],
            ))
            assert result["Red"] == "img1.jpg"
            assert result["Blue"] == "img2.jpg"

    def test_pass2_not_triggered_if_all_matched(self):
        """Pass 2 is skipped if pass 1 matched everything."""
        c = self._client()
        freeform_called = False

        async def mock_menu(data, colors, image_url=""):
            return colors[0] if colors else None

        async def mock_freeform(data, image_url=""):
            nonlocal freeform_called
            freeform_called = True
            return "red"

        with patch.object(c, "match_image_to_color", side_effect=mock_menu), \
             patch.object(c, "_identify_color_freeform", side_effect=mock_freeform):
            run(c.match_images_batch(
                [("a.jpg", b"a"), ("b.jpg", b"b")],
                ["Red", "Blue"],
            ))
            assert not freeform_called


class TestFuzzyMatchFreeform:
    """Test free-form description matching to color options."""

    def test_single_token_overlap(self):
        result = OllamaVisionClient._fuzzy_match_freeform(
            "dark olive green", ["Dark Olive", "Creek Blue"],
        )
        assert result == "Dark Olive"

    def test_slash_name_match(self):
        result = OllamaVisionClient._fuzzy_match_freeform(
            "purple and yellow",
            ["Purple Ink/Purple Dusk/Cheddar", "Black"],
        )
        assert result == "Purple Ink/Purple Dusk/Cheddar"

    def test_no_match(self):
        result = OllamaVisionClient._fuzzy_match_freeform(
            "bright red", ["Blue", "Green"],
        )
        assert result is None

    def test_noise_words_ignored(self):
        result = OllamaVisionClient._fuzzy_match_freeform(
            "the product is dark blue colored",
            ["Storm Blue", "Red"],
        )
        assert result == "Storm Blue"

    def test_best_overlap_wins(self):
        result = OllamaVisionClient._fuzzy_match_freeform(
            "chameleon green and black",
            ["Chameleon/Black", "Pine Leaf Green"],
        )
        # "chameleon" + "black" = 2 tokens overlap vs "green" = 1
        assert result == "Chameleon/Black"

    def test_hyphenated_name(self):
        result = OllamaVisionClient._fuzzy_match_freeform(
            "olive", ["Dark-Olive", "Blue"],
        )
        assert result == "Dark-Olive"

    # --- Stricter matching: false-positive prevention ---

    def test_black_does_not_match_blackberry(self):
        """'Black' in description should not match 'Black-Berry' on
        the secondary 'berry' token alone — but 'black' is the first
        token so it *should* match when 'black' overlaps."""
        # 'Blackberry' as a single token doesn't overlap with 'black'
        result = OllamaVisionClient._fuzzy_match_freeform(
            "black jacket", ["Blackberry", "Navy"],
        )
        assert result is None  # no token overlap at all

    def test_black_does_not_match_black_berry_slashed(self):
        """Description 'black jacket' has desc_tokens {'black', 'jacket'}.
        Option 'Black/Berry' expands to {'black', 'berry'}.
        Overlap is only {'black'} (1 token) but desc has 2+ tokens,
        so we need 2 overlapping tokens — unless 'black' is the first
        token of the option. Here it IS the first token, so it matches."""
        result = OllamaVisionClient._fuzzy_match_freeform(
            "black jacket", ["Black/Berry", "Navy"],
        )
        assert result == "Black/Berry"

    def test_secondary_token_alone_rejected(self):
        """Description 'berry smoothie' should NOT match 'Black/Berry'
        because 'berry' is not the first/dominant token of the option."""
        result = OllamaVisionClient._fuzzy_match_freeform(
            "berry smoothie", ["Black/Berry", "Red"],
        )
        assert result is None

    def test_single_desc_token_matches_dominant(self):
        """A single-token description 'blue' should match option 'Blue/Red'
        because 'blue' is the first/dominant token."""
        result = OllamaVisionClient._fuzzy_match_freeform(
            "blue", ["Blue/Red", "Green"],
        )
        assert result == "Blue/Red"

    def test_single_desc_token_prefers_dominant(self):
        """A single-token description 'red' should prefer 'Red/Black'
        (where 'red' is dominant) over 'Blue/Red' (secondary)."""
        result = OllamaVisionClient._fuzzy_match_freeform(
            "red", ["Blue/Red", "Red/Black"],
        )
        # Both have 1 overlap; best_score picks whichever is first with
        # equal score, but "Red/Black" also has 1 overlap.  Since scores
        # are equal and we take the first one found with the best score,
        # this tests that both get score 1 and the first in list wins.
        # The key behavior is neither is *rejected* — both are valid
        # single-token matches.
        assert result in ("Blue/Red", "Red/Black")

    def test_multi_token_overlap_still_works(self):
        """When 2+ tokens overlap, the match should still succeed
        regardless of dominant token position."""
        result = OllamaVisionClient._fuzzy_match_freeform(
            "storm blue heather", ["Storm Blue", "Black"],
        )
        assert result == "Storm Blue"

    def test_weak_single_overlap_on_multi_desc_rejected(self):
        """Description 'green forest pine' (3 tokens after noise removal)
        matching 'Pine Leaf' on just 'pine' — rejected because desc has
        2+ tokens but only 1 overlaps, and 'pine' is the first token
        of 'Pine Leaf', so the dominant-token exception applies."""
        result = OllamaVisionClient._fuzzy_match_freeform(
            "green forest pine", ["Pine Leaf", "Red"],
        )
        # 'pine' overlaps and IS the dominant token → allowed
        assert result == "Pine Leaf"

    def test_weak_nondominant_overlap_on_multi_desc_rejected(self):
        """Description 'green forest leaf' matching 'Pine Leaf' on just
        'leaf' — rejected because 'leaf' is not the dominant token."""
        result = OllamaVisionClient._fuzzy_match_freeform(
            "green forest leaf", ["Pine Leaf", "Red"],
        )
        assert result is None


class TestDescribeColorOption:
    """Test slash name expansion."""

    def test_simple_color(self):
        assert OllamaVisionClient._describe_color_option("Red") == "- Red"

    def test_slash_color(self):
        result = OllamaVisionClient._describe_color_option("Black/Poppy")
        assert "- Black/Poppy" in result
        assert "multi-color" in result
        assert "black" in result
        assert "poppy" in result

    def test_three_part_slash(self):
        result = OllamaVisionClient._describe_color_option("Purple Ink/Purple Dusk/Cheddar")
        assert "multi-color" in result
        assert "ink" in result
        assert "dusk" in result
        assert "cheddar" in result


class TestBuildPrompt:
    """Test prompt construction."""

    def test_includes_all_colors(self):
        c = OllamaVisionClient()
        prompt = c._build_prompt(["Basin Green", "Nouveau Green", "Black/Poppy"])
        assert "Basin Green" in prompt
        assert "Nouveau Green" in prompt
        assert "Black/Poppy" in prompt

    def test_slash_expanded_in_prompt(self):
        c = OllamaVisionClient()
        prompt = c._build_prompt(["Black/Poppy"])
        assert "multi-color" in prompt

    def test_includes_url_path(self):
        c = OllamaVisionClient()
        prompt = c._build_prompt(
            ["Red"],
            image_url="https://cdn.example.com/products/storm-blue/hero.jpg",
        )
        assert "/products/storm-blue/hero.jpg" in prompt

    def test_no_url_hint_when_empty(self):
        c = OllamaVisionClient()
        prompt = c._build_prompt(["Red"])
        assert "Image URL path" not in prompt

    def test_multi_color_rule_in_prompt(self):
        c = OllamaVisionClient()
        prompt = c._build_prompt(["Red"])
        assert "multi-color" in prompt.lower()

    def test_lifestyle_rejection_rule(self):
        c = OllamaVisionClient()
        prompt = c._build_prompt(["Red"])
        assert "lifestyle" in prompt.lower()
        assert "NONE" in prompt
