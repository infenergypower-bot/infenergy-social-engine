import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from agents.on_image_typography_designer import _normalize_zone


def test_normalize_zone_trims_overflow_without_moving_origin():
    assert _normalize_zone({"x": 0.8, "y": 0.75, "width": 0.4, "height": 0.5}) == {
        "x": 0.8,
        "y": 0.75,
        "width": pytest.approx(0.2),
        "height": 0.25,
    }


def test_normalize_zone_accepts_numeric_json_strings():
    assert _normalize_zone({"x": "0.08", "y": "0.1", "width": "0.42", "height": "0.2"}) == {
        "x": 0.08,
        "y": 0.1,
        "width": 0.42,
        "height": 0.2,
    }


@pytest.mark.parametrize(
    "zone",
    [
        {"x": 0.2, "y": 0.2, "width": 0, "height": 0.3},
        {"x": -0.1, "y": 0.2, "width": 0.3, "height": 0.3},
        {"x": 1, "y": 0.2, "width": 0.3, "height": 0.3},
        {"x": 0.2, "y": 0.2, "width": 0.3},
    ],
)
def test_normalize_zone_rejects_invalid_rectangles(zone):
    with pytest.raises(RuntimeError, match="typography_designer_zone_invalid"):
        _normalize_zone(zone)