from lookout.enrich.colors import (
    colors_match,
    deduplicate_color_images,
    find_matching_color,
    normalize_color,
)


def test_normalize_slash():
    assert normalize_color("Matte Black / Polarized Gray") == "matte black polarized gray"


def test_normalize_pipe():
    assert normalize_color("Matte Black | Polarized Gray") == "matte black polarized gray"


def test_normalize_dash():
    assert normalize_color("Smolder Blue - Light Smolder Blue X-Dye") == "smolder blue light smolder blue x-dye"


def test_normalize_w_slash():
    assert normalize_color("Black w/Black") == "black w black"


def test_slash_and_pipe_match():
    assert colors_match("Matte Black / Polarized Gray", "Matte Black | Polarized Gray")


def test_exact_match():
    assert colors_match("Black", "Black")


def test_case_insensitive():
    assert colors_match("matte black", "Matte Black")


def test_no_match():
    assert not colors_match("Black", "Red")


def test_substring_match():
    # "Matte Black" is < 50% of "Matte Black Polarized Gray", so strict match fails
    # This is intentional — loose matching causes false positives
    assert not colors_match("Matte Black", "Matte Black / Polarized Gray")
    # But "Matte Black Polarized" is close enough
    assert colors_match("Matte Black Polarized", "Matte Black / Polarized Gray")


def test_short_substring_no_match():
    # "Red" is too short relative to the full name
    assert not colors_match("Red", "Matte Red / Polarized Gray")


def test_find_matching_color_exact():
    candidates = {"Black": "url1", "Red": "url2"}
    assert find_matching_color("Black", candidates) == "Black"


def test_find_matching_color_normalized():
    candidates = {"Matte Black | Polarized Gray": "url1"}
    assert find_matching_color("Matte Black / Polarized Gray", candidates) == "Matte Black | Polarized Gray"


def test_find_matching_color_no_match():
    candidates = {"Black": "url1"}
    assert find_matching_color("Red", candidates) is None


def test_deduplicate_color_images():
    color_images = {
        "Matte Black / Polarized Gray": ["catalog_url"],
        "Matte Black | Polarized Gray": ["scraped_url"],
        "Red": ["red_url"],
    }
    result = deduplicate_color_images(color_images)
    assert len(result) == 2  # merged the two blacks
    # First key wins as canonical
    assert "Matte Black / Polarized Gray" in result
    assert "catalog_url" in result["Matte Black / Polarized Gray"]
    assert "scraped_url" in result["Matte Black / Polarized Gray"]
    assert "Red" in result
