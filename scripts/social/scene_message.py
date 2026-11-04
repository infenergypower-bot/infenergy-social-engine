"""One contract for a scene and the exact text generated inside it."""

from __future__ import annotations

import json
from typing import Any


SCENE_MESSAGE_VERSION = 1
SEMANTIC_QUALITY_VERSION = 3


def scene_message_direction(
    *,
    headline: str,
    supporting: str,
    resolution: str = "",
    problem: str,
    outcome: str,
    scene: str,
    comic: bool = False,
) -> dict[str, Any]:
    lines = [str(line).strip() for line in (headline, supporting, resolution if comic else "") if str(line).strip()]
    if not lines:
        raise ValueError("generated_image_message_missing")
    story = {"problem": problem, "outcome": outcome, "scene": scene}
    instruction = (
        "Generate the scene and all typography together in this single image request. "
        "Render only these exact approved lines, each once and spelled exactly: "
        f"{json.dumps(lines, ensure_ascii=False)}. "
        + (
            "Place the three lines in story order, one in each corresponding panel: problem, Infenergy action, outcome. "
            if comic else
            "Use the first line as the clear headline and the second, if supplied, as a smaller supporting explanation. "
        )
        + "Compose the lettering with the scene from the outset. Use crisp, naturally contrasting letterforms, "
        "professional kerning, and deliberate line breaks without covering faces, hands, connections, or the key action. "
        "No blue shadow, glow, outline, translucent text box, floating card, or generic poster layout. "
        "Do not repeat the headline or invent any additional copy. Do not reserve space for a later overlay. "
        "Return a finished image; no text will be added afterward."
    )
    return {
        "text_overlay": {"enabled": False},
        "gemini_rendered_text": lines,
        "scene_message_version": SCENE_MESSAGE_VERSION,
        "visual_story": story,
        "typography_render_instruction": instruction,
    }


def visual_story_instruction(story: dict[str, Any]) -> str:
    return (
        "VISUAL PURPOSE: The viewer must understand why this post exists without reading its caption. "
        f"Situation or problem: {story.get('problem', '')}. "
        f"Useful outcome: {story.get('outcome', '')}. "
        f"Authored scene: {story.get('scene', '')}. "
        "Make the relationship between the situation, the visible action, and its consequence readable in the scene. "
        "For a product post, show its actual connection and useful role; proximity to a device is not proof of powering it. "
        "Show only applications supported by the verified product brief. Do not imply unverified compatibility, "
        "runtime, output, automatic switchover, or safety guarantees. The wording must explain the same idea the scene shows. "
        "Avoid decorative product placement and generic aspirational slogans."
    )
