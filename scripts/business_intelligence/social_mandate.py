"""Social media mandate + content territories + brand right-to-speak.

Master Build §31-§34.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import paths
from .schemas import ContentTerritory, SocialMandate


def _social_pillars_path() -> str:
    return os.path.join(paths.data_dir(), "social", "pillars.json")


def load_social_pillars() -> dict[str, Any]:
    p = _social_pillars_path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def build_mandate() -> SocialMandate:
    return SocialMandate(
        social_account_role="Preparedness coach + practical power educator",
        social_account_promise="Follow us and you'll always know what to power first, what to buy when, and how not to overspend on capacity you won't use.",
        audience_value_exchange="You give us attention; we give you actionable preparedness literacy.",
        what_followers_should_gain=[
            "Load-matching literacy",
            "Confidence during outages",
            "Category clarity (bank vs station vs generator)",
            "No-panic preparedness rituals",
        ],
        what_the_account_should_be_known_for=[
            "Plain-language power education",
            "Real-scenario checklists",
            "Product-fit honesty",
        ],
        what_the_account_should_never_become=[
            "Doomsday marketing",
            "Spec-sheet commodity feed",
            "Fear-driven urgency loop",
        ],
        commercial_role="secondary — driven by content usefulness, not by sales cadence",
        educational_role="primary — teach load matching, category basics, outage habits",
        community_role="build a preparedness-minded, calm community",
        authority_role="become the trusted place for power fit guidance",
        entertainment_role="original edutainment, cinematic superhero stories, observational humor and community participation that earn shares, follows and return visits",
        conversation_role="answer real questions; invite scenario sharing",
    )


def build_territories() -> list[ContentTerritory]:
    pillars = load_social_pillars()
    pillar_map = pillars.get("pillars", {}) if isinstance(pillars, dict) else {}
    out: list[ContentTerritory] = []
    for pid, cfg in pillar_map.items():
        out.append(
            ContentTerritory(
                territory_id=pid,
                name=cfg.get("name", pid.replace("_", " ").title()),
                description=cfg.get("description", ""),
                brand_relevance=float(cfg.get("weight", 0.5)),
                audience_relevance=0.7 if pid in {"preparedness", "portable_power", "energy_education", "battery_knowledge"} else 0.55,
                offering_connection=list(cfg.get("engine_fit", [])),
                authority_basis="curated preparedness-first catalog + founder background + per-SKU fit briefs",
                recommended_depth="single-topic educational post" if cfg.get("evergreen") else "series or campaign",
            )
        )
    return out


# --- Brand right-to-speak (§33) --------------------------------------


def right_to_speak(topic_id: str, territories: list[ContentTerritory]) -> dict[str, Any]:
    match = next((t for t in territories if t.territory_id == topic_id), None)
    if not match:
        return {
            "topic": topic_id,
            "eligible": False,
            "reason": "topic not in any registered content territory",
        }
    return {
        "topic": topic_id,
        "eligible": True,
        "brand_relevance": match.brand_relevance,
        "audience_relevance": match.audience_relevance,
        "authority_basis": match.authority_basis,
        "recommended_depth": match.recommended_depth,
    }
