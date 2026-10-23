"""Publish already-final content from the durable outbox without regenerating it."""

from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import publish_facebook
import publish_instagram
import publish_linkedin
from platform_publishing import get_status as get_platform_status
from social import model_router
from social_visuals import build_product_image_approval, generate_strict_gemini_image, review_rendered_visual
from build_monthly_content import _captions, _gemini_generation_plan, _load_current_news
from content_operations import (
    PLATFORMS,
    begin_platform_transaction,
    claim_due,
    complete_platform_transaction,
    configured_platforms,
    evaluate_outbox_readiness,
    finalize_outbox,
    platform_transaction,
    record_publication_history,
    reconcile_confirmed_transactions,
    recover_outbox,
    release_outbox,
    transition_package,
    upcoming_ready_packages,
    update_claimed_package,
    update_ready_package,
)
from publication_contract import PackageState
from runtime_config import load_runtime_config
from social.gemini_budget import GeminiBudgetExceeded, preflight_gemini_workflow

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
FORBIDDEN_PUBLIC_LABELS = ("POV:", "FIELD TRUTH")
AMBIGUOUS_COPY_PHRASES = (
    "technical handoff",
    "environmental variable",
    "real-world rehearsal",
    "protect the message",
    "power the handoff",
)
ACTION_WORDS = {
    "assemble", "bring", "build", "carry", "charge", "check", "connect", "download", "keep", "label", "pack",
    "place", "plug", "power", "run", "save", "set", "store", "test", "use",
}


def _copy_clarity_issues(result: dict[str, Any]) -> list[str]:
    captions = result.get("platform_captions") if isinstance(result.get("platform_captions"), dict) else {}
    issues: list[str] = []
    for platform in PLATFORMS:
        caption = re.sub(r"\s+", " ", str(captions.get(platform) or "")).strip()
        normalized = re.sub(r"[^a-z0-9 ]+", " ", caption.lower())
        words = set(normalized.split())
        if any(phrase in normalized for phrase in AMBIGUOUS_COPY_PHRASES):
            issues.append(f"{platform}_ambiguous_language")
        if "infenergy" not in words:
            issues.append(f"{platform}_infenergy_role_missing")
        if not words.intersection(ACTION_WORDS):
            issues.append(f"{platform}_concrete_action_missing")
        if len(words) < 14 or len(re.findall(r"[.!?](?:\s|$)", caption)) < 2:
            issues.append(f"{platform}_problem_action_outcome_unclear")
    return issues


def _delivery_enforced() -> bool:
    return str(os.environ.get("ENFORCE_SOCIAL_DELIVERY", "false")).strip().lower() in {"1", "true", "yes", "on"}


def _enabled_platforms(package: dict[str, Any]) -> list[str]:
    configured = configured_platforms(package)
    if configured:
        return configured
    return list(PLATFORMS) if _delivery_enforced() else []


def _platform_due_at(package: dict[str, Any], platform: str, fallback: str) -> datetime:
    schedule = package.get("platform_schedule") if isinstance(package.get("platform_schedule"), dict) else {}
    value = str(schedule.get(platform) or fallback)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _payload(package: dict[str, Any], platform: str) -> dict[str, Any]:
    platform_posts = package.get("platform_posts") if isinstance(package.get("platform_posts"), dict) else {}
    post = platform_posts.get(platform) if isinstance(platform_posts.get(platform), dict) else {}
    carousel_media = [
        str(asset.get("local_path") if platform == "facebook" else asset.get("public_url") or "").strip()
        for asset in package.get("carousel_assets", []) or []
        if isinstance(asset, dict)
    ]
    return {
        "final_caption": str(post.get("final_caption") or post.get("caption") or ""),
        "destination_url": str(post.get("destination_url") or package.get("destination_url") or ""),
        "utm_url": str(post.get("utm_url") or post.get("destination_url") or package.get("destination_url") or ""),
        "media": str((package.get("generated_visuals") or {}).get(platform) or ""),
        "carousel_media": [media for media in carousel_media if media] if platform in {"facebook", "instagram"} else [],
    }


def _publish(package: dict[str, Any], platform: str) -> dict[str, Any]:
    post = ((package.get("platform_posts") or {}).get(platform) or {})
    delivery_package = dict(package)
    image_url = str(post.get("image_url") or package.get("image_url") or "").strip()
    if image_url.startswith("http") and not str(delivery_package.get("primary_publish_image_url") or "").strip():
        delivery_package["primary_publish_image_url"] = image_url
        delivery_package["owner_supplied_visual"] = True
    final_caption = str(post.get("final_caption") or post.get("caption") or "")
    caption_keys = {"facebook": "fb_caption", "instagram": "ig_caption", "linkedin": "li_text"}
    delivery_package[caption_keys[platform]] = final_caption
    destination = str(post.get("utm_url") or post.get("destination_url") or package.get("destination_url") or "")
    if platform == "facebook":
        return publish_facebook.publish(delivery_package, destination, dry_run=False)
    if platform == "instagram":
        return publish_instagram.publish(delivery_package, dry_run=False)
    if platform == "linkedin":
        return publish_linkedin.publish(delivery_package, destination, dry_run=False)
    raise ValueError(f"unsupported platform: {platform}")


def _public_media_url(file_name: str) -> str:
    base = str(os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if base and not base.startswith("http"):
        base = f"https://{base}"
    if not base:
        raise RuntimeError("public_media_base_url_missing")
    return f"{base}/media/{file_name}"


def _gemini_assets_ready(package: dict[str, Any], required_count: int) -> bool:
    generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
    assets = generation.get("assets") if isinstance(generation.get("assets"), list) else []
    return (
        generation.get("status") == "COMPLETE"
        and len(assets) == required_count
        and all(
            isinstance(asset, dict)
            and asset.get("render_engine") == "gemini"
            and (asset.get("generation") or {}).get("semantic_quality_gate_version") == 2
            and os.path.isfile(str(asset.get("local_path") or ""))
            and str(asset.get("public_url") or "").startswith("http")
            and review_rendered_visual(str(asset.get("local_path") or ""), "instagram").get("verdict") == "PASS"
            for asset in assets
        )
    )


def _refresh_current_news_package(package: dict[str, Any]) -> dict[str, Any]:
    if package.get("weekly_role") != "current_news":
        return package
    news = _load_current_news(10)
    if not news:
        raise RuntimeError("current_news_refresh_unavailable")
    content_date = str(package.get("content_date") or "")
    selected = news[int(hashlib.sha256(content_date.encode("utf-8")).hexdigest()[:8], 16) % len(news)]
    thought = package.get("generation_thought") if isinstance(package.get("generation_thought"), dict) else {}
    thought.update({
        "statement": selected["title"],
        "overlay_text": selected["title"],
        "instagram_hook": selected["title"],
        "source_note": selected["url"],
        "source_published_at": selected.get("published", ""),
        "event_series": f"daily-news-{content_date}",
    })
    captions = _captions(thought)
    package.update({
        "thought_statement": thought["statement"],
        "editorial_sources": [selected["url"]],
        "event_series": thought["event_series"],
        "generation_thought": thought,
        "fb_caption": captions["facebook"],
        "ig_caption": captions["instagram"],
        "li_text": captions["linkedin"],
        "gemini_generation": _gemini_generation_plan(thought),
        "news_freshness": {
            "status": "REFRESHED",
            "refreshed_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_published_at": selected.get("published", ""),
            "source_url": selected["url"],
        },
    })
    copy_plan = package.get("gemini_copy") if isinstance(package.get("gemini_copy"), dict) else {}
    if copy_plan.get("strict_provider") is True:
        copy_plan.update({"status": "PENDING", "model_output_sha256": "", "source_statement": thought["statement"]})
        package["gemini_copy"] = copy_plan
    for platform, caption in captions.items():
        post = ((package.get("platform_posts") or {}).get(platform) or {})
        post["caption"] = caption
        post["final_caption"] = caption
    package["master_copy"] = {
        key: thought.get(key)
        for key in ("statement", "expansion", "useful_detail", "action", "prompt", "linkedin_lens", "editorial_mode", "audience", "source_note", "overlay_text")
        if thought.get(key)
    }
    return package


def _strict_publish_artifact_error(package: dict[str, Any], platform: str) -> str:
    generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
    if generation.get("strict_provider") is not True:
        return ""
    visuals = package.get("generated_visuals") if isinstance(package.get("generated_visuals"), dict) else {}
    artifact_path = str(visuals.get(platform) or "").strip()
    review_platform = {
        "4:5": "iis_carousel",
        "9:16": "iis_reel_cover",
    }.get(str(generation.get("aspect_ratio") or "").strip(), "instagram")
    review = review_rendered_visual(artifact_path, review_platform)
    if review.get("verdict") == "PASS":
        return ""
    issues = review.get("issues") if isinstance(review.get("issues"), list) else []
    return f"{platform}_strict_artifact_invalid:{','.join(str(issue) for issue in issues) or 'artifact_review_failed'}"


def _gemini_copy_ready(package: dict[str, Any]) -> bool:
    copy_plan = package.get("gemini_copy") if isinstance(package.get("gemini_copy"), dict) else {}
    return (
        copy_plan.get("provider") == "gemini"
        and copy_plan.get("strict_provider") is True
        and copy_plan.get("status") == "COMPLETE"
        and bool(copy_plan.get("model_output_sha256"))
        and (copy_plan.get("qa") or {}).get("clarity") == "PASS"
    )


def _hydrate_product_image_approval(package: dict[str, Any]) -> dict[str, Any]:
    if not str(package.get("product_id") or "").strip():
        return package
    approval = build_product_image_approval(package, str(package.get("product_image_url") or "").strip())
    if not approval:
        raise RuntimeError("product_reference_identity_not_approved")
    package["product_image_approval"] = approval
    return package


def _prepare_gemini_copy(package: dict[str, Any], data_dir: str = DATA_DIR) -> dict[str, Any]:
    copy_plan = package.get("gemini_copy") if isinstance(package.get("gemini_copy"), dict) else {}
    if copy_plan.get("provider") != "gemini" or copy_plan.get("strict_provider") is not True or copy_plan.get("fallback_allowed") is not False:
        raise RuntimeError("strict_gemini_copy_contract_missing")
    if _gemini_copy_ready(package):
        return package

    thought = package.get("generation_thought") if isinstance(package.get("generation_thought"), dict) else {}
    contract = package.get("generation_contract") if isinstance(package.get("generation_contract"), dict) else {}
    receipt = package.get("consumer_receipt") if isinstance(package.get("consumer_receipt"), dict) else {}
    brief = {
        "brand": "Infenergy Power",
        "content_date": package.get("content_date"),
        "format": contract.get("format"),
        "audience": contract.get("audience_name") or thought.get("audience"),
        "creative_territory": contract.get("creative_territory") or thought.get("pillar"),
        "content_job": contract.get("content_job"),
        "consumer_moment": receipt,
        "story_sequence": contract.get("story_sequence") or [],
        "product": {
            "name": package.get("product_name"),
            "verified_facts": package.get("product_verified_facts") or [],
            "proof_rule": package.get("product_proof_rule"),
            "role": contract.get("product_role"),
        },
        "source_concept": {
            "hook": contract.get("hook") or thought.get("statement"),
            "takeaway": contract.get("takeaway") or thought.get("expansion"),
            "cta": contract.get("cta") or thought.get("action"),
        },
    }
    from agents.dispatcher import run_agent
    author_receipt = run_agent("on_image_text_author", data_dir, {"brief": brief})
    if author_receipt.get("error"):
        raise RuntimeError(f"on_image_text_author_failed:{author_receipt.get('detail') or author_receipt['error']}")
    result = author_receipt.get("authored")
    if not isinstance(result, dict):
        raise RuntimeError("on_image_text_author_invalid_output")

    required_strings = ("statement", "expansion", "action", "image_scene")
    visible = result.get("visible_text") if isinstance(result.get("visible_text"), dict) else {}
    captions = result.get("platform_captions") if isinstance(result.get("platform_captions"), dict) else {}
    missing = [key for key in required_strings if not str(result.get(key) or "").strip()]
    missing.extend(f"visible_text.{key}" for key in ("headline", "infenergy_line", "resolution_line") if not str(visible.get(key) or "").strip())
    missing.extend(f"platform_captions.{key}" for key in PLATFORMS if not str(captions.get(key) or "").strip())
    if missing:
        raise RuntimeError(f"gemini_copy_schema_invalid:{','.join(missing)}")

    public_strings = [str(result[key]).strip() for key in required_strings]
    public_strings.extend(str(visible[key]).strip() for key in ("headline", "infenergy_line", "resolution_line"))
    public_strings.extend(str(captions[key]).strip() for key in PLATFORMS)
    public_copy = "\n".join(public_strings)
    leaked = [label for label in FORBIDDEN_PUBLIC_LABELS if label in public_copy.upper()]
    if leaked:
        raise RuntimeError(f"gemini_copy_forbidden_label:{','.join(leaked)}")
    if any(len(str(captions[platform])) > 5000 for platform in PLATFORMS):
        raise RuntimeError("gemini_copy_caption_too_long")
    visible_limits = {"headline": (36, 5), "infenergy_line": (48, 7), "resolution_line": (48, 7)}
    oversized_visible = [
        key for key, (character_limit, word_limit) in visible_limits.items()
        if len(str(visible[key]).strip()) > character_limit or len(str(visible[key]).split()) > word_limit
    ]
    if oversized_visible:
        raise RuntimeError(f"gemini_copy_visible_text_too_long:{','.join(oversized_visible)}")
    generic_headlines = {
        "stay connected", "power your day", "power anywhere", "stay powered", "be prepared", "never stop",
    }
    normalized_headline = re.sub(r"[^a-z0-9 ]+", "", str(visible["headline"]).lower()).strip()
    if normalized_headline in generic_headlines:
        raise RuntimeError("gemini_copy_generic_headline")
    clarity_issues = _copy_clarity_issues(result)
    if clarity_issues:
        raise RuntimeError(f"gemini_copy_clarity_rejected:{','.join(clarity_issues)}")

    thought.update({
        "statement": str(result["statement"]).strip(),
        "overlay_text": str(visible["headline"]).strip(),
        "instagram_hook": str(result["statement"]).strip(),
        "expansion": str(result["expansion"]).strip(),
        "action": str(result["action"]).strip(),
        "image_scene": str(result["image_scene"]).strip(),
    })
    contract["visible_text"] = {key: str(visible[key]).strip() for key in ("headline", "infenergy_line", "resolution_line")}
    package.update({
        "thought_statement": thought["statement"],
        "generation_thought": thought,
        "generation_contract": contract,
        "fb_caption": str(captions["facebook"]).strip(),
        "ig_caption": str(captions["instagram"]).strip(),
        "li_text": str(captions["linkedin"]).strip(),
        "master_copy": {
            "statement": thought["statement"],
            "expansion": thought["expansion"],
            "action": thought["action"],
            "overlay_text": thought["overlay_text"],
        },
        "copy_generation_source": "gemini",
        "agent_orchestration": {
            **(package.get("agent_orchestration") if isinstance(package.get("agent_orchestration"), dict) else {}),
            "on_image_text_author": {
                "status": author_receipt.get("status"),
                "model_route": author_receipt.get("model_route"),
                "snapshot_path": author_receipt.get("snapshot_path"),
            },
        },
    })
    for platform, caption in (("facebook", package["fb_caption"]), ("instagram", package["ig_caption"]), ("linkedin", package["li_text"])):
        post = ((package.get("platform_posts") or {}).get(platform) or {})
        post["caption"] = caption
        post["final_caption"] = caption
        post["final_caption_qa"] = {
            "status": "PRESENTATION_READY",
            "reasons": [],
            "provider": "gemini",
            "clarity": "PASS",
        }

    from validate_product_claims import validate_generated_content
    claim_review = validate_generated_content(package)
    if not claim_review.passed:
        raise RuntimeError(f"gemini_copy_claims_rejected:{','.join(claim_review.get('errors') or [])}")

    old_statement = str(copy_plan.get("source_statement") or package.get("thought_statement") or "")
    from build_monthly_content import _gemini_generation_plan, _load_product_brief
    product_id = str(package.get("product_id") or thought.get("product_id") or "")
    product = _load_product_brief(data_dir, product_id) if product_id else None
    generation = _gemini_generation_plan(thought, product)
    package["gemini_generation"] = generation
    for prompt_plan in generation.get("prompts") or []:
        if not isinstance(prompt_plan, dict):
            continue
        scene_prompt = str(prompt_plan.get("gemini_image_prompt") or "")
        if old_statement:
            scene_prompt = scene_prompt.replace(old_statement, thought["statement"])
        for legacy_instruction in (
            "Keep every exact dialogue line readable inside Instagram Story safe areas. ",
            "Do not render words, letters, logos, numbers, labels, watermarks, UI, badges, product claims, or fake specifications. ",
            "Do not draw dialogue, captions, speech bubbles, labels, or lettering. ",
        ):
            scene_prompt = scene_prompt.replace(legacy_instruction, "")
        prompt_plan["gemini_image_prompt"] = scene_prompt
        prompt_plan["prompt_sha256"] = hashlib.sha256(scene_prompt.encode("utf-8")).hexdigest()
        direction = prompt_plan.get("v5_direction") if isinstance(prompt_plan.get("v5_direction"), dict) else {}
        overlay = direction.get("text_overlay") if isinstance(direction.get("text_overlay"), dict) else {}
        rendered_text = [str(contract["visible_text"]["headline"]).strip()]
        overlay.clear()
        overlay["enabled"] = False
        direction["gemini_rendered_text"] = rendered_text
        direction["gemini_typography_contract"] = {
            "exact_text_only": True,
            "blue_shadow_allowed": False,
            "blue_glow_allowed": False,
            "duplicated_text_allowed": False,
            "extra_text_allowed": False,
            "hierarchy": "single editorial headline",
            "placement": "scene-aware negative space",
            "style": "premium editorial grotesk",
        }
        text_instruction = (
            "Render these Gemini-authored lines directly into the finished image, each exactly once and spelled exactly: "
            + json.dumps(rendered_text, ensure_ascii=True)
            + ". Treat the typography with the same craft and specificity as the photography. Use one confident premium editorial grotesk with optical kerning, balanced tracking, and a deliberate line break only when it strengthens meaning. Build a clear single-headline hierarchy at roughly 8-12% of canvas height. Align it to a strong scene edge or subject gesture inside genuinely open negative space; never center it mechanically across the product or person's face. Choose warm white, charcoal, or restrained amber according to the local background for strong natural contrast. Integrate the headline into the composition so image and message read as one idea. Use crisp solid letterforms with no blue shadow, no blue glow, no neon edge, no outline, no bevel, no fake 3D extrusion, and no dark or translucent text box. Do not use a floating card, banner, pill, label, or generic poster lockup. Render the supplied headline once only. Do not repeat it at the bottom or add secondary captions. Render no other words, letters, numbers, captions, dialogue, labels, or logos."
        )
        direction["typography_render_instruction"] = text_instruction
        prompt_plan["gemini_image_prompt"] = scene_prompt
        prompt_plan["prompt_sha256"] = hashlib.sha256(scene_prompt.encode("utf-8")).hexdigest()

    output_digest = hashlib.sha256(json.dumps(result, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()
    copy_plan.update({
        "status": "COMPLETE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_route": model_router.route_for(str(copy_plan.get("task") or "copy_editing")),
        "model_output_sha256": output_digest,
        "qa": {"schema": "PASS", "forbidden_labels": "PASS", "clarity": "PASS", "product_claims": "PASS"},
    })
    package["gemini_copy"] = copy_plan
    return package


def _prepare_gemini_assets(package: dict[str, Any], data_dir: str) -> dict[str, Any]:
    generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
    copy_plan = package.get("gemini_copy") if isinstance(package.get("gemini_copy"), dict) else {}
    if copy_plan.get("strict_provider") is True and not _gemini_copy_ready(package):
        raise RuntimeError("strict_gemini_copy_not_ready")
    prompts = generation.get("prompts") if isinstance(generation.get("prompts"), list) else []
    required_count = int(generation.get("required_image_count") or 0)
    if generation.get("provider") != "gemini" or generation.get("fallback_allowed") is not False:
        raise RuntimeError("strict_gemini_generation_contract_missing")
    if required_count < 1 or len(prompts) != required_count:
        raise RuntimeError("gemini_prompt_count_mismatch")
    if _gemini_assets_ready(package, required_count):
        return package

    public_dir = os.path.join(data_dir, "public_media")
    os.makedirs(public_dir, exist_ok=True)
    post_id = str(package.get("post_id") or package.get("content_id") or "monthly")
    generation_platform = {
        "9:16": "iis_reel_cover",
        "4:5": "iis_carousel",
    }.get(str(generation.get("aspect_ratio") or ""), "instagram")
    assets: list[dict[str, Any]] = []
    for prompt_plan in prompts:
        if not isinstance(prompt_plan, dict):
            raise RuntimeError("gemini_prompt_invalid")
        slide_index = int(prompt_plan.get("slide_index") or len(assets) + 1)
        file_name = f"{post_id}_gemini_{slide_index}.png"
        output_path = os.path.abspath(os.path.join(public_dir, file_name))
        result = generate_strict_gemini_image(
            package,
            prompt_plan=prompt_plan,
            output_path=output_path,
            platform=generation_platform,
        )
        result.update({
            "slide_index": slide_index,
            "role": str(prompt_plan.get("role") or "image"),
            "public_url": _public_media_url(file_name),
        })
        assets.append(result)

    if len(assets) != required_count or any(asset.get("render_engine") != "gemini" for asset in assets):
        raise RuntimeError("strict_gemini_generation_incomplete")
    typography_runs = [
        asset["generation"]["agent_orchestration"]["on_image_typography_designer"]
        for asset in assets
        if isinstance(asset.get("generation"), dict)
        and isinstance(asset["generation"].get("agent_orchestration"), dict)
        and isinstance(asset["generation"]["agent_orchestration"].get("on_image_typography_designer"), dict)
    ]
    if typography_runs:
        orchestration = package.get("agent_orchestration") if isinstance(package.get("agent_orchestration"), dict) else {}
        orchestration["on_image_typography_designer"] = {
            "status": "COMPLETE",
            "asset_count": len(typography_runs),
            "runs": typography_runs,
        }
        package["agent_orchestration"] = orchestration
    generation.update({
        "status": "COMPLETE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "actual_image_count": len(assets),
        "assets": assets,
        "qa": {
            gate: {
                "status": "PASS",
                "explanation": "All generated assets passed semantic QA v2 and deterministic artifact review.",
                "severity": "BLOCKING",
                "evidence": [str(asset.get("local_path") or "") for asset in assets],
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "evaluator": "social_visuals.semantic_and_artifact_review",
                "evaluator_version": "2",
            }
            for gate in ("TEXT_QA", "STORY_QA", "VISUAL_QA", "CONTINUITY_QA", "ORIGINALITY_QA", "EMOTIONAL_QA")
        },
    })
    package["gemini_generation"] = generation
    package["carousel_assets"] = assets if len(assets) > 1 else []
    first = assets[0]
    package["primary_publish_image_url"] = first["public_url"]
    package["generated_visuals"] = {
        "facebook": first["local_path"],
        "instagram": first["local_path"],
        "linkedin": first["local_path"],
        "render_engine": "gemini",
        "render_engines": {platform: "gemini" for platform in PLATFORMS},
        "artifact_reviews": {platform: first["review"] for platform in PLATFORMS},
        "gemini_generated_at_utc": generation["generated_at_utc"],
    }
    return package


def pregenerate_upcoming(*, data_dir: str = DATA_DIR) -> dict[str, Any]:
    lead_minutes = load_runtime_config().preparation_lead_time_minutes
    now_utc = datetime.now(timezone.utc)
    before_utc = (now_utc + timedelta(minutes=lead_minutes)).isoformat()
    stale_grace_hours = max(0, int(os.environ.get("CONTENT_PREGENERATION_STALE_GRACE_HOURS", "6")))
    after_utc = (now_utc - timedelta(hours=stale_grace_hours)).isoformat()
    rows = upcoming_ready_packages(data_dir, before_utc=before_utc, after_utc=after_utc, limit=100)
    news_before_utc = datetime.now(timezone.utc) + timedelta(hours=24)
    for row in rows:
        package = row["package"]
        if package.get("weekly_role") == "current_news":
            scheduled_at = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
            if scheduled_at > news_before_utc:
                continue
            package = _refresh_current_news_package(package)
        generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
        required_count = int(generation.get("required_image_count") or 0)
        copy_plan = package.get("gemini_copy") if isinstance(package.get("gemini_copy"), dict) else {}
        if generation.get("strict_provider") is not True or (_gemini_copy_ready(package) and _gemini_assets_ready(package, required_count)):
            continue
        outbox_id = str(row["outbox_id"])
        try:
            lifecycle_state = row.get("lifecycle_state")
            if lifecycle_state == PackageState.BLOCKED_BUDGET.value:
                attempts_per_image = max(0, min(int(os.environ.get("GEMINI_IMAGE_REPAIR_ATTEMPTS", "0")), 5)) + 1
                image_calls = required_count * attempts_per_image
                reasoning_calls = image_calls + (0 if _gemini_copy_ready(package) else 1)
                try:
                    preflight_gemini_workflow(image_calls=image_calls, reasoning_calls=reasoning_calls)
                except GeminiBudgetExceeded:
                    continue
            if lifecycle_state in {PackageState.FAILED_PREPARATION.value, PackageState.BLOCKED_BUDGET.value}:
                transition_package(
                    data_dir,
                    outbox_id,
                    PackageState.PREPARATION_REQUIRED.value,
                    "BUDGET_CAPACITY_RESTORED" if lifecycle_state == PackageState.BLOCKED_BUDGET.value else "PREPARATION_RETRY_SCHEDULED",
                    actor="dispatch_outbox.pregenerate",
                )
            transition_package(data_dir, outbox_id, PackageState.PREPARING.value, "PREPARATION_WINDOW_OPEN", actor="dispatch_outbox.pregenerate")
            existing_approval = package.get("product_image_approval")
            package = _hydrate_product_image_approval(package)
            if package.get("product_image_approval") != existing_approval and not update_ready_package(data_dir, outbox_id, package):
                return {"status": "DEFERRED", "outbox_id": outbox_id, "detail": "package_no_longer_ready"}
            if copy_plan.get("strict_provider") is True:
                copy_was_ready = _gemini_copy_ready(package)
                package = _prepare_gemini_copy(package, data_dir)
                if not copy_was_ready and not update_ready_package(data_dir, outbox_id, package):
                    return {"status": "DEFERRED", "outbox_id": outbox_id, "detail": "package_no_longer_ready"}
            prepared = _prepare_gemini_assets(package, data_dir)
            if not update_ready_package(data_dir, outbox_id, prepared):
                return {"status": "DEFERRED", "outbox_id": outbox_id, "detail": "package_no_longer_ready"}
            transition_package(data_dir, outbox_id, PackageState.AWAITING_QA.value, "CREATIVE_GENERATED", actor="dispatch_outbox.pregenerate")
            readiness = evaluate_outbox_readiness(data_dir, outbox_id, dispatch_enabled=True, actor="dispatch_outbox.pregenerate")
            transition_package(data_dir, outbox_id, readiness["state"], "READINESS_PASSED" if readiness["ready"] else readiness["reason_codes"][0], actor="dispatch_outbox.pregenerate", detail=readiness)
            return {"status": "PREGENERATED", "outbox_id": outbox_id}
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            if "budget" in error.lower() or "exhausted" in error.lower():
                state, reason = PackageState.BLOCKED_BUDGET.value, "AI_BUDGET_EXHAUSTED"
            elif "canon" in error.lower():
                state, reason = PackageState.BLOCKED_CANON.value, "CANON_NOT_FOUND"
            else:
                state, reason = PackageState.FAILED_PREPARATION.value, "PREPARATION_FAILED"
            transition_package(data_dir, outbox_id, state, reason, actor="dispatch_outbox.pregenerate", detail={"error": error})
            return {"status": state, "outbox_id": outbox_id, "reason_code": reason, "error": error}
    return {"status": "IDLE", "detail": "no_upcoming_gemini_assets_needed"}


def _creative_package_error(package: dict[str, Any], platforms: list[str]) -> str:
    visuals = package.get("generated_visuals") if isinstance(package.get("generated_visuals"), dict) else {}
    engines = visuals.get("render_engines") if isinstance(visuals.get("render_engines"), dict) else {}
    reviews = visuals.get("artifact_reviews") if isinstance(visuals.get("artifact_reviews"), dict) else {}
    visual_plan = package.get("visual_plan") if isinstance(package.get("visual_plan"), dict) else {}
    route = str(visual_plan.get("creative_route") or visual_plan.get("visual_format") or "").strip().upper()
    explicit_packshot = route in {"PACKSHOT", "PACKSHOT_ONLY", "PREMIUM_PRODUCT_HERO"}
    for platform in platforms:
        review = reviews.get(platform) if isinstance(reviews.get(platform), dict) else {}
        if review.get("verdict") == "REGENERATE_VISUAL":
            return f"{platform}_visual_requires_recovery"
        if str(engines.get(platform) or "") == "approved_product_photo" and not explicit_packshot:
            return f"{platform}_packshot_only_without_explicit_route"
    return ""


def dispatch_due(
    *,
    data_dir: str = DATA_DIR,
    now_utc: str | None = None,
    outbox_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    claimed = claim_due(data_dir, now_utc, outbox_id=outbox_id, force=force)
    if not claimed:
        return {"status": "IDLE", "detail": "no_due_ready_content"}

    outbox_id = str(claimed["outbox_id"])
    package = claimed["package"]
    platforms = _enabled_platforms(package)
    if not platforms:
        finalize_outbox(data_dir, outbox_id, status="EXTERNAL_ACTION_REQUIRED", error="no_routed_platforms")
        return {"status": "EXTERNAL_ACTION_REQUIRED", "outbox_id": outbox_id, "error": "no_routed_platforms"}
    now = datetime.fromisoformat(now_utc.replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    due_platforms = platforms if force else [
        platform for platform in platforms
        if _platform_due_at(package, platform, str(claimed["scheduled_at"])) <= now
    ]
    if not due_platforms:
        next_due = min(_platform_due_at(package, platform, str(claimed["scheduled_at"])) for platform in platforms)
        transition_package(data_dir, outbox_id, PackageState.READY_TO_DISPATCH.value, "PLATFORM_WINDOW_NOT_DUE", actor="dispatch_outbox", attempt_id=claimed.get("active_attempt_id"))
        release_outbox(data_dir, outbox_id, "", next_attempt_at=next_due.isoformat())
        return {"status": "DEFERRED", "outbox_id": outbox_id, "next_attempt_at": next_due.isoformat()}
    readiness = evaluate_outbox_readiness(data_dir, outbox_id, dispatch_enabled=True, actor="dispatch_outbox.preflight")
    if not readiness["ready"]:
        transition_package(
            data_dir, outbox_id, readiness["state"], readiness["reason_codes"][0],
            actor="dispatch_outbox.preflight", attempt_id=claimed.get("active_attempt_id"), detail=readiness,
        )
        release_outbox(data_dir, outbox_id, readiness["reason_codes"][0], reset_attempts=True)
        return {"status": "AWAITING_PREPARATION", "outbox_id": outbox_id, "readiness": readiness}
    creative_error = "" if _delivery_enforced() else _creative_package_error(package, platforms)
    if creative_error:
        recover_outbox(data_dir, outbox_id, creative_error)
        return {"status": "CONTENT_RECOVERING", "outbox_id": outbox_id, "error": creative_error}
    for platform in due_platforms:
        artifact_error = _strict_publish_artifact_error(package, platform)
        if artifact_error:
            retry_at = now + timedelta(minutes=30)
            release_outbox(data_dir, outbox_id, artifact_error, next_attempt_at=retry_at.isoformat())
            return {
                "status": "RETRYABLE_FAILURE",
                "outbox_id": outbox_id,
                "error": artifact_error,
                "next_attempt_at": retry_at.isoformat(),
            }

    results: dict[str, Any] = {}
    retry_errors: list[str] = []
    ambiguous: list[str] = []
    for platform in due_platforms:
        existing = platform_transaction(data_dir, outbox_id, platform)
        if existing.get("state") == "CONFIRMED_SUCCESS":
            results[platform] = {"state": "CONFIRMED_SUCCESS", "external_id": existing.get("external_id"), "reused": True}
            continue
        if existing.get("state") in {"AMBIGUOUS", "AUTH_ACTION_REQUIRED"} and not _delivery_enforced():
            ambiguous.append(f"{platform}:{existing.get('state')}")
            results[platform] = existing
            continue

        payload = _payload(package, platform)
        begin_platform_transaction(data_dir, outbox_id=outbox_id, platform=platform, payload=payload)
        try:
            response = _publish(package, platform)
            external_id = str(response.get("id") or "").strip()
            if not external_id or external_id in {"skipped", "dry-run"}:
                reason = str(response.get("reason") or "publisher_did_not_confirm_success")
                complete_platform_transaction(
                    data_dir,
                    outbox_id=outbox_id,
                    platform=platform,
                    state="CONFIRMED_FAILURE",
                    provider_response=response,
                    error=reason,
                )
                retry_errors.append(f"{platform}:{reason}")
                results[platform] = {"state": "CONFIRMED_FAILURE", "error": reason}
                continue
            complete_platform_transaction(
                data_dir,
                outbox_id=outbox_id,
                platform=platform,
                state="CONFIRMED_SUCCESS",
                external_id=external_id,
                provider_response=response,
            )
            record_publication_history(data_dir, outbox_id=outbox_id, platform=platform, external_id=external_id)
            results[platform] = {"state": "CONFIRMED_SUCCESS", "external_id": external_id}
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            lowered = error.lower()
            if any(token in lowered for token in ("token", "oauth", "unauthorized", "forbidden")):
                state = "AUTH_ACTION_REQUIRED"
            elif any(token in lowered for token in ("timeout", "timed out", "connection", "502", "503", "504")):
                state = "AMBIGUOUS"
            else:
                state = "CONFIRMED_FAILURE"
            complete_platform_transaction(
                data_dir,
                outbox_id=outbox_id,
                platform=platform,
                state=state,
                error=error,
            )
            if state in {"AUTH_ACTION_REQUIRED", "AMBIGUOUS"}:
                ambiguous.append(f"{platform}:{error}")
            else:
                retry_errors.append(f"{platform}:{error}")
            results[platform] = {"state": state, "error": error}

    if ambiguous:
        error = " | ".join(ambiguous)
        transition_package(data_dir, outbox_id, PackageState.RECONCILIATION_REQUIRED.value, "PLATFORM_DELIVERY_AMBIGUOUS", actor="dispatch_outbox", attempt_id=claimed.get("active_attempt_id"), detail={"platforms": results})
        finalize_outbox(data_dir, outbox_id, status="EXTERNAL_ACTION_REQUIRED", error=error)
        return {"status": "EXTERNAL_ACTION_REQUIRED", "outbox_id": outbox_id, "platforms": results, "error": error}
    if retry_errors:
        error = " | ".join(retry_errors)
        attempt_count = int(claimed.get("attempt_count") or 1)
        max_attempts = max(1, int(os.environ.get("OUTBOX_MAX_ATTEMPTS", "4")))
        if attempt_count >= max_attempts:
            terminal_status = "EXTERNAL_ACTION_REQUIRED" if any(
                result.get("state") == "CONFIRMED_SUCCESS" for result in results.values()
            ) else "FAILED"
            destination = PackageState.PARTIALLY_PUBLISHED.value if any(result.get("state") == "CONFIRMED_SUCCESS" for result in results.values()) else PackageState.FAILED_DELIVERY.value
            transition_package(data_dir, outbox_id, destination, "PLATFORM_RETRY_LIMIT_REACHED", actor="dispatch_outbox", attempt_id=claimed.get("active_attempt_id"), detail={"platforms": results})
            finalize_outbox(data_dir, outbox_id, status=terminal_status, error=error)
            return {"status": terminal_status, "outbox_id": outbox_id, "platforms": results, "error": error}
        retry_at = now
        retry_at += timedelta(seconds=min(1800, 30 * (2 ** (attempt_count - 1))))
        destination = PackageState.PARTIALLY_PUBLISHED.value if any(result.get("state") == "CONFIRMED_SUCCESS" for result in results.values()) else PackageState.FAILED_DELIVERY.value
        transition_package(data_dir, outbox_id, destination, "PLATFORM_RETRYABLE_ERROR", actor="dispatch_outbox", attempt_id=claimed.get("active_attempt_id"), detail={"platforms": results})
        release_outbox(data_dir, outbox_id, error, next_attempt_at=retry_at.isoformat())
        return {"status": "PARTIAL_RETRY", "outbox_id": outbox_id, "platforms": results, "error": error}

    remaining = [
        platform for platform in platforms
        if platform_transaction(data_dir, outbox_id, platform).get("state") != "CONFIRMED_SUCCESS"
    ]
    if remaining:
        next_due = min(_platform_due_at(package, platform, str(claimed["scheduled_at"])) for platform in remaining)
        transition_package(data_dir, outbox_id, PackageState.PARTIALLY_PUBLISHED.value, "PLATFORM_WINDOWS_REMAIN", actor="dispatch_outbox", attempt_id=claimed.get("active_attempt_id"), detail={"remaining_platforms": remaining})
        release_outbox(data_dir, outbox_id, "", next_attempt_at=next_due.isoformat())
        return {
            "status": "PARTIAL_SCHEDULED", "outbox_id": outbox_id,
            "platforms": results, "remaining_platforms": remaining,
            "next_attempt_at": next_due.isoformat(),
        }
    transition_package(data_dir, outbox_id, PackageState.PUBLISHED.value, "ALL_PLATFORM_RECEIPTS_CONFIRMED", actor="dispatch_outbox", attempt_id=claimed.get("active_attempt_id"), detail={"platforms": results})
    finalize_outbox(data_dir, outbox_id, status="PUBLISHED")
    return {"status": "PUBLISHED", "outbox_id": outbox_id, "platforms": results}


def reconcile_ambiguous_transactions(*, data_dir: str = DATA_DIR) -> dict[str, Any]:
    """Query provider status where possible; never resend an uncertain request."""
    import sqlite3
    from inventory_db import get_db_path

    connection = sqlite3.connect(get_db_path(data_dir))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT outbox_id, platform, external_id FROM platform_transactions WHERE state='AMBIGUOUS'"
        ).fetchall()
    finally:
        connection.close()
    results: list[dict[str, Any]] = []
    for row in rows:
        outbox_id, platform = str(row["outbox_id"]), str(row["platform"])
        external_id = str(row["external_id"] or "")
        if not external_id:
            results.append({"outbox_id": outbox_id, "platform": platform, "state": "RECONCILIATION_REQUIRED", "reason": "external_id_unavailable"})
            continue
        try:
            status_result = get_platform_status(platform, external_id)
            status = str(status_result.get("status") or "").upper()
        except Exception as exc:
            results.append({"outbox_id": outbox_id, "platform": platform, "state": "RECONCILIATION_REQUIRED", "reason": f"{type(exc).__name__}:{exc}"})
            continue
        if status in {"SUCCEEDED", "PUBLISH_COMPLETE", "PUBLISHED"}:
            complete_platform_transaction(data_dir, outbox_id=outbox_id, platform=platform, state="CONFIRMED_SUCCESS", external_id=external_id, provider_response=status_result)
            record_publication_history(data_dir, outbox_id=outbox_id, platform=platform, external_id=external_id)
            results.append({"outbox_id": outbox_id, "platform": platform, "state": "CONFIRMED_SUCCESS"})
        elif status in {"FAILED", "PROCESSING_FAILED", "REJECTED", "NOT_FOUND"}:
            complete_platform_transaction(data_dir, outbox_id=outbox_id, platform=platform, state="CONFIRMED_FAILURE", external_id=external_id, provider_response=status_result, error=status)
            results.append({"outbox_id": outbox_id, "platform": platform, "state": "CONFIRMED_FAILURE"})
        else:
            results.append({"outbox_id": outbox_id, "platform": platform, "state": "RECONCILIATION_REQUIRED", "reason": status or "status_unknown"})
    reconcile_confirmed_transactions(data_dir)
    return {"checked": len(rows), "results": results}


def dispatch_due_batch(*, data_dir: str = DATA_DIR, now_utc: str | None = None, limit: int = 25) -> dict[str, Any]:
    reconciliation = reconcile_ambiguous_transactions(data_dir=data_dir)
    results: list[dict[str, Any]] = []
    for _ in range(max(1, limit)):
        result = dispatch_due(data_dir=data_dir, now_utc=now_utc)
        if result.get("status") == "IDLE":
            break
        results.append(result)
    return {
        "status": "COMPLETE",
        "processed": len(results),
        "published": sum(1 for result in results if result.get("status") == "PUBLISHED"),
        "failed": sum(1 for result in results if result.get("status") in {"FAILED", "EXTERNAL_ACTION_REQUIRED"}),
        "reconciliation": reconciliation,
        "results": results,
    }


def main() -> None:
    action = str(os.environ.get("OUTBOX_ACTION", "dispatch")).strip().lower()
    result = pregenerate_upcoming() if action == "pregenerate" else dispatch_due_batch()
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
