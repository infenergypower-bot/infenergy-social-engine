"""Gemini specialist for public copy and exact on-image text."""

from __future__ import annotations

import json
import re
from typing import Any

from social import model_router

from ._base import utc_now, write_snapshot

FORBIDDEN_LABELS = ("POV:", "FIELD TRUTH")
GENERIC_HEADLINES = {
    "stay connected",
    "power your day",
    "power anywhere",
    "stay powered",
    "be prepared",
    "never stop",
}


def _normalize_headline(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", value.lower()).strip()


def author(brief: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "Create the complete final public copy and exact on-image message for one Infenergy social post. "
        "Return one JSON object with exactly these keys: statement, expansion, action, image_scene, visible_text, platform_captions. "
        "visible_text must contain headline, infenergy_line, resolution_line. platform_captions must contain facebook, instagram, linkedin. "
        "Make each platform caption native, concise, non-repetitive, human, and specific to the consumer moment. Every caption must plainly name the real situation or problem, the action a person should take, the useful outcome, and Infenergy's concrete role. Each of the three platform captions must include the exact standalone word Infenergy. "
        "image_scene must name a specific person, place, activity, power interruption, and visible product use that resolves the moment. "
        "Start with the reason for the post: one specific problem, the product's useful action, and a visible outcome. "
        "The image must communicate that cause and effect without needing the caption to explain it. "
        "Show the actual supported device connection and a visible sign of the useful result, not a product merely sitting nearby. "
        "Do not imply compatibility, automatic backup, runtime, or output not established by the verified facts. "
        "Reject empty tabletop, isolated desk, packshot, generic workspace, and product-only compositions. The product must be actively used by a person in a credible scene. "
        "The visible headline must be five words or fewer and 36 characters or fewer. The other visible lines must be seven words or fewer and 48 characters or fewer. "
        "The headline must express the scene's specific tension, surprise, or human payoff. It must be memorable without becoming a slogan. "
        "Write natural, direct wording that names the actual activity or consequence. Avoid cryptic hooks, vague metaphors, and generic motivation. "
        "infenergy_line is a short supporting explanation of Infenergy's concrete role, not another slogan. "
        "resolution_line names the visible useful outcome. All approved visible words will be generated inside the image, never added afterward. "
        "Reject generic phrases such as Stay Connected, Power Your Day, Power Anywhere, Stay Powered, Be Prepared, or Never Stop. "
        "Make the statement and captions earn the headline with a concrete observation and useful decision. A reader must understand the post without seeing the image. "
        "Avoid abstract shorthand and vague claims such as technical handoff, environmental variables, real-world rehearsal, protect the message, power the handoff, seamless, dependable, essential, freedom, confidence, or peace of mind. Replace them with the actual device, setting, failure, action, and result. "
        "Use plain punctuation and no hashtags in visible text. Never output POV:, FIELD TRUTH, internal taxonomy, unsupported specifications, prices, runtime, guarantees, testimonials, or invented product claims. "
        "Use only verified product facts in the brief. Do not output markdown or additional keys.\n\n"
        f"FACTUAL BRIEF:\n{json.dumps(brief, ensure_ascii=True, sort_keys=True)}"
    )
    result = model_router.generate_json(
        "copy_editing",
        prompt,
        system_instruction=(
            "You are Infenergy's on-image text author. Write publication-ready original copy whose message and visual scene form one precise idea."
        ),
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"gemini_copy_generation_failed:{model_router.last_error() or 'empty_or_invalid_response'}")

    required = ("statement", "expansion", "action", "image_scene")
    visible = result.get("visible_text") if isinstance(result.get("visible_text"), dict) else {}
    captions = result.get("platform_captions") if isinstance(result.get("platform_captions"), dict) else {}
    missing = [key for key in required if not str(result.get(key) or "").strip()]
    missing.extend(f"visible_text.{key}" for key in ("headline", "infenergy_line", "resolution_line") if not str(visible.get(key) or "").strip())
    missing.extend(f"platform_captions.{key}" for key in ("facebook", "instagram", "linkedin") if not str(captions.get(key) or "").strip())
    if missing:
        raise RuntimeError(f"gemini_copy_schema_invalid:{','.join(missing)}")

    public_copy = "\n".join(
        [str(result[key]).strip() for key in required]
        + [str(visible[key]).strip() for key in ("headline", "infenergy_line", "resolution_line")]
        + [str(captions[key]).strip() for key in ("facebook", "instagram", "linkedin")]
    )
    leaked = [label for label in FORBIDDEN_LABELS if label in public_copy.upper()]
    if leaked:
        raise RuntimeError(f"gemini_copy_forbidden_label:{','.join(leaked)}")
    limits = {"headline": (36, 5), "infenergy_line": (48, 7), "resolution_line": (48, 7)}
    oversized = [
        key for key, (characters, words) in limits.items()
        if len(str(visible[key]).strip()) > characters or len(str(visible[key]).split()) > words
    ]
    if oversized:
        raise RuntimeError(f"gemini_copy_visible_text_too_long:{','.join(oversized)}")
    if _normalize_headline(str(visible["headline"])) in GENERIC_HEADLINES:
        raise RuntimeError("gemini_copy_generic_headline")
    if any(len(str(captions[platform])) > 5000 for platform in ("facebook", "instagram", "linkedin")):
        raise RuntimeError("gemini_copy_caption_too_long")
    missing_brand = [
        platform
        for platform in ("facebook", "instagram", "linkedin")
        if "infenergy" not in re.sub(r"[^a-z0-9 ]+", " ", str(captions[platform]).lower()).split()
    ]
    if missing_brand:
        raise RuntimeError(f"gemini_copy_infenergy_role_missing:{','.join(missing_brand)}")
    return result


def run(data_dir: str, brief: dict[str, Any]) -> dict[str, Any]:
    authored = author(brief)
    payload = {
        "agent": "on_image_text_author",
        "time_utc": utc_now(),
        "status": "COMPLETE",
        "model_route": model_router.route_for("copy_editing"),
        "authored": authored,
    }
    payload["snapshot_path"] = write_snapshot(data_dir, "on_image_text_author", payload)
    return payload
