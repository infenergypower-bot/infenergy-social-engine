import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from agents.on_image_typography_designer import _normalize_typography, _normalize_zone


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


def test_normalize_typography_uses_safe_brand_defaults():
    result = _normalize_typography({
        "zone": {"x": 0.08, "y": 0.1, "width": 0.42, "height": 0.2},
        "alignment": "diagonal",
        "color": "electric_blue",
        "weight": "light",
        "max_width_ratio": 0.9,
        "canvas_height_ratio": 0.05,
        "tracking": 0.2,
        "line_break": "Changed words",
        "decoration": "No box or glow",
    }, "EXACT HEADLINE")

    assert result == {
        "zone": {"x": 0.08, "y": 0.1, "width": 0.42, "height": 0.2},
        "alignment": "left",
        "color": "warm_white",
        "weight": "bold",
        "max_width_ratio": 0.75,
        "canvas_height_ratio": 0.12,
        "tracking": 0.04,
        "line_break": "EXACT HEADLINE",
        "decoration": "restrained amber accent rule",
    }


def test_normalize_typography_uses_safe_zone_when_designer_zone_is_invalid():
    result = _normalize_typography({
        "zone": {"x": None},
        "alignment": "left",
        "color": "warm_white",
        "weight": "bold",
        "max_width_ratio": 0.5,
        "canvas_height_ratio": 0.15,
        "tracking": 0,
        "line_break": "EXACT HEADLINE",
        "decoration": "restrained amber accent rule",
    }, "EXACT HEADLINE")

    assert result["zone"] == {"x": 0.08, "y": 0.08, "width": 0.55, "height": 0.22}


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