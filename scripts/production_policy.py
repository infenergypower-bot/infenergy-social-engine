"""Resolved, format-specific creative instructions. Owner direction outranks defaults."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION = "creative-policy.v2"
FAMILIES = {
    "cinematic_brand_poster": {"job":"identity","cards":1,"character":False,"product":"optional","typography":"integrated","rubric":["visual meaning","mission connection","originality","mobile impact"]},
    "today_news": {"job":"educate","cards":1,"character":False,"product":"optional","typography":"integrated","rubric":["current source","verified facts","useful perspective","relevance expiry"]},
    "infenergy_micro_mission": {"job": "entertain", "cards": 8, "character": True, "product": "optional", "typography": "integrated", "rubric": ["opening", "story progression", "character action", "continuity", "payoff"]},
    "infenergy_storypage": {"job": "entertain", "cards": 1, "panels":4,"aspect_ratio":"9:16", "character": True, "product": "optional", "typography": "integrated", "rubric": ["reading order", "concise dialogue", "hero action", "mobile readability"]},
    "superhero_text_integration": {"job": "identity", "cards": 1, "character": True, "product": "optional", "typography": "integrated", "rubric": ["one second meaning", "physical word interaction", "character identity", "originality"]},
    "product_proof_story": {"job": "educate", "cards": 1, "character": False, "product": "required", "typography": "integrated", "rubric": ["human situation", "verified product role", "product fidelity", "honest boundary"]},
    "product_intervention": {"job": "entertain", "cards": 3, "character": True, "product": "required", "typography": "integrated", "rubric": ["recognition", "causal product use", "humor or surprise", "earned resolution"]},
    "educational_story_carousel": {"job": "educate", "cards": 4, "character": False, "product": "optional", "typography": "integrated", "rubric": ["curiosity", "useful discovery", "slide progression", "save value"]},
    "humor": {"job": "engage", "cards": 1, "character": False, "product": "optional", "typography": "integrated", "rubric": ["recognition", "timing", "originality", "respect for audience"]},
    "community": {"job": "engage", "cards": 1, "character": False, "product": "optional", "typography": "integrated", "rubric": ["specific question", "reason to participate", "belonging"]},
    "culture_current": {"job": "engage", "cards": 1, "character": False, "product": "optional", "typography": "integrated", "rubric": ["relevance", "source freshness", "taste", "brand connection"]},
    "try_this_irl": {"job": "educate", "cards": 4, "character": False, "product": "optional", "typography": "integrated", "rubric": ["practical action", "safe exercise", "save value"]},
    "energy_identity_statement": {"job": "identity", "cards": 1, "character": True, "product": "optional", "typography": "integrated", "rubric": ["memorable thought", "matched scene", "mission meaning"]},
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def resolve(format_id: str, *, objective: str = "audience_growth", overrides: dict | None = None) -> dict:
    if format_id not in FAMILIES:
        raise ValueError("unknown_content_format")
    if objective not in {"audience_growth", "education", "community", "conversion"}:
        raise ValueError("unknown_objective")
    overrides = dict(overrides or {})
    allowed = {"tone", "natural_response", "typography"}
    if set(overrides) - allowed:
        raise ValueError("unsupported_creative_override")
    family = dict(FAMILIES[format_id])
    from social.content_formats import CONTENT_FORMATS
    original_contract=CONTENT_FORMATS.get(format_id,{})
    typography = overrides.get("typography", family["typography"])
    if typography == "controlled":
        raise ValueError("text_must_be_generated_inside_the_image")
    if typography not in {"integrated", "none"}:
        raise ValueError("invalid_typography_mode")
    if format_id == "superhero_text_integration" and typography != "integrated":
        raise ValueError("physical_typography_requires_integrated_mode")
    response = overrides.get("natural_response", "EXPLORE" if objective == "conversion" else "SHARE")
    if response not in {"SAVE", "SHARE", "FOLLOW", "REFLECT", "PLAN", "LEARN", "COMPARE", "EXPLORE", "CHOOSE", "RESPOND", "PURCHASE", "NONE"}:
        raise ValueError("invalid_natural_response")
    rules = {
        "version": VERSION, "format_id": format_id, **family, "objective": objective,
        "aspect_ratio":family.get("aspect_ratio","4:5"),"format_contract":original_contract,
        "typography": typography, "natural_response": response,
        "tone": overrides.get("tone", "intelligent, human, original; humor, wonder and cinematic action are welcome"),
        "authority": ["explicit_owner_direction", "approved_canons", "format_contract", "campaign", "platform", "experiment"],
        "caption_job": "Add context, insight or payoff; do not transcribe the image. No universal paragraph, hashtag or checkout requirement.",
        "brand": "Preparedness over panic; capability over gadgets; relationship over transaction.",
        "product_rule": "A required product must causally serve the scene using supported capabilities. Otherwise product-free content is valid.",
        "character_rule": "Use approved identity/reference assets; no invented permanent canon or generic superhero substitution.",
        "image_message_rule": "Generate the exact approved wording and the image together. The scene must make its situation, action and useful payoff clear without relying on the caption. No text is added afterward.",
        "legacy_conflicts_superseded": ["no_nonconverting_copy", "checkout_first", "light_entertainment_only", "universal_typography_mode"],
    }
    rules["digest"] = digest(rules)
    return rules


def visual_message_digest(payload: dict, index: int) -> str:
    concept = {key: value for key, value in payload["concept"].items() if key not in {
        "storyboard", "text_layers", "protected_regions", "protected_regions_by_frame",
    }}
    frames = payload["concept"].get("storyboard") or []
    return digest({
        "concept": concept, "frame": frames[index:index+1], "image_text": payload["image_text"][index:index+1],
        "frame_index": index, "rules": payload["rules"], "product_id": payload["product_id"],
        "product_role": payload["product_role"], "evidence": payload["evidence"], "references": payload["references"],
    })


def context() -> dict:
    """Owner sources, not compiled profiles or historical sales defaults."""
    files = ["data/marketing/founder_brand_manifesto.json", "data/marketing/human_truth/constitution.json", "data/marketing/infenergy_company_knowledge.json"]
    sources = []
    for name in files:
        data = json.loads((ROOT / name).read_text())
        sources.append({"path": name, "sha256": digest(data), "data": data})
    return {"sources": sources}


def caption_review(caption: str, *, image_text: list[str], recent: list[str] = ()) -> dict:
    text = " ".join(caption.split())
    findings = []
    if not text:
        findings.append("caption_missing")
    if any(term in text.lower() for term in ["in today's fast-paced world", "game-changer", "unlock the power", "revolutionize"]):
        findings.append("generic_copy")
    image = " ".join(" ".join(image_text).lower().split())
    if image and text.lower() == image:
        findings.append("caption_only_repeats_image")
    if text and text.lower() in {" ".join(x.lower().split()) for x in recent}:
        findings.append("recent_caption_duplicate")
    return {"findings": findings, "requires_editorial_review": True, "automated_checks_passed": not findings}
