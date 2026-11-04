from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from agents import on_image_text_author  # noqa: E402


def _authored_result(*, include_brand: bool) -> dict:
    brand = "Infenergy " if include_brand else ""
    return {
        "statement": "A storm interrupts dinner prep.",
        "expansion": "Keep the refrigerator running while the grid is down.",
        "action": "Connect the backup battery before severe weather arrives.",
        "image_scene": "A parent connects a battery to a refrigerator in a dark kitchen during a storm.",
        "visible_text": {
            "headline": "Dinner Stays On",
            "infenergy_line": "Infenergy keeps the fridge running",
            "resolution_line": "Food stays cold tonight",
        },
        "platform_captions": {
            platform: f"The power is out. Connect {brand}backup power to the refrigerator so food stays cold."
            for platform in ("facebook", "instagram", "linkedin")
        },
    }


def test_author_requires_infenergy_in_every_platform_caption():
    with patch.object(on_image_text_author.model_router, "generate_json", return_value=_authored_result(include_brand=False)) as generate:
        with pytest.raises(RuntimeError, match="gemini_copy_infenergy_role_missing:facebook,instagram,linkedin"):
            on_image_text_author.author({"content_job": "TEACH"})

    assert "exact standalone word Infenergy" in generate.call_args.args[1]


def test_author_accepts_infenergy_in_every_platform_caption():
    with patch.object(on_image_text_author.model_router, "generate_json", return_value=_authored_result(include_brand=True)):
        result = on_image_text_author.author({"content_job": "TEACH"})

    assert all("Infenergy" in caption for caption in result["platform_captions"].values())


def test_production_editor_does_not_force_brand_words_or_hardware(monkeypatch):
    monkeypatch.setenv('PRODUCTION_QUALITY_ENABLED','true')
    with patch.object(on_image_text_author.model_router,'generate_json',return_value=_authored_result(include_brand=False)) as generate:
        on_image_text_author.author({'content_job':'TEACH'})
    prompt=generate.call_args.args[1]
    assert 'do not force a brand mention' in prompt
    assert 'A product-free or non-outage scene is valid' in prompt
