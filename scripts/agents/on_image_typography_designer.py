"""Gemini vision specialist for image-aware typography placement and decoration."""

from __future__ import annotations

import json
import os
from typing import Any

from ._base import utc_now, write_snapshot
from social.gemini_budget import tracked_gemini_call

_ALLOWED_ALIGNMENTS = {"left", "center", "right"}
_ALLOWED_COLORS = {"warm_white", "charcoal", "restrained_amber"}
_ALLOWED_WEIGHTS = {"bold", "black"}


def _normalize_zone(value: Any) -> dict[str, float]:
    required = {"x", "y", "width", "height"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise RuntimeError("typography_designer_zone_invalid")
    try:
        zone = {
            key: float(value[key])
            for key in required
            if not isinstance(value[key], bool)
        }
    except (TypeError, ValueError):
        raise RuntimeError("typography_designer_zone_invalid")
    if set(zone) != required or any(not 0 <= zone[key] <= 1 for key in required):
        raise RuntimeError("typography_designer_zone_invalid")
    normalized = dict(zone)
    if normalized["width"] <= 0 or normalized["height"] <= 0:
        raise RuntimeError("typography_designer_zone_invalid")
    normalized["width"] = min(normalized["width"], 1 - normalized["x"])
    normalized["height"] = min(normalized["height"], 1 - normalized["y"])
    if normalized["width"] <= 0 or normalized["height"] <= 0:
        raise RuntimeError("typography_designer_zone_invalid")
    return normalized


def design(image_bytes: bytes, headline: str, platform: str) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("typography_designer_gemini_api_key_missing")
    if not image_bytes:
        raise RuntimeError("typography_designer_image_missing")
    if not headline.strip():
        raise RuntimeError("typography_designer_headline_missing")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("typography_designer_sdk_unavailable") from exc

    prompt = (
        "You are Infenergy's senior on-image typography designer. Inspect the actual image pixels and design the exact headline as part of the photographic composition. "
        f"Platform: {platform}. Exact headline: {json.dumps(headline, ensure_ascii=True)}. "
        "Return JSON only with exactly these keys: zone, anchor, alignment, max_width_ratio, canvas_height_ratio, line_break, color, weight, tracking, decoration, rationale, protected_regions. "
        "zone must be a normalized rectangle with numeric x, y, width, height values from 0 to 1 identifying genuinely open negative space; x + width and y + height must each be no greater than 1. "
        "anchor must be a concrete visual edge or gesture in the image. alignment must be left, center, or right. max_width_ratio must be 0.35 to 0.75. canvas_height_ratio must be 0.12 to 0.18 so the headline is an unmistakable first-read element, not a small label. "
        "line_break must contain the exact headline words with either one line or one deliberate newline; do not change spelling or punctuation. "
        "color must be warm_white, charcoal, or restrained_amber. weight must be bold or black. tracking must be between -0.01 and 0.04. "
        "decoration must describe a restrained image-specific accent rule or subtle underline that visibly connects the headline to the composition. Never use a box, pill, banner, card, outline, glow, blue shadow, extrusion, generic poster lockup, or no decoration. "
        "protected_regions must list the person, face, hands, product, cable, and key action regions that typography must not cover. "
        "Choose optical hierarchy, contrast, line break, and placement from the actual image, not from a template."
    )
    client = genai.Client(api_key=api_key)
    model = os.environ.get("GEMINI_VISUAL_QA_MODEL", "gemini-2.5-flash")
    with tracked_gemini_call("reasoning", model, f"typography design for {platform}", initiating_subsystem="on_image_typography_designer"):
        response = client.models.generate_content(
            model=model,
            contents=[prompt, types.Part.from_bytes(data=image_bytes, mime_type="image/png")],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
    result = json.loads(str(response.text or "{}"))
    if not isinstance(result, dict):
        raise RuntimeError("typography_designer_invalid_response")
    required = {
        "zone", "anchor", "alignment", "max_width_ratio", "canvas_height_ratio", "line_break",
        "color", "weight", "tracking", "decoration", "rationale", "protected_regions",
    }
    missing = sorted(required - set(result))
    if missing:
        raise RuntimeError(f"typography_designer_schema_invalid:{','.join(missing)}")
    result["zone"] = _normalize_zone(result.get("zone"))
    if result["alignment"] not in _ALLOWED_ALIGNMENTS or result["color"] not in _ALLOWED_COLORS or result["weight"] not in _ALLOWED_WEIGHTS:
        raise RuntimeError("typography_designer_style_invalid")
    if not 0.35 <= float(result["max_width_ratio"]) <= 0.75 or not 0.12 <= float(result["canvas_height_ratio"]) <= 0.18 or not -0.01 <= float(result["tracking"]) <= 0.04:
        raise RuntimeError("typography_designer_scale_invalid")
    if str(result["line_break"]).replace("\n", " ").strip() != headline.strip():
        raise RuntimeError("typography_designer_text_changed")
    banned = ("box", "pill", "banner", "card", "outline", "glow", "blue shadow", "extrusion", "no decoration", "none")
    if any(token in str(result["decoration"]).lower() for token in banned):
        raise RuntimeError("typography_designer_decoration_rejected")
    return result


def design_receipt(data_dir: str, image_bytes: bytes, headline: str, platform: str) -> dict[str, Any]:
    typography = design(image_bytes, headline, platform)
    payload = {
        "agent": "on_image_typography_designer",
        "time_utc": utc_now(),
        "status": "COMPLETE",
        "headline": headline,
        "platform": platform,
        "typography": typography,
    }
    payload["snapshot_path"] = write_snapshot(data_dir, "on_image_typography_designer", payload)
    return payload


def run(data_dir: str, image_path: str, headline: str, platform: str = "instagram") -> dict[str, Any]:
    if not image_path or not os.path.isfile(image_path):
        raise RuntimeError("typography_designer_image_missing")
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()
    payload = design_receipt(data_dir, image_bytes, headline, platform)
    payload["image_path"] = image_path
    return payload
