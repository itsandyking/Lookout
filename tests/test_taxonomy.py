from lookout.taxonomy.mappings import (
    EXCLUDED_VENDORS,
    LOCATIONS,
    LOW_INVENTORY_THRESHOLD,
    MERCH_WEIGHTS,
    NEW_ARRIVAL_DAYS,
)


def test_excluded_vendors():
    assert "The Switchback" in EXCLUDED_VENDORS
    assert "The Mountain Air" in EXCLUDED_VENDORS


def test_merch_weights_sum_to_one():
    total = sum(MERCH_WEIGHTS.values())
    assert abs(total - 1.0) < 0.01


def test_merch_weights_keys():
    expected = {"sales_velocity", "margin", "inventory_health", "new_arrival_boost", "low_inventory_penalty"}
    assert set(MERCH_WEIGHTS.keys()) == expected


def test_location_ids():
    assert LOCATIONS["The Mountain Air"]["id"] > 0


def test_new_arrival_days():
    assert NEW_ARRIVAL_DAYS == 30


def test_low_inventory_threshold():
    assert LOW_INVENTORY_THRESHOLD == 3
