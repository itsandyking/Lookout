"""Tests for the enrichment quality scorer."""

import json
from pathlib import Path

import pytest

from lookout.enrich.models import ExtractedFacts, MerchOutput
from lookout.enrich.scorer import (
    AxisScore,
    QualityScore,
    score_anti_hype,
    score_coverage,
    score_factual_fidelity_from_verification,
    score_length_targets,
    score_output_dir,
    score_quality,
    score_structural_compliance,
)


# ---------------------------------------------------------------------------
# Fixtures: representative product data
# ---------------------------------------------------------------------------

def _make_facts(**overrides) -> ExtractedFacts:
    """Build an ExtractedFacts with sensible defaults."""
    defaults = {
        "canonical_url": "https://vendor.example.com/product",
        "product_name": "Men's Nano Puff Jacket",
        "brand": "Patagonia",
        "description_blocks": [
            "Windproof and water-resistant, the Nano Puff Jacket uses 60-g PrimaLoft "
            "Gold Insulation Eco with 55% postconsumer recycled content."
        ],
        "feature_bullets": [
            "60-g PrimaLoft Gold Insulation Eco",
            "Windproof and water-resistant shell",
            "Zippered handwarmer pockets",
            "Internal zippered chest pocket",
            "Stuff-sack style internal chest pocket",
        ],
        "specs": {
            "Weight": "337 g (11.9 oz)",
            "Fit": "Regular",
            "Insulation": "60-g PrimaLoft Gold Insulation Eco",
        },
        "materials": "Shell: 1.4-oz 22-denier ripstop recycled polyester",
        "care": "Machine wash cold, tumble dry low",
    }
    defaults.update(overrides)
    return ExtractedFacts(**defaults)


# A well-formed description that should score highly
GOOD_HTML = """
<p>Windproof and water-resistant, the Nano Puff Jacket uses 60-g PrimaLoft
Gold Insulation Eco with 55% postconsumer recycled content.</p>

<h3>Features</h3>
<ul>
<li>60-g PrimaLoft Gold Insulation Eco</li>
<li>Windproof and water-resistant shell</li>
<li>Zippered handwarmer pockets</li>
<li>Internal zippered chest pocket</li>
<li>Stuff-sack style internal chest pocket</li>
</ul>

<h3>Specifications</h3>
<table>
<tr><td>Weight</td><td>337 g (11.9 oz)</td></tr>
<tr><td>Fit</td><td>Regular</td></tr>
<tr><td>Insulation</td><td>60-g PrimaLoft Gold Insulation Eco</td></tr>
</table>
""".strip()

# A poor description: no structure, hype words, too short
BAD_HTML = "<p>This amazing jacket is an incredible must-have for winter.</p>"

# Missing features section even though facts have features
PARTIAL_HTML = """
<p>Windproof and water-resistant, the Nano Puff Jacket uses 60-g PrimaLoft
Gold Insulation Eco with 55% postconsumer recycled content.</p>
""".strip()


# ---------------------------------------------------------------------------
# Axis 1: Factual fidelity
# ---------------------------------------------------------------------------

class TestFactualFidelity:
    def test_all_supported(self):
        verification = {
            "supported": ["claim1", "claim2", "claim3"],
            "unsupported": [],
            "embellished": [],
            "verdict": "PASS",
        }
        axis = score_factual_fidelity_from_verification(verification)
        assert axis.score == 30
        assert axis.max_score == 30

    def test_some_unsupported(self):
        verification = {
            "supported": ["claim1", "claim2"],
            "unsupported": ["fabricated claim"],
            "embellished": [],
            "verdict": "FAIL",
        }
        axis = score_factual_fidelity_from_verification(verification)
        assert axis.score == 20  # 2/3 * 30 = 20
        assert "1 unsupported" in axis.details[0]

    def test_embellished(self):
        verification = {
            "supported": ["claim1"],
            "unsupported": [],
            "embellished": ["stretched claim"],
            "verdict": "FAIL",
        }
        axis = score_factual_fidelity_from_verification(verification)
        assert axis.score == 15  # 1/2 * 30

    def test_error_verdict(self):
        verification = {"verdict": "ERROR"}
        axis = score_factual_fidelity_from_verification(verification)
        assert axis.score == 0

    def test_no_claims(self):
        verification = {
            "supported": [],
            "unsupported": [],
            "embellished": [],
            "verdict": "PASS",
        }
        axis = score_factual_fidelity_from_verification(verification)
        assert axis.score == 30


# ---------------------------------------------------------------------------
# Axis 2: Structural compliance
# ---------------------------------------------------------------------------

class TestStructuralCompliance:
    def test_good_html(self):
        facts = _make_facts()
        axis = score_structural_compliance(GOOD_HTML, facts)
        assert axis.score == 25  # all elements present
        assert axis.max_score == 25

    def test_missing_features_section(self):
        facts = _make_facts()
        axis = score_structural_compliance(PARTIAL_HTML, facts)
        # Has paragraph (+8), missing features list (-7), missing specs table (-5)
        assert axis.score < 20
        assert "missing feature list" in str(axis.details)

    def test_no_features_in_facts_ok(self):
        facts = _make_facts(feature_bullets=[], specs={})
        axis = score_structural_compliance(PARTIAL_HTML, facts)
        # No features/specs in facts = full marks for those sections
        assert axis.score >= 20

    def test_too_many_features(self):
        html = """
<p>Intro paragraph.</p>
<h3>Features</h3>
<ul>
<li>Feature one</li>
<li>Feature two</li>
<li>Feature three</li>
<li>Feature four</li>
<li>Feature five</li>
<li>Feature six</li>
<li>Feature seven</li>
<li>Feature eight</li>
</ul>
""".strip()
        facts = _make_facts()
        axis = score_structural_compliance(html, facts)
        assert "too long (8 items" in str(axis.details)
        # Partial credit (3) not full credit (7)
        # Total: 8 (paragraph) + 3 (partial features) + 0 (no table) + semantic
        assert axis.score < 20

    def test_six_features_ok(self):
        html = """
<p>Intro paragraph.</p>
<h3>Features</h3>
<ul>
<li>Feature one</li>
<li>Feature two</li>
<li>Feature three</li>
<li>Feature four</li>
<li>Feature five</li>
<li>Feature six</li>
</ul>
""".strip()
        facts = _make_facts()
        axis = score_structural_compliance(html, facts)
        assert "6 items" in str(axis.details)
        assert "too long" not in str(axis.details)

    def test_bad_html(self):
        facts = _make_facts()
        axis = score_structural_compliance(BAD_HTML, facts)
        assert axis.score < 15


# ---------------------------------------------------------------------------
# Axis 3: Length targets
# ---------------------------------------------------------------------------

class TestLengthTargets:
    def test_good_length(self):
        axis = score_length_targets(GOOD_HTML)
        assert axis.score >= 10  # within 100-400 words

    def test_too_short(self):
        axis = score_length_targets(BAD_HTML)
        assert axis.score < 10

    def test_bullets_within_limit(self):
        html = "<ul><li>Short bullet</li><li>Another one</li></ul>"
        axis = score_length_targets(html)
        # Bullets are short — should get bullet credit
        assert "bullets ≤12 words" in str(axis.details)

    def test_long_bullets(self):
        long = "word " * 15
        html = f"<ul><li>{long}</li><li>ok bullet</li></ul>"
        axis = score_length_targets(html)
        assert "1/2 bullets >12 words" in str(axis.details)


# ---------------------------------------------------------------------------
# Axis 4: Anti-hype
# ---------------------------------------------------------------------------

class TestAntiHype:
    def test_clean_copy(self):
        axis = score_anti_hype(GOOD_HTML)
        assert axis.score == 15

    def test_banned_words(self):
        axis = score_anti_hype(BAD_HTML)
        assert axis.score < 15
        assert "amazing" in str(axis.details)
        assert "incredible" in str(axis.details)

    def test_single_banned_word(self):
        html = "<p>This revolutionary jacket keeps you warm.</p>"
        axis = score_anti_hype(html)
        assert axis.score == 10  # 15 - 5

    def test_no_false_positive_on_substring(self):
        """Words like 'imperfect' should not trigger 'perfect'."""
        html = "<p>This jacket has imperfect stitching but ultimately works well.</p>"
        axis = score_anti_hype(html)
        assert axis.score == 15
        assert "no banned words" in str(axis.details)


# ---------------------------------------------------------------------------
# Axis 5: Coverage
# ---------------------------------------------------------------------------

class TestCoverage:
    def test_good_coverage(self):
        facts = _make_facts()
        axis = score_coverage(GOOD_HTML, facts)
        assert axis.score >= 10  # should use most facts

    def test_bad_coverage(self):
        facts = _make_facts()
        html = "<p>A warm jacket for cold weather.</p>"
        axis = score_coverage(html, facts)
        assert axis.score < 10

    def test_materials_paraphrased(self):
        """Materials coverage should use keyword matching, not prefix substring."""
        facts = _make_facts(materials="Shell: 1.4-oz 22-denier ripstop recycled polyester")
        # Output paraphrases materials but uses key words
        html = "<p>Made with recycled polyester ripstop shell fabric.</p>"
        axis = score_coverage(html, facts)
        # Should find keyword matches for materials
        assert "rich fields" in str(axis.details)

    def test_empty_facts_full_marks(self):
        facts = _make_facts(
            description_blocks=[], feature_bullets=[], specs={},
            materials="", care="",
        )
        axis = score_coverage("<p>Simple description.</p>", facts)
        assert axis.score == 15  # nothing to miss = full marks


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

class TestCompositeScore:
    def test_good_product_no_verification(self):
        facts = _make_facts()
        qs = score_quality(GOOD_HTML, facts)
        # Should score well on axes 2-5, 0 on fidelity (no verification)
        assert qs.axes["factual_fidelity"].score == 0
        assert qs.axes["structural_compliance"].score > 0
        assert qs.total > 0

    def test_good_product_with_verification(self):
        facts = _make_facts()
        verification = {
            "supported": ["claim1", "claim2"],
            "unsupported": [],
            "embellished": [],
            "verdict": "PASS",
        }
        qs = score_quality(GOOD_HTML, facts, verification)
        assert qs.axes["factual_fidelity"].score == 30
        assert qs.total >= 80

    def test_bad_product(self):
        facts = _make_facts()
        qs = score_quality(BAD_HTML, facts)
        # Bad on structure, length, hype, coverage
        assert qs.total < 40

    def test_summary_dict(self):
        facts = _make_facts()
        qs = score_quality(GOOD_HTML, facts)
        d = qs.summary_dict()
        assert "handle" in d
        assert "axes" in d
        assert d["max"] == 100


# ---------------------------------------------------------------------------
# Output directory scoring
# ---------------------------------------------------------------------------

class TestScoreOutputDir:
    def test_scores_from_artifacts(self, tmp_path):
        handle = "test-product"
        handle_dir = tmp_path / handle
        handle_dir.mkdir()

        facts = _make_facts()
        merch = MerchOutput(handle=handle, body_html=GOOD_HTML, confidence=80)

        (handle_dir / "facts.json").write_text(facts.model_dump_json())
        (handle_dir / "merch_output.json").write_text(merch.model_dump_json())

        scores = score_output_dir(tmp_path)
        assert len(scores) == 1
        assert scores[0].handle == handle
        assert scores[0].total > 0

    def test_skips_missing_artifacts(self, tmp_path):
        handle_dir = tmp_path / "incomplete"
        handle_dir.mkdir()
        # Only merch_output, no facts
        merch = MerchOutput(handle="incomplete", body_html="<p>test</p>", confidence=50)
        (handle_dir / "merch_output.json").write_text(merch.model_dump_json())

        scores = score_output_dir(tmp_path)
        assert len(scores) == 0

    def test_filter_by_handles(self, tmp_path):
        for name in ["product-a", "product-b"]:
            d = tmp_path / name
            d.mkdir()
            facts = _make_facts()
            merch = MerchOutput(handle=name, body_html=GOOD_HTML, confidence=80)
            (d / "facts.json").write_text(facts.model_dump_json())
            (d / "merch_output.json").write_text(merch.model_dump_json())

        scores = score_output_dir(tmp_path, handles=["product-a"])
        assert len(scores) == 1
        assert scores[0].handle == "product-a"
