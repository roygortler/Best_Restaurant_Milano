"""Tests for the cuisine table and display labels."""
from app.cuisines import CUISINES, label_for


def test_label_for_known_cuisine():
    assert label_for("japanese_restaurant") == "Japanese"
    assert label_for("steak_house") == "Steakhouse"


def test_label_for_other_google_types_is_readable():
    assert label_for("sandwich_shop") == "Sandwich shop"
    assert label_for("ramen_restaurant") == "Ramen"


def test_label_for_missing_type():
    # Places cached before primary_type was fetched have none.
    assert label_for(None) == "Restaurant"
    assert label_for("restaurant") == "Restaurant"


def test_every_cuisine_has_a_label_and_google_type():
    for key, cuisine in CUISINES.items():
        assert cuisine["label"], key
        assert cuisine["google_type"], key
