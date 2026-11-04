import os
import json
import random
import hashlib
import csv
import glob
import re
import uuid
import unicodedata
from datetime import date, datetime, timezone, timedelta
from typing import Any
from google import genai
from google.genai import types
from social import carousel_director
from social.gemini_budget import GeminiBudgetExceeded, tracked_gemini_call
from consumer_life import assess_copy_fidelity, assess_product_compatibility, select_consumer_root


# ---------------------------------------------------------------------------
# Autonomous Social Creative Intelligence hook (Master Build §107).
# Enabled by setting ENABLE_SOCIAL_INTELLIGENCE=1. When enabled, callers can
# invoke ``run_social_intelligence(count, platform)`` to generate posts via
# the new orchestrator (engines A/B/C, audience-value pipeline, quality gate,
# creative director test). Falls back to the existing generator when the
# flag is not set, so the default behavior is unchanged.
#
# When ENABLE_BUSINESS_INTELLIGENCE=1 is *also* set, the orchestrator
# auto-hydrates its inputs from the Living Business Intelligence Foundation
# (identity, voice, audience segments, verified product facts, forbidden
# claims, visual prohibitions), and every generated post carries the
# ``business_context`` + ``anchored_offering`` payloads.
# ---------------------------------------------------------------------------


def _social_intelligence_enabled() -> bool:
    return os.environ.get("ENABLE_SOCIAL_INTELLIGENCE", "true").lower() in {"1", "true", "yes", "on"}


def _business_intelligence_enabled() -> bool:
    return os.environ.get("ENABLE_BUSINESS_INTELLIGENCE", "true").lower() in {"1", "true", "yes", "on"}


def _text_only_generation() -> bool:
    return os.environ.get("POST_TEXT_ONLY", "false").lower() in {"1", "true", "yes", "on"}


_PIPELINE_ALIASES = {
    "legacy": "legacy", "classic": "legacy", "conversion": "legacy",
    "orchestrator": "orchestrator", "social_intelligence": "orchestrator", "new": "orchestrator",
    "best_of": "best_of", "both": "best_of", "combined": "best_of", "compare": "best_of",
}


def _pipeline_mode(explicit: str = "") -> str:
    """Resolve which pipeline(s) to run for this call.

    Precedence: explicit ``pipeline_override`` kwarg > ``CONTENT_PIPELINE``/
    ``POST_PIPELINE_OVERRIDE`` env vars > legacy ``ENABLE_SOCIAL_INTELLIGENCE``
    flag. Normal production uses the Conversion Strategist pipeline, which owns
    the required copy framework and business-profile grounding.
    """
    raw = (explicit or os.environ.get("CONTENT_PIPELINE", "") or os.environ.get("POST_PIPELINE_OVERRIDE", "")).strip().lower()
    return _PIPELINE_ALIASES.get(raw, "legacy")


def _product_consumer_context(product: dict[str, Any] | None, audience: str, data_dir: str = "") -> dict[str, Any]:
    if not product:
        return {}
    roots = [
        data_dir or os.environ.get("DATA_DIR") or os.environ.get("BI_DATA_DIR") or "",
        os.path.join(os.path.dirname(__file__), "..", "data"),
    ]
    catalog: dict[str, Any] = {}
    for root in roots:
        if not root:
            continue
        path = os.path.abspath(os.path.join(root, "marketing", "product_consumer_profiles.json"))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        catalog = payload.get("profiles", {}) if isinstance(payload, dict) else {}
        if catalog:
            break
    keys = [str(product.get(key) or "").strip() for key in ("sku", "id", "product_id")]
    profile = next((catalog[key] for key in keys if key and isinstance(catalog.get(key), dict)), {})
    personas = [item for item in profile.get("personas", []) if isinstance(item, dict)]
    audience_terms = set(re.findall(r"[a-z]+", str(audience).lower()))
    selected = next(
        (
            persona for persona in personas
            if audience_terms.intersection(re.findall(r"[a-z]+", str(persona.get("identity") or persona.get("name") or "").lower()))
        ),
        personas[0] if personas else {},
    )
    return {"profile": profile, "persona": selected}


def _product_consumer_prompt_context(context: dict[str, Any]) -> str:
    profile = context.get("profile") if isinstance(context.get("profile"), dict) else {}
    persona = context.get("persona") if isinstance(context.get("persona"), dict) else {}
    if not profile or not persona:
        return ""
    return (
        "CONSUMER PROFILE FOR THIS PRODUCT POST:\n"
        "- Write to exactly this one person; do not blend audiences or use cases.\n"
        f"- Selected persona: {json.dumps(persona, sort_keys=True)}\n"
        f"- Product positioning: {profile.get('positioning_statement', '')}\n"
        f"- Infenergy reason why: {profile.get('infenergy_reason_why', '')}\n"
        f"- Value pillars: {json.dumps(profile.get('value_pillars', []))}\n"
        f"- Content mandates: {json.dumps(profile.get('content_mandates', []))}\n"
        f"- Content boundaries: {json.dumps(profile.get('content_boundaries', []))}\n"
        "Open in the persona's lived situation, connect one verified capability to the desired outcome, "
        "answer one stated objection, and close with the persona's call to action."
    )


def run_social_intelligence(count: int = 1, platform: str = "instagram_feed", **kw: Any) -> list[dict[str, Any]]:
    """Generate ``count`` posts through the new Social Intelligence layer.

    Returns a list of package dicts. Safe to import — the ``social`` package
    is only touched when this function is actually called.
    """
    if _business_intelligence_enabled():
        # Ensure the profile is fresh before the orchestrator reads it.
        try:
            from business_intelligence import api as bi_api
            from business_intelligence import paths as bi_paths
            from business_intelligence import profile as bi_profile
            current_profile = bi_profile.load_current()
            cached_offerings = current_profile.get("offerings", []) if isinstance(current_profile, dict) else []
            profile_catalog_available = os.path.isfile(bi_paths.consumer_profiles_path())
            profile_cache_stale = profile_catalog_available and not any(
                isinstance(offering, dict) and offering.get("consumer_profile")
                for offering in cached_offerings
            )
            if not current_profile or profile_cache_stale:
                bi_api.rebuild_profile()
        except Exception:
            pass

    from social.orchestrator import SocialIntelligenceOrchestrator
    from social.visual_provider import TemplateRenderProvider

    # The production adapter renders final pixels below via generate_visuals().
    # Keep the orchestrator on a recipe-only provider so it supplies art direction
    # without spending a second Gemini render for the same candidate.
    learning_data_dir = (
        os.environ.get("DATA_DIR")
        or os.environ.get("BI_DATA_DIR")
        or os.path.join(os.path.dirname(__file__), "..", "data")
    )
    orchestrator = SocialIntelligenceOrchestrator(
        provider=TemplateRenderProvider(),
        data_dir=os.environ.get("DATA_DIR"),
    )
    batch = orchestrator.create_batch(count=count, platform=platform, **kw)
    packages = [p.as_dict() for p in batch]
    try:
        from agents.learning_context import load_operational_learning

        learning = load_operational_learning(learning_data_dir)
        for package in packages:
            package["operational_learning"] = learning
    except (OSError, ValueError):
        pass
    return packages


def _social_platform_key(platform: str) -> str:
    """Normalize runtime publisher names to social-library platform identifiers."""
    value = str(platform or "").strip().lower()
    return {
        "facebook": "facebook_feed",
        "instagram": "instagram_feed",
        "linkedin": "linkedin_feed",
    }.get(value, value or "instagram_feed")


def _select_social_platforms(strategy_lock: dict[str, Any]) -> dict[str, dict[str, str | bool]]:
    """Select channels from the approved strategy; presentation never changes its truth."""
    strategy_text = " ".join(str(strategy_lock.get(key, "")) for key in ("audience", "customer_moment", "topic", "angle", "reader_job", "positioning")).lower()
    professional_context = any(term in strategy_text for term in ("business", "professional", "operator", "work", "workplace", "continuity", "industry", "team", "organization"))
    return {
        "facebook": {"selected": True, "reason": "recognizable customer context supports conversation and sharing"},
        "instagram": {"selected": True, "reason": "visual-first educational expression is available"},
        "linkedin": {
            "selected": professional_context,
            "reason": "professional decision-support context is supported" if professional_context else "no supported professional or business context",
        },
    }


def _living_strategy_for_generation() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Use the persisted Council selection when living evidence supports one."""
    try:
        from social import living_intelligence

        data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
        state = living_intelligence.load(data_dir)
        existing = state.get("approved_strategy")
        if isinstance(existing, dict) and existing:
            return existing, {"decision": "strategy_selected", "source": "persisted_council"}

        customers = state.get("consumer_relationships") if isinstance(state.get("consumer_relationships"), list) else []
        customer = next((item for item in customers if isinstance(item, dict)), {})
        if not customer:
            return None, {"decision": "fallback_runtime_lock", "reason": "no persisted customer relationship"}
        inputs = {
            "customer": customer,
            "capability": str(customer.get("offering_capability") or "verified product facts"),
            "benefit": str(customer.get("benefit") or "practical product-fit guidance"),
            "positioning": state.get("positioning") if isinstance(state.get("positioning"), dict) else {},
            "competitor_context": str(customer.get("competitor_context") or ""),
            "human_value": str(customer.get("human_meaning") or customer.get("human_need") or "practical clarity"),
            "topic": str(customer.get("question") or customer.get("human_need") or "product guidance"),
            "reader_job": "HELP_ME_CHOOSE",
            "important_capability": str(customer.get("offering_capability") or "verified product facts"),
            "human_outcome": str(customer.get("outcome") or customer.get("human_need") or "confidence"),
            "proof": [],
            "claim_limits": "Use only verified product facts.",
            "visual_objective": "make the supported customer decision easier to understand",
            "CTA_strategy": "Learn more",
        }
        decision = living_intelligence.council(state, strategy_inputs=inputs)
        approved = decision.get("approved_strategy") if isinstance(decision.get("approved_strategy"), dict) else None
        if approved:
            state["approved_strategy"] = approved
            state["last_council_decision"] = decision
            for opportunity in state.get("opportunities", []):
                if opportunity.get("id") == decision.get("opportunity_id"):
                    opportunity["state"] = "SELECTED"
            living_intelligence.save(data_dir, state)
            return approved, decision
        return None, decision
    except Exception as exc:
        return None, {"decision": "fallback_runtime_lock", "reason": f"living_state_unavailable:{type(exc).__name__}"}


def _apply_gemini_platform_copy(
    platform_posts: dict[str, dict[str, Any]],
    copy_pkg: dict[str, Any],
    components: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Preserve Gemini's ordered public copy instead of replacing it with template prose."""
    if str(copy_pkg.get("generation_method") or "").lower() != "gemini":
        return platform_posts
    beats = copy_pkg.get("body_beats") if isinstance(copy_pkg.get("body_beats"), dict) else {}
    ordered = [
        str(value).strip()
        for key, value in beats.items()
        if key not in {"cta", "hashtags"} and str(value).strip()
    ]
    cta = str(copy_pkg.get("cta") or beats.get("cta") or "").strip()
    hashtags = str(beats.get("hashtags") or "").strip()
    public_parts = list(dict.fromkeys([*ordered, cta, hashtags]))
    if not public_parts:
        return platform_posts

    from social import platform_presentation

    for platform, package in platform_posts.items():
        caption = platform_presentation.render_platform_caption(
            "\n\n".join(public_parts),
            destination_url=str(package.get("utm_url") or package.get("destination_url") or "").strip(),
            platform=platform,
        )
        package["caption"] = caption
        package["final_caption"] = caption
        package["presentation"] = {
            **(package.get("presentation") if isinstance(package.get("presentation"), dict) else {}),
            **platform_presentation.evaluate(caption, platform=platform, components=components),
            "copy_source": "gemini",
        }
        package["final_caption_qa"] = platform_presentation.final_caption_qa(
            caption,
            platform=platform,
            components=components,
        )
    return platform_posts


def _route_generate_orchestrator(
    slot: str = "",
    *,
    platform: str = "instagram_feed",
    funnel_stage_override: str = "",
    **kw: Any,
) -> dict[str, Any]:
    """Return the first orchestrated post package in the legacy payload shape used by the runtime."""
    social_platform = _social_platform_key(platform)
    consumer_root = kw.pop("consumer_root", {}) if isinstance(kw.get("consumer_root"), dict) else {}
    recurring_series = kw.pop("recurring_series", {}) if isinstance(kw.get("recurring_series"), dict) else {}
    no_product = bool(kw.get("no_product")) or os.environ.get("CONTENT_BUCKET_OVERRIDE", "").strip().lower() == "no_product"
    kw["no_product"] = no_product
    council_decision: dict[str, Any] = {}
    if no_product:
        kw.pop("product_id_override", None)
        council_decision = {"decision": "organic_strategy", "source": "product_free_override"}
    else:
        strategy_source = "caller_override"
        approved_strategy = kw.get("approved_strategy")
        if not isinstance(approved_strategy, dict):
            strategy_source = "persisted_council"
            approved_strategy, council_decision = _living_strategy_for_generation()
        else:
            council_decision = {"decision": "strategy_selected", "source": strategy_source}
        if approved_strategy:
            from social.strategy_lock import REQUIRED as REQUIRED_STRATEGY_FIELDS

            missing_strategy_fields = [
                field for field in REQUIRED_STRATEGY_FIELDS if not approved_strategy.get(field)
            ]
            if missing_strategy_fields:
                kw.pop("approved_strategy", None)
                council_decision = {
                    "decision": "fallback_runtime_lock",
                    "reason": f"{strategy_source}_strategy_incomplete",
                    "missing_fields": missing_strategy_fields,
                }
            else:
                kw["approved_strategy"] = approved_strategy
    if consumer_root and isinstance(kw.get("approved_strategy"), dict):
        approved_strategy = kw["approved_strategy"]
        moment = consumer_root["moment"]
        kw["approved_strategy"] = {
            **approved_strategy,
            "consumer_root_id": consumer_root["root_id"],
            "audience": moment["person"],
            "topic": moment["activity"],
            "angle": moment["useful_discovery"],
            "reader_job": moment["immediate_action"],
            "lived_moment": moment,
            "consumer_receipt": consumer_root["consumer_receipt"],
        }
    if recurring_series.get("id") == "infenergy_intervention" and isinstance(kw.get("approved_strategy"), dict):
        approved_strategy = kw["approved_strategy"]
        kw["approved_strategy"] = {
            **approved_strategy,
            "series_id": recurring_series["id"],
            "topic": "Infenergy Intervention",
            "angle": "Dramatize a fresh avoidable energy mistake, then earn a product-enabled resolution through Infenergy's action.",
            "reader_job": "Recognize the mistake, enjoy the intervention, and remember the practical solution.",
            "preferred_format": recurring_series.get("preferred_format"),
            "required_story_pattern": recurring_series.get("story_pattern"),
            "originality_dimensions": recurring_series.get("originality_dimensions", []),
        }
    batch = run_social_intelligence(count=1, platform=social_platform, **kw)
    if not batch:
        return {}

    first = batch[0]
    copy_pkg = first.get("copy") or {}
    visual_pkg = first.get("visual") or {}
    quality_pkg = first.get("quality") or {}
    offering = first.get("anchored_offering") or {}

    # The BI Offering schema has no purchase-URL/CSV-row fields (product_url,
    # price, in_stock, etc.) that the legacy product-claims validator
    # requires -- resolve the matching catalog row (same sku/id, always
    # available via the CSV loader) to fill those in.
    catalog_product: dict[str, Any] | None = None
    offering_lookup_id = str(offering.get("sku") or offering.get("offering_id") or "").strip()
    if offering_lookup_id:
        try:
            catalog_product = _pick_product_by_id(load_products(), offering_lookup_id)
        except Exception:
            catalog_product = None

    brief = first.get("brief") or {}
    copy_body = str(copy_pkg.get("body_text") or "").strip()
    takeaway = str(copy_pkg.get("takeaway") or copy_pkg.get("memory_anchor") or "").strip()
    selected_hook = str(copy_pkg.get("hook") or "").strip()
    selected_cta = str(copy_pkg.get("cta") or "Learn more").strip()
    funnel_stage = _normalize_funnel_stage_override(funnel_stage_override) or "EDUCATION"
    product_for_adaptation = {
        "id": offering.get("offering_id") or offering.get("sku") or "",
        "name": offering.get("name", ""),
        "sku": offering.get("sku", ""),
        "categories": [offering.get("category", "")] if offering.get("category") else [],
        "metrics": list(offering.get("verified_facts", [])) or (catalog_product or {}).get("metrics", []),
        "fact_snippet": (catalog_product or {}).get("fact_snippet", "") or offering.get("description_clean", ""),
        "image_url": (offering.get("images") or [""])[0],
    }
    topic = str((brief.get("topic_path") or {}).get("topic") or "Product education").strip()
    components = _build_post_components(
        topic=topic,
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        product=product_for_adaptation,
        funnel_stage=funnel_stage,
        editorial_framework=copy_pkg.get("editorial_framework") if isinstance(copy_pkg.get("editorial_framework"), dict) else {},
    )
    components.update({
        "pillar": str(brief.get("pillar_id") or (brief.get("topic_path") or {}).get("pillar") or ""),
        "logic_hook": selected_hook,
        "logic_bridge": copy_body or components["logic_bridge"],
        "proof": "; ".join(str(fact) for fact in (offering.get("verified_facts") or [])[:4]) or components["proof"],
        "emotional_outcome": components["emotional_outcome"],
        "on_image_headline": selected_hook or components["on_image_headline"],
        "on_image_subline": takeaway or components["on_image_subline"],
    })
    platform_posts = _build_platform_posts(
        post_id=str(first.get("post_id") or ""),
        campaign_id="",
        audience_segment=str(brief.get("audience_segment") or ""),
        funnel_stage=funnel_stage,
        destination_url=(catalog_product or {}).get("product_url") or SITE_URL,
        components=components,
        quality_score=float(quality_pkg.get("overall") or 0),
        strategy_lock=copy_pkg.get("strategy_lock") if isinstance(copy_pkg.get("strategy_lock"), dict) else {},
        creative_reviews=first.get("creative_director") if isinstance(first.get("creative_director"), dict) else {},
        platform_interpretations=(first.get("creative_decision_packet") or {}).get("platform_interpretations") or {},
    )
    platform_posts = _apply_platform_presentation_priority(platform_posts, components)
    platform_posts = _apply_gemini_platform_copy(platform_posts, copy_pkg, components)
    creative_packet = first.get("creative_decision_packet") or {}
    platform_selection = _select_social_platforms(copy_pkg.get("strategy_lock") if isinstance(copy_pkg.get("strategy_lock"), dict) else {})
    for platform, package in platform_posts.items():
        package["platform_selection"] = platform_selection[platform]
        package["creative_interpretation"] = (creative_packet.get("platform_interpretations") or {}).get(platform, {})
    try:
        from social import quality_intelligence
        final_copy_reviews = {
            platform: quality_intelligence.copy_critic(
                copy={"hook": package.get("hook", ""), "body_text": package.get("caption", ""), "cta": package.get("cta", "")},
                strategy=copy_pkg.get("strategy_lock") if isinstance(copy_pkg.get("strategy_lock"), dict) else {},
                platform=platform,
            )
            for platform, package in platform_posts.items()
        }
    except Exception:
        final_copy_reviews = {}
    wp_content = _join_paragraphs(selected_hook, copy_body, takeaway, selected_cta)

    legacy = {
        "post_id": first.get("post_id"),
        "copy_generation_source": "social_intelligence_orchestrator",
        "consumer_root": consumer_root,
        "consumer_root_id": consumer_root.get("root_id", ""),
        "consumer_world_id": consumer_root.get("world_id", ""),
        "consumer_moment_id": consumer_root.get("moment_id", ""),
        "consumer_receipt": consumer_root.get("consumer_receipt", {}),
        "business_context": first.get("business_context") or {},
        "anchored_offering": offering,
        "consumer_profile": offering.get("consumer_profile") or {},
        "selected_consumer_persona": (copy_pkg.get("strategy_lock") or {}).get("consumer_persona") or {},
        "product_id": offering.get("offering_id") or offering.get("sku") or None,
        "product_name": offering.get("name", ""),
        "product_sku": offering.get("sku", ""),
        "product_image_url": (offering.get("images") or [""])[0],
        "product_image_candidates": (offering.get("images") or [])[1:],
        "product_url": (catalog_product or {}).get("product_url", ""),
        "destination_url": SITE_URL,
        "product_price": (catalog_product or {}).get("price", ""),
        "product_sale_price": (catalog_product or {}).get("sale_price", ""),
        "product_metrics": list(offering.get("verified_facts", [])) or (catalog_product or {}).get("metrics", []),
        "product_categories": product_for_adaptation["categories"] or (catalog_product or {}).get("categories", []),
        "product_facts": (catalog_product or {}).get("fact_snippet", "") or offering.get("description_clean", ""),
        "product_in_stock": (catalog_product or {}).get("in_stock", "") or offering.get("stock_status", ""),
        "selected_hook": selected_hook,
        "selected_cta": selected_cta,
        "copy": copy_pkg,
        "strategy_lock": copy_pkg.get("strategy_lock") if isinstance(copy_pkg.get("strategy_lock"), dict) else {},
        "visual": visual_pkg,
        "layout_grammar": visual_pkg.get("layout_grammar", {}),
        "information_priority": visual_pkg.get("information_priority", {}),
        "benefit_translation": visual_pkg.get("benefit_translation", {}),
        "platform_interpretations": creative_packet.get("platform_interpretations", {}),
        "human_connection": {
            "person": (copy_pkg.get("strategy_lock") or {}).get("audience", ""),
            "situation": (copy_pkg.get("strategy_lock") or {}).get("customer_moment", ""),
            "need": (copy_pkg.get("strategy_lock") or {}).get("human_need", ""),
            "human_value": (copy_pkg.get("strategy_lock") or {}).get("human_value", ""),
            "outcome": (copy_pkg.get("strategy_lock") or {}).get("human_outcome", ""),
        },
        "creative_decision_packet": first.get("creative_decision_packet") or {},
        "quality_score": quality_pkg.get("overall", 0),
        "quality_checks": quality_pkg.get("checks", []),
        "quality_warnings": quality_pkg.get("warnings", []),
        "quality": quality_pkg,
        "slot": slot,
        "platform": social_platform,
        "funnel_stage": funnel_stage,
        "audience_segment": brief.get("audience_segment", ""),
        "pillar": brief.get("pillar_id", ""),
        "topic": topic,
        "microtopic": (brief.get("topic_path") or {}).get("microtopic", ""),
        "genre_id": brief.get("genre_id", ""),
        "reader_job": brief.get("reader_job", ""),
        "emotional_driver": brief.get("emotional_driver", ""),
        "strategic_brief": brief,
        "claim_ledger": first.get("claim_ledger") or {},
        "creative_director": first.get("creative_director") or {},
        "final_platform_copy_reviews": final_copy_reviews,
        "orchestrator_quality": quality_pkg,
        "copy_generation_method": copy_pkg.get("generation_method", ""),
        "copy_fallback_reason": copy_pkg.get("fallback_reason"),
        "wp_title": selected_hook or topic,
        "wp_content": wp_content,
        "wp_excerpt": takeaway or selected_hook,
        "fb_caption": platform_posts["facebook"]["final_caption"],
        "ig_caption": platform_posts["instagram"]["final_caption"],
        "li_text": platform_posts["linkedin"]["final_caption"],
        "platform_posts": platform_posts,
        "platform_selection": platform_selection,
        "decision_trace": {
            "council": council_decision,
            "strategy_lock": copy_pkg.get("strategy_lock") or {},
            "feed_need": creative_packet.get("feed_intelligence", {}),
            "campaign_state": creative_packet.get("campaign_guidance", {}),
            "creative_concept": creative_packet.get("SELECTED_ANSWER", {}).get("creative_concept", ""),
            "copy_concept": creative_packet.get("selected_copy_concept", {}),
            "benefit_priority": creative_packet.get("information_priority", {}),
            "platform_selection": platform_selection,
        },
    }
    for key in ("hook", "body_text", "takeaway", "memory_anchor"):
        legacy.setdefault(key, copy_pkg.get(key))

    # The orchestrator's own "visual" package is art-direction/prompt
    # metadata only -- it never actually calls Gemini. Reuse the same
    # generate_visuals() step the legacy pipeline uses so orchestrator
    # posts get real, product-anchored creative instead of staying empty.
    visual_pkg = _apply_recurring_series_visual_plan(visual_pkg, recurring_series)
    _apply_recurring_series_package(legacy, platform_posts, recurring_series)
    _apply_recurring_series_generation_contract(legacy, visual_pkg, recurring_series, product_for_adaptation)
    legacy["visual_plan"] = visual_pkg
    product_image_approval = build_product_image_approval(
        legacy,
        str((catalog_product or {}).get("image_url") or ""),
    )
    if product_image_approval:
        legacy["product_image_approval"] = product_image_approval
    legacy["generated_visuals"] = (
        {"deferred": True, "reason": "text_only_candidate_pool"}
        if _text_only_generation()
        else generate_visuals(legacy, visual_plan=visual_pkg)
    )
    from social import reels

    instagram_decision = reels.choose_instagram_media(
        strategy_lock=legacy["strategy_lock"],
        components=components,
        visual_plan=visual_pkg,
    )
    legacy["instagram_media_decision"] = instagram_decision
    platform_posts["instagram"]["media_type"] = instagram_decision["selected_format"]
    platform_posts["instagram"]["instagram_media_decision"] = instagram_decision
    if recurring_series.get("id") == "infenergy_intervention":
        platform_posts["instagram"]["media_type"] = "CAROUSEL" if recurring_series.get("preferred_format") == "educational_story_carousel" else "STATIC"
        legacy["instagram_media_decision"]["selected_format"] = platform_posts["instagram"]["media_type"]
    if instagram_decision["selected_format"] == "REEL":
        reel_plan = reels.build_reel_plan(
            post_id=str(legacy.get("post_id") or ""),
            components=components,
            decision=instagram_decision,
            strategy_lock=legacy["strategy_lock"],
        )
        legacy["reel_plan"] = reel_plan
        reel_gate = reels.validate_reel_plan(reel_plan)
        legacy["reel_pre_render_gate"] = reel_gate
        if reel_gate["status"] == "REEL_READY":
            reel_artifacts = reels.render_reel(
                reel_plan,
                source_image=str((legacy.get("generated_visuals") or {}).get("instagram") or ""),
            )
            reel_artifacts["technical_qa"] = reels.technical_qa(reel_artifacts, reel_plan)
            reel_artifacts["freeze_qa"] = reels.freeze_qa(reel_artifacts, reel_plan)
            reel_artifacts["final_frame_qa"] = reels.final_frame_qa(reel_artifacts)
            reel_artifacts["cover_qa"] = reels.cover_qa(reel_artifacts)
            reel_artifacts["motion_qa"] = reels.motion_qa(reel_plan)
            legacy["instagram_reel"] = reel_artifacts
            platform_posts["instagram"]["reel"] = reel_artifacts
        else:
            legacy["instagram_media_decision"]["selected_format"] = "STATIC"
            platform_posts["instagram"]["media_type"] = "STATIC"
    return legacy


try:
    import inventory_db
except ImportError:  # pragma: no cover
    from scripts import inventory_db
from campaign_runtime import (
    apply_claim_guardrails,
    choose_cta_for_stage,
    cta_is_valid_for_stage,
    ensure_campaign_runtime_files,
    has_explicit_cta_keyword,
    load_cta_library,
    load_channel_schedule,
    load_funnel_config,
    score_generated_content,
    select_weekly_sequence,
    stable_text_hash,
    stage_for_slot,
)
from generate_hooks import select_hook
from anti_repeat import load_anti_repeat_windows
from build_utm_url import build_utm_url
from social.candidate_pool import build_rotation_ledger, select_least_recently_used
from social.product_eligibility import filter_evidence_eligible_products
from social_visuals import build_product_image_approval, generate_visuals, normalize_brand_content, normalize_brand_text
from agents import product_intelligence
from agents import conversion_strategist
from agent_control_plane import (
    SCHEMAS_VERSION,
    build_gate_record,
    build_run_context,
    evaluate_global_gates,
    validate_agent_output,
)

FUNNEL_STAGES = {"ATTENTION", "EDUCATION", "DESIRE", "TRUST", "CONVERSION"}

INFENERGY_BUSINESS_GOALS = {
    "positioning": "Preparedness-first portable power brand helping families, travelers, and mobile users stay powered before, during, and after outages.",
    "audience_summary": "Families preparing for outages | Travelers and commuters who need device continuity | RV, camping, and mobile users who need off-grid confidence | Caregivers and small operators who cannot afford power disruption",
    "focus_statement": "portable backup power, emergency readiness, mobile autonomy, outage continuity, and product-fit guidance built around real device needs",
    "core_outcome": "help customers stay charged, connected, lit, and prepared when traditional power is unavailable",
    "action_bias": "move people from panic and guesswork into a practical purchase decision grounded in real specs and real use cases",
    "voice_anchors": [
        "preparedness over panic",
        "power is protection",
        "specs into confidence",
        "from guesswork to control",
        "built for real outages and real life",
        "right-size before you overspend",
        "portable power with a job to do",
    ],
    "talking_point_lenses": [
        "home resilience",
        "storm readiness",
        "mobile autonomy",
        "family safety continuity",
        "travel backup confidence",
        "device-priority planning",
        "spec-backed purchase confidence",
    ],
}

SITE_URL = os.environ.get("WP_URL", "https://www.infenergypower.com")
# DATA_DIR can be overridden by Railway volume mount (set DATA_DIR=/app/data in Railway Variables)
BASE_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DATA_DIR = os.environ.get("DATA_DIR", BASE_DATA_DIR)

DEFAULT_TOPIC_QUEUE = {
    "pillars": [
        "preparedness_education",
        "energy_literacy",
        "customer_problem_solving",
        "brand_authority",
        "trust_and_company_values",
        "community_engagement",
        "home_resilience",
        "travel_and_outdoor_preparedness",
        "caregiver_preparedness",
        "small_business_continuity",
        "category_education",
        "product_education",
        "readiness_assessment_lead_gen",
    ],
    "topics": {
        "preparedness_education": [
            "The 24-hour outage plan every household should write down before storm season",
            "What to power first when the grid goes down: a practical priority list",
            "The three preparedness mistakes people only discover during their first real outage",
            "How to build a device-priority list for your household in 10 minutes",
            "Preparedness is a plan, not a purchase: the steps that matter before you buy anything",
        ],
        "energy_literacy": [
            "Watts, wattage, and runtime: the three numbers that actually decide what backup power can do",
            "Why battery capacity labels alone do not tell you what a device can run",
            "Recharge speed vs output vs portability: what to compare and why it matters",
            "A plain-English guide to inverters for people who are not electricians",
            "How to translate your appliance's power draw into a real backup-power decision",
        ],
        "customer_problem_solving": [
            "The most common question we get about backup power, answered honestly",
            "How to figure out what your household actually needs before you shop",
            "What people usually get wrong when comparing backup power options",
            "A simple framework for matching real device needs to real capacity",
            "What to ask before you buy any portable power product",
        ],
        "brand_authority": [
            "Why we evaluate every product by real use cases before we ever recommend it",
            "The preparedness-first philosophy behind how we build product guidance",
            "What separates a genuinely useful power-readiness recommendation from a sales pitch",
            "Our approach to only publishing specs we can verify",
            "How we think about the difference between hype and honest product fit",
        ],
        "trust_and_company_values": [
            "Why we would rather tell you not to buy something than oversell it",
            "The values behind how Infenergy Power talks about products and specs",
            "What 'proof over hype' actually means in how we write product guidance",
            "Behind the scenes: how we vet the claims we publish",
            "Why transparency about limitations builds more trust than perfect marketing",
        ],
        "community_engagement": [
            "What's the one device you can't afford to lose power to during an outage?",
            "Tell us about the last time you lost power unexpectedly, what happened?",
            "Poll: what's your household's biggest backup-power blind spot?",
            "What would your 24-hour outage plan actually look like right now?",
            "Share your best (or worst) outage story, we read every one",
        ],
        "home_resilience": [
            "Building a whole-household backup plan without overspending",
            "How families are building outage resilience one device at a time",
            "The home resilience checklist most people skip",
            "What a realistic backup-power setup looks like for a family of four",
            "How to keep essentials running during a multi-day outage",
        ],
        "travel_and_outdoor_preparedness": [
            "Travel power checklist: charging phones, laptops, and essentials off-grid",
            "What campers and RV owners actually need from portable power",
            "How to match solar panel wattage to your portable generator for weekend trips",
            "The off-grid power mistakes first-time campers make",
            "Packing for backup power: what belongs in every travel bag",
        ],
        "caregiver_preparedness": [
            "What caregivers need to know about backup power for medical devices",
            "Building a preparedness plan when someone in your home depends on powered equipment",
            "The questions caregivers should ask before choosing any backup power option",
            "How to plan for outages when reliability is not optional",
            "A caregiver's guide to prioritizing what must stay powered",
        ],
        "small_business_continuity": [
            "What small businesses lose in the first hour of an unplanned outage",
            "Building a business continuity plan around portable backup power",
            "How small operators keep point-of-sale and communication running during outages",
            "The real cost of downtime for a small business, and how to plan around it",
            "A practical continuity checklist for shops that cannot afford to close",
        ],
        "category_education": [
            "Portable generator vs power station: what actually matters for your use case",
            "Power banks vs power stations: which one solves your actual problem",
            "Solar panels paired with portable power: how the pairing actually works",
            "Jump starters vs power banks: what each one is really built for",
            "What separates a real emergency-ready product category from a marketing label",
        ],
        "product_education": [
            "How to compare recharge speed, output, and portability before buying",
            "Reading a spec sheet: what the numbers on a power station label actually mean",
            "What to verify before trusting a product's advertised capacity",
            "How to match a specific product's specs to your specific must-run devices",
            "The verified details worth checking before you commit to any backup power product",
        ],
        "readiness_assessment_lead_gen": [
            "Book a free portable-power readiness assessment",
            "How to get a product match in under 15 minutes",
            "What to expect from your first product-fit consultation",
            "Not sure what you need? Start with a two-minute readiness check",
            "Get a tailored backup-power recommendation before you spend a dollar",
        ],
    },
}

# Business-first editorial director: default product-inclusion mode per content pillar.
# no_product: never attach a product. category_reference: may reference a product category
# generically but not push a specific SKU. optional_product: product may or may not be attached.
# required_product: always needs a specific product. multiple_products: conversion-style content
# that may reference more than one product; treated like required_product for selection purposes.
PILLAR_PRODUCT_MODE = {
    "preparedness_education": "no_product",
    "energy_literacy": "no_product",
    "customer_problem_solving": "optional_product",
    "brand_authority": "no_product",
    "trust_and_company_values": "no_product",
    "community_engagement": "no_product",
    "home_resilience": "optional_product",
    "travel_and_outdoor_preparedness": "optional_product",
    "caregiver_preparedness": "no_product",
    "small_business_continuity": "optional_product",
    "category_education": "category_reference",
    "product_education": "required_product",
    "readiness_assessment_lead_gen": "multiple_products",
}

# Target weekly content mix (business-first, not product-first). Ratios are guidance for
# biasing pillar selection, not hard quotas — see _decide_content_bucket().
CONTENT_MIX_TARGETS = {
    "no_product_min": 0.60,
    "no_product_max": 0.70,
    "product_education_min": 0.20,
    "product_education_max": 0.30,
    "conversion_min": 0.10,
    "conversion_max": 0.15,
}
MAX_CONSECUTIVE_PRODUCT_POSTS = 2

DEFAULT_STAGE_PAIN_POINTS = {
    "ATTENTION": "Most buyers realize too late that their backup plan does not match real device usage.",
    "EDUCATION": "People compare battery products by headline numbers and miss the specs that actually determine reliability.",
    "DESIRE": "Families want confidence during outages but hesitate because options look similar and confusing.",
    "TRUST": "Buyers worry about making an expensive mistake and need transparent, verifiable guidance.",
    "CONVERSION": "People postpone decisions until the next outage because the first step feels unclear.",
}

DEFAULT_STAGE_PAIN_POINT_VARIANTS = {
    "ATTENTION": [
        "Most buyers realize too late that their backup plan does not match real device usage.",
        "People often discover during the first outage that their backup setup cannot run what matters most.",
        "The expensive mistake is buying backup power before mapping real device priorities.",
    ],
    "EDUCATION": [
        "People compare battery products by headline numbers and miss the specs that actually determine reliability.",
        "Most shoppers focus on capacity labels and skip output and recharge details that decide real-world performance.",
        "A lot of buyers pick the wrong unit because they do not compare wattage, runtime, and recharge together.",
    ],
    "DESIRE": [
        "Families want confidence during outages but hesitate because options look similar and confusing.",
        "Buyers want peace of mind, but uncertainty about fit keeps them stuck in research mode.",
        "People want reliable backup power without overpaying for features they will not use.",
    ],
    "TRUST": [
        "Buyers worry about making an expensive mistake and need transparent, verifiable guidance.",
        "Trust breaks when specs are vague, so buyers need clear facts tied to real use cases.",
        "Most customers do not need hype, they need a product-fit recommendation backed by measurable details.",
    ],
    "CONVERSION": [
        "People postpone decisions until the next outage because the first step feels unclear.",
        "Many ready-to-buy customers delay because no one translates specs into a simple purchase decision.",
        "When next steps are vague, even motivated buyers wait instead of acting.",
    ],
}

POSITIONING_REPLACEMENTS = {
    "rooftop solar installation": "portable backup power setup",
    "residential solar installation": "portable backup power planning",
    "home solar installation": "portable power setup",
    "solar installation": "portable power setup",
    "rooftop solar": "portable solar panel",
    "net metering": "portable runtime planning",
    "home energy audit": "power readiness assessment",
    "solar tax credits": "equipment value and readiness planning",
}

DEFAULT_CATEGORY_IMAGE_FALLBACKS = {
    "portable power": "https://infenergypower.com/wp-content/uploads/2024/11/IMG_4214.png",
    "travel power": "https://infenergypower.com/wp-content/uploads/2024/12/IMG_4806.jpg",
    "solar generators": "https://infenergypower.com/wp-content/uploads/2024/08/1.png",
    "solar panels": "https://infenergypower.com/wp-content/uploads/2025/08/AF-S400A1-1.jpg",
    "home backup": "https://infenergypower.com/wp-content/uploads/2025/06/a0fc14752770c414bd090fa2f986454-scaled.png",
    "emergency power": "https://infenergypower.com/wp-content/uploads/2022/05/1642710505878.png",
}

CONVERSION_COPY_BRIEF = """
You are the Logical-Emotional Social Media Engine and conversion copywriter for Infenergy Power.

Objective:
- Stop the scroll.
- Make the reader recognize a real situation.
- Educate before selling.
- Connect the product as a logical solution.
- Use only verified product facts.
- Build trust and reduce hesitation.
- End with one clear next action.

Writing requirements:
- Focus on one primary angle and one specific customer moment.
- Use this sequence: Attention -> Problem/Desire -> Consequence -> Education -> Product fit -> Verified proof -> Objection reduction -> CTA.
- Use the assigned formal-logic principle as the narrative structure, then connect it to one honest emotional outcome such as control, relief, confidence, freedom, or readiness.
- Keep the logic valid: never turn an implication, comparison, or equivalence into an unsupported guarantee or claim that this is literally the only product that can work.
- Keep lines short, punchy, and scannable. Prefer concrete nouns and verbs over abstract marketing language.
- Do not invent runtime, appliance compatibility, savings, certifications, durability, warranty, or unsupported specs.
- Avoid generic ad language and banned cliches (for example: game changer, revolutionary, unlock the power of, don't miss out).
- Keep platform-native tone:
    - Facebook: conversational, educational, community trust.
    - Instagram: short high-impact first line, visual relevance, readable line breaks.
    - LinkedIn: authority and business continuity perspective, professional credibility.

Brand:
- Use Infenergy naming and trustworthy practical tone.
- The business is primarily portable power, emergency readiness, and outdoor/off-grid power products.
- Do not position the brand as a residential rooftop solar installer.
- Solar can be referenced only as portable/foldable solar panels paired with portable generators or power stations.
""".strip()

VISUAL_DIRECTOR_BRIEF = """
You are the visual prompt director inside the Logical-Emotional Social Media Engine for Infenergy Power social creatives.

Goal:
- Produce visual direction that increases click-through and trust.
- Ensure image concept supports the copy angle, funnel stage, and CTA.
- Decide when to feature product photos versus concept visuals.
- Push the creative beyond a plain promo tile into a high-end campaign ad with layered design elements.

Rules:
- Keep visuals premium, realistic, and brand-safe.
- Prefer practical scenarios (home backup, preparedness, energy confidence) over abstract art.
- If product image quality is strong, suggest a hybrid composition that highlights the product naturally.
- Translate the assigned logic principle into an emotionally clear scene: vulnerability, old-way versus smart-way contrast, a new standard, cross-setting adaptability, or a credible rescue result.
- Plan only the photorealistic background scene. The system adds the real product, exact headline, verified specs, brand, and CTA afterward.
- Build layered depth with foreground, midground, and atmospheric background structure.
- Use visual drama through contrast, lighting, framing, environment, and human emotion, not fake UI or decorative ad furniture.
- Never include baked-in text, numerals, logos, labels, signage, fake products, device mockups, badges, buttons, charts, or placeholder frames.
- Return only the requested JSON shape.
""".strip()

AGENT_CONFERENCE_BRIEF = """
You are facilitating a conference room discussion between specialized creative agents for Infenergy Power.

Participants:
- Copywriter Agent: maximizes clarity, persuasion, and conversion.
- Visual Director Agent: ensures image concept and composition amplify the message.
- Product Truth Agent: blocks unsupported claims and keeps facts verifiable.
- Platform Editor Agent: adapts execution for Facebook, Instagram, and LinkedIn behavior.
- Product Intelligence Agent: ensures the topic, product, audience, benefits, and proof actually fit together.

Task:
- Have the agents debate strengths, weaknesses, and risks in the current draft.
- Produce a unified plan to improve collective performance.
- Keep recommendations practical and directly applicable in this run.
- Explicitly discuss whether the topic truly matches the featured product.
- Explicitly discuss whether the copy names the product and includes concrete product details.

Constraints:
- No invented product specs, warranties, or guarantees.
- Keep tone trustworthy and practical.
- Prefer specific improvements over generic feedback.
- Flag any mismatch between the chosen topic and the featured product as a high-priority issue.
""".strip()

PREGEN_CONFERENCE_BRIEF = """
You are facilitating a pre-generation conference room meeting for Infenergy Power before any draft is written.

Participants:
- Copywriter Agent
- Visual Director Agent
- Product Truth Agent
- Platform Editor Agent
- Product Intelligence Agent

Objective:
- Decide the single best post direction for this run before writing starts.
- Agree on the strongest hook angle, CTA framing, and visual focus.
- Reduce duplication risk by choosing a fresh direction versus recent posts.
- Ensure the selected topic is native to the featured product's use case, not just the funnel stage.
- Ensure the draft direction will explicitly name the product and mention its real benefits or specs.

Constraints:
- Use only supported product facts.
- Keep recommendations practical, specific, and conversion-oriented.
- Return only JSON in the requested shape.
""".strip()

IDEATION_DIVERGENCE_BRIEF = """
You are the Ideation Divergence Agent.

Goal:
- Produce fresh, genuinely distinct creative angles for this run.
- Avoid repeating recent hooks and topics.
- Return only JSON in the requested shape.
""".strip()

AUDIENCE_PSYCHOGRAPHICS_BRIEF = """
You are the Audience Psychographics Agent.

Goal:
- Translate the chosen angle into emotional drivers, objections, trust triggers, and CTA framing.
- Keep language practical and directly actionable for copy generation.
- Return only JSON in the requested shape.
""".strip()

NARRATIVE_ARCHITECT_BRIEF = """
You are the Narrative Architect Agent.

Goal:
- Define the narrative sequence and mandatory proof elements.
- Keep structure conversion-oriented and platform-ready.
- Return only JSON in the requested shape.
""".strip()

PLATFORM_VOICE_CALIBRATOR_BRIEF = """
You are the Platform Voice Calibrator Agent.

Goal:
- Provide platform-native voice directives for Facebook, Instagram, and LinkedIn.
- Keep each directive concise, practical, and distinct.
- Return only JSON in the requested shape.
""".strip()

HOOK_STRESS_TEST_BRIEF = """
You are the Hook Stress-Test Agent.

Goal:
- Evaluate candidate hooks for clarity, specificity, credibility, and curiosity.
- Select one best hook for this run.
- Return only JSON in the requested shape.
""".strip()

PRECISION_CLAIMS_VERIFIER_BRIEF = """
You are the Precision Claims Verifier Agent.

Goal:
- Validate claim precision and factual grounding.
- Flag unsupported, absolute, or unverifiable statements.
- Return only JSON in the requested shape.
""".strip()

COMPLIANCE_POLICY_SENTINEL_BRIEF = """
You are the Compliance and Policy Sentinel Agent.

Goal:
- Detect compliance and policy risk in social copy.
- Assign a risk level and required remediation actions.
- Return only JSON in the requested shape.
""".strip()

SEMANTIC_NOVELTY_BRIEF = """
You are the Semantic Novelty Agent.

Goal:
- Evaluate novelty versus recent posts.
- Provide rewrite guidance if the concept is too similar.
- Return only JSON in the requested shape.
""".strip()

VISUAL_STRATEGY_BRIEF = """
You are the Visual Strategy Agent.

Goal:
- Improve visual intent, composition, and per-platform emphasis.
- Keep recommendations practical for immediate generation.
- Return only JSON in the requested shape.
""".strip()

CTA_OPTIMIZATION_BRIEF = """
You are the CTA Optimization Agent.

Goal:
- Optimize CTA wording for funnel stage and audience friction.
- Provide one recommended CTA plus alternates.
- Return only JSON in the requested shape.
""".strip()


def _default_phase2_stack(
    *,
    selected_hook: str,
    selected_cta: str,
    audience_segment: str,
    topic: str,
    funnel_stage: str,
) -> dict:
    return {
        "ideation_divergence": {
            "concepts": [
                {
                    "angle": f"Practical decision framework for {topic.lower()}",
                    "hook_candidate": selected_hook,
                    "narrative_focus": "problem to practical framework to clear next step",
                    "risk_note": "avoid unsupported performance guarantees",
                }
            ],
            "winner_angle": f"Practical decision framework for {topic.lower()}",
            "winner_hook": selected_hook,
            "novelty_rationale": "Chosen for practical specificity and low repetition risk.",
        },
        "audience_psychographics": {
            "primary_segment": audience_segment,
            "emotional_driver": "confidence in making a smart and safe power decision",
            "core_objection": "fear of buying the wrong system for real-world usage",
            "trust_trigger": "specific verifiable specs tied to everyday use",
            "cta_framing": selected_cta,
        },
        "narrative_architect": {
            "narrative_sequence": ["attention", "pain", "education", "product_fit", "proof", "cta"],
            "must_include": [
                "one practical scenario",
                "at least one verifiable metric",
                "one objection handling line",
            ],
            "proof_style": "specific and verifiable without overclaiming",
            "close_style": "single direct call to action",
        },
        "platform_voice_calibrator": {
            "facebook": "conversational community educator with practical examples",
            "instagram": "short visual-first lines with concrete benefit and urgency",
            "linkedin": "professional advisory tone with clear framework and business relevance",
        },
        "hook_stress_test": {
            "candidate_hooks": [selected_hook],
            "recommended_hook": selected_hook,
            "reason": f"Best fit for {funnel_stage.lower()} stage clarity and credibility.",
        },
    }


def _run_phase2_creative_stack(
    model_candidates: list[str],
    *,
    topic: str,
    funnel_stage: str,
    stage_objective: str,
    selected_hook: str,
    selected_cta: str,
    audience_segment: str,
    product_name: str,
    product_categories: str,
    product_metrics: str,
    recent_hooks: list[str],
    recent_topics: list[str],
) -> dict:
    stack = _default_phase2_stack(
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        audience_segment=audience_segment,
        topic=topic,
        funnel_stage=funnel_stage,
    )
    segment_constraints = _segment_creative_constraints(audience_segment)

    try:
        from agents.learning_context import load_operational_learning
        lessons = load_operational_learning(DATA_DIR)
    except Exception:
        lessons = {}
    winning_hooks = lessons.get("winning_hook_examples", []) if isinstance(lessons, dict) else []
    losing_hooks = lessons.get("losing_hook_examples", []) if isinstance(lessons, dict) else []
    winning_patterns = lessons.get("winning_patterns", []) if isinstance(lessons, dict) else []
    losing_patterns = lessons.get("losing_patterns", []) if isinstance(lessons, dict) else []
    top_warnings = lessons.get("quality_warnings_to_resolve", []) if isinstance(lessons, dict) else []

    ideation_prompt = f"""{IDEATION_DIVERGENCE_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Stage objective: {stage_objective}
- Audience segment: {audience_segment}
- Product: {product_name or 'N/A'}
- Product categories: {product_categories or 'N/A'}
- Product measurable specs: {product_metrics or 'N/A'}
- Current hook candidate: {selected_hook}
- Recent hooks to avoid: {recent_hooks}
- Recent topics to avoid: {recent_topics}
- Recent winning hook patterns (echo the style, not the words): {winning_hooks}
- Recent losing hook patterns (avoid this style entirely): {losing_hooks}
- Supported performance patterns (apply only in the named dimension): {winning_patterns}
- Underperforming patterns to avoid in the named dimension: {losing_patterns}
- Recent quality warnings to actively resolve in the new draft: {top_warnings}

Return ONLY valid JSON with this exact shape:
{{
  "concepts": [
    {{"angle": "string", "hook_candidate": "string", "narrative_focus": "string", "risk_note": "string"}},
    {{"angle": "string", "hook_candidate": "string", "narrative_focus": "string", "risk_note": "string"}}
  ],
  "winner_angle": "string",
  "winner_hook": "string",
  "novelty_rationale": "string"
}}"""
    ideation = _generate_json_with_gemini(ideation_prompt, model_candidates)
    if isinstance(ideation, dict):
        stack["ideation_divergence"] = ideation

    winner_angle = str(stack["ideation_divergence"].get("winner_angle", "")).strip() or topic
    winner_hook = str(stack["ideation_divergence"].get("winner_hook", "")).strip() or selected_hook

    psychographics_prompt = f"""{AUDIENCE_PSYCHOGRAPHICS_BRIEF}

Run context:
- Topic: {topic}
- Chosen angle: {winner_angle}
- Chosen hook: {winner_hook}
- Funnel stage: {funnel_stage}
- Audience segment: {audience_segment}

Return ONLY valid JSON with this exact shape:
{{
  "primary_segment": "string",
  "emotional_driver": "string",
  "core_objection": "string",
  "trust_trigger": "string",
  "cta_framing": "string"
}}"""
    psychographics = _generate_json_with_gemini(psychographics_prompt, model_candidates)
    if isinstance(psychographics, dict):
        stack["audience_psychographics"] = psychographics

    narrative_prompt = f"""{NARRATIVE_ARCHITECT_BRIEF}

Run context:
- Topic: {topic}
- Chosen angle: {winner_angle}
- Hook: {winner_hook}
- Funnel stage: {funnel_stage}
- Stage objective: {stage_objective}
- CTA direction: {selected_cta}
- Segment narrative requirement: {segment_constraints.get('narrative_requirement', '')}

Return ONLY valid JSON with this exact shape:
{{
  "narrative_sequence": ["string", "string", "string"],
  "must_include": ["string", "string"],
  "proof_style": "string",
  "close_style": "string"
}}"""
    narrative = _generate_json_with_gemini(narrative_prompt, model_candidates)
    if isinstance(narrative, dict):
        stack["narrative_architect"] = narrative

    voice_prompt = f"""{PLATFORM_VOICE_CALIBRATOR_BRIEF}

Run context:
- Topic: {topic}
- Chosen angle: {winner_angle}
- Hook: {winner_hook}
- Funnel stage: {funnel_stage}

Return ONLY valid JSON with this exact shape:
{{
  "facebook": "string",
  "instagram": "string",
  "linkedin": "string"
}}"""
    voice = _generate_json_with_gemini(voice_prompt, model_candidates)
    if isinstance(voice, dict):
        stack["platform_voice_calibrator"] = voice

    hook_candidates = [winner_hook, selected_hook] + [
        str(row.get("hook_candidate", "")).strip()
        for row in stack.get("ideation_divergence", {}).get("concepts", [])
        if isinstance(row, dict)
    ]
    hook_candidates = [h for h in hook_candidates if h]
    unique_hook_candidates: list[str] = []
    seen = set()
    for hook in hook_candidates:
        k = hook.lower().strip()
        if not k or k in seen:
            continue
        seen.add(k)
        unique_hook_candidates.append(hook)

    # Keep at least three candidates so stress-testing can rotate archetypes.
    fallback_candidates = [
        f"What changes first in your plan if this outage lasts 4 hours?",
        f"Myth: bigger labels always mean better {topic.lower()} outcomes.",
        f"Compare 2 options before buying: what actually decides fit for {topic.lower()}?",
    ]
    for fallback in fallback_candidates:
        if len(unique_hook_candidates) >= 3:
            break
        key = fallback.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        unique_hook_candidates.append(fallback)

    hook_test_prompt = f"""{HOOK_STRESS_TEST_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Candidate hooks: {unique_hook_candidates[:6]}

Return ONLY valid JSON with this exact shape:
{{
  "candidate_hooks": ["string", "string"],
  "recommended_hook": "string",
  "reason": "string"
}}"""
    hook_test = _generate_json_with_gemini(hook_test_prompt, model_candidates)
    if isinstance(hook_test, dict):
        stack["hook_stress_test"] = hook_test

    return stack


def _default_phase3_safety_stack(*, selected_hook: str, selected_cta: str, recent_topics: list[str]) -> dict:
    novelty_signal = "high" if selected_hook not in recent_topics else "medium"
    novelty_score = 0.82 if novelty_signal == "high" else 0.66
    return {
        "precision_claims_verifier": {
            "passed": True,
            "issues": [],
            "required_fixes": [],
        },
        "compliance_policy_sentinel": {
            "risk_level": "low",
            "blocked_terms": [],
            "required_actions": [],
        },
        "semantic_novelty": {
            "novelty_score": novelty_score,
            "signal": novelty_signal,
            "rewrite_guidance": [] if novelty_signal == "high" else ["Use a less familiar opening scenario."],
        },
    }


def _run_phase3_safety_stack(
    model_candidates: list[str],
    *,
    topic: str,
    funnel_stage: str,
    selected_hook: str,
    selected_cta: str,
    recent_hooks: list[str],
    recent_topics: list[str],
    content_preview: str,
) -> dict:
    stack = _default_phase3_safety_stack(
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        recent_topics=recent_topics,
    )

    precision_prompt = f"""{PRECISION_CLAIMS_VERIFIER_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Hook: {selected_hook}
- CTA: {selected_cta}

Draft preview:
{content_preview}

Return ONLY valid JSON with this exact shape:
{{
  "passed": true,
  "issues": ["string"],
  "required_fixes": ["string"]
}}"""
    precision = _generate_json_with_gemini(precision_prompt, model_candidates)
    if isinstance(precision, dict):
        stack["precision_claims_verifier"] = precision

    compliance_prompt = f"""{COMPLIANCE_POLICY_SENTINEL_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Hook: {selected_hook}
- CTA: {selected_cta}

Draft preview:
{content_preview}

Return ONLY valid JSON with this exact shape:
{{
  "risk_level": "low",
  "blocked_terms": ["string"],
  "required_actions": ["string"]
}}"""
    compliance = _generate_json_with_gemini(compliance_prompt, model_candidates)
    if isinstance(compliance, dict):
        stack["compliance_policy_sentinel"] = compliance

    novelty_prompt = f"""{SEMANTIC_NOVELTY_BRIEF}

Run context:
- Topic: {topic}
- Hook: {selected_hook}
- Recent hooks: {recent_hooks}
- Recent topics: {recent_topics}

Return ONLY valid JSON with this exact shape:
{{
  "novelty_score": 0.75,
  "signal": "high|medium|low",
  "rewrite_guidance": ["string"]
}}"""
    novelty = _generate_json_with_gemini(novelty_prompt, model_candidates)
    if isinstance(novelty, dict):
        stack["semantic_novelty"] = novelty

    return stack


def _default_phase4_optimization_stack(*, selected_cta: str, funnel_stage: str) -> dict:
    return {
        "visual_strategy": {
            "visual_objective": f"Reinforce {funnel_stage.lower()} intent with practical context.",
            "composition_adjustments": [
                "Show one concrete real-world use case.",
                "Keep product visibility natural and not over-styled.",
            ],
            "platform_focus": {
                "facebook": "education-first composition with readable focal hierarchy",
                "instagram": "high-contrast focal point and depth",
                "linkedin": "clean credibility composition",
            },
        },
        "cta_optimization": {
            "recommended_cta": selected_cta,
            "alternates": [
                "Get a practical system match for your setup.",
                "Compare options based on your real daily loads.",
            ],
            "friction_note": "Reduce effort language and specify the first step.",
        },
    }


def _run_phase4_optimization_stack(
    model_candidates: list[str],
    *,
    topic: str,
    funnel_stage: str,
    selected_hook: str,
    selected_cta: str,
    audience_segment: str,
) -> dict:
    stack = _default_phase4_optimization_stack(
        selected_cta=selected_cta,
        funnel_stage=funnel_stage,
    )

    visual_prompt = f"""{VISUAL_STRATEGY_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Hook: {selected_hook}
- Audience: {audience_segment}

Return ONLY valid JSON with this exact shape:
{{
  "visual_objective": "string",
  "composition_adjustments": ["string", "string"],
  "platform_focus": {{
    "facebook": "string",
    "instagram": "string",
    "linkedin": "string"
  }}
}}"""
    visual = _generate_json_with_gemini(visual_prompt, model_candidates)
    if isinstance(visual, dict):
        stack["visual_strategy"] = visual

    cta_prompt = f"""{CTA_OPTIMIZATION_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Hook: {selected_hook}
- Current CTA: {selected_cta}
- Audience: {audience_segment}

Return ONLY valid JSON with this exact shape:
{{
  "recommended_cta": "string",
  "alternates": ["string", "string"],
  "friction_note": "string"
}}"""
    cta = _generate_json_with_gemini(cta_prompt, model_candidates)
    if isinstance(cta, dict):
        stack["cta_optimization"] = cta

    return stack


def _build_phase7_conference_packets(
    *,
    run_context: dict,
    pre_generation_conference: dict,
    phase2_stack: dict,
    phase3_stack: dict,
    phase4_stack: dict,
    conference_summary: dict,
) -> dict:
    return {
        "pre_generation_packet": {
            "topic": run_context.get("topic", ""),
            "funnel_stage": run_context.get("funnel_stage", ""),
            "hook_direction": run_context.get("draft_direction", {}).get("selected_hook", ""),
            "cta_direction": run_context.get("draft_direction", {}).get("selected_cta", ""),
            "inputs": {
                "pre_generation_conference": pre_generation_conference,
                "phase2": phase2_stack,
            },
        },
        "pre_publish_packet": {
            "safety": phase3_stack,
            "optimization": phase4_stack,
            "conference_refinement": conference_summary,
        },
        "post_run_packet": {
            "expected_next_actions": [
                "record outcomes",
                "compare channel-level performance",
                "feed lessons into next prompt context",
            ],
            "trace": {
                "schema_version": SCHEMAS_VERSION,
                "phase2_keys": sorted(list((phase2_stack or {}).keys())),
                "phase3_keys": sorted(list((phase3_stack or {}).keys())),
                "phase4_keys": sorted(list((phase4_stack or {}).keys())),
            },
        },
    }


def _conversion_caption_gate(platform_posts: dict, talking_point: dict, want_product: bool = True) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    concrete_noun_pattern = re.compile(
        r"\b(?:battery|capacity|charger|device|filter|fan|light|phone|laptop|"
        r"outage|panel|plug|port|power|runtime|station|vehicle|water|watt|wh|voltage)\b",
        flags=re.IGNORECASE,
    )
    action_pattern = re.compile(
        r"\b(?:build|check|compare|get|list|match|name|open|order|read|review|save|shop|tap|visit)\b",
        flags=re.IGNORECASE,
    )
    for platform_name in ("facebook", "instagram", "linkedin"):
        row = platform_posts.get(platform_name, {}) if isinstance(platform_posts, dict) else {}
        caption = str(row.get("caption", "") if isinstance(row, dict) else "")
        if not caption.strip():
            reasons.append(f"{platform_name}:missing_caption")
            continue
        low = caption.lower()
        if not concrete_noun_pattern.search(caption):
            reasons.append(f"{platform_name}:missing_concrete_subject")
        if want_product and not action_pattern.search(caption):
            reasons.append(f"{platform_name}:missing_next_step")
        # Numeric specs evidence only applies to product-led posts — business-first, no-product
        # posts have nothing to cite a spec/number for and shouldn't be penalized for that.
        if want_product and not _contains_numeric_evidence(caption):
            reasons.append(f"{platform_name}:missing_numeric_evidence")
        for bad in POSITIONING_REPLACEMENTS.keys():
            if bad in low:
                reasons.append(f"{platform_name}:off_brand_phrase:{bad}")
    return len(reasons) == 0, reasons


def _topic_queue_needs_migration(existing: dict) -> bool:
    """True if a persisted topic_queue.json predates the business-first pillar taxonomy."""
    if not isinstance(existing, dict):
        return True
    pillars = existing.get("pillars", [])
    if not isinstance(pillars, list) or not pillars:
        return True
    current_pillars = set(DEFAULT_TOPIC_QUEUE["pillars"])
    existing_pillars = {str(p).strip() for p in pillars if str(p).strip()}
    # Stale if it is missing most of the current pillars (legacy/off-brand queue), or if it
    # still contains pillars from a generic residential-solar template that never applied
    # to this business (e.g. solar_savings, net_metering-flavored content).
    known_off_brand_pillars = {"solar_savings", "energy_independence", "battery_storage", "case_studies", "industry_news"}
    if existing_pillars & known_off_brand_pillars:
        return True
    overlap = existing_pillars & current_pillars
    return len(overlap) < max(1, len(current_pillars) // 2)


def ensure_runtime_data() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    ensure_campaign_runtime_files()

    topic_primary = os.path.join(DATA_DIR, "topic_queue.json")
    topic_fallback = os.path.join(BASE_DATA_DIR, "topic_queue.json")
    if not os.path.exists(topic_primary) and not os.path.exists(topic_fallback):
        with open(topic_primary, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_TOPIC_QUEUE, f, indent=2)
    else:
        for path in (topic_primary, topic_fallback):
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
            if _topic_queue_needs_migration(existing):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_TOPIC_QUEUE, f, indent=2)

    history_primary = os.path.join(DATA_DIR, "post_history.json")
    history_fallback = os.path.join(BASE_DATA_DIR, "post_history.json")
    if not os.path.exists(history_primary) and not os.path.exists(history_fallback):
        with open(history_primary, "w", encoding="utf-8") as f:
            json.dump({"posts": []}, f, indent=2)

    try:
        sync_inventory_database(force=False)
    except Exception as e:
        print(f"[WARN] inventory database sync skipped: {e}")


def _read_json_with_fallback(filename: str) -> dict:
    primary = os.path.join(DATA_DIR, filename)
    fallback = os.path.join(BASE_DATA_DIR, filename)
    for path in (primary, fallback):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

    if filename == "post_history.json":
        return {"posts": []}

    if filename == "topic_queue.json":
        return DEFAULT_TOPIC_QUEUE

    raise FileNotFoundError(f"Missing required JSON file: {filename}")


def load_topic_queue() -> dict:
    return _read_json_with_fallback("topic_queue.json")


def load_history() -> dict:
    return _read_json_with_fallback("post_history.json")


def save_history(history: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    target = os.path.join(DATA_DIR, "post_history.json")
    temp = target + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    os.replace(temp, target)


def _load_latest_marketing_strategy() -> dict:
    paths = []
    for base in (DATA_DIR, BASE_DATA_DIR):
        paths.extend(glob.glob(os.path.join(base, "marketing", "marketing_strategy_*.json")))
        # Backward compatibility with older artifact name.
        paths.extend(glob.glob(os.path.join(base, "marketing", "marketing_bundle_*.json")))

    if not paths:
        return {}

    latest = max(paths, key=os.path.getmtime)
    try:
        with open(latest, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _load_latest_structured_campaign() -> dict:
    paths = []
    for base in (DATA_DIR, BASE_DATA_DIR):
        paths.extend(glob.glob(os.path.join(base, "marketing", "campaigns", "campaign_*.json")))
    if not paths:
        return {}
    latest = max(paths, key=os.path.getmtime)
    try:
        with open(latest, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _load_founder_brand_manifesto() -> dict:
    for base in (DATA_DIR, BASE_DATA_DIR):
        path = os.path.join(base, "marketing", "founder_brand_manifesto.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
                if isinstance(payload, dict):
                    return payload
        except Exception:
            continue
    return {}


def _dedupe_str_list(values: list) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _build_brand_profile_seed(marketing_strategy: dict, manifesto: dict) -> dict:
    strategy = marketing_strategy if isinstance(marketing_strategy, dict) else {}
    man = manifesto if isinstance(manifesto, dict) else {}

    strategy_brand_profile = strategy.get("brand_profile", {}) if isinstance(strategy.get("brand_profile", {}), dict) else {}
    strategy_founder = strategy_brand_profile.get("founder_manifesto", {}) if isinstance(strategy_brand_profile.get("founder_manifesto", {}), dict) else {}
    strategy_voice = strategy.get("voice", {}) if isinstance(strategy.get("voice", {}), dict) else {}

    man_personality = man.get("brand_personality", {}) if isinstance(man.get("brand_personality", {}), dict) else {}
    man_approved = man.get("approved_sales_verbiage", {}) if isinstance(man.get("approved_sales_verbiage", {}), dict) else {}
    man_guardrails = man.get("guardrails", {}) if isinstance(man.get("guardrails", {}), dict) else {}
    man_business = man.get("business_profile", {}) if isinstance(man.get("business_profile", {}), dict) else {}

    audience_segments = man.get("audience_segments", [])
    if not isinstance(audience_segments, list):
        audience_segments = []

    words_to_avoid = strategy_voice.get("words_to_avoid", [])
    if not isinstance(words_to_avoid, list):
        words_to_avoid = []

    words_to_use = strategy_voice.get("words_to_use", [])
    if not isinstance(words_to_use, list):
        words_to_use = []

    seed = {
        "brand_name": man.get("brand_name") or strategy.get("brand", {}).get("brand_name") or "Infenergy Power",
        "tagline": strategy_founder.get("tagline") or man_approved.get("hero_line") or "",
        "mission": strategy_founder.get("mission") or man.get("mission") or "",
        "positioning": man_business.get("positioning") or INFENERGY_BUSINESS_GOALS["positioning"],
        "audience_summary": " | ".join([str(x) for x in audience_segments[:5]]) or INFENERGY_BUSINESS_GOALS["audience_summary"],
        "personality_name": man_personality.get("voice_name") or "Calm Strength",
        "personality_traits": _dedupe_str_list(man_personality.get("traits", [])),
        "tone_rules": _dedupe_str_list(man_personality.get("tone_rules", [])),
        "voice_rules": _dedupe_str_list(strategy_voice.get("voice_rules", [])),
        "approved_phrases": _dedupe_str_list(man_approved.get("core_phrases", [])),
        "cta_style": _dedupe_str_list(man_approved.get("cta_style", [])),
        "trust_close": man_approved.get("trust_close", "") or "",
        "words_to_use": _dedupe_str_list(words_to_use),
        "words_to_avoid": _dedupe_str_list(words_to_avoid),
        "forbidden_phrases": _dedupe_str_list(man_guardrails.get("disallowed_claim_patterns", [])),
        "core_values": _dedupe_str_list(man.get("core_values", [])),
        "additional_notes": str(man_guardrails.get("allowed_solar_context", "")).strip(),
    }
    return seed


def _products_csv_fingerprint() -> str:
    products_dir = os.path.join(BASE_DATA_DIR, "products")
    csv_paths = sorted(glob.glob(os.path.join(products_dir, "*.csv")))
    digest = hashlib.sha256()
    for csv_path in csv_paths:
        digest.update(csv_path.encode("utf-8"))
        with open(csv_path, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def _manifesto_fingerprint(manifesto: dict) -> str:
    canonical = json.dumps(
        manifesto if isinstance(manifesto, dict) else {},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compile_human_connection_context(
    *, audience_segment: str, topic: str, selected_hook: str, funnel_stage: str
) -> dict:
    """Load a compact owner-truth context for either copy-generation pipeline."""
    try:
        from business_intelligence import api as bi_api
        moment_id = bi_api.resolve_human_connection_moment(topic, selected_hook)
        job = {
            "ATTENTION": "start_a_conversation",
            "EDUCATION": "teach",
            "DESIRE": "help_decide",
            "TRUST": "build_trust",
            "CONVERSION": "sell",
        }.get(str(funnel_stage or "").upper(), "")
        return bi_api.compile_human_connection_context(
            segment_id=audience_segment,
            moment_id=moment_id,
            job=job,
        )
    except Exception:
        return {}


def _human_connection_prompt_context(context: dict) -> str:
    moment = context.get("moment_world") if isinstance(context.get("moment_world"), dict) else {}
    if not moment:
        return ""
    creation_logic = context.get("preemptive_creation_logic") if isinstance(context.get("preemptive_creation_logic"), dict) else {}
    before_generation = creation_logic.get("before_generation") if isinstance(creation_logic.get("before_generation"), dict) else {}
    return (
        "HUMAN CONNECTION CONTEXT (owner truth; use before product context):\n"
        f"- Person: {moment.get('person', '')}\n"
        f"- Decision state: {moment.get('decision_state', '')}\n"
        f"- Responsibility: {moment.get('responsibility', '')}\n"
        f"- Human question: {moment.get('human_question', '')}\n"
        f"- Capability goal: {moment.get('capability_goal', '')}\n"
        f"- Product role: {moment.get('product_role', '')}\n"
        f"- Human Brain movement: {(before_generation.get('brain_movement', {}) or {}).get('question', '')}\n"
        f"- Human Heart result: {(before_generation.get('heart_after', {}) or {}).get('question', '')}\n"
        "- Start with this lived moment. Define the useful movement in thought before expressing its earned emotional result. Do not manufacture fear or force a product into the post.\n"
    )


def _sync_manifesto_brand_profile(manifesto: dict, strategy: dict, *, force: bool = False) -> tuple[bool, bool, list[str]]:
    """Synchronize only manifesto-owned operational profile data, never catalog/runtime state."""
    fingerprint = _manifesto_fingerprint(manifesto)
    previous_fingerprint = inventory_db.get_brand_profile_manifesto_checksum(DATA_DIR)
    changed = bool(fingerprint) and fingerprint != previous_fingerprint
    if not force and not changed and inventory_db.has_brand_profile(DATA_DIR):
        return False, False, []

    seed = _build_brand_profile_seed(strategy, manifesto)
    previous_profile = inventory_db.fetch_brand_profile(DATA_DIR)
    affected_fields = sorted(
        key for key, value in seed.items()
        if previous_profile.get(key) != value
    )
    seeded = inventory_db.upsert_brand_profile(DATA_DIR, seed) if seed else False
    if seeded:
        inventory_db.set_brand_profile_manifesto_checksum(DATA_DIR, fingerprint)
    return seeded, changed, affected_fields


def sync_inventory_database(force: bool = False) -> dict:
    inventory_db.init_inventory_db(DATA_DIR)
    before_count = inventory_db.products_count(DATA_DIR)
    products_seeded = 0
    brand_seeded = False
    ideology_seeded = False

    current_fingerprint = _products_csv_fingerprint()
    stored_fingerprint = inventory_db.get_products_source_fingerprint(DATA_DIR)
    csv_changed = bool(current_fingerprint) and current_fingerprint != stored_fingerprint

    should_seed_products = (
        force or before_count == 0 or csv_changed or bool(_discover_local_product_image_files())
    )
    if should_seed_products:
        csv_products = _load_products_from_csv()
        if csv_products:
            products_seeded = inventory_db.upsert_products(DATA_DIR, csv_products, source="wc_csv")
        if current_fingerprint:
            inventory_db.set_products_source_fingerprint(DATA_DIR, current_fingerprint)

    strategy = _load_latest_marketing_strategy()
    manifesto = _load_founder_brand_manifesto()
    brand_seeded, manifesto_changed, brand_fields_changed = _sync_manifesto_brand_profile(
        manifesto,
        strategy,
        force=force,
    )

    if force or not inventory_db.has_selling_ideology(DATA_DIR):
        ideology_seeded = inventory_db.upsert_selling_ideology(DATA_DIR, conference_selling_ideology_payload())

    after_count = inventory_db.products_count(DATA_DIR)
    return {
        "db_path": inventory_db.get_db_path(DATA_DIR),
        "products_before": before_count,
        "products_seeded": products_seeded,
        "products_after": after_count,
        "products_csv_changed": csv_changed,
        "brand_seeded": brand_seeded,
        "brand_manifesto_changed": manifesto_changed,
        "brand_profile_fields_changed": brand_fields_changed,
        "ideology_seeded": ideology_seeded,
        "brand_profile_present": inventory_db.has_brand_profile(DATA_DIR),
        "selling_ideology_present": inventory_db.has_selling_ideology(DATA_DIR),
    }


def load_brand_profile() -> dict:
    sync_inventory_database(force=False)
    profile = inventory_db.fetch_brand_profile(DATA_DIR)
    if profile:
        return profile

    strategy = _load_latest_marketing_strategy()
    manifesto = _load_founder_brand_manifesto()
    return _build_brand_profile_seed(strategy, manifesto)


def conference_brand_profile_payload() -> dict:
    """Canonical conference output for positioning and brand voice."""
    return {
        "brand_name": "Infenergy Power",
        "tagline": "From chaos to control.",
        "mission": "Help families and everyday people stay powered, prepared, safe, and connected when normal systems fail.",
        "positioning": "Preparedness-first portable power and resilience brand for families, travelers, outdoor users, seniors, and small operators.",
        "audience_summary": (
            "Working families in storm-prone regions | RV and outdoor users | Emergency-minded households "
            "| Seniors and caregivers | Small operators who need uptime continuity"
        ),
        "personality_name": "Calm Strength",
        "personality_traits": [
            "Protective",
            "Mission-driven",
            "Practical",
            "Empathetic",
            "Confident",
            "Coach-like",
            "Preparedness-first",
            "Steady under pressure",
            "Spec-literate",
            "Action-oriented",
        ],
        "tone_rules": [
            "Lead with a real-life risk or customer moment.",
            "Translate technical specs into plain-language outcomes.",
            "Be urgent without fear-mongering.",
            "Sound human and committed, never generic.",
            "Close with one clear, low-friction next step.",
            "Make every message feel like guidance from someone who wants the customer ready before the next outage.",
            "Favor concrete preparedness language over broad lifestyle fluff.",
            "Keep the message anchored in why the product matters when normal power is unavailable.",
        ],
        "voice_rules": [
            "Lead with empathy and urgency rooted in real family preparedness moments.",
            "Translate specs into immediate and long-term outcomes people can feel.",
            "Use value stacking, practical guidance, and one clear next step.",
            "Sound committed and human: protective, coach-like, and trustworthy.",
            "Anchor every claim in concrete facts and examples.",
            "Use language that helps customers feel more in control, more prepared, and less exposed.",
            "Frame products as tools with a real job: keeping essential devices powered when people cannot afford interruption.",
            "Reinforce readiness, continuity, mobility, and peace of mind in nearly every message.",
        ],
        "approved_phrases": [
            "Preparedness over panic",
            "Power is protection",
            "From chaos to control",
            "Practical power for real life",
            "Built for outages, travel, and everyday resilience",
            "Spec-backed recommendations you can trust",
            "Stay charged when the unexpected hits",
            "Backup power with a purpose",
            "Know what powers your must-run devices",
            "Right-size your power before you buy",
            "Reliable power for the moments that matter most",
            "Portable power built for real interruptions",
        ],
        "cta_style": [
            "Get your readiness plan",
            "Map your must-run devices",
            "Book your product-fit consultation",
            "Build your outage-ready setup",
        ],
        "trust_close": "We do not just sell products. We help people stay calm, connected, and prepared when it matters most.",
        "words_to_use": [
            "preparedness",
            "protection",
            "resilience",
            "continuity",
            "practical",
            "spec-backed",
            "confidence",
            "control",
            "outage-ready",
            "must-run",
            "backup",
            "portable",
            "dependable",
            "storm-ready",
            "real-world",
            "fit",
            "power plan",
            "device priority",
        ],
        "words_to_avoid": [
            "revolutionary",
            "game-changing",
            "once-in-a-lifetime",
            "guaranteed",
            "instant savings",
        ],
        "forbidden_phrases": [
            "Unverifiable guarantees",
            "Fear-only manipulation",
            "Rooftop installation positioning",
            "Tax-credit-first sales framing",
        ],
        "core_values": [
            "Protection",
            "Preparedness",
            "Reliability",
            "Integrity",
            "Service",
            "Commitment",
            "Practical guidance",
        ],
        "additional_notes": "Primary lifestyle positioning: home resilience, mobile autonomy, and family safety continuity.",
    }


def apply_conference_brand_profile() -> dict:
    sync_inventory_database(force=False)
    payload = conference_brand_profile_payload()
    ok = inventory_db.upsert_brand_profile(DATA_DIR, payload)
    profile = inventory_db.fetch_brand_profile(DATA_DIR)
    return {
        "ok": bool(ok),
        "brand_profile": profile,
    }


def conference_selling_ideology_payload() -> dict:
    return {
        "schema_version": "v1",
        "framework_mode": "mixed_with_campaign_overlay",
        "primary_conversion": "audience_relationship",
        "tone_blend": "calm_protective_plus_assertive_urgent",
        "value_lens": "benefit_first",
        "message_filter": "human_value_and_format_fit",
        "cta_mode": "natural_response",
        "campaign_behavior": "overlay_not_replace",
        "proof_rule": "scenario_plus_spec_evidence",
        "disqualify_alternative": "patchwork_guesswork",
        "core_promise": "reliable_peace_of_mind",
        "audience_priority": [
            "working_families_outage_prone",
            "rv_and_outdoor_autonomy",
            "small_operator_and_caregiver_continuity",
        ],
        "psychographics": [
            "risk_aware_planners",
            "reliability_over_hype_buyers",
            "control_seekers_wanting_clarity",
            "value_protectors_avoiding_wrong_fit",
        ],
        "lifestyle_positioning": [
            "home_resilience",
            "mobile_autonomy",
            "family_safety_continuity",
            "preparedness_routine",
        ],
        "pillar_messages": [
            "preparedness_over_panic",
            "specs_to_outcomes",
            "right_size_before_upsell",
            "everyday_value_plus_emergency_value",
            "single_checkout_next_step",
            "power_is_protection",
            "stay_connected_when_the_grid_fails",
            "real_device_planning_before_purchase",
            "mobility_plus_resilience",
            "backup_power_with_a_job_to_do",
        ],
        "objection_handling": [
            "too_expensive_vs_cost_of_outage",
            "too_complex_use_readiness_checklist",
            "fit_uncertainty_use_scenario_proof",
            "delay_risk_reframe_with_action_cost",
            "i_can_wait_until_the_next_storm_reframe_with_preparation_timing",
            "not_sure_what_i_need_map_must_run_devices_first",
            "concerned_about_overbuying_match_specs_to_daily_use",
        ],
        "cta_ladder": [
            "attention_save_checklist",
            "education_map_top_3_devices",
            "desire_get_fit_recommendation",
            "trust_review_spec_backed_options",
            "conversion_checkout_now",
        ],
        "banned_phrases": [
            "generic_non_directional_copy",
            "hype_without_proof",
            "vague_cta_without_path",
            "fear_mongering",
            "unverifiable_guarantees",
        ],
    }


def load_selling_ideology() -> dict:
    sync_inventory_database(force=False)
    ideology = inventory_db.fetch_selling_ideology(DATA_DIR)
    if ideology:
        return ideology
    return conference_selling_ideology_payload()


def apply_conference_selling_ideology() -> dict:
    sync_inventory_database(force=False)
    payload = conference_selling_ideology_payload()
    ok = inventory_db.upsert_selling_ideology(DATA_DIR, payload)
    ideology = inventory_db.fetch_selling_ideology(DATA_DIR)
    return {
        "ok": bool(ok),
        "selling_ideology": ideology,
    }


def _is_usable_image_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    u = url.strip().lower()
    if not u.startswith("http"):
        return False
    if any(x in u for x in ("placeholder", "no-image", "default")):
        return False
    return True


def _is_supported_local_image_file(path: str) -> bool:
    if not path:
        return False
    src = str(path).strip()
    if src.lower().startswith("file://"):
        src = src[7:]
    if not os.path.isfile(src):
        return False
    return os.path.splitext(src)[1].lower() in {".png", ".jpg", ".jpeg", ".webp", ".avif"}


def _normalize_asset_key(text: str) -> str:
    base = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    ascii_text = base.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_text)


def _local_product_image_roots() -> list[str]:
    roots = [
        os.path.join(DATA_DIR, "products"),
        os.path.join(BASE_DATA_DIR, "products"),
        os.path.join(DATA_DIR, "product_images"),
        os.path.join(BASE_DATA_DIR, "product_images"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        norm = os.path.normpath(root)
        if norm in seen or not os.path.isdir(norm):
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _discover_local_product_image_files() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for root in _local_product_image_roots():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.avif"):
            for path in glob.glob(os.path.join(root, "**", ext), recursive=True):
                norm = os.path.normpath(path)
                if norm in seen or not os.path.isfile(norm):
                    continue
                seen.add(norm)
                found.append(norm)
    return found


def _score_local_asset_match(stem_key: str, sku: str, product_id: str, name: str) -> int:
    score = 0
    sku_key = _normalize_asset_key(sku)
    product_id_key = _normalize_asset_key(product_id)
    name_key = _normalize_asset_key(name)
    if sku_key and (sku_key == stem_key or sku_key in stem_key):
        score += 100
    if product_id_key and (product_id_key == stem_key or product_id_key in stem_key):
        score += 80
    if name_key:
        if name_key == stem_key:
            score += 90
        elif name_key in stem_key or stem_key in name_key:
            score += 70
    for token in ("main", "hero", "primary", "front", "cover", "product"):
        if token in stem_key:
            score += 5
    return score


def _match_local_product_images(name: str, sku: str, product_id: str) -> list[str]:
    ranked: list[tuple[int, str]] = []
    for path in _discover_local_product_image_files():
        stem = os.path.splitext(os.path.basename(path))[0]
        stem_key = _normalize_asset_key(stem)
        if not stem_key:
            continue
        score = _score_local_asset_match(stem_key, sku, product_id, name)
        if score <= 0:
            continue
        ranked.append((score, path))
    ranked.sort(key=lambda row: (-row[0], row[1].lower()))
    return [path for _, path in ranked[:6]]


def _load_category_image_fallbacks() -> dict[str, str]:
    custom = os.environ.get("IG_CATEGORY_FALLBACKS_JSON", "").strip()
    merged = dict(DEFAULT_CATEGORY_IMAGE_FALLBACKS)
    if custom:
        try:
            parsed = json.loads(custom)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    key = str(k).strip().lower()
                    val = str(v).strip()
                    if key and _is_usable_image_url(val):
                        merged[key] = val
        except Exception:
            pass
    return merged


def _category_tokens(categories: list[str]) -> list[str]:
    tokens = []
    for c in categories:
        raw = (c or "").strip().lower()
        if not raw:
            continue
        tokens.append(raw)
        if ">" in raw:
            tokens.append(raw.split(">", 1)[0].strip())
    out = []
    seen = set()
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _fallback_images_for_categories(categories: list[str]) -> list[str]:
    mapping = _load_category_image_fallbacks()
    out = []
    seen = set()
    for token in _category_tokens(categories):
        url = mapping.get(token, "")
        if _is_usable_image_url(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out[:4]


def _score_image_url(url: str) -> int:
    score = 0
    u = url.lower()
    if "-300x300" in u or "-150x150" in u:
        score -= 3
    if "scaled" in u:
        score += 2
    if u.endswith(".jpg") or u.endswith(".jpeg"):
        score += 2
    if u.endswith(".png"):
        score += 1
    if "wp-content/uploads" in u:
        score += 1
    return score


def _pick_best_image_urls(image_urls: list[str]) -> tuple[str, list[str]]:
    usable = [u for u in image_urls if _is_usable_image_url(u)]
    if not usable:
        return "", []
    ranked = sorted(usable, key=_score_image_url, reverse=True)
    primary = ranked[0]
    candidates = []
    seen = set()
    for u in ranked:
        if u not in seen:
            seen.add(u)
            candidates.append(u)
    return primary, candidates[:4]


def _strip_html(text: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", text or "", flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_metrics(text: str) -> list[str]:
    if not text:
        return []
    pattern = re.compile(
        r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?(?:Wh|mAh|W|kW|V|A|lbs|lb|in|mm|g|hours|hour|%|PD\s?\d+W|QC\s?\d+\.\d+)\b",
        flags=re.IGNORECASE,
    )
    seen = set()
    out = []
    for m in pattern.findall(text):
        k = m.lower()
        if k not in seen:
            seen.add(k)
            out.append(m)
    return out[:8]


def _canonical_product_url_from_row(row: dict) -> str:
    external = str(row.get("External URL") or "").strip()
    if re.match(r"^https://(?:www\.)?infenergypower\.com/product/[^\s]+/?$", external, flags=re.IGNORECASE):
        return external
    owned_html = " ".join((str(row.get("Short description") or ""), str(row.get("Description") or "")))
    match = re.search(
        r"https://(?:www\.)?infenergypower\.com/product/[^\s\"'<>\\]+/?",
        owned_html,
        flags=re.IGNORECASE,
    )
    return match.group(0).rstrip(".,;)") if match else ""


def _destination_url_for_content(product_url: str, structured_campaign: dict) -> str:
    return str(product_url).strip() or str(structured_campaign.get("destination_url", SITE_URL)).strip() or SITE_URL


def _load_products_from_csv() -> list[dict]:
    products_dir = os.path.join(BASE_DATA_DIR, "products")
    csv_paths = sorted(glob.glob(os.path.join(products_dir, "*.csv")))
    products = []

    for csv_path in csv_paths:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Published", "").strip() != "1":
                    continue

                name = (row.get("Name") or "").strip()
                if not name:
                    continue

                sku = (row.get("SKU") or "").strip()
                short_text = _strip_html(row.get("Short description") or "")
                long_text = _strip_html(row.get("Description") or "")
                merged_text = f"{short_text} {long_text}".strip()
                metrics = _extract_metrics(merged_text)
                categories = [c.strip() for c in (row.get("Categories") or "").split(",") if c.strip()]
                image_urls = [u.strip() for u in (row.get("Images") or "").split(",") if u.strip()]
                primary_image, image_candidates = _pick_best_image_urls(image_urls)
                raw_id = str(row.get("ID") or "").strip()
                stable_fallback_id = str(sku or hashlib.md5(name.lower().encode("utf-8")).hexdigest()[:12]).strip()
                resolved_product_id = raw_id or stable_fallback_id
                local_images = _match_local_product_images(name, sku, resolved_product_id)
                if local_images:
                    primary_image = local_images[0]
                    image_candidates = local_images + [u for u in image_candidates if u not in local_images]

                products.append(
                    {
                        "id": resolved_product_id,
                        "name": name,
                        "sku": sku,
                        "price": (row.get("Regular price") or "").strip(),
                        "sale_price": (row.get("Sale price") or "").strip(),
                        "in_stock": (row.get("In stock?") or "").strip(),
                        "stock": (row.get("Stock") or "").strip(),
                        "product_url": (row.get("External URL") or "").strip(),
                        "categories": categories[:4],
                        "metrics": metrics,
                        "fact_snippet": merged_text[:500],
                        "image_url": primary_image,
                        "image_candidates": image_candidates,
                        "category_image_candidates": _fallback_images_for_categories(categories),
                    }
                )

    return products


_LAST_PRODUCT_SELECTION_REPORT: dict[str, Any] = {}


def product_selection_report() -> dict[str, Any]:
    return dict(_LAST_PRODUCT_SELECTION_REPORT)


def _filter_selectable_products(products: list[dict]) -> list[dict]:
    global _LAST_PRODUCT_SELECTION_REPORT
    eligible, report = filter_evidence_eligible_products(products, DATA_DIR)
    _LAST_PRODUCT_SELECTION_REPORT = report
    return eligible


def load_products() -> list[dict]:
    sync_inventory_database(force=False)
    from_db = inventory_db.fetch_products(DATA_DIR)
    if from_db:
        return _filter_selectable_products(from_db)

    fallback = _load_products_from_csv()
    if fallback:
        inventory_db.upsert_products(DATA_DIR, fallback, source="wc_csv")
    return _filter_selectable_products(fallback)


def _pick_product(products: list[dict], history: dict) -> dict | None:
    if not products:
        return None

    windows = load_anti_repeat_windows()
    ledger = build_rotation_ledger(history, windows)
    candidates = []
    for product in products:
        candidate = dict(product)
        candidate["_rotation_product_id"] = str(product.get("id") or product.get("sku") or product.get("name") or "")
        if candidate["_rotation_product_id"]:
            candidates.append(candidate)
    last_used = ledger.get("last_used_at", {}).get("product_id", {})
    cooldown_days = int(ledger.get("cooldown_days", {}).get("product_id", 0) or 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, cooldown_days))

    def recently_used(product_id: str) -> bool:
        raw = str(last_used.get(product_id) or "").strip()
        if not raw:
            return False
        try:
            used_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if used_at.tzinfo is None:
                used_at = used_at.replace(tzinfo=timezone.utc)
            return used_at >= cutoff
        except ValueError:
            return False

    eligible = [
        candidate for candidate in candidates
        if not recently_used(candidate["_rotation_product_id"])
    ]
    pool = eligible or candidates
    if not pool:
        return None
    selected = random.choice(pool)
    decision = {
        "dimension": "product_id",
        "pool_size": len(candidates),
        "excluded_count": len(candidates) - len(eligible),
        "selected": selected["_rotation_product_id"],
        "selection_reason": "random_recent_exclusion" if eligible else "random_rotation_exhausted",
        "rotation_exhausted": bool(candidates and not eligible),
    }
    selected.pop("_rotation_product_id", None)
    selected["_rotation_decision"] = decision
    return selected


def _pick_series_product(products: list[dict], history: dict) -> dict | None:
    candidates = [dict(product) for product in products if product.get("id") or product.get("sku") or product.get("name")]
    if not candidates:
        return None
    ledger = build_rotation_ledger(history, load_anti_repeat_windows())
    last_used = ledger.get("last_used_at", {}).get("product_id", {})

    def rotation_key(product: dict) -> tuple[str, str]:
        product_id = str(product.get("id") or product.get("sku") or product.get("name") or "")
        return str(last_used.get(product_id) or ""), product_id.lower()

    selected = min(candidates, key=rotation_key)
    selected_id = str(selected.get("id") or selected.get("sku") or selected.get("name") or "")
    selected["_rotation_decision"] = {
        "dimension": "product_id",
        "pool_size": len(candidates),
        "selected": selected_id,
        "selection_reason": "least_recently_used_catalog",
        "last_used_at": str(last_used.get(selected_id) or ""),
    }
    return selected


def _apply_recurring_series_visual_plan(visual_plan: dict, series: dict) -> dict:
    if series.get("id") != "infenergy_intervention":
        return visual_plan
    preferred_format = str(series.get("preferred_format") or "cinematic_brand_poster")
    directives = {
        "cinematic_brand_poster": "Create one cinematic character-led brand poster with a single decisive intervention moment and a bold one-second visual hierarchy.",
        "product_micro_mission_comic": "Create one complete multi-panel comic page: avoidable energy mistake, Infenergy confrontation, product reveal, and product-enabled resolution with readable sequential action.",
        "educational_story_carousel": "Create a platform-native educational story carousel with distinct standalone frames for hook, mistake, consequence, intervention, product proof, and resolution.",
        "product_comic_strip_carousel": "Create a three-frame platform-native comic strip carousel with one sequential comic panel per 4:5 image: problem, intervention, and product-enabled resolution.",
        "product_story_page": "Create one complete 9:16 Story page with three readable sequential panels: problem, Infenergy intervention, and product-enabled resolution.",
    }
    directive = directives.get(preferred_format, directives["cinematic_brand_poster"])
    current_prompt = str(visual_plan.get("gemini_image_prompt") or "").strip()
    visual_plan["gemini_image_prompt"] = f"INFENERGY INTERVENTION SERIES: {directive} Lock Infenergy character canon and verified product appearance. {current_prompt}".strip()
    visual_plan["creative_route"] = preferred_format.upper()
    visual_plan["series_id"] = series["id"]
    visual_plan["series_story_pattern"] = series.get("story_pattern")
    visual_plan["originality_dimensions"] = list(series.get("originality_dimensions") or [])
    return visual_plan


def _apply_recurring_series_package(content: dict, platform_posts: dict, series: dict) -> None:
    if series.get("id") != "infenergy_intervention":
        return
    preferred_format = str(series.get("preferred_format") or "cinematic_brand_poster")
    content["recurring_series"] = dict(series)
    content["content_archetype"] = "character_led_edutainment"
    content["series_format"] = preferred_format
    for package in platform_posts.values():
        if isinstance(package, dict):
            package["series_id"] = series["id"]
            package["content_format"] = preferred_format
    instagram = platform_posts.get("instagram")
    if isinstance(instagram, dict):
        instagram["media_type"] = "CAROUSEL" if preferred_format in {"educational_story_carousel", "product_comic_strip_carousel"} else "STORY" if preferred_format == "product_story_page" else "STATIC"


def _apply_recurring_series_generation_contract(content: dict, visual_plan: dict, series: dict, product: dict | None) -> None:
    if series.get("id") != "infenergy_intervention" or series.get("preferred_format") not in {"educational_story_carousel", "product_comic_strip_carousel"}:
        return
    from build_monthly_content import _gemini_generation_plan

    headline = str(content.get("selected_hook") or content.get("on_image_headline") or "The preventable power mistake").strip()
    expansion = str(content.get("wp_excerpt") or content.get("educational_lesson") or "A small energy decision can protect an important routine.").strip()
    cta = str(content.get("selected_cta") or "Stay connected. Stay prepared.").strip()
    product_name = str((product or {}).get("name") or "the selected Infenergy product").strip()
    scene = str(visual_plan.get("visual_objective") or visual_plan.get("gemini_image_prompt") or "A recognizable real-life energy interruption").strip()
    product_image = str((product or {}).get("image_url") or content.get("product_image_url") or "").strip()
    thought = {
        "format": "carousel",
        "statement": headline,
        "expansion": expansion,
        "pillar": str(content.get("pillar") or "everyday_power"),
        "prompt": cta,
        "cta": cta,
        "mission_title": "INFENERGY INTERVENTION",
        "moral": expansion,
        "visual_motif": scene,
        "image_scene": scene,
        "visual_execution": "canonical Infenergy character-led educational story",
        "characters": ["Infenergy"],
        "reference_image_urls": [product_image] if product_image.startswith("http") else [],
        "slides": [
            {"role": "mistake", "headline": "The avoidable mistake", "supporting": headline},
            {"role": "consequence", "headline": "What it interrupts", "supporting": expansion},
            {"role": "intervention", "headline": "Infenergy steps in", "supporting": "Show the practical decision that changes the outcome."},
            {"role": "product_proof", "headline": product_name, "supporting": "Show only verified product appearance and supported facts."},
        ],
    }
    product_reference = {
        "name": product_name,
        "visual_direction": "Show the exact referenced product in physically credible use without redesigning it.",
    }
    content["gemini_generation"] = _gemini_generation_plan(thought, product_reference)


def _pick_product_by_id(products: list[dict], product_id: str) -> dict | None:
    requested = str(product_id or "").strip()
    if not requested:
        return None
    requested_low = requested.lower()
    for product in products:
        product_id_value = str(product.get("id", "")).strip()
        product_sku_value = str(product.get("sku", "")).strip()
        if product_id_value == requested:
            return product
        if product_id_value.lower() == requested_low:
            return product
        if product_sku_value and product_sku_value.lower() == requested_low:
            return product
    return None


def _normalize_funnel_stage_override(value: str) -> str:
    stage = str(value or "").strip().upper()
    return stage if stage in FUNNEL_STAGES else ""


def _preferred_pillars_for_stage(funnel_stage: str) -> list[str]:
    stage = str(funnel_stage or "").strip().upper()
    if stage == "CONVERSION":
        return ["readiness_assessment_lead_gen", "product_education", "small_business_continuity"]
    if stage == "TRUST":
        return ["trust_and_company_values", "product_education", "customer_problem_solving"]
    if stage == "DESIRE":
        return ["home_resilience", "travel_and_outdoor_preparedness", "customer_problem_solving"]
    if stage == "EDUCATION":
        return ["preparedness_education", "energy_literacy", "category_education"]
    return ["community_engagement", "brand_authority", "preparedness_education"]


def _product_category_text(product: dict | None) -> str:
    categories = (product or {}).get("categories", []) if isinstance(product, dict) else []
    if not isinstance(categories, list):
        categories = []
    return " ".join(str(c or "").strip().lower() for c in categories if str(c or "").strip())


def _topic_off_brand_penalty(topic: str) -> int:
    low = str(topic or "").strip().lower()
    penalty_terms = (
        "net metering",
        "utility bill",
        "rooftop solar",
        "solar evaluation",
        "solar install",
        "tax credit",
        "electricity bills",
    )
    return 100 if any(term in low for term in penalty_terms) else 0


def _product_topic_fit_score(product: dict | None, topic: str, pillar: str, funnel_stage: str) -> int:
    low_topic = str(topic or "").strip().lower()
    low_pillar = str(pillar or "").strip().lower()
    category_text = _product_category_text(product)
    product_name = str((product or {}).get("name", "")).strip().lower()
    score = 0
    accessory_product = any(
        token in product_name
        for token in (
            "fan", "light", "power bank", "charger", "jump starter", "cable",
            "filter", "purifier", "straw", "bottle", "lantern", "flashlight", "cooler",
        )
    )

    if any(token in category_text for token in ("travel power", "phone power bank", "phone charging", "portable laptop")):
        if any(token in low_topic for token in ("travel", "phone", "laptop", "off-grid", "camping", "rv", "portable")):
            score += 8
        if any(token in low_pillar for token in ("outdoor", "use_case", "portable")):
            score += 5

    if any(token in category_text for token in ("home backup", "emergency power")):
        if any(token in low_topic for token in ("outage", "backup", "emergency", "must-run", "24-hour", "priority")):
            score += 8
        if any(token in low_pillar for token in ("emergency", "readiness", "use_case")):
            score += 5

    if any(token in category_text for token in ("solar generator", "solar panel", "portable power", "outdoors & camping")):
        if any(token in low_topic for token in ("portable", "power station", "generator", "solar panel", "charge", "runtime", "battery", "camping", "rv")):
            score += 8
        if any(token in low_pillar for token in ("portable", "product_education", "outdoor", "use_case")):
            score += 4

    if product_name:
        name_tokens = [token for token in re.split(r"[^a-z0-9]+", product_name) if len(token) > 3]
        if any(token in low_topic for token in name_tokens[:3]):
            score += 5

    if accessory_product and any(token in low_topic for token in ("power station", "solar panel", "solar generator", "home backup", "must-run devices")):
        score -= 12

    stage = str(funnel_stage or "").strip().upper()
    if stage == "DESIRE" and any(token in low_topic for token in ("compare", "fit", "choose", "what can", "how to match")):
        score += 3
    if stage == "TRUST" and any(token in low_topic for token in ("spec", "compare", "verified", "what actually matters")):
        score += 3
    if stage == "CONVERSION" and any(token in low_topic for token in ("consultation", "assessment", "product match")):
        score += 3

    score -= _topic_off_brand_penalty(low_topic)
    return score


def _fallback_topic_for_product(product: dict | None, funnel_stage: str) -> tuple[str, str]:
    product_name = str((product or {}).get("name", "")).strip() or "this Infenergy portable power product"
    category_text = _product_category_text(product)
    product_name_low = product_name.lower()
    stage = str(funnel_stage or "").strip().upper()

    if "fan" in product_name_low:
        return "use_case_breakdowns", f"When {product_name} makes sense for camping, outages, and travel"
    if "jump starter" in product_name_low:
        return "use_case_breakdowns", f"How {product_name} fits into a vehicle and emergency backup kit"
    if "power bank" in product_name_low or "charger" in product_name_low:
        return "use_case_breakdowns", f"How {product_name} supports travel charging and everyday backup"
    if "filter straw" in product_name_low or "water filter" in product_name_low:
        return "use_case_breakdowns", f"Why {product_name} belongs in a preparedness and travel kit"

    if "travel power" in category_text or "phone power bank" in category_text or "phone charging" in category_text:
        if stage == "CONVERSION":
            return "use_case_breakdowns", f"How to choose {product_name} for travel, charging, and everyday backup"
        return "use_case_breakdowns", f"Where {product_name} fits in a travel and backup-power plan"
    if "home backup" in category_text or "emergency power" in category_text:
        if stage == "TRUST":
            return "emergency_preparedness", f"How to verify whether {product_name} fits your outage plan"
        return "emergency_preparedness", f"How {product_name} fits into a practical home-backup setup"
    if "solar generator" in category_text or "solar panel" in category_text or "portable power" in category_text:
        return "portable_power_readiness", f"How to match {product_name} to your real portable-power needs"
    return "product_education", f"What to compare before buying {product_name}"


def _pick_topic_for_product(
    queue: dict,
    history: dict,
    product: dict | None,
    funnel_stage: str,
    preferred_pillars: list[str] | None = None,
) -> tuple[str, str, str]:
    windows = load_anti_repeat_windows()
    queue_pillars = [str(p).strip() for p in queue.get("pillars", []) if str(p).strip()]
    preferred = [p for p in (preferred_pillars or []) if p in queue_pillars]
    remaining = [p for p in queue_pillars if p not in preferred]
    pillars = preferred + remaining
    scored: list[tuple[int, str, str, str]] = []
    for pillar in pillars:
        topics = queue.get("topics", {}).get(pillar, [])[:]
        for topic in topics:
            topic_hash = hashlib.md5(topic.encode()).hexdigest()
            score = _product_topic_fit_score(product, topic, pillar, funnel_stage)
            scored.append((score, pillar, topic, topic_hash))

    if scored:
        best_score = max(row[0] for row in scored)
        candidates = [
            {"pillar": pillar, "topic": topic, "topic_hash": topic_hash}
            for score, pillar, topic, topic_hash in scored
            if score == best_score
        ]
        selected, _ = select_least_recently_used(candidates, "topic", build_rotation_ledger(history, windows))
        if selected and best_score > 0:
            return selected["pillar"], selected["topic"], selected["topic_hash"]

    fallback_pillar, fallback_topic = _fallback_topic_for_product(product, funnel_stage)
    return fallback_pillar, fallback_topic, hashlib.md5(fallback_topic.encode()).hexdigest()


def _pick_topic(queue: dict, history: dict, preferred_pillars: list[str] | None = None) -> tuple[str, str, str]:
    windows = load_anti_repeat_windows()
    queue_pillars = [str(p).strip() for p in queue.get("pillars", []) if str(p).strip()]
    preferred = [p for p in (preferred_pillars or []) if p in queue_pillars]
    remaining = [p for p in queue_pillars if p not in preferred]
    pillars = preferred + remaining
    if not pillars:
        raise ValueError("topic queue has no valid pillars")
    candidates = [
        {"pillar": pillar, "topic": topic, "topic_hash": hashlib.md5(topic.encode()).hexdigest()}
        for pillar in pillars
        for topic in queue["topics"][pillar]
    ]
    selected, _ = select_least_recently_used(candidates, "topic", build_rotation_ledger(history, windows))
    if not selected:
        raise ValueError("topic queue has no valid topics")
    return selected["pillar"], selected["topic"], selected["topic_hash"]


def _recent_history_window(history: dict, limit: int = 14) -> list[dict]:
    posts = [p for p in (history.get("posts", []) if isinstance(history, dict) else []) if isinstance(p, dict)]
    return posts[-limit:]


def _weekly_mix_snapshot(history: dict, window: int = 14) -> dict[str, float | int]:
    """Business-first content mix snapshot: how product-first vs pillar-first recent posts have been."""
    recent = _recent_history_window(history, window)
    total = len(recent)
    if total == 0:
        return {"non_product_ratio": 0.0, "product_education_ratio": 0.0, "conversion_ratio": 0.0, "total": 0}
    non_product = sum(1 for p in recent if not str(p.get("product_name", "")).strip())
    product_education = sum(1 for p in recent if str(p.get("pillar", "")) == "product_education")
    conversion = sum(
        1
        for p in recent
        if str(p.get("pillar", "")) == "readiness_assessment_lead_gen"
        or (str(p.get("funnel_stage", "")).upper() == "CONVERSION" and str(p.get("product_name", "")).strip())
    )
    return {
        "non_product_ratio": non_product / total,
        "product_education_ratio": product_education / total,
        "conversion_ratio": conversion / total,
        "total": total,
    }


def _consecutive_product_posts(history: dict) -> int:
    posts = [p for p in (history.get("posts", []) if isinstance(history, dict) else []) if isinstance(p, dict)]
    count = 0
    for p in reversed(posts):
        if str(p.get("product_name", "")).strip():
            count += 1
        else:
            break
    return count


def _decide_content_bucket(history: dict) -> str:
    """Decide whether the next post should be non-product, product-education, or conversion.

    Implements the mission's default weekly mix (60-70% non-product, 20-30% product education,
    10-15% direct conversion) and the max-2-consecutive-product-posts guardrail, favoring
    whichever bucket is furthest below its target given recent history.
    """
    if _consecutive_product_posts(history) >= MAX_CONSECUTIVE_PRODUCT_POSTS:
        return "no_product"

    snapshot = _weekly_mix_snapshot(history)
    if snapshot["total"] < 3:
        # Not enough history yet: bias toward non-product content to seed a healthy mix.
        return "no_product"

    deficits = {
        "no_product": CONTENT_MIX_TARGETS["no_product_min"] - snapshot["non_product_ratio"],
        "product_education": CONTENT_MIX_TARGETS["product_education_min"] - snapshot["product_education_ratio"],
        "conversion": CONTENT_MIX_TARGETS["conversion_min"] - snapshot["conversion_ratio"],
    }
    bucket = max(deficits, key=deficits.get)
    if deficits[bucket] <= 0:
        # All targets already met or exceeded — default to the non-product majority.
        return "no_product"
    return bucket


def _pillars_for_bucket(bucket: str) -> list[str]:
    if bucket == "product_education":
        return [p for p, mode in PILLAR_PRODUCT_MODE.items() if mode == "required_product"]
    if bucket == "conversion":
        return [p for p, mode in PILLAR_PRODUCT_MODE.items() if mode in ("multiple_products", "optional_product")]
    return [p for p, mode in PILLAR_PRODUCT_MODE.items() if mode in ("no_product", "optional_product", "category_reference")]


def select_editorial_plan(
    queue: dict,
    history: dict,
    products: list[dict],
    funnel_stage: str,
    *,
    no_product: bool = False,
    pillar_override: str = "",
) -> dict:
    """Business-first editorial decision chain: pillar/topic and product-inclusion decision.

    Decides the content pillar and whether a specific product belongs in this post *before*
    picking a product, so products are one source of content rather than the organizing
    principle of every post.
    """
    requested_pillar = str(pillar_override or "").strip().lower()
    preferred_pillars = _preferred_pillars_for_stage(funnel_stage)
    if requested_pillar in PILLAR_PRODUCT_MODE:
        preferred_pillars = [requested_pillar] + [pillar for pillar in preferred_pillars if pillar != requested_pillar]
    bucket = "no_product" if no_product else _decide_content_bucket(history)
    bucket_pillars = _pillars_for_bucket(bucket)
    queue_pillars = [str(p).strip() for p in queue.get("pillars", []) if str(p).strip()]
    trimmed_pillars = [p for p in queue_pillars if p in bucket_pillars] or queue_pillars
    if not trimmed_pillars:
        raise ValueError("topic queue has no valid pillars")
    trimmed_queue = {
        "pillars": trimmed_pillars,
        "topics": {p: queue.get("topics", {}).get(p, []) for p in trimmed_pillars},
    }
    ranked_preferred = [p for p in preferred_pillars if p in trimmed_pillars]

    if bucket == "no_product":
        pillar, topic, topic_hash = _pick_topic(trimmed_queue, history, preferred_pillars=ranked_preferred)
        return {
            "pillar": pillar,
            "topic": topic,
            "topic_hash": topic_hash,
            "content_bucket": bucket,
            "product_mode": PILLAR_PRODUCT_MODE.get(pillar, "no_product"),
            "product": None,
            "want_product": False,
        }

    product = _pick_product(products, history)
    pillar, topic, topic_hash = _pick_topic_for_product(
        trimmed_queue, history, product, funnel_stage, preferred_pillars=ranked_preferred
    )
    return {
        "pillar": pillar,
        "topic": topic,
        "topic_hash": topic_hash,
        "content_bucket": bucket,
        "product_mode": PILLAR_PRODUCT_MODE.get(pillar, "optional_product"),
        "product": product,
        "want_product": True,
    }


def _pick_non_repeating_text(candidates: list[str], recent_hashes: set[str], fallback: str) -> str:
    cleaned = [c.strip() for c in candidates if isinstance(c, str) and c.strip()]
    random.shuffle(cleaned)
    for item in cleaned:
        if stable_text_hash(item) not in recent_hashes:
            return item
    if cleaned:
        return cleaned[0]
    return fallback


def _recent_unique_values(history: dict, key: str, limit: int = 8) -> list[str]:
    posts = history.get("posts", []) if isinstance(history, dict) else []
    out = []
    seen = set()
    for row in reversed(posts):
        if not isinstance(row, dict):
            continue
        value = str(row.get(key, "")).strip()
        if not value:
            continue
        low = value.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _one_line(text: str, limit: int = 220) -> str:
    out = re.sub(r"\s+", " ", (text or "").strip())
    if len(out) <= limit:
        return out
    return out[: limit - 3].rstrip() + "..."


def _enforce_pain_point_opening(text: str, pain_point: str, lead_chars: int = 220) -> str:
    body = str(text or "").strip()
    pain = str(pain_point or "").strip()
    if not pain:
        return body
    if not body:
        return pain
    lead = body[:lead_chars].lower()
    if pain.lower() in lead:
        return body
    return f"{pain}\n\n{body}"


def _sanitize_positioning_terms(text: str) -> str:
    body = str(text or "")
    if not body:
        return body
    for source, replacement in POSITIONING_REPLACEMENTS.items():
        pattern = re.compile(re.escape(source), flags=re.IGNORECASE)
        body = pattern.sub(replacement, body)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in body.splitlines()]
    body = "\n".join(lines)
    body = body.replace(" .", ".")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _contains_numeric_evidence(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    if re.search(r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b", t):
        return True
    if re.search(r"\b(top\s*\d+|\d+\s*(hours?|minutes?|wh|w|kw|mah|%)|24-hour|same-day)\b", t, flags=re.IGNORECASE):
        return True
    return False


def _fallback_cta_for_stage(stage: str) -> str:
    normalized = str(stage or "").strip().upper()
    if normalized == "CONVERSION":
        return "Shop now and build your backup-power setup."
    if normalized == "TRUST":
        return "Review verified specs and map your must-run devices."
    if normalized == "DESIRE":
        return "Compare product options and see what fits your daily loads."
    if normalized == "EDUCATION":
        return "Save this and compare your setup today."
    return "Comment with your top outage priority."


def _replace_legacy_cta_text(text: str) -> str:
    body = str(text or "")
    if not body:
        return body
    body = re.sub(
        r"review verified specs and see your custom runtime plan\.?",
        "Review verified specs and map your must-run devices.",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"see your custom runtime plan",
        "Map your must-run devices and build your outage-ready setup",
        body,
        flags=re.IGNORECASE,
    )
    return body


def _sanitize_legacy_cta_in_payload(value):
    if isinstance(value, str):
        return _replace_legacy_cta_text(value)
    if isinstance(value, list):
        return [_sanitize_legacy_cta_in_payload(item) for item in value]
    if isinstance(value, dict):
        return {k: _sanitize_legacy_cta_in_payload(v) for k, v in value.items()}
    return value


def _ensure_explicit_cta(text: str, stage: str) -> str:
    cta = _replace_legacy_cta_text(str(text or "")).strip()
    if cta and has_explicit_cta_keyword(cta):
        return cta
    fallback = _replace_legacy_cta_text(_fallback_cta_for_stage(stage))
    if not cta:
        return fallback
    return _replace_legacy_cta_text(f"{fallback} {cta}").strip()


def _segment_creative_constraints(audience_segment: str) -> dict:
    low = str(audience_segment or "").lower()
    if "rv" in low or "mobile" in low or "travel" in low:
        return {
            "segment_key": "mobile_autonomy",
            "narrative_requirement": "include one mobile scenario with a 24-hour load plan",
            "visual_requirement": "show compact, mobile-ready setup context",
            "platform_focus": "quick setup clarity and portability proof",
        }
    if "business" in low or "operator" in low:
        return {
            "segment_key": "business_uptime",
            "narrative_requirement": "include one continuity scenario with downtime-cost framing",
            "visual_requirement": "show continuity-first workstation or storefront context",
            "platform_focus": "uptime continuity and decision confidence",
        }
    return {
        "segment_key": "home_resilience",
        "narrative_requirement": "include one family-home outage scenario with top 3 device priorities",
        "visual_requirement": "show realistic home preparedness context",
        "platform_focus": "family safety continuity and practical control",
    }


def _numeric_proof_line(stage: str, talking_point: dict) -> str:
    anchor = str((talking_point or {}).get("proof_anchor", "")).strip()
    base = "Map your top 3 must-run devices and estimate 24-hour usage before selecting a system."
    if anchor:
        base = f"{anchor} {base}"
    normalized = str(stage or "").strip().upper()
    if normalized == "TRUST":
        return f"Proof checkpoint: {base}"
    return f"Quick fit check: {base}"


def _append_numeric_proof(text: str, stage: str, talking_point: dict, html: bool = False) -> str:
    body = str(text or "").strip()
    if _contains_numeric_evidence(body):
        return body
    line = _numeric_proof_line(stage, talking_point)
    if not body:
        return f"<p>{line}</p>" if html else line
    if html:
        return f"{body}\n<p>{line}</p>"
    return f"{body}\n\n{line}"


def _product_detail_summary(product: dict | None) -> str:
    if not isinstance(product, dict):
        return ""
    metrics = [str(x).strip() for x in (product.get("metrics", []) or []) if str(x).strip()]
    categories = [str(x).strip() for x in (product.get("categories", []) or []) if str(x).strip()]
    parts: list[str] = []
    if metrics:
        parts.append("Key specs: " + ", ".join(metrics[:3]))
    if categories:
        parts.append("Categories: " + ", ".join(categories[:2]))
    fact = _one_line(_strip_html(str(product.get("fact_snippet", "") or "")), 160)
    if fact:
        parts.append(fact)
    return " | ".join(parts[:3])


def _ensure_product_led_text(text: str, product: dict | None, *, html: bool = False, short_limit: int = 0) -> str:
    body = str(text or "").strip()
    if not isinstance(product, dict):
        return body
    product_name = str(product.get("name", "") or "").strip()
    if not product_name:
        return body

    detail = _product_detail_summary(product)
    mention_line = f"Featured product: {product_name}."
    if detail:
        mention_line = f"Featured product: {product_name}. {detail}"
    if short_limit > 0:
        mention_line = _one_line(mention_line, short_limit)

    if body and product_name.lower() in body.lower():
        return body

    if not body:
        if html:
            return f"<p>{mention_line}</p>"
        return mention_line
    if html:
        return f"<p>{mention_line}</p>\n{body}"
    return f"{mention_line}\n\n{body}"


def _enforce_product_led_copy(content: dict, product: dict | None) -> None:
    if not isinstance(content, dict) or not isinstance(product, dict):
        return
    product_name = str(product.get("name", "") or "").strip()
    if not product_name:
        return

    title = str(content.get("wp_title", "") or "").strip()
    if title and product_name.lower() not in title.lower():
        content["wp_title"] = _one_line(f"{title} | {product_name}", 65)
    excerpt = str(content.get("wp_excerpt", "") or "").strip()
    content["wp_excerpt"] = _ensure_product_led_text(excerpt, product, short_limit=160)
    content["wp_content"] = _ensure_product_led_text(str(content.get("wp_content", "") or ""), product, html=True)
    content["fb_caption"] = _ensure_product_led_text(str(content.get("fb_caption", "") or ""), product)
    content["ig_caption"] = _ensure_product_led_text(str(content.get("ig_caption", "") or ""), product)
    content["li_text"] = _ensure_product_led_text(str(content.get("li_text", "") or ""), product)


def _product_use_case_line(product: dict | None) -> str:
    if not isinstance(product, dict):
        return "Keep it ready in your emergency kit, vehicle, backpack, or travel bag."
    name_low = str(product.get("name", "") or "").lower()
    categories = " ".join(str(x or "") for x in (product.get("categories", []) or [])).lower()
    if any(token in f"{name_low} {categories}" for token in ("e-bike", "ebike", "electric bike", "electric bicycle")):
        return "Use it for routes that match the published range, rider fit, terrain, and charging requirements."
    if "jump starter" in name_low:
        return "Keep it in your vehicle, roadside kit, garage, or travel bag so backup power is there when the unexpected hits."
    if "power bank" in name_low or "charger" in name_low:
        return "Keep it in your home emergency kit, vehicle, backpack, or travel bag so you are not searching for power when you need it most."
    if "fan" in name_low:
        return "Keep it in your camping kit, vehicle, outage closet, or travel bag so airflow and backup charging are already covered."
    if "filter" in name_low or "purifier" in name_low or "straw" in name_low or "water" in categories:
        return "Keep it in your preparedness kit, vehicle, backpack, or travel gear so clean-water backup is ready before you need it."
    if "travel" in categories or "portable" in categories:
        return "Keep it in your emergency kit, vehicle, backpack, or travel bag so backup power is ready before you actually need it."
    return "Keep it ready in your emergency kit, vehicle, backpack, or travel bag."


def _product_comparison_line(product: dict | None) -> str:
    if not isinstance(product, dict):
        return "Compare the published specifications with the job this item must perform."
    name_low = str(product.get("name", "") or "").lower()
    categories = " ".join(str(item or "") for item in (product.get("categories", []) or [])).lower()
    if any(token in f"{name_low} {categories}" for token in ("e-bike", "ebike", "electric bike", "electric bicycle")):
        return "Compare the published range, motor, battery, rider fit, terrain, and charging requirements with your route."
    if "filter" in name_low or "purifier" in name_low or "straw" in name_low or "water" in categories:
        return "Check the published filtration stages, filter life, bottle capacity, and replacement-filter requirements."
    if "jump starter" in name_low:
        return "Compare the published starting current and supported engine sizes with your vehicle."
    if "solar" in name_low or "panel" in name_low:
        return "Compare the panel voltage, connector, and published wattage with the power station input."
    if "fan" in name_low:
        return "Compare the published runtime, airflow settings, light modes, and charging output with the campsite or room."
    if "power bank" in name_low or "charger" in name_low:
        return "Compare the battery capacity, ports, and charging output with your phone, tablet, or other listed device."
    return "List each device, its plug, its watts, and the hours it must run, then compare the total with the published output and capacity."


def _product_comparison_cta(product: dict | None) -> str:
    name = str((product or {}).get("name", "") or "this product").strip()
    name_low = name.lower()
    categories = " ".join(str(item or "") for item in ((product or {}).get("categories", []) or [])).lower()
    if any(token in f"{name_low} {categories}" for token in ("e-bike", "ebike", "electric bike", "electric bicycle")):
        return f"Compare {name} range, fit, and riding specifications with your route."
    if "filter" in name_low or "purifier" in name_low or "straw" in name_low or "water" in categories:
        return f"Check {name} filtration specifications and replacement-filter requirements."
    if "jump starter" in name_low:
        return f"Compare {name} starting specifications with your vehicle."
    if "solar" in name_low or "panel" in name_low:
        return f"Compare {name} voltage, connector, and wattage with your power station."
    if "fan" in name_low:
        return f"Check {name} runtime, airflow settings, and charging output."
    if "power bank" in name_low or "charger" in name_low:
        return f"Compare {name} ports and charging output with your devices."
    return f"Compare {name} output and capacity with your device list."


def _product_copy_profile(product: dict | None) -> dict[str, str]:
    name = str((product or {}).get("name", "") or "").strip()
    name_low = name.lower()
    categories = " ".join(str(x or "") for x in ((product or {}).get("categories", []) or [])).lower()
    fact_low = _strip_html(str((product or {}).get("fact_snippet", "") or "")).lower()
    metrics = [str(x).strip() for x in ((product or {}).get("metrics", []) or []) if str(x).strip()]
    primary_metric = metrics[0] if metrics else "published product specs"
    power_station_evidence = any(token in name_low for token in ("power station", "power system", "generator", "inverter")) or any(
        token in categories for token in ("power station", "generator")
    ) or any(token in fact_low for token in ("home backup powerhouse", "solar generator", "pure sine wave inverter"))
    integrated_charger_evidence = any(
        token in fact_low
        for token in ("built-in cable", "built in cable", "wireless charger", "wall charger", "5-in-1", "all-in-one charging")
    )

    profile = {
        "role": "preparedness product",
        "benefit": "supports a more reliable preparedness setup",
        "after_state": "You can move through the day with the right backup already matched to the job instead of improvising after power becomes a problem.",
        "transformation": "The change is practical: your preparedness gear has a defined role, a known place, and a reason to be there.",
        "why_it_matters": "That gives you a plan you can act on instead of another product you hope will be useful.",
        "fit_line": f"helps buyers compare real needs against {primary_metric}",
        "proof_intro": "Every recommendation here is checked against the published product specs",
        "offer_line": f"The {name or 'product'} is built for buyers who want a practical preparedness tool matched to a real use case.",
        "category_pain": "Generic recommendations often leave buyers with the wrong tool for the job.",
    }

    if any(token in f"{name_low} {categories}" for token in ("e-bike", "ebike", "electric bike", "electric bicycle")):
        profile.update({
            "role": "electric bicycle",
            "benefit": "adds powered mobility for commuting and longer rides",
            "after_state": "You can plan daily travel around a route, rider fit, and charging schedule that match the bicycle's published specifications.",
            "transformation": "The commute becomes a defined mobility decision based on range, terrain, fit, and charging access instead of a generic distance claim.",
            "why_it_matters": "Published range and speed only become useful when they are compared with the rider, route, load, terrain, and charging plan.",
            "fit_line": f"helps riders compare their route with published details such as {primary_metric}",
            "proof_intro": "Checked against the published motor, battery, range, speed, and riding specifications",
            "offer_line": f"The {name or 'electric bicycle'} is for riders comparing powered mobility with the demands of a real route.",
            "category_pain": "An electric bicycle is a poor fit when range, terrain, rider fit, and charging needs are not evaluated together.",
        })
    elif "solar" in name_low or "panel" in name_low or (("solar" in categories or "panel" in categories) and not power_station_evidence):
        profile.update({
            "role": "foldable solar charging panel",
            "benefit": "adds off-grid charging support for compatible power stations and devices",
            "after_state": "You can plan charging around available sunlight instead of treating the nearest wall outlet as the only way to restore compatible gear.",
            "transformation": "That turns portable solar from a vague backup idea into a repeatable charging option for travel, outages, and off-grid use.",
            "why_it_matters": "Your power plan can keep working beyond the energy already stored in a battery when the published wattage and compatibility fit the setup.",
            "fit_line": f"helps recharge compatible gear when sunlight is available, using specs like {primary_metric}",
            "proof_intro": "Checked against the published wattage, output, and charging compatibility details",
            "offer_line": f"The {name or 'product'} is for buyers who need portable solar charging support, not vague emergency promises.",
            "category_pain": "Many buyers assume any solar panel will recharge their gear the way they need, then discover the output or compatibility is wrong.",
        })
    elif integrated_charger_evidence:
        profile.update({
            "role": "all-in-one daily charging hub",
            "benefit": "combines portable charging and built-in connection options in one device",
            "after_state": "You can leave the spare cable bundle and separate wall adapter behind because the charging tools you use most are already built into one compact device.",
            "transformation": "Travel, commuting, and daily carry become simpler when one charging hub replaces several loose accessories in your bag.",
            "why_it_matters": "You spend less time untangling, searching, and swapping charging gear and more time using the devices that keep your day moving.",
            "fit_line": f"helps reduce charging clutter while providing portable backup using details like {primary_metric}",
            "proof_intro": "Checked against the published battery, built-in cable, wireless charging, and wall-charging details",
            "offer_line": f"The {name or 'product'} is for people who want fewer charging accessories without giving up daily backup power.",
            "category_pain": "Daily charging gets frustrating when every device requires another cable, adapter, or loose accessory to remember.",
        })
    elif "fan" not in name_low and (any(token in name_low for token in ("power bank", "powerbank", "charger")) or any(token in categories for token in ("power bank", "power banks", "phone power bank", "phone power banks", "charger", "chargers"))):
        profile.update({
            "role": "portable charging backup",
            "benefit": "keeps phones, tablets, and small daily devices charged when wall power is not available",
            "after_state": "You can leave home knowing compatible daily devices have a backup plan in your bag instead of searching for an outlet when the battery warning appears.",
            "transformation": "Daily travel, commuting, and mobile work become easier when charging backup is close enough to use and matched to what you actually carry.",
            "why_it_matters": "You stay connected to the calls, directions, messages, and work that depend on those devices without carrying the wrong kind of backup.",
            "fit_line": f"supports everyday charging backup using details like {primary_metric}",
            "proof_intro": "Checked against the published battery, port, and charging specs",
            "offer_line": f"The {name or 'product'} is for buyers who need daily carry charging backup that is easy to keep close.",
            "category_pain": "A cheap power bank is useless when capacity, ports, or charging speed do not match what you actually carry.",
        })
    elif "jump starter" in name_low:
        profile.update({
            "role": "vehicle emergency jump starter",
            "benefit": "supports roadside readiness and emergency vehicle starts",
            "after_state": "You can face a dead vehicle battery with a tool already in the car instead of beginning the problem by searching for another driver.",
            "transformation": "Roadside readiness becomes something you carry with you rather than something you hope arrives after the battery fails.",
            "why_it_matters": "That can turn an unexpected stop into a problem you are equipped to address with the published starting capability.",
            "fit_line": f"adds vehicle-ready emergency support using specs like {primary_metric}",
            "proof_intro": "Checked against the published starting, battery, and output specs",
            "offer_line": f"The {name or 'product'} is for drivers who want roadside readiness without guessing what will work when the battery dies.",
            "category_pain": "Roadside emergencies go from stressful to expensive fast when the tool in your trunk cannot actually handle the job.",
        })
    elif "filter" in name_low or "straw" in name_low or "water" in categories:
        profile.update({
            "role": "portable water filtration backup",
            "benefit": "adds clean-water support to emergency and travel kits",
            "after_state": "You can carry a defined water-backup option instead of discovering too late that your preparedness kit only covered power and food.",
            "transformation": "Travel and emergency planning become more complete when clean-water support is portable and ready to deploy.",
            "why_it_matters": "Water is a basic operating need, so the backup plan must be practical to carry and use where the need occurs.",
            "fit_line": f"supports water-prep planning using details like {primary_metric}",
            "proof_intro": "Checked against the published filtration and capacity specs",
            "offer_line": f"The {name or 'product'} is for buyers who want a water-backup plan that is practical to carry and easy to deploy.",
            "category_pain": "Preparedness kits fail fast when water backup is treated like an afterthought instead of a real requirement.",
        })
    elif "fan" in name_low:
        profile.update({
            "role": "portable airflow and charging support",
            "benefit": "adds comfort and small-device support during outages, camping, and travel",
            "after_state": "You can keep air moving and compatible small devices supported when heat, travel, or an outage removes the comfort of a powered room.",
            "transformation": "Your camping or outage setup becomes more livable because airflow and charging support are already part of the kit.",
            "why_it_matters": "Comfort and communication become harder to manage when there is no airflow and no charging plan.",
            "fit_line": f"supports comfort and charging backup using specs like {primary_metric}",
            "proof_intro": "Checked against the published runtime and charging specs",
            "offer_line": f"The {name or 'product'} is for buyers who want portable airflow support that earns space in a real preparedness kit.",
            "category_pain": "Comfort becomes a real problem fast when outages or travel leave you with no airflow and no plan.",
        })
    elif power_station_evidence or "battery" in name_low or any(token in categories for token in ("battery", "emergency power", "portable power")):
        profile.update({
            "role": "backup power station",
            "benefit": "powers compatible must-run devices during outages and off-grid use",
            "after_state": "You can keep compatible priority devices available away from wall power instead of organizing the day around the next outlet.",
            "transformation": "Match each device's watts and required runtime to the station's published output and capacity before travel or an outage.",
            "why_it_matters": "List the devices that must run, record their watts, and compare the total with the station's published output and capacity.",
            "fit_line": f"helps buyers match loads and runtime needs using specs like {primary_metric}",
            "proof_intro": "Checked against the published output, battery, runtime, and charging specs",
            "offer_line": f"The {name or 'product'} is for buyers who want real backup power capacity matched to real outage needs.",
            "category_pain": "Backup power decisions get expensive fast when output, runtime, and charging limits are not matched to the devices you actually need to run.",
        })

    return profile


def _sales_cta_line(product: dict | None, first_step: str, platform: str) -> str:
    name = str((product or {}).get("name", "") or "this product").strip()
    base = str(first_step or "Get yours today and stay powered when the unexpected happens.").strip()
    if platform == "instagram":
        return f"Tap to get {name} and stay powered when the unexpected happens."
    if platform == "linkedin":
        return f"Order {name} today and add reliable backup power to your preparedness plan."
    if not base.lower().startswith(("get", "shop", "order", "build", "tap")):
        return f"Get {name} today and stay powered when the unexpected happens."
    return base


def _enforce_product_sales_platform_copy(content: dict, product: dict | None, talking_point: dict) -> None:
    if not isinstance(content, dict) or not isinstance(product, dict):
        return
    product_name = str(product.get("name", "") or "").strip()
    if not product_name:
        return
    existing_captions = {
        key: str(content.get(key, "") or "").strip()
        for key in ("fb_caption", "ig_caption", "li_text")
    }
    metrics = [str(x).strip() for x in (product.get("metrics", []) or []) if str(x).strip()]
    feature_items = _sales_feature_bullets(product, limit=5)
    bullets = [f"- {item}" for item in feature_items]
    bullet_block = "\n".join(bullets)
    short_feature_block = "\n".join([f"- {item}" for item in feature_items[:3]])
    use_case_line = _product_use_case_line(product)
    pain_point = str((talking_point or {}).get("pain_point", "") or "Dead batteries and limited power become a real problem when an outlet is not nearby.").strip()
    first_step = str((talking_point or {}).get("first_step", "") or "Get yours today and stay powered when the unexpected happens.").strip()
    proof_line = _one_line(_product_detail_summary(product), 220)
    metric_line = ", ".join(metrics[:3]) if metrics else proof_line
    profile = _product_copy_profile(product)
    if not bullet_block:
        bullet_block = "- Portable backup power built for real outages and daily carry\n- Practical, ready-to-use design for emergency planning\n- Built for home, vehicle, and travel preparedness"
    if not short_feature_block:
        short_feature_block = "- Portable backup power\n- Real preparedness use case\n- Designed for home, vehicle, and travel"
    urgency_line = "The gap between \"we'll figure it out\" and being actually ready is smaller than most people think."
    offer_line = profile["offer_line"]
    action_line = f"The {product_name} is one practical way to close that gap."

    content["fb_caption"] = (
        f"{pain_point or profile['category_pain']}\n\n"
        f"{offer_line}\n\n"
        f"Why people buy it:\n{bullet_block}\n\n"
        f"{profile['proof_intro']}: {proof_line}.\n\n"
        f"{use_case_line}\n\n"
        f"{urgency_line}\n\n"
        f"{action_line}\n\n"
        f"{_sales_cta_line(product, first_step, 'facebook')}\n"
        f"#PortablePower #BackupPower #EmergencyPreparedness #PowerOutage #StayPowered #PreparedNotPanicked"
    )
    content["ig_caption"] = (
        f"Match the specs to the job before you buy.\n\n"
        f"{offer_line}\n\n"
        f"Built for real use:\n{short_feature_block}\n\n"
        f"{use_case_line}\n\n"
        f"{profile['proof_intro']}: {metric_line}.\n\n"
        f"{_sales_cta_line(product, first_step, 'instagram')}\n"
        f"#PortablePower #BackupPower #EmergencyPreparedness #PowerOutage #StayPowered #StormReady #TravelPower #PreparedNotPanicked"
    )
    content["li_text"] = (
        f"Most bad product decisions happen because buyers never translate specs into the actual job they need done.\n\n"
        f"{pain_point or profile['category_pain']}\n\n"
        f"{offer_line}\n\n"
        f"Key features include:\n{bullet_block}\n\n"
        f"{profile['proof_intro']}: {proof_line}.\n\n"
        f"{use_case_line}\n\n"
        f"For teams and households, this is about response speed, continuity, and confidence under pressure.\n\n"
        f"{_sales_cta_line(product, first_step, 'linkedin')}\n"
        f"#EmergencyPreparedness #PortablePower #BackupPower #BusinessContinuity #Resilience"
    )
    product_name_lower = product_name.lower()
    for key, existing in existing_captions.items():
        if len(existing) >= 80 and product_name_lower in existing.lower():
            content[key] = existing


def _enforce_numeric_proof_requirements(content: dict, funnel_stage: str, talking_point: dict) -> None:
    stage = str(funnel_stage or "").strip().upper()
    if stage not in {"DESIRE", "TRUST"}:
        return
    content["wp_content"] = _append_numeric_proof(str(content.get("wp_content", "")), stage, talking_point, html=True)
    for key in ("fb_caption", "ig_caption", "li_text"):
        content[key] = _append_numeric_proof(str(content.get(key, "")), stage, talking_point, html=False)


def _hook_family(hook: str) -> str:
    low = str(hook or "").strip().lower()
    if "?" in low or low.startswith("what") or low.startswith("how") or low.startswith("why"):
        return "question"
    if "myth" in low:
        return "myth"
    if "mistake" in low:
        return "common_mistake"
    if " vs " in low or "compare" in low:
        return "comparison"
    if "checklist" in low or low.startswith("before you buy"):
        return "checklist"
    if low.startswith("if ") or "imagine" in low:
        return "scenario"
    if "fit" in low and "plan" in low:
        return "product_use_case"
    return "curiosity"


def _enforce_hook_diversity(selected_hook: str, phase2_stack: dict, recent_hooks: list[str]) -> tuple[str, str]:
    counts: dict[str, int] = {}
    for raw in (recent_hooks or [])[:8]:
        fam = _hook_family(str(raw or ""))
        counts[fam] = counts.get(fam, 0) + 1
    if not counts:
        return selected_hook, ""

    dominant_family, dominant_count = max(counts.items(), key=lambda kv: kv[1])
    selected_family = _hook_family(selected_hook)
    if dominant_count < 2 or selected_family != dominant_family:
        return selected_hook, ""

    candidates: list[str] = []
    hook_test = phase2_stack.get("hook_stress_test", {}) if isinstance(phase2_stack, dict) else {}
    if isinstance(hook_test, dict):
        candidates.extend([str(x).strip() for x in hook_test.get("candidate_hooks", []) if str(x).strip()])
    ideation = phase2_stack.get("ideation_divergence", {}) if isinstance(phase2_stack, dict) else {}
    if isinstance(ideation, dict):
        candidates.append(str(ideation.get("winner_hook", "")).strip())
        for row in ideation.get("concepts", []) or []:
            if isinstance(row, dict):
                candidates.append(str(row.get("hook_candidate", "")).strip())
    candidates.append(str(selected_hook or "").strip())

    seen: set[str] = set()
    unique = []
    for c in candidates:
        key = c.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(c)

    for candidate in unique:
        if _hook_family(candidate) != dominant_family:
            note = f"Switched hook family from {dominant_family} to {_hook_family(candidate)} for novelty rotation."
            return candidate, note
    return selected_hook, ""


def _apply_segment_constraints_to_stacks(phase2_stack: dict, phase4_stack: dict, audience_segment: str) -> dict:
    constraints = _segment_creative_constraints(audience_segment)
    narrative = phase2_stack.get("narrative_architect", {}) if isinstance(phase2_stack, dict) else {}
    if isinstance(narrative, dict):
        must_include = narrative.get("must_include", [])
        if not isinstance(must_include, list):
            must_include = []
        requirement = constraints.get("narrative_requirement", "")
        if requirement and requirement not in must_include:
            must_include.append(requirement)
        narrative["must_include"] = must_include
        phase2_stack["narrative_architect"] = narrative

    visual = phase4_stack.get("visual_strategy", {}) if isinstance(phase4_stack, dict) else {}
    if isinstance(visual, dict):
        adjustments = visual.get("composition_adjustments", [])
        if not isinstance(adjustments, list):
            adjustments = []
        visual_requirement = constraints.get("visual_requirement", "")
        if visual_requirement and visual_requirement not in adjustments:
            adjustments.append(visual_requirement)
        visual["composition_adjustments"] = adjustments
        phase4_stack["visual_strategy"] = visual

    return constraints


def _enforce_conversion_caption(text: str, talking_point: dict, platform: str = "") -> str:
    body = str(text or "").strip()
    pain_point = str((talking_point or {}).get("pain_point", "")).strip()
    proof_anchor = str((talking_point or {}).get("proof_anchor", "")).strip()
    first_step = str((talking_point or {}).get("first_step", "")).strip()
    platform_name = str(platform or "").strip().lower()

    body = _sanitize_positioning_terms(body)
    copy_source = str((talking_point or {}).get("copy_generation_source", ""))
    if copy_source in {"gemini", "deterministic_fallback"} and len(body) >= 80:
        return body

    body = _enforce_pain_point_opening(body, pain_point)

    low = body.lower()
    if proof_anchor and proof_anchor.lower() not in low:
        body = f"{body}\n\n{proof_anchor}"

    if not _contains_numeric_evidence(body):
        body = f"{body}\n\nStart with your top 3 must-run devices and expected hours of use."

    if first_step and first_step.lower() not in body.lower():
        if platform_name == "facebook":
            body = f"{body}\n\nNext step: {first_step}\nWhat device is non-negotiable for you in an outage?"
        elif platform_name == "instagram":
            body = f"{body}\n\nNext step: {first_step}"
        elif platform_name == "linkedin":
            body = f"{body}\n\nOperational next step: {first_step}"
        else:
            body = f"{body}\n\nNext step: {first_step}"

    return body.strip()


def _pick_pain_point_variant(topic: str, funnel_stage: str, product: dict | None) -> str:
    stage = str(funnel_stage or "").strip().upper()
    variants = DEFAULT_STAGE_PAIN_POINT_VARIANTS.get(stage, [])
    if not variants:
        return DEFAULT_STAGE_PAIN_POINTS.get(stage, "Most people choose backup power without mapping real usage first.")
    seed = f"{stage}|{topic}|{str((product or {}).get('name', ''))}"
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(variants)
    return variants[idx]


def _build_talking_point(topic: str, funnel_stage: str, product: dict | None) -> dict:
    stage = str(funnel_stage or "").strip().upper()
    pain_point = _pick_pain_point_variant(topic, stage, product)

    product_name = str((product or {}).get("name", "")).strip() or "this Infenergy preparedness solution"
    metrics = (product or {}).get("metrics", []) if isinstance(product, dict) else []
    m1 = metrics[0] if len(metrics) > 0 else "published output specs"
    m2 = metrics[1] if len(metrics) > 1 else "runtime and charging details"

    if len(metrics) > 1:
        proof_anchor = f"{m1} and {m2} are the specs worth comparing before you buy."
    elif metrics:
        proof_anchor = f"{m1} is worth comparing before you buy."
    else:
        proof_anchor = "Compare the published specs on the product page against your real needs before you buy."
    angle = f"{topic} through Infenergy's preparedness-first buying framework, not hype."

    first_step = "Comment with your top 3 must-run devices for a practical match today."
    if stage == "EDUCATION":
        first_step = "Save this checklist and compare your current setup tonight."
    elif stage == "DESIRE":
        first_step = f"Compare {product_name} against your top 3 daily loads in one quick review."
    elif stage == "TRUST":
        first_step = "Review your device list and verify specs before committing."
    elif stage == "CONVERSION":
        first_step = "Book your free readiness assessment and get a tailored recommendation in under 15 minutes."

    return {
        "pain_point": pain_point,
        "proof_anchor": proof_anchor,
        "angle": angle,
        "first_step": first_step,
    }


def _build_talking_point_no_product(topic: str, funnel_stage: str, pillar: str) -> dict:
    """Talking point for business-first content with no product attached.

    Keeps the same shape as _build_talking_point() but never frames the post around
    "compare specs before you buy" language, since there is no product to compare.
    """
    stage = str(funnel_stage or "").strip().upper()
    pain_point = DEFAULT_STAGE_PAIN_POINT_VARIANTS.get(stage, DEFAULT_STAGE_PAIN_POINT_VARIANTS["EDUCATION"])[0]
    angle = f"{topic}, viewed through Infenergy's preparedness-first perspective."

    first_step = "Comment below with your take, we read every reply."
    if pillar == "readiness_assessment_lead_gen":
        first_step = "Book your free readiness assessment and get a tailored recommendation in under 15 minutes."
    elif pillar == "community_engagement":
        first_step = "Tell us in the comments, we want to hear your experience."
    elif pillar in ("preparedness_education", "energy_literacy", "category_education"):
        first_step = "Save this so it's ready the next time you need it."
    elif pillar in ("home_resilience", "travel_and_outdoor_preparedness", "caregiver_preparedness"):
        first_step = "Share this with someone who is still figuring out their own plan."
    elif pillar == "small_business_continuity":
        first_step = "Walk through this checklist with your team this week."
    elif pillar in ("brand_authority", "trust_and_company_values"):
        first_step = "Follow along for more of how we think about preparedness."

    proof_anchor = "The details that actually matter here are specific, not headline-level claims."

    return {
        "pain_point": pain_point,
        "proof_anchor": proof_anchor,
        "angle": angle,
        "first_step": first_step,
    }


def _build_product_intelligence_handoff(
    *,
    product: dict | None,
    topic: str,
    funnel_stage: str,
    selected_hook: str,
    selected_cta: str,
    audience_segment: str,
    talking_point: dict,
) -> dict:
    payload = product_intelligence.run(
        DATA_DIR,
        product=product or {
            "id": "GENERAL-PREPAREDNESS",
            "name": "Infenergy preparedness solution",
            "categories": ["Preparedness"],
        },
        topic=topic,
        funnel_stage=funnel_stage,
        audience_segment=audience_segment,
        selected_hook=selected_hook,
        selected_cta=selected_cta,
    )
    brief = payload.get("product_brief", {})
    if isinstance(brief, dict):
        talking_point["product_brief"] = brief
        talking_point["pain_point"] = str(brief.get("primary_pain_point", "") or talking_point.get("pain_point", ""))
        # NOTE: brief["proof_rule"] (e.g. "Use only published wattage...") is an
        # internal validation instruction for copywriters/agents, never customer
        # copy. Derive a customer-safe grounding line from verified facts instead.
        verified_facts = [str(v).strip() for v in brief.get("verified_facts", []) if str(v).strip()]
        if verified_facts:
            talking_point["proof_anchor"] = f"Published details: {'; '.join(verified_facts[:2])}."
        talking_point["first_step"] = str(brief.get("recommended_cta", "") or talking_point.get("first_step", ""))
    return payload


def _build_business_profile(products: list[dict]) -> dict:
    category_counts: dict[str, int] = {}
    keyword_counts: dict[str, int] = {
        "portable power": 0,
        "power station": 0,
        "portable generator": 0,
        "solar panel": 0,
        "battery": 0,
        "emergency": 0,
        "camping": 0,
        "rv": 0,
    }

    for product in products:
        if not isinstance(product, dict):
            continue
        categories = product.get("categories", [])
        if isinstance(categories, list):
            for raw in categories:
                c = str(raw).strip().lower()
                if not c:
                    continue
                category_counts[c] = category_counts.get(c, 0) + 1

        text = " ".join(
            [
                str(product.get("name", "")),
                str(product.get("fact_snippet", "")),
                " ".join([str(x) for x in product.get("categories", []) if isinstance(product.get("categories", []), list)]),
            ]
        ).lower()
        if "portable" in text and "power" in text:
            keyword_counts["portable power"] += 1
        if "power station" in text:
            keyword_counts["power station"] += 1
        if "portable generator" in text:
            keyword_counts["portable generator"] += 1
        if "solar panel" in text:
            keyword_counts["solar panel"] += 1
        if "battery" in text:
            keyword_counts["battery"] += 1
        if "emergency" in text or "outage" in text:
            keyword_counts["emergency"] += 1
        if "camping" in text:
            keyword_counts["camping"] += 1
        if "rv" in text:
            keyword_counts["rv"] += 1

    top_categories = sorted(category_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    offers = [name for name, _ in top_categories]
    return {
        "focus_statement": INFENERGY_BUSINESS_GOALS["focus_statement"],
        "top_categories": top_categories,
        "keyword_signals": keyword_counts,
        "offers": offers,
    }


LOGICAL_EMOTIONAL_PRINCIPLES = {
    "contrapositive": {
        "name": "Law of Contrapositive",
        "logic": "(P -> Q) is equivalent to (not Q -> not P)",
        "visual_concept": "Show the vulnerable moment that exposes the absence of a workable plan.",
        "caption_strategy": "Start with the desired result, then show why waiting until that result is impossible reveals the missing preparation.",
        "on_image_headline": "DON'T WAIT FOR FAILURE",
        "headline_variants": [
            "DON'T WAIT FOR FAILURE",
            "THE MOMENT IT MATTERS",
            "WHAT SILENCE COSTS YOU",
            "NO PLAN IS A PLAN TO FAIL",
            "WAITING IS NOT A STRATEGY",
            "THE COST OF STAYING UNREADY",
            "POWER OUT. PLAN STILL IN?",
            "DON'T FIND OUT THE HARD WAY",
        ],
    },
    "disjunctive_syllogism": {
        "name": "Law of Disjunctive Syllogism",
        "logic": "P or Q; not P; therefore Q",
        "visual_concept": "Contrast the cluttered, reactive method with a measured and prepared method.",
        "caption_strategy": "Present two paths, disqualify the wasteful or unreliable path with evidence, then make the product-fit path the rational conclusion.",
        "on_image_headline": "REACTIVE OR READY?",
        "headline_variants": [
            "REACTIVE OR READY?",
            "GUESSWORK OR GEAR THAT WORKS",
            "HOPE IS NOT A STRATEGY",
            "PICK YOUR SIDE OF THE OUTAGE",
            "SCRAMBLE OR STAY IN CONTROL",
            "STOP GUESSING. START KNOWING",
            "READY WINS. EVERY TIME",
            "CHOOSE PROOF OVER PANIC",
        ],
    },
    "double_implication": {
        "name": "Law of Double Implication",
        "logic": "P if and only if Q",
        "visual_concept": "Present a high-status new-standard scene where practical preparation and control visibly belong together.",
        "caption_strategy": "Define real preparedness through verified capability and fit, without claiming one product is the only possible solution.",
        "on_image_headline": "THE NEW READY STANDARD",
        "headline_variants": [
            "THE NEW READY STANDARD",
            "WHAT 24 HOURS NEEDS",
            "BUILT FOR WHEN IT MATTERS",
            "ZERO COMPROMISE POWER",
            "REAL READY LOOKS LIKE THIS",
            "THIS IS WHAT READY MEANS",
            "MADE FOR THE MOMENT OF TRUTH",
            "A STANDARD, NOT A STRETCH GOAL",
        ],
    },
    "symmetrical_equivalence": {
        "name": "Principle of Symmetrical Equivalence",
        "logic": "p + q = r is equivalent to q + p = r",
        "visual_concept": "Show that the setting can change while the emotional result of control stays consistent.",
        "caption_strategy": "Move across credible use settings and hold the same proof-backed outcome constant.",
        "on_image_headline": "CONTROL TRAVELS WITH YOU",
        "headline_variants": [
            "CONTROL TRAVELS WITH YOU",
            "YOUR GRID, YOUR RULES",
            "POWER THAT EARNS TRUST",
            "SAME CONFIDENCE, ANY SETTING",
            "POWER FOLLOWS, ANYWHERE YOU GO",
            "ONE STANDARD. EVERY LOCATION",
            "TAKE YOUR CONFIDENCE WITH YOU",
            "CONSISTENT POWER, ANY PLACE",
        ],
    },
    "implication_of_result": {
        "name": "Principle of Implication of the Result",
        "logic": "R -> (p and q)",
        "visual_concept": "Show the successful human result, then trace it back to the verified features and preparation that enabled it.",
        "caption_strategy": "Lead with the save-the-day result and explain which real product facts made that outcome plausible.",
        "on_image_headline": "READY BEFORE IT MATTERS",
        "headline_variants": [
            "READY BEFORE IT MATTERS",
            "THE RESULT SPEAKS FIRST",
            "EARNED, NOT LUCKY",
            "PROOF BEFORE PROMISES",
            "THE OUTCOME PROVES THE PLAN",
            "RESULTS YOU CAN TRACE BACK",
            "NOT LUCK. PREPARATION",
            "THE PROOF IS IN THE OUTCOME",
        ],
    },
}


AUDIENCE_ARCHETYPES = {
    "mobile_professional": {
        "name": "The Mobile Professional / Digital Nomad",
        "emotion": "calm productivity and control away from a fixed outlet",
        "environment": "urban travel, a clean shared workspace, or a compact everyday-carry setup",
        "environments": [
            "a modern co-working lounge at dusk, floor-to-ceiling windows showing a city skyline, warm task lighting on a shared desk",
            "an airport lounge corner, soft ambient light, a window overlooking the tarmac at golden hour",
            "a boutique hotel work nook, warm brass lamp light, a laptop and notebook on a marble side table",
            "a rooftop coffee bar at sunrise, city rooftops in soft focus, a small bistro table catching first light",
            "a train carriage window seat at dusk, blurred city lights streaking past, warm interior cabin glow",
        ],
    },
    "preparedness_buyer": {
        "name": "The Preparedness / Contingency Buyer",
        "emotion": "relief and family confidence before a disruption",
        "environment": "a credible home outage, storm-readiness, vehicle-kit, or contingency setting",
        "environments": [
            "a living room at night during a storm, rain streaking the window, a single warm lamp glowing against the dark",
            "a garage workshop with a single overhead work light, tools and a workbench softly visible in the background",
            "a kitchen counter at dusk during a blackout, candle and phone glow mixing with fading window light",
            "a family entryway with storm gear staged by the door, moody blue exterior light through the glass",
            "a suburban driveway at twilight with a vehicle trunk open, warm dome light spilling onto the pavement",
        ],
    },
    "outdoor_adventurer": {
        "name": "The Outdoor / Adventure Enthusiast",
        "emotion": "off-grid freedom without losing practical control",
        "environment": "a realistic campsite, trailhead, RV stop, or rugged travel setting",
        "environments": [
            "an alpine campsite at golden hour, a tent glowing warmly, pine silhouettes against a fading sky",
            "a forest clearing at dawn with soft mist through the trees and a low campfire ember glow",
            "a desert campsite at dusk with the first stars appearing and warm firelight on nearby rocks",
            "a lakeside dock at sunset, still water reflecting the sky, a canoe pulled up on the shore",
            "an RV pull-off at a scenic overlook, warm string lights strung along the awning at twilight",
        ],
    },
    "water_purity_seeker": {
        "name": "The Trail & Hydration Purist",
        "emotion": "clear-headed trust in the water they carry",
        "environment": "a mountain stream, alpine lake shore, or backcountry trail water source",
        "environments": [
            "a clear mountain stream flowing over smooth stones, forest framing soft-focus in the background",
            "an alpine lake shoreline at midday, snow-capped peaks reflected in still water",
            "a backcountry trail creek crossing, dappled sunlight through the canopy above",
            "a mossy waterfall base with fine mist catching the light, ferns framing the scene",
        ],
    },
}


def _stable_pick(seed_text: str, options: list) -> Any:
    """Deterministic rotation: same product+stage always yields the same pick, different ones vary."""
    if not options:
        return None
    digest = hashlib.sha256(str(seed_text or "").encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(options)
    return options[index]


def _resolve_proof_anchor(product: dict | None) -> str:
    """Real spec first; never fall back to a literal placeholder phrase."""
    metrics = [str(item).strip() for item in ((product or {}).get("metrics", []) or []) if str(item).strip()]
    if metrics:
        return metrics[0]
    fact_snippet = re.sub(r"\s+", " ", str((product or {}).get("fact_snippet") or "")).strip()
    if fact_snippet:
        return fact_snippet[:48].rstrip()
    configuration = re.sub(r"\s+", " ", str((product or {}).get("configuration") or "")).strip()
    if configuration:
        return configuration
    sale_price = str((product or {}).get("sale_price") or "").strip()
    if sale_price:
        return f"${sale_price}"
    price = str((product or {}).get("price") or "").strip()
    if price:
        return f"${price}"
    sku = str((product or {}).get("sku") or "").strip()
    if sku:
        return sku
    return ""


def _select_logical_emotional_strategy(product: dict | None, audience_segment: str, funnel_stage: str) -> dict:
    principle_key = {
        "ATTENTION": "contrapositive",
        "EDUCATION": "disjunctive_syllogism",
        "DESIRE": "double_implication",
        "TRUST": "symmetrical_equivalence",
        "CONVERSION": "implication_of_result",
    }.get(str(funnel_stage or "ATTENTION").strip().upper(), "contrapositive")

    product_text = " ".join(
        [
            str((product or {}).get("name", "")),
            " ".join(str(item) for item in ((product or {}).get("categories", []) or [])),
            str((product or {}).get("fact_snippet", "")),
            str(audience_segment or ""),
        ]
    ).lower()
    if any(token in product_text for token in ("water filter", "water purifier", "purification", "filtration", "filter straw", "hydration", "drinking water")):
        archetype_key = "water_purity_seeker"
    elif any(token in product_text for token in ("camp", "outdoor", "rv", "trail", "hiking", "adventure")):
        archetype_key = "outdoor_adventurer"
    elif any(token in product_text for token in ("professional", "digital nomad", "office", "business", "commuter", "airport", "power bank", "charger")):
        archetype_key = "mobile_professional"
    else:
        archetype_key = "preparedness_buyer"

    principle = LOGICAL_EMOTIONAL_PRINCIPLES[principle_key]
    archetype = AUDIENCE_ARCHETYPES[archetype_key]
    product_id = str((product or {}).get("id") or (product or {}).get("sku") or (product or {}).get("name") or "generic").strip()
    variety_seed = f"{product_id}|{principle_key}|{archetype_key}"
    product_name = str((product or {}).get("name") or "the selected Infenergy product").strip()
    proof_anchor = _resolve_proof_anchor(product)
    proof_clause = f"and {proof_anchor} " if proof_anchor else ""
    anchor_clause = f": {proof_anchor} " if proof_anchor else " "
    logic_copy = {
        "contrapositive": (
            "If staying powered matters, waiting until power is gone is not a plan.",
            f"The missing preparation becomes obvious at the exact moment {product_name} {proof_clause}would have mattered.",
        ),
        "disjunctive_syllogism": (
            "Keep reacting at the last minute, or build around verified fit.",
            f"Once guesswork is ruled out, {product_name} can be judged by what matters{anchor_clause}and the job you need it to do.",
        ),
        "double_implication": (
            "List the devices that must run before choosing a battery.",
            f"Compare {product_name} {proof_clause}with each device's watts, plug, and required runtime.",
        ),
        "symmetrical_equivalence": (
            "Use the same device list at home, in transit, and off-grid.",
            f"Compare {product_name}{anchor_clause}with the watts, plug, and runtime required for that list.",
        ),
        "implication_of_result": (
            "The save-the-day moment starts before the crisis.",
            f"That successful result traces back to choosing {product_name} for the right job{(' and verifying details such as ' + proof_anchor) if proof_anchor else ''} first.",
        ),
    }[principle_key]
    headline = _stable_pick(variety_seed, principle.get("headline_variants") or [principle["on_image_headline"]]) or principle["on_image_headline"]
    environment = _stable_pick(variety_seed, archetype.get("environments") or [archetype["environment"]]) or archetype["environment"]
    subline = f"{product_name} | {proof_anchor}" if proof_anchor else product_name
    return {
        "principle_key": principle_key,
        "principle_name": principle["name"],
        "formal_logic": principle["logic"],
        "visual_concept": principle["visual_concept"],
        "caption_strategy": principle["caption_strategy"],
        "archetype_key": archetype_key,
        "audience_archetype": archetype["name"],
        "emotional_outcome": archetype["emotion"],
        "environment": environment,
        "variety_seed": variety_seed,
        "logic_hook": logic_copy[0],
        "logic_bridge": logic_copy[1],
        "on_image_headline": headline,
        "on_image_subline": subline,
    }


LIGHTING_SCHEMES = [
    "warm golden-hour rim light from one side with a soft cool fill from the other, long soft shadows",
    "a single dramatic practical light source such as a lamp, headlamp, or dashboard glow cutting through near-darkness",
    "diffused overcast daylight with gentle even shadows and true-to-life color",
    "cinematic blue-hour ambient light mixed with one warm practical source, moody but legible",
    "crisp golden morning sidelight with long shadows and clean highlight separation",
    "soft window or canopy light with visible atmosphere in the air, quiet and intimate",
]

CANVAS_ENERGY_NOTES = [
    "let one continuous diagonal light shaft or reflection connect the copy zone to the product zone so the whole frame reads as one image, not two halves",
    "use a soft environmental gradient of haze, smoke, or bokeh drifting from the product zone toward the copy zone so no area of the canvas feels empty or unfinished",
    "carry one repeating material or color accent from the background into the foreground so every zone of the frame feels connected",
    "let soft depth-of-field foreground elements gently enter the extreme edges of the frame so the composition never feels like a flat cutout",
]

CATEGORY_TEXTURES = {
    "preparedness_buyer": "brushed metal, matte charcoal surfaces, warm amber practical light, soft reflections, natural depth haze",
    "mobile_professional": "brushed aluminum, warm walnut wood, soft leather textures, clean glass reflections, ambient city bokeh",
    "outdoor_adventurer": "weathered wood, canvas and rope textures, granite and pine textures, campfire-lit particulate haze",
    "water_purity_seeker": "wet stone, moss, clear flowing water with natural light caustics, cool mineral tones, fine water mist",
}


def _build_ai_image_prompt_bank(strategy: dict, product: dict | None) -> dict[str, str]:
    product_name = str((product or {}).get("name") or "the selected product").strip()
    concept = str(strategy.get("visual_concept", "")).strip()
    environment = str(strategy.get("environment", "")).strip()
    archetype_key = str(strategy.get("archetype_key") or "preparedness_buyer").strip()
    variety_seed = str(strategy.get("variety_seed") or f"{product_name}|{archetype_key}").strip()
    lighting = _stable_pick(variety_seed + "|lighting", LIGHTING_SCHEMES)
    canvas_note = _stable_pick(variety_seed + "|canvas", CANVAS_ENERGY_NOTES)
    texture = CATEGORY_TEXTURES.get(archetype_key, CATEGORY_TEXTURES["preparedness_buyer"])
    shared = (
        f"Create a photorealistic commercial background scene plate informed by this concept: {concept} "
        f"Audience environment: {environment}. The real {product_name} product cutout will be added later. "
        "Do not render any product, device, package, placeholder, text, letters, numerals, logos, labels, signs, screens, UI, badges, buttons, charts, or watermarks. "
        f"Use physically believable materials such as {texture}, one coherent light direction ({lighting}), and natural human emotion where appropriate. "
        f"Fill the entire frame with rich, in-focus environmental detail — no flat, empty, or dead space anywhere in the canvas; {canvas_note}. "
    )
    return {
        "lifestyle_aesthetic": shared + "Instagram/Pinterest direction: refined lifestyle editorial photography, 1:1 square, mobile-first focal depth, calm low-detail left 42%, grounded open right 38%, clear bottom 16%.",
        "crisis_preparedness": shared + "Facebook direct-response direction: the exact tense moment before or during a credible outage, travel disruption, or preparedness gap, 1:1 square, emotionally legible but not sensational, calm left 44%, grounded open right 38%, clear bottom 16%.",
        "professional_everyday_carry": shared + "LinkedIn/X direction: executive everyday-carry or business-continuity photography, wide 16:9, composed and credible, calm low-detail left 46%, grounded open right 36%, no staged stock-photo handshake.",
    }


def _apply_strategic_brief_to_visual(visual_plan: dict, run_context: dict, product: dict | None) -> dict:
    """Phase F - unify visual plan with StrategicBrief.design (spec Section 20/23).

    Enforces that the visual direction matches the law's visual strategy and
    seeds a carousel storyboard from the law's narrative_template. Never
    mutates strategic_brief itself; only augments visual_plan.
    """
    if not isinstance(visual_plan, dict):
        return visual_plan
    brief = run_context.get("strategic_brief") if isinstance(run_context.get("strategic_brief"), dict) else None
    strategist = run_context.get("conversion_strategist") if isinstance(run_context.get("conversion_strategist"), dict) else None
    if not brief:
        return visual_plan
    design = brief.get("design") if isinstance(brief.get("design"), dict) else {}
    persuasion = brief.get("persuasion") if isinstance(brief.get("persuasion"), dict) else {}
    strategist = strategist or {}

    law_name = str(brief.get("logic_principle", "") or "").strip()
    visual_direction = str(design.get("visual_direction", "") or "").strip()
    template = str(design.get("template", "") or "").strip()

    if visual_direction:
        current_objective = str(visual_plan.get("visual_objective", "") or "").strip()
        if visual_direction.lower() not in current_objective.lower():
            visual_plan["visual_objective"] = (current_objective + " " + visual_direction).strip()
        visual_plan["strategic_visual_direction"] = visual_direction

    if template and not visual_plan.get("strategic_template_family"):
        visual_plan["strategic_template_family"] = template

    prompt = str(visual_plan.get("gemini_image_prompt", "") or "")
    law_readable = law_name.replace("_", " ") if law_name else ""
    if law_readable and law_readable not in prompt.lower():
        # Prepend a directive rather than break the model's existing scene brief.
        prefix = f"[{law_readable} visual strategy] "
        visual_plan["gemini_image_prompt"] = (prefix + prompt).strip()

    # Carousel storyboard aligned to the law's narrative_template
    beats = list(strategist.get("law_narrative_template", []) or [])
    if not beats:
        beats = [
            "Open with the persona in their situation",
            "Show the problem or objection",
            "Reveal the mechanism or reframe",
            "Show the outcome or transformation",
            "End with the pinned CTA",
        ]
    storyboard = []
    from_state = str(persuasion.get("transformation_from", "") or "").strip()
    to_state = str(persuasion.get("transformation_to", "") or "").strip()
    proof_hint = str(persuasion.get("proof", "") or "").strip()
    cta_pinned = str((strategist.get("downstream_instructions") or {}).get("cta_pinned", "") or "").strip()
    for idx, beat in enumerate(beats[:5], start=1):
        slide = {
            "slide": idx,
            "beat": beat,
            "visual_direction": visual_direction or "photo-real, natural light, hero-shot composition",
            "on_image_text_hint": "",
        }
        if idx == 1 and from_state:
            slide["on_image_text_hint"] = from_state
        elif idx == len(beats[:5]) and cta_pinned:
            slide["on_image_text_hint"] = cta_pinned
        elif to_state and idx == 4:
            slide["on_image_text_hint"] = to_state
        elif proof_hint and idx == 3:
            slide["on_image_text_hint"] = proof_hint[:80]
        storyboard.append(slide)

    visual_plan["strategic_carousel_storyboard"] = carousel_director.normalize_slide_dicts(
        storyboard,
        title=str((storyboard[0] if storyboard else {}).get("beat") or "INFENERGY MICRO MISSION"),
        moral=to_state or carousel_director.DEFAULT_MORAL,
        call_to_action=cta_pinned or carousel_director.DEFAULT_CTA,
    )
    visual_plan["strategic_brief_alignment"] = {
        "logic_principle": law_name,
        "template_family": template,
        "visual_direction_present": bool(visual_direction and visual_direction.lower() in str(visual_plan.get("visual_objective", "")).lower()),
        "law_signal_in_prompt": law_readable in str(visual_plan.get("gemini_image_prompt", "")).lower() if law_readable else False,
        "storyboard_slides": len(storyboard),
    }
    return visual_plan


def _apply_logical_visual_strategy(visual_plan: dict, strategy: dict, product: dict | None) -> dict:
    prompt_bank = _build_ai_image_prompt_bank(strategy, product)
    visual_plan["logical_emotional_strategy"] = dict(strategy)
    visual_plan["ai_image_prompt_bank"] = prompt_bank
    platform_overrides = visual_plan.setdefault("platform_overrides", {})
    platform_prompt_keys = {
        "facebook": "crisis_preparedness",
        "instagram": "lifestyle_aesthetic",
        "linkedin": "professional_everyday_carry",
    }
    for platform, prompt_key in platform_prompt_keys.items():
        platform_config = platform_overrides.setdefault(platform, {})
        platform_config["prompt_variant"] = prompt_key
        platform_config["scene_prompt"] = prompt_bank[prompt_key]
    return visual_plan


def _build_logical_carousel_campaign(components: dict) -> dict:
    features = [str(item).strip() for item in (components.get("feature_bullets", []) or []) if str(item).strip()]
    proof_line = features[0] if features else str(components.get("proof") or "Review the published product details.")
    second_proof = features[1] if len(features) > 1 else str(components.get("detail_summary") or proof_line)
    strategy = components.get("logical_emotional_strategy", {}) if isinstance(components.get("logical_emotional_strategy"), dict) else {}
    narrative_slides = [
            {
                "slide": 1,
                "purpose": "visual_anxiety_or_desire_hook",
                "headline": str(components.get("on_image_headline") or components.get("hook") or "Choose readiness"),
                "body": str(components.get("logic_hook") or "Start with the real problem."),
            },
            {
                "slide": 2,
                "purpose": "logical_contrast",
                "headline": "THE DECISION",
                "body": str(components.get("logic_bridge") or components.get("info") or "Compare the old path with verified fit."),
            },
            {
                "slide": 3,
                "purpose": "product_and_verified_proof",
                "headline": str(components.get("product_name") or "PRODUCT PROOF"),
                "body": f"{proof_line}. {second_proof}.",
            },
            {
                "slide": 4,
                "purpose": "emotional_result",
                "headline": "CHECK THE DEVICE LIST",
                "body": str(components.get("use_case_line") or "Name each device, its watts, its plug, and the hours it must run."),
            },
            {
                "slide": 5,
                "purpose": "single_next_step",
                "headline": "MAKE THE NEXT MOVE",
                "body": str(components.get("cta") or "Compare the published specifications with your device list."),
            },
        ]
    return {
        "format": "framed_carousel",
        "principle": str(strategy.get("principle_name") or "Logical product-fit narrative"),
        "audience_archetype": str(strategy.get("audience_archetype") or "Prepared buyer"),
        "slides": carousel_director.normalize_slide_dicts(
            narrative_slides,
            title=str(components.get("on_image_headline") or components.get("hook") or "INFENERGY MICRO MISSION"),
            moral=str(components.get("emotional_outcome") or carousel_director.DEFAULT_MORAL),
            call_to_action=str(components.get("cta") or carousel_director.DEFAULT_CTA),
        ),
    }


def _build_social_media_assets(components: dict, platform_posts: dict, visual_plan: dict) -> dict:
    prompt_bank = visual_plan.get("ai_image_prompt_bank", {}) if isinstance(visual_plan.get("ai_image_prompt_bank"), dict) else {}
    prompt_keys = {
        "facebook": "crisis_preparedness",
        "instagram": "lifestyle_aesthetic",
        "linkedin": "professional_everyday_carry",
    }
    single_image: dict[str, dict] = {}
    for platform, prompt_key in prompt_keys.items():
        post = platform_posts.get(platform, {}) if isinstance(platform_posts.get(platform), dict) else {}
        caption = str(post.get("caption") or "")
        single_image[platform] = {
            "visual_concept_description": str(
                components.get("logical_emotional_strategy", {}).get("visual_concept", "")
                if isinstance(components.get("logical_emotional_strategy"), dict)
                else ""
            ),
            "ai_image_generation_prompt": str(prompt_bank.get(prompt_key) or ""),
            "ad_headline": str(components.get("on_image_headline") or ""),
            "on_image_text_overlay": str(components.get("on_image_subline") or ""),
            "caption_copy": caption,
            "hashtags": re.findall(r"#[A-Za-z0-9_]+", caption)[-8:],
        }
    return {
        "asset_1_single_image_social_ads": single_image,
        "asset_2_carousel_campaign": _build_logical_carousel_campaign(components),
        "asset_3_ai_image_prompt_bank": prompt_bank,
    }


def _build_post_components(
    topic: str,
    selected_hook: str,
    selected_cta: str,
    product: dict | None,
    funnel_stage: str,
    product_intelligence: dict | None = None,
    logical_strategy: dict | None = None,
    editorial_framework: dict | None = None,
    resolved_pain_point: str = "",
) -> dict:
    product_name = (product or {}).get("name", "Infenergy preparedness solution")
    product_id = (product or {}).get("id", "")
    metrics = (product or {}).get("metrics", [])
    feature_bullets = _sales_feature_bullets(product, limit=5)
    m1 = metrics[0] if len(metrics) > 0 else "verified output specs"
    m2 = metrics[1] if len(metrics) > 1 else "runtime and charging context"
    profile = _product_copy_profile(product)

    situation = "Infenergy customers usually come to us because they want to stay charged, connected, and prepared before the next outage or travel disruption exposes a weak setup."
    info = f"The stronger path is to map your must-run devices first and compare them against measured specs like {m1} and {m2}."
    why = "That reduces guesswork, protects against wrong-fit purchases, and moves the buyer toward a confident preparedness decision."
    product_connection = f"For this topic, {product_name} supports Infenergy's goal of giving customers a right-size power solution matched to real daily loads."
    proof = f"Real specs to compare: {m1} and {m2}."

    situation = profile["category_pain"]
    info = f"Match the devices you need to support with published capacity and output details such as {m1} and {m2}."
    why = profile["benefit"]
    product_connection = f"{product_name} {profile['fit_line']}."
    proof = f"{profile['proof_intro']}."

    if isinstance(product_intelligence, dict) and product_intelligence:
        audiences = product_intelligence.get("best_fit_audiences", [])
        benefits = product_intelligence.get("core_benefits", [])
        proofs = product_intelligence.get("proof_points", [])
        product_brief = product_intelligence.get("product_brief", {})
        sales_angle = str(product_intelligence.get("sales_angle", "")).strip()
        audience_line = str(audiences[0]).strip() if isinstance(audiences, list) and audiences else "buyers preparing for outages"
        proof_line = str(proofs[0]).strip() if isinstance(proofs, list) and proofs else f"{m1} and {m2}"
        brief_pain = str(product_brief.get("primary_pain_point", "")).strip() if isinstance(product_brief, dict) else ""
        situation = brief_pain or f"{audience_line} often choose a product before listing the devices, plugs, watts, and required runtime."
        info = _product_comparison_line(product)
        why = str(profile.get("benefit", "match real usage to the right product specs"))
        product_connection = f"Compare {product_name} with the listed devices and the published {proof_line}."
        proof = f"The specs that matter here: {proof_line}."

    if str(resolved_pain_point).strip():
        situation = str(resolved_pain_point).strip()

    benefit_fragment = _normalize_benefit_fragment(why)

    cta = selected_cta
    stage = funnel_stage.upper()
    if stage == "EDUCATION":
        cta = "Save this checklist and compare your current setup."
    elif stage == "DESIRE":
        cta = _product_comparison_cta(product)
    elif stage == "CONVERSION":
        cta = "Build your backup-power setup."

    strategy = logical_strategy if isinstance(logical_strategy, dict) else {}
    framework = editorial_framework if isinstance(editorial_framework, dict) else {}
    return {
        "product_id": product_id or None,
        "hook": selected_hook,
        "situation": situation,
        "info": info,
        "why": why,
        "benefit_fragment": benefit_fragment,
        "after_state": profile["after_state"],
        "transformation": profile["transformation"],
        "why_it_matters": profile["why_it_matters"],
        "product_connection": product_connection,
        "proof": proof,
        "feature_bullets": feature_bullets,
        "product_name": product_name,
        "copy_profile": profile,
        "use_case_line": _product_use_case_line(product),
        "detail_summary": _product_detail_summary(product),
        "cta": cta,
        "topic": topic,
        "logical_emotional_strategy": strategy,
        "editorial_framework": framework,
        "logic_hook": str(strategy.get("logic_hook") or selected_hook),
        "logic_bridge": str(strategy.get("logic_bridge") or info),
        "emotional_outcome": str(strategy.get("emotional_outcome") or profile["after_state"]),
        "on_image_headline": str(strategy.get("on_image_headline") or selected_hook),
        "on_image_subline": str(strategy.get("on_image_subline") or m1),
    }


def _sales_feature_bullets(product: dict | None, limit: int = 5) -> list[str]:
    if not isinstance(product, dict):
        return []
    bullets: list[str] = []
    metrics = [str(x).strip() for x in (product.get("metrics", []) or []) if str(x).strip()]
    for metric in metrics[:limit]:
        bullets.append(_one_line(_strip_html(metric), 72))
    categories = [str(x).strip() for x in (product.get("categories", []) or []) if str(x).strip()]
    if categories:
        bullets.append(f"Built for {categories[0]}")
    fact = _one_line(_strip_html(str(product.get("fact_snippet", "") or "")), 82)
    if fact:
        bullets.append(fact)
    cleaned: list[str] = []
    seen: set[str] = set()
    for bullet in bullets:
        key = bullet.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(bullet)
        if len(cleaned) >= limit:
            break
    return cleaned


def _normalize_benefit_fragment(text: str) -> str:
    cleaned = _one_line(_strip_html(str(text or "")), 140).strip()
    if not cleaned:
        return "supports a clearer buying decision"
    return cleaned[:1].lower() + cleaned[1:]


def _adapt_facebook(components: dict, funnel_stage: str) -> tuple[str, str, str]:
    from social import platform_presentation

    caption, _ = platform_presentation.format_caption(components, platform="facebook")
    return caption, str(components.get("cta") or "Learn more"), "community_story"


def _adapt_instagram(components: dict, funnel_stage: str) -> tuple[str, str, str, str]:
    from social import platform_presentation

    caption, _ = platform_presentation.format_caption(components, platform="instagram")
    visual_direction = "reel" if funnel_stage.upper() in ("ATTENTION", "DESIRE") else "carousel"
    return caption, str(components.get("cta") or "Learn more"), visual_direction, "Platform-native visual with complementary decision context."


def _adapt_linkedin(components: dict, funnel_stage: str) -> tuple[str, str, str]:
    from social import platform_presentation

    caption, _ = platform_presentation.format_caption(components, platform="linkedin")
    return caption, str(components.get("cta") or "Learn more"), "authority_post"


def _build_platform_posts(
    post_id: str,
    campaign_id: str,
    audience_segment: str,
    funnel_stage: str,
    destination_url: str,
    components: dict,
    quality_score: float,
    caption_overrides: dict | None = None,
    strategy_lock: dict | None = None,
    creative_reviews: dict | None = None,
    platform_interpretations: dict | None = None,
) -> dict:
    fb_caption, fb_cta, fb_format = _adapt_facebook(components, funnel_stage)
    ig_caption, ig_cta, ig_visual, ig_alt = _adapt_instagram(components, funnel_stage)
    li_text, li_cta, li_format = _adapt_linkedin(components, funnel_stage)

    utm_fb = build_utm_url(
        destination_url,
        source="facebook",
        campaign=campaign_id or "campaign",
        content=f"{components.get('product_id') or 'general'}_{funnel_stage.lower()}",
        term=audience_segment.lower().replace(" ", "_"),
    )
    utm_ig = build_utm_url(
        destination_url,
        source="instagram",
        campaign=campaign_id or "campaign",
        content=f"{components.get('product_id') or 'general'}_{funnel_stage.lower()}",
        term=audience_segment.lower().replace(" ", "_"),
    )
    utm_li = build_utm_url(
        destination_url,
        source="linkedin",
        campaign=campaign_id or "campaign",
        content=f"{components.get('product_id') or 'general'}_{funnel_stage.lower()}",
        term=audience_segment.lower().replace(" ", "_"),
    )

    platform_posts = {
        "facebook": {
            "post_id": post_id,
            "campaign_id": campaign_id,
            "platform": "facebook",
            "funnel_stage": funnel_stage,
            "audience_segment": audience_segment,
            "product_id": components.get("product_id"),
            "hook": components["hook"],
            "caption": fb_caption,
            "cta": fb_cta,
            "destination_url": destination_url,
            "utm_url": utm_fb.get("utm_url", destination_url),
            "content_format": fb_format,
            "visual_direction": "single_image",
            "alt_text": "Facebook visual illustrating practical home energy preparedness.",
            "quality_score": float(quality_score),
            "validation_status": "ok",
            "validation_errors": [],
        },
        "instagram": {
            "post_id": post_id,
            "campaign_id": campaign_id,
            "platform": "instagram",
            "funnel_stage": funnel_stage,
            "audience_segment": audience_segment,
            "product_id": components.get("product_id"),
            "hook": components["hook"],
            "caption": ig_caption,
            "cta": ig_cta,
            "destination_url": destination_url,
            "utm_url": utm_ig.get("utm_url", destination_url),
            "content_format": "short_caption",
            "visual_direction": ig_visual,
            "alt_text": ig_alt,
            "quality_score": float(quality_score),
            "validation_status": "ok",
            "validation_errors": [],
        },
        "linkedin": {
            "post_id": post_id,
            "campaign_id": campaign_id,
            "platform": "linkedin",
            "funnel_stage": funnel_stage,
            "audience_segment": audience_segment,
            "product_id": components.get("product_id"),
            "hook": components["hook"],
            "caption": li_text,
            "cta": li_cta,
            "destination_url": destination_url,
            "utm_url": utm_li.get("utm_url", destination_url),
            "content_format": li_format,
            "visual_direction": "insight_graphic",
            "alt_text": "LinkedIn visual focused on resilience strategy and verified product details.",
            "quality_score": float(quality_score),
            "validation_status": "ok",
            "validation_errors": [],
        },
    }
    overrides = caption_overrides or {}
    for platform in ("facebook", "instagram", "linkedin"):
        platform_override = overrides.get(platform, {}) if isinstance(overrides, dict) else {}
        if not isinstance(platform_override, dict):
            continue
        override_caption = str(platform_override.get("caption", "")).strip()
        override_cta = str(platform_override.get("cta", "")).strip()
        if override_caption:
            platform_posts[platform]["caption"] = override_caption
        if override_cta:
            platform_posts[platform]["cta"] = override_cta

    lock = dict(strategy_lock or {})
    reviews = dict(creative_reviews or {})
    native_posture = {
        "facebook": "conversational education and discussion",
        "instagram": "visual-first memorable takeaway",
        "linkedin": "professional decision-support insight",
    }
    interpretations = dict(platform_interpretations or {})
    from social import platform_presentation
    for platform, package in platform_posts.items():
        interpretation = interpretations.get(platform, {})
        if isinstance(interpretation, dict):
            hook_posture = str(interpretation.get("hook_posture") or "").strip()
            cta_expression = str(interpretation.get("cta_expression") or "").strip()
            visual_composition = str(interpretation.get("visual_composition") or "").strip()
            format_hint = str(interpretation.get("format") or "").strip()
            if hook_posture:
                package["hook"] = f"{hook_posture}: {package['hook']}"
            if cta_expression:
                package["planning_instructions"] = [cta_expression]
            if format_hint:
                package["content_format"] = format_hint
            if visual_composition:
                package["visual_direction"] = visual_composition
            package["creative_interpretation"] = interpretation
        package["strategy_lock"] = lock
        package["human_connection_review"] = reviews.get("independent_human_connection_review", {})
        package["strategy_integrity_review"] = reviews.get("strategy_integrity_review", {})
        package["platform_posture"] = native_posture[platform]
        presentation = platform_presentation.evaluate(
            package["caption"],
            platform=platform,
            visual_specs=list(components.get("feature_bullets") or []),
        )
        package["presentation"] = presentation

    return platform_posts


def _apply_platform_presentation_priority(platform_posts: dict, components: dict) -> dict:
    from social import platform_presentation

    for platform in ("facebook", "instagram", "linkedin"):
        package = platform_posts.get(platform, {}) if isinstance(platform_posts, dict) else {}
        if not isinstance(package, dict):
            continue
        refined_caption, priority = platform_presentation.refine_caption(
            str(package.get("caption", "")),
            components=components,
            platform=platform,
            product_led=bool(components.get("product_id")),
        )
        final_caption = platform_presentation.render_platform_caption(
            refined_caption,
            destination_url=str(package.get("utm_url") or package.get("destination_url") or ""),
            platform=platform,
        )
        semantic_evidence = priority.get("semantic_layer_evidence", {})
        qa_components = dict(components)
        approved_benefit = str(semantic_evidence.get("primary_benefit") or "").strip()
        if approved_benefit:
            qa_components["benefit_fragment"] = approved_benefit
        final_qa = platform_presentation.final_caption_qa(
            final_caption,
            platform=platform,
            components=qa_components,
            planning_instructions=list(package.get("planning_instructions") or []),
        )
        metrics = final_qa["metrics"]
        package["caption"] = final_caption
        package["final_caption"] = final_caption
        package["final_caption_hash"] = hashlib.sha256(final_caption.encode("utf-8")).hexdigest()
        package["final_caption_qa"] = final_qa
        priority.update(metrics)
        priority.update({
            "above_fold_value": {
                "product_present": bool(metrics["product_intro_position"]),
                "primary_benefit_present": bool(metrics["primary_benefit_position"]),
                "product_intro_position": metrics["product_intro_position"],
                "primary_benefit_position": metrics["primary_benefit_position"],
            },
            "commercial_layers": semantic_evidence,
            "device_use_case_intelligence": semantic_evidence.get("device_use_case", ""),
            "optional_depth": semantic_evidence.get("optional_depth", []),
            "presentation_density": metrics["reading_burden"],
            "hashtag_portfolio": priority.get("selected_hashtags", []),
            "copy_visual_complementarity": {
                "reinforcing_proof": metrics["reinforcing_proof"],
                "score": metrics["complementarity_score"],
            },
            "final_render_consistency": {
                "caption_equals_final_caption": package["caption"] == package["final_caption"],
                "qa_evaluated_final_caption": metrics["final_caption"] == package["final_caption"],
                "metadata_derived_from_final_caption": True,
            },
        })
        package["presentation"] = priority
        package["message_hierarchy"] = ["hook", "product", "primary_benefit", "selected_proof", "human_use", "action"]
        if platform == "instagram":
            package["reel_caption"] = platform_presentation.format_reel_caption(components, priority)
            package["reel_presentation"] = {
                "message_hierarchy": package["message_hierarchy"],
                "on_screen_copy_rule": "concise phrases only; optional depth belongs in the caption",
                "freeze_frame_priority": ["product", "headline", "primary_benefit", "selected_proof", "cta"],
                "caption_complementarity": "adds optional depth without restating the on-screen sequence",
            }
    return platform_posts


def _model_caption_overrides(content: dict) -> dict[str, dict[str, str]]:
    cta = str(content.get("selected_cta", ""))
    return {
        "facebook": {"caption": str(content.get("fb_caption", "")), "cta": cta},
        "instagram": {"caption": str(content.get("ig_caption", "")), "cta": cta},
        "linkedin": {"caption": str(content.get("li_text", "")), "cta": cta},
    }


def _join_paragraphs(*parts: str) -> str:
    """Join non-empty caption paragraphs with blank lines, skipping empty ones cleanly."""
    return "\n\n".join(str(p).strip() for p in parts if str(p or "").strip())


def _build_scenario_fingerprint(talking_point: dict | None, components: dict) -> str:
    """Text used for anti-repeat scenario-duplicate detection.

    Must reflect the actually-varying creative decision (talking_point angle/pain_point,
    diversified by ideation divergence / the conversion strategist brief) rather than
    components["situation"], which is a static per-product-category template (only a
    handful of fixed strings across the whole catalog) that would otherwise guarantee a
    "duplicate scenario" false positive for any two posts sharing a product category.
    """
    talking_point = talking_point or {}
    return _join_paragraphs(
        str(talking_point.get("angle", "") or ""),
        str(talking_point.get("pain_point", "") or ""),
    ) or components.get("situation", "")


def _build_fallback_content(
    slot: str,
    topic: str,
    product: dict | None,
    marketing_strategy: dict | None,
    talking_point: dict | None = None,
    strategic_brief: dict | None = None,
) -> dict:
    marketing_strategy = marketing_strategy or {}
    talking_point = talking_point or {}
    # Even when Gemini is unavailable, honor the Conversion Logic Engine's decisions
    # (problem/objection/transformation) instead of falling back to a generic template.
    persuasion = (strategic_brief or {}).get("persuasion", {}) if isinstance((strategic_brief or {}).get("persuasion"), dict) else {}
    brief_problem = str(persuasion.get("problem", "") or "").strip()
    brief_objection = str(persuasion.get("objection", "") or "").strip()
    transformation_from = str(persuasion.get("transformation_from", "") or "").strip()
    transformation_to = str(persuasion.get("transformation_to", "") or "").strip()
    marketing_copy = marketing_strategy.get("copy", {})
    name = (product or {}).get("name", "INF Energy Power solution")
    sku = (product or {}).get("sku", "")
    price = (product or {}).get("price", "")
    sale_price = (product or {}).get("sale_price", "")
    metrics = (product or {}).get("metrics", [])
    m1 = metrics[0] if len(metrics) > 0 else "high-capacity output"
    m2 = metrics[1] if len(metrics) > 1 else "fast charging performance"

    price_line = ""
    if sale_price:
        price_line = f" Current sale price is ${sale_price}."
    elif price:
        price_line = f" Current listed price is ${price}."

    hero = (marketing_copy.get("hero") or "").strip()
    cta_bank = marketing_copy.get("cta_bank") or []
    cta = cta_bank[0] if cta_bank else "Book your free power readiness assessment"
    pain_point = str(talking_point.get("pain_point") or "Most people buy backup power without validating real usage first.").strip()
    proof_anchor = str(talking_point.get("proof_anchor") or f"Use {m1} and {m2} to validate fit before buying.").strip()
    first_step = str(talking_point.get("first_step") or cta).strip()
    angle = str(talking_point.get("angle") or f"{topic} through practical decision-making.").strip()
    brief = talking_point.get("product_brief", {}) if isinstance(talking_point.get("product_brief"), dict) else {}
    if not brief:
        brief = product_intelligence.build_product_brief(product or {
            "id": "GENERAL-PREPAREDNESS",
            "name": str(name),
            "categories": ["Preparedness"],
        })
        talking_point["product_brief"] = brief
    role = str(brief.get("role", "") or "preparedness product")
    brief_benefits = [str(value).strip() for value in brief.get("core_benefits", []) if str(value).strip()]
    benefit = brief_benefits[0] if brief_benefits else "fills a specific gap in a practical preparedness plan"
    pain_point = str(brief.get("primary_pain_point", "") or pain_point)
    if brief_problem:
        pain_point = brief_problem
    # NOTE: brief["proof_rule"] is an internal validation instruction, never customer copy —
    # derive proof_anchor from customer-safe verified_facts instead (see _build_product_intelligence_handoff).
    brief_verified_facts = [str(v).strip() for v in brief.get("verified_facts", []) if str(v).strip()]
    if brief_verified_facts:
        proof_anchor = f"Published details: {'; '.join(brief_verified_facts[:2])}."
    first_step = str(brief.get("recommended_cta", "") or first_step)
    use_cases = [str(value).strip() for value in brief.get("best_fit_use_cases", []) if str(value).strip()]
    usage_line = f"Best suited to {', '.join(use_cases[:3])}." if use_cases else "Match the product to the specific job it needs to do."
    hashtag_values = [str(value).strip().lstrip("#") for value in brief.get("hashtag_themes", []) if str(value).strip()]
    hashtag_line = " ".join(f"#{value}" for value in hashtag_values[:5]) or "#Preparedness #EmergencyKit"
    talking_point["pain_point"] = pain_point
    talking_point["first_step"] = first_step
    talking_point["proof_anchor"] = proof_anchor
    metric_line = " and ".join(str(value).strip() for value in metrics[:2] if str(value).strip())
    proof_line = (
        f"Published specs include {metric_line}."
        if metric_line
        else f"Review the published {role} details and compatibility for your setup."
    )
    transformation_line = (
        f"The real shift here is {transformation_from} to {transformation_to}."
        if transformation_from and transformation_to
        else ""
    )
    objection_line = (
        f'If you\'re thinking "{brief_objection}" — that\'s exactly the gap worth closing before you need it.'
        if brief_objection
        else ""
    )

    wp_title = f"{name}: What To Know Before You Buy"
    if len(wp_title) > 64:
        wp_title = f"{name[:52]}: Buyer Guide"
    if hero and len(hero) <= 62:
        wp_title = hero

    wp_content = (
        f"<p>{pain_point} This guide evaluates <strong>{name}</strong> as a {role} through this lens: {angle}</p>"
        f"<h2>What It's Built For</h2><p>{name} {benefit}.{price_line}</p>"
        f"<h2>Best-Fit Use Cases</h2><p>{usage_line}</p>"
        f"<h2>What the Specs Say</h2><p>{proof_line}</p>"
        f"<h2>Next Step</h2><p>{first_step}</p>"
    )

    fb_caption = _join_paragraphs(
        pain_point,
        f"Meet {name}, a {role} that {benefit}.{price_line}",
        proof_line,
        transformation_line,
        objection_line,
        usage_line,
        f"{first_step}\nWhat's the one device you can't afford to lose power to?\n{hashtag_line}",
    )

    ig_caption = _join_paragraphs(
        f"{name}: {role}.",
        pain_point,
        f"{benefit.capitalize()}.{price_line}",
        proof_line,
        transformation_line,
        usage_line,
        f"{first_step}\n{hashtag_line}",
    )

    li_text = _join_paragraphs(
        pain_point,
        f"{name}{' (' + sku + ')' if sku else ''} is a {role} that {benefit}.{price_line}",
        proof_line,
        transformation_line,
        objection_line,
        "For households and mobile teams, the practical value is continuity without a complicated setup.",
        first_step,
    )

    return {
        "wp_title": wp_title,
        "wp_content": wp_content,
        "wp_excerpt": f"{name}: practical buying guidance, key specs, and what to compare before you purchase.",
        "fb_caption": fb_caption,
        "ig_caption": ig_caption,
        "li_text": li_text,
    }


_PILLAR_HASHTAGS = {
    "preparedness_education": "#Preparedness #OutagePlan #EmergencyReady",
    "energy_literacy": "#EnergyLiteracy #Preparedness #PowerBasics",
    "customer_problem_solving": "#Preparedness #BackupPower #BuyingGuide",
    "brand_authority": "#InfenergyPower #Preparedness #ProofOverHype",
    "trust_and_company_values": "#InfenergyPower #Trust #Preparedness",
    "community_engagement": "#Preparedness #OutageStory #StayPowered",
    "home_resilience": "#HomeResilience #Preparedness #BackupPower",
    "travel_and_outdoor_preparedness": "#TravelPower #CampingReady #RVLife",
    "caregiver_preparedness": "#CaregiverPrep #Preparedness #BackupPower",
    "small_business_continuity": "#BusinessContinuity #SmallBusiness #Preparedness",
    "category_education": "#Preparedness #BackupPower #BuyingGuide",
    "readiness_assessment_lead_gen": "#PowerReadiness #Preparedness #GetPrepared",
}


def _build_fallback_content_no_product(
    slot: str,
    topic: str,
    pillar: str,
    marketing_strategy: dict | None,
    talking_point: dict | None = None,
    strategic_brief: dict | None = None,
) -> dict:
    """Deterministic fallback content for business-first posts with no product attached.

    Discusses the pillar/topic at a category or brand level. Never names a specific product,
    price, or SKU, and never uses a purchase-style CTA (except the lead-gen pillar, which uses
    a free-assessment CTA rather than a direct sale).
    """
    marketing_strategy = marketing_strategy or {}
    talking_point = talking_point or _build_talking_point_no_product(topic, "EDUCATION", pillar)
    persuasion = (strategic_brief or {}).get("persuasion", {}) if isinstance((strategic_brief or {}).get("persuasion"), dict) else {}
    pain_point = str(persuasion.get("problem", "") or talking_point.get("pain_point") or "").strip()
    angle = str(talking_point.get("angle") or topic).strip()
    first_step = str(talking_point.get("first_step") or "Comment below, we read every reply.").strip()
    hashtag_line = _PILLAR_HASHTAGS.get(pillar, "#Preparedness #InfenergyPower")
    transformation_from = str(persuasion.get("transformation_from", "") or "").strip()
    transformation_to = str(persuasion.get("transformation_to", "") or "").strip()
    transformation_line = (
        f"The real shift here is {transformation_from} to {transformation_to}."
        if transformation_from and transformation_to
        else ""
    )

    wp_title = _one_line(topic, 64) if len(topic) <= 64 else _one_line(topic, 60)
    wp_content = (
        f"<p>{pain_point} Here is how we think about it: {angle}</p>"
        f"<h2>What Actually Matters Here</h2><p>{topic}</p>"
        f"<h2>The Practical Takeaway</h2><p>Focus on the specifics that change your real-world outcome, not the headline claim.</p>"
        f"<h2>Next Step</h2><p>{first_step}</p>"
    )

    fb_caption = _join_paragraphs(pain_point, topic, angle, transformation_line, f"{first_step}\n{hashtag_line}")

    ig_caption = _join_paragraphs(topic, pain_point, angle, transformation_line, f"{first_step}\n{hashtag_line}")

    li_text = _join_paragraphs(
        pain_point,
        angle,
        transformation_line,
        "For households, caregivers, travelers, and small operators, this kind of practical clarity is what actually reduces risk.",
        first_step,
    )

    return {
        "wp_title": wp_title,
        "wp_content": wp_content,
        "wp_excerpt": _one_line(f"{topic}: {pain_point}", 160),
        "fb_caption": fb_caption,
        "ig_caption": ig_caption,
        "li_text": li_text,
    }


def _generate_json_with_gemini(prompt: str, model_candidates: list[str], attempts_per_model: int = 2) -> dict | None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[Gemini] caption generation skipped: GEMINI_API_KEY not set")
        return None

    client = genai.Client(api_key=api_key)
    last_error = "no_model_candidates_configured"

    for model_name in model_candidates:
        for attempt in range(max(1, attempts_per_model)):
            try:
                with tracked_gemini_call("reasoning", model_name, f"legacy caption generation attempt {attempt + 1}", attempt_number=attempt + 1, initiating_subsystem="generate_posts"):
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json"),
                    )
                raw = (response.text or "").strip()
                if not raw:
                    last_error = f"{model_name}:empty_response"
                    continue
                if raw.startswith("```"):
                    parts = raw.split("```")
                    raw = parts[1]
                    if raw.lower().startswith("json"):
                        raw = raw[4:]
                return json.loads(raw.strip())
            except GeminiBudgetExceeded:
                raise
            except Exception as e:
                last_error = f"{model_name}:attempt{attempt + 1}:{type(e).__name__}:{str(e)[:200]}"
                continue

    # Every model/attempt failed. Log the real reason so this is diagnosable in
    # Railway logs instead of silently reverting to the disconnected legacy template.
    print(f"[Gemini] caption generation failed after retries, using deterministic fallback: {last_error}")
    return None


def _build_default_visual_plan(topic: str, funnel_stage: str, selected_hook: str, selected_cta: str, product: dict | None) -> dict:
    has_product_image = bool((product or {}).get("image_url"))
    strategy = "hybrid" if has_product_image else "gemini_generated"
    return {
        "style_intent": "Premium commercial photography for practical energy resilience, with physically believable materials and lighting",
        "mood": "trustworthy, confident, modern, restrained",
        "image_strategy": strategy,
        "composition": "low-detail copy-safe negative space on the left, grounded product-safe stage on the lower right, natural center depth",
        "use_product_photo": has_product_image,
        "text_on_image": "none",
        "gemini_image_prompt": (
            f"Create a premium social background plate for topic '{topic}' with hook '{selected_hook}'. "
            f"Convey {funnel_stage.lower()} stage intent and support CTA direction: {selected_cta}. "
            "Do not render readable text or a product device; leave safe zones for local overlay and product compositing. "
            "Show a credible modern home or small-business preparedness environment with directional warm light, charcoal and deep-navy materials, natural depth, and no decorative interface elements."
        ),
        "platform_overrides": {
            "facebook": {"composition": "left 44% calm copy-safe zone, right 38% grounded product zone, bottom 16% clear, credible household setting", "visual_direction": "square commercial scene plate"},
            "instagram": {"composition": "left 42% calm headline-safe zone, right 38% grounded product zone, bottom 16% clear, mobile-first depth", "visual_direction": "square editorial scene plate"},
            "linkedin": {"composition": "left 46% calm copy-safe zone, right 36% grounded product zone, restrained workplace setting", "visual_direction": "wide professional scene plate"},
        },
    }


def _build_visual_plan_with_gemini(
    model_candidates: list[str],
    *,
    topic: str,
    funnel_stage: str,
    stage_objective: str,
    selected_hook: str,
    selected_cta: str,
    product_name: str,
    product_categories: str,
    product_metrics: str,
    has_product_image: bool,
) -> dict | None:
    prompt = f"""{VISUAL_DIRECTOR_BRIEF}

Campaign input:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Stage objective: {stage_objective}
- Hook: {selected_hook}
- CTA: {selected_cta}
- Product name: {product_name or 'N/A'}
- Product categories: {product_categories or 'N/A'}
- Product measurable specs: {product_metrics or 'N/A'}
- Product image available: {has_product_image}

You are planning a BACKGROUND SCENE PLATE, not a finished advertisement. The final system adds the exact product cutout, brand, headline, verified specs, and CTA locally after generation.
Describe only environment, physical materials, lighting, depth, palette, and negative-space geometry. Never ask the image model to create a product, device, package, logo, readable text, number, badge, button, chart, interface, sign, frame, or placeholder silhouette.
Use a restrained Infenergy palette of charcoal, deep navy, amber, and warm gold. Keep the scene photorealistic and commercially polished, with one coherent light direction and a believable surface where the real product can land.
Platform geometry is mandatory:
- Facebook: square; left 44% low-detail copy zone; right 38% grounded product zone; bottom 16% clear.
- Instagram: square; left 42% low-detail copy zone; right 38% grounded product zone; bottom 16% clear; mobile-first contrast.
- LinkedIn: wide; left 46% low-detail copy zone; right 36% grounded product zone; restrained professional environment.

Return only valid JSON with this exact shape:
{{
  "style_intent": "string",
  "mood": "string",
  "image_strategy": "gemini_generated|product_photo_featured|hybrid",
  "composition": "string",
  "use_product_photo": true,
  "text_on_image": "none|minimal",
    "gemini_image_prompt": "Concise scene-only prompt describing environment, materials, lighting, palette, depth, and safe zones",
  "platform_overrides": {{
    "facebook": {{"composition": "string", "visual_direction": "string"}},
    "instagram": {{"composition": "string", "visual_direction": "string"}},
    "linkedin": {{"composition": "string", "visual_direction": "string"}}
  }}
}}"""
    return _generate_json_with_gemini(prompt, model_candidates)


def _run_agent_conference(
    model_candidates: list[str],
    *,
    topic: str,
    funnel_stage: str,
    selected_hook: str,
    selected_cta: str,
    content: dict,
    visual_plan: dict,
    product_name: str,
    product_metrics: str,
) -> dict:
    prompt = f"""{AGENT_CONFERENCE_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Hook: {selected_hook}
- CTA: {selected_cta}
- Product: {product_name or 'N/A'}
- Product measurable specs: {product_metrics or 'N/A'}

Draft copy:
- wp_title: {str(content.get('wp_title', ''))[:200]}
- wp_excerpt: {str(content.get('wp_excerpt', ''))[:220]}
- fb_caption: {str(content.get('fb_caption', ''))[:700]}
- ig_caption: {str(content.get('ig_caption', ''))[:700]}
- li_text: {str(content.get('li_text', ''))[:700]}

Current visual plan:
{json.dumps(visual_plan, ensure_ascii=True)}

Return ONLY valid JSON with this exact shape:
{{
  "copywriter_feedback": ["string", "string"],
  "visual_director_feedback": ["string", "string"],
  "product_truth_feedback": ["string", "string"],
  "platform_editor_feedback": ["string", "string"],
  "collective_actions": ["string", "string", "string"],
  "refined": {{
    "hook": "optional refined hook",
    "cta": "optional refined CTA",
    "gemini_image_prompt": "optional refined visual prompt",
    "image_strategy": "gemini_generated|product_photo_featured|hybrid|",
    "fb_caption": "optional refined Facebook caption",
    "ig_caption": "optional refined Instagram caption",
    "li_text": "optional refined LinkedIn caption"
  }}
}}"""
    result = _generate_json_with_gemini(prompt, model_candidates)
    return result if isinstance(result, dict) else {}


def _apply_conference_refinements(content: dict, visual_plan: dict, conference: dict) -> tuple[dict, dict]:
    refined = conference.get("refined", {}) if isinstance(conference, dict) else {}
    if not isinstance(refined, dict):
        return content, visual_plan

    hook = str(refined.get("hook", "")).strip()
    cta = str(refined.get("cta", "")).strip()
    image_prompt = str(refined.get("gemini_image_prompt", "")).strip()
    image_strategy = str(refined.get("image_strategy", "")).strip().lower()

    if hook:
        content["selected_hook"] = hook
    if cta:
        content["selected_cta"] = cta
    if image_prompt:
        visual_plan["gemini_image_prompt"] = image_prompt
    if image_strategy in {"gemini_generated", "product_photo_featured", "hybrid"}:
        visual_plan["image_strategy"] = image_strategy

    return content, visual_plan


def _run_pre_generation_conference(
        model_candidates: list[str],
        *,
        topic: str,
        funnel_stage: str,
        stage_objective: str,
        selected_hook: str,
        selected_cta: str,
        product_name: str,
        product_categories: str,
        product_metrics: str,
        recent_hooks: list[str],
        recent_topics: list[str],
        recent_ctas: list[str],
) -> dict:
        prompt = f"""{PREGEN_CONFERENCE_BRIEF}

Run context:
- Topic: {topic}
- Funnel stage: {funnel_stage}
- Stage objective: {stage_objective}
- Current hook candidate: {selected_hook}
- Current CTA candidate: {selected_cta}
- Product: {product_name or 'N/A'}
- Product categories: {product_categories or 'N/A'}
- Product measurable specs: {product_metrics or 'N/A'}
- Recent hooks: {recent_hooks}
- Recent topics: {recent_topics}
- Recent CTAs: {recent_ctas}

Return ONLY valid JSON with this exact shape:
{{
    "recommended_hook": "string",
    "recommended_cta": "string",
    "primary_angle": "string",
    "visual_focus": "string",
    "platform_notes": {{
        "facebook": "string",
        "instagram": "string",
        "linkedin": "string"
    }},
    "collective_actions": ["string", "string", "string"],
    "risk_checks": ["string", "string"]
}}"""
        result = _generate_json_with_gemini(prompt, model_candidates)
        return result if isinstance(result, dict) else {}


def _default_pre_generation_conference(selected_hook: str, selected_cta: str) -> dict:
    return {
        "recommended_hook": selected_hook,
        "recommended_cta": selected_cta,
        "primary_angle": "practical education plus clear next step",
        "visual_focus": "real-world preparedness scenario",
        "platform_notes": {
            "facebook": "educational and conversational",
            "instagram": "visual-first concise delivery",
            "linkedin": "framework-driven professional tone",
        },
        "collective_actions": [
            "prioritize clarity over hype",
            "use verifiable product facts only",
            "end with one direct CTA",
        ],
        "risk_checks": [
            "no unsupported claims",
            "no repeated hook framing from recent runs",
        ],
    }


def _run_phase_c_conversion_gates(content: dict, run_context: dict, gate_records: list[dict]) -> dict:
    """Phase C — 18-factor CQS + claim integrity gate before publish.

    Runs conversion.claims.classify_text on every visible text field and
    conversion.cqs.score on the primary caption. Attaches results to content
    and appends gate records that the global gate evaluator will honour.
    Never raises - always returns the content so downstream flow continues.
    """
    try:
        from conversion import claims as _claims_mod
        from conversion import cqs as _cqs_mod
    except Exception as _err:
        gate_records.append(
            build_gate_record(
                gate_id="phase_c_conversion_gates",
                passed=False,
                severity="warning",
                reasons=[f"conversion_modules_unavailable:{_err}"],
                details={},
            )
        )
        return content

    brief = run_context.get("strategic_brief") if isinstance(run_context.get("strategic_brief"), dict) else {}
    brief = brief or {}
    verified_facts = list(content.get("product_metrics") or []) + list(brief.get("verified_facts") or [])
    verified_facts = [str(f) for f in verified_facts if f]
    warranty_available = bool((content.get("product_facts") or "").lower().find("warranty") >= 0)

    per_field: dict[str, dict] = {}
    aggregate = {"prohibited": [], "unsupported": [], "reasonable_inference": [], "supported": [], "verified": []}
    for field in ("wp_content", "fb_caption", "ig_caption", "li_text"):
        text = str(content.get(field, "") or "")
        if not text.strip():
            continue
        scan = _claims_mod.classify_text(text, verified_facts=verified_facts, warranty_available=warranty_available)
        worst = _claims_mod.worst_tier(scan)
        publishable, reasons = _claims_mod.is_publishable(scan)
        per_field[field] = {
            "worst_tier": worst,
            "publishable": publishable,
            "reasons": reasons,
            "scan": scan,
        }
        for tier in aggregate:
            for phrase in scan.get(tier, []) or []:
                aggregate[tier].append({"field": field, "phrase": phrase})

    claims_report = {
        "per_field": per_field,
        "aggregate": aggregate,
        "publishable": not (aggregate["prohibited"] or aggregate["unsupported"]),
    }
    content["conversion_claims_report"] = claims_report

    if aggregate["prohibited"]:
        gate_records.append(
            build_gate_record(
                gate_id="phase_c_claim_integrity",
                passed=False,
                severity="error",
                reasons=[f"prohibited:{item['field']}:{item['phrase']}" for item in aggregate["prohibited"][:5]],
                details={"count": len(aggregate["prohibited"])},
            )
        )
    else:
        gate_records.append(
            build_gate_record(
                gate_id="phase_c_claim_integrity",
                passed=True,
                severity="error",
                reasons=[],
                details={"count": 0},
            )
        )

    if aggregate["unsupported"]:
        gate_records.append(
            build_gate_record(
                gate_id="phase_c_unsupported_claims",
                passed=False,
                severity="warning",
                reasons=[f"unsupported:{item['field']}:{item['phrase']}" for item in aggregate["unsupported"][:5]],
                details={"count": len(aggregate["unsupported"])},
            )
        )

    hook_engine_scores = ((content.get("phase2_creative_stack") or {}).get("hook_engine") or {}).get("component_scores") or {}
    if hook_engine_scores:
        hook_engine_scores = dict(hook_engine_scores)
        hook_engine_scores.setdefault("total", sum(v for v in hook_engine_scores.values() if isinstance(v, (int, float))))
    visual_prompt = ""
    visual_plan = content.get("visual_plan") if isinstance(content.get("visual_plan"), dict) else {}
    if isinstance(visual_plan, dict):
        visual_prompt = str(visual_plan.get("gemini_image_prompt", "") or "")
    caption_for_score = str(content.get("fb_caption", "") or content.get("ig_caption", "") or content.get("li_text", "") or "")
    hook_text = str(content.get("selected_hook", "") or "")
    cta_text = str(content.get("selected_cta", "") or "")

    try:
        cqs_result = _cqs_mod.score(
            caption=caption_for_score,
            hook=hook_text,
            cta=cta_text,
            brief_dict=brief,
            hook_scores=hook_engine_scores or None,
            visual_prompt=visual_prompt,
            recent_captions=[],
            verified_facts=verified_facts,
        )
    except Exception as _cqs_err:
        cqs_result = {"total": 0.0, "component_scores": {}, "band": "improve", "error": str(_cqs_err)}

    content["conversion_quality_score"] = cqs_result
    total = float(cqs_result.get("total") or 0.0)
    band = str(cqs_result.get("band") or "")
    if total < 60.0:
        gate_records.append(
            build_gate_record(
                gate_id="phase_c_cqs_score",
                passed=False,
                severity="error",
                reasons=[f"cqs_below_minimum:{total}"],
                details={"total": total, "band": band, "minimum": 60.0},
            )
        )
    elif total < 80.0:
        gate_records.append(
            build_gate_record(
                gate_id="phase_c_cqs_score",
                passed=False,
                severity="warning",
                reasons=[f"cqs_below_target:{total}"],
                details={"total": total, "band": band, "target": 80.0},
            )
        )
    else:
        gate_records.append(
            build_gate_record(
                gate_id="phase_c_cqs_score",
                passed=True,
                severity="warning",
                reasons=[],
                details={"total": total, "band": band},
            )
        )

    return content


def _run_phase_d_brief_adherence(content: dict, run_context: dict, gate_records: list[dict]) -> dict:
    """Phase D - verify captions actually honour the StrategicBrief's persuasion block.

    Measures presence of: objection reframe, transformation from->to, proof anchor,
    and must-include phrases from downstream_instructions. Emits an adherence
    percentage as a warning gate; no text mutation.
    """
    brief = run_context.get("strategic_brief") if isinstance(run_context.get("strategic_brief"), dict) else {}
    strategist = run_context.get("conversion_strategist") if isinstance(run_context.get("conversion_strategist"), dict) else {}
    brief = brief or {}
    strategist = strategist or {}
    if not brief:
        gate_records.append(
            build_gate_record(
                gate_id="phase_d_brief_adherence",
                passed=True,
                severity="warning",
                reasons=[],
                details={"enabled": False, "reason": "no_brief"},
            )
        )
        return content

    persuasion = brief.get("persuasion", {}) if isinstance(brief.get("persuasion"), dict) else {}
    downstream = strategist.get("downstream_instructions", {}) if isinstance(strategist.get("downstream_instructions"), dict) else {}

    combined_text = " ".join(
        str(content.get(k, "") or "")
        for k in ("wp_title", "wp_content", "wp_excerpt", "fb_caption", "ig_caption", "li_text")
    ).lower()

    def _has_signal(signal: str, min_words: int = 2) -> bool:
        signal = (signal or "").lower().strip()
        if not signal:
            return True
        tokens = [t for t in re.findall(r"[a-z]{3,}", signal)]
        if len(tokens) < min_words:
            return any(t in combined_text for t in tokens) if tokens else True
        hits = sum(1 for t in tokens if t in combined_text)
        return hits >= max(2, len(tokens) // 3)

    checks: dict[str, bool] = {}
    checks["objection_present"] = _has_signal(persuasion.get("objection", ""))
    checks["transformation_from_present"] = _has_signal(persuasion.get("transformation_from", ""))
    checks["transformation_to_present"] = _has_signal(persuasion.get("transformation_to", ""))
    checks["proof_present"] = _has_signal(persuasion.get("proof", ""))
    checks["desire_present"] = _has_signal(persuasion.get("desire", ""))
    checks["problem_present"] = _has_signal(persuasion.get("problem", ""))

    must_include = downstream.get("must_include", []) or []
    if isinstance(must_include, list) and must_include:
        included = 0
        for phrase in must_include:
            if _has_signal(str(phrase), min_words=1):
                included += 1
        checks["must_include_ratio"] = included / max(len(must_include), 1)
    else:
        checks["must_include_ratio"] = 1.0

    must_avoid = downstream.get("must_avoid", []) or []
    avoided_violations: list[str] = []
    for phrase in must_avoid:
        p = str(phrase).lower().strip()
        if p and p in combined_text:
            avoided_violations.append(phrase)
    checks["must_avoid_respected"] = len(avoided_violations) == 0

    bool_checks = [k for k, v in checks.items() if isinstance(v, bool)]
    hits = sum(1 for k in bool_checks if checks[k])
    total = max(len(bool_checks), 1)
    adherence_ratio = hits / total
    adherence_pct = round(adherence_ratio * 100.0, 1)

    content["conversion_brief_adherence"] = {
        "adherence_pct": adherence_pct,
        "checks": checks,
        "must_avoid_violations": avoided_violations,
    }

    if avoided_violations:
        gate_records.append(
            build_gate_record(
                gate_id="phase_d_must_avoid_violation",
                passed=False,
                severity="warning",
                reasons=[f"contains_forbidden:{v}" for v in avoided_violations[:5]],
                details={"count": len(avoided_violations)},
            )
        )

    if adherence_pct < 60.0:
        gate_records.append(
            build_gate_record(
                gate_id="phase_d_brief_adherence",
                passed=False,
                severity="warning",
                reasons=[f"low_adherence:{adherence_pct}"],
                details={"adherence_pct": adherence_pct, "checks": checks},
            )
        )
    else:
        gate_records.append(
            build_gate_record(
                gate_id="phase_d_brief_adherence",
                passed=True,
                severity="warning",
                reasons=[],
                details={"adherence_pct": adherence_pct, "checks": checks},
            )
        )

    return content


def _run_phase_f_visual_alignment(content: dict, run_context: dict, gate_records: list[dict]) -> dict:
    """Phase F - verify visual_plan is unified with StrategicBrief.design.

    Scores alignment on four signals: visual_direction embedded in objective,
    template family present, law signal present in gemini_image_prompt, and
    storyboard slides emitted. Attaches summary to content and emits a warning
    gate. No text mutation.
    """
    brief = run_context.get("strategic_brief") if isinstance(run_context.get("strategic_brief"), dict) else {}
    if not brief:
        gate_records.append(
            build_gate_record(
                gate_id="phase_f_visual_alignment",
                passed=True,
                severity="warning",
                reasons=[],
                details={"enabled": False, "reason": "no_brief"},
            )
        )
        return content

    visual_plan = content.get("visual_plan") if isinstance(content.get("visual_plan"), dict) else {}
    alignment = visual_plan.get("strategic_brief_alignment") if isinstance(visual_plan.get("strategic_brief_alignment"), dict) else {}

    checks = {
        "visual_direction_present": bool(alignment.get("visual_direction_present", False)),
        "law_signal_in_prompt": bool(alignment.get("law_signal_in_prompt", False)),
        "template_family_present": bool(alignment.get("template_family")),
        "storyboard_emitted": int(alignment.get("storyboard_slides", 0) or 0) >= 3,
    }
    passed_checks = sum(1 for v in checks.values() if v)
    total = len(checks)
    alignment_pct = round(100.0 * passed_checks / max(total, 1), 1)

    content["conversion_visual_alignment"] = {
        "alignment_pct": alignment_pct,
        "checks": checks,
        "logic_principle": alignment.get("logic_principle", ""),
        "template_family": alignment.get("template_family", ""),
        "storyboard_slides": alignment.get("storyboard_slides", 0),
    }

    if alignment_pct < 50.0:
        gate_records.append(
            build_gate_record(
                gate_id="phase_f_visual_alignment",
                passed=False,
                severity="warning",
                reasons=[f"low_visual_alignment:{alignment_pct}"],
                details={"alignment_pct": alignment_pct, "checks": checks},
            )
        )
    else:
        gate_records.append(
            build_gate_record(
                gate_id="phase_f_visual_alignment",
                passed=True,
                severity="warning",
                reasons=[],
                details={"alignment_pct": alignment_pct, "checks": checks},
            )
        )
    return content


def _apply_control_plane_metadata(content: dict, run_context: dict, gate_records: list[dict]) -> dict:
    global_gate = evaluate_global_gates(gate_records)
    content["agent_control_plane"] = {
        "schema_version": SCHEMAS_VERSION,
        "run_context": run_context,
        "gates": gate_records,
        "global_gate": global_gate,
    }
    # Promote Phase 0 (Conversion Strategist) outputs to top-level fields so
    # dashboard, history, and analytics can trace every post back to its
    # strategic decisions (spec Section 44).
    if isinstance(run_context.get("strategic_brief"), dict):
        content["strategic_brief"] = run_context["strategic_brief"]
    if isinstance(run_context.get("conversion_strategist"), dict):
        content["conversion_strategist"] = run_context["conversion_strategist"]
    if content.get("strategic_brief"):
        content["copy_generation_source"] = content.get("copy_generation_source") or "conversion_strategist_v1"
    # Phase E - expose experiment variables & variant_id on top level for A/B analytics.
    _strategist_out = content.get("conversion_strategist") if isinstance(content.get("conversion_strategist"), dict) else {}
    _exp = _strategist_out.get("experiment") if isinstance(_strategist_out.get("experiment"), dict) else {}
    _brief_out = content.get("strategic_brief") if isinstance(content.get("strategic_brief"), dict) else {}
    _brief_exp = _brief_out.get("experiment") if isinstance(_brief_out.get("experiment"), dict) else {}
    _variant_id = _exp.get("variant_id") or _brief_exp.get("variant_id") or ""
    if _variant_id:
        content["conversion_variant_id"] = _variant_id
        content["experiment_variables"] = _exp.get("variables") or _brief_exp.get("variables") or {}
    blocked = not bool(global_gate.get("passed", False))
    hard_block = str(os.environ.get("ORCHESTRATION_HARD_BLOCK", "false")).strip().lower() in {"1", "true", "yes", "on"}
    content["orchestration_blocked"] = blocked and hard_block
    if blocked:
        for gate in global_gate.get("blocking_failures", []):
            gate_id = str(gate.get("gate_id", "unknown_gate"))
            reasons = gate.get("reasons", [])
            if hard_block and isinstance(reasons, list):
                for reason in reasons:
                    content.setdefault("validation_errors", []).append(f"{gate_id}:{reason}")
            content.setdefault("quality_warnings", []).append(f"orchestration_gate_failed:{gate_id}")
        if blocked and not hard_block:
            content.setdefault("quality_warnings", []).append("orchestration_soft_fail_publish_allowed")
    return content


def _generate_best_of(slot: str, *, funnel_stage_override: str = "", product_id_override: str = "") -> dict:
    """Run both pipelines for the same slot and keep the higher-scoring result."""
    try:
        from score_content import score_content
    except ImportError:  # pragma: no cover
        from scripts.score_content import score_content

    candidates: list[dict] = []
    for forced_mode in ("legacy", "orchestrator"):
        candidate = generate(
            slot,
            funnel_stage_override=funnel_stage_override,
            product_id_override=product_id_override,
            pipeline_override=forced_mode,
        )
        try:
            candidate["quality_score"] = score_content(candidate).get("total", candidate.get("quality_score", 0))
        except Exception:
            candidate.setdefault("quality_score", 0)
        candidate["pipeline_used"] = forced_mode
        candidates.append(candidate)

    candidates.sort(key=lambda c: float(c.get("quality_score") or 0), reverse=True)
    winner = candidates[0]
    winner["pipeline_selected"] = winner.get("pipeline_used")
    winner["pipeline_comparison"] = [
        {"pipeline": c.get("pipeline_used"), "quality_score": c.get("quality_score")} for c in candidates
    ]
    return winner


def generate(
    slot: str,
    *,
    funnel_stage_override: str = "",
    product_id_override: str = "",
    pipeline_override: str = "",
    pillar_override: str = "",
    approved_strategy: dict[str, Any] | None = None,
    revision_feedback: list[str] | None = None,
    no_product: bool = False,
) -> dict:
    mode = _pipeline_mode(pipeline_override)
    platform = os.environ.get("POST_PLATFORMS", "instagram_feed").split(",")[0].strip() or "instagram_feed"
    generation_date = datetime.now(timezone.utc).date()
    consumer_history_payload = load_history()
    consumer_history = consumer_history_payload.get("posts", []) if isinstance(consumer_history_payload, dict) else []
    consumer_root = select_consumer_root(current_date=generation_date, slot=slot, history=consumer_history)
    no_product = no_product or os.environ.get("CONTENT_BUCKET_OVERRIDE", "").strip().lower() == "no_product"
    if consumer_root["moment"]["product_fit"]["mode"] == "none":
        no_product = True
        product_id_override = ""
    weekly_sequence = select_weekly_sequence(slot, now_utc=datetime.now(timezone.utc))
    recurring_series = weekly_sequence.get("series", {}) if isinstance(weekly_sequence.get("series"), dict) else {}
    if no_product:
        recurring_series = {}
    elif recurring_series.get("product_required") and not product_id_override:
        series_product = _pick_series_product(load_products(), load_history())
        if series_product:
            product_id_override = str(series_product.get("id") or series_product.get("sku") or "")

    if mode == "best_of":
        return _generate_best_of(slot, funnel_stage_override=funnel_stage_override, product_id_override=product_id_override)
    if mode == "orchestrator":
        return _route_generate_orchestrator(
            slot,
            platform=platform,
            product_id_override=product_id_override,
            funnel_stage_override=funnel_stage_override,
            approved_strategy=approved_strategy,
            revision_feedback=revision_feedback,
            no_product=no_product,
            preferred_pillar=pillar_override or None,
            recurring_series=recurring_series,
            consumer_root=consumer_root,
        )
    if mode != "legacy" and _social_intelligence_enabled():
        return _route_generate_orchestrator(
            slot,
            platform=platform,
            product_id_override=product_id_override,
            funnel_stage_override=funnel_stage_override,
            approved_strategy=approved_strategy,
            revision_feedback=revision_feedback,
            no_product=no_product,
            preferred_pillar=pillar_override or None,
            recurring_series=recurring_series,
            consumer_root=consumer_root,
        )

    ensure_runtime_data()
    preferred_model = os.environ.get("GEMINI_MODEL", "").strip()
    model_candidates = [
        preferred_model,
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]
    model_candidates = [m for m in model_candidates if m]
    preferred_visual_director_model = os.environ.get("GEMINI_VISUAL_DIRECTOR_MODEL", "").strip()
    visual_director_candidates = [
        preferred_visual_director_model,
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ]
    visual_director_candidates = [m for m in visual_director_candidates if m]

    queue = load_topic_queue()
    history = load_history()
    funnel_config = load_funnel_config()
    channel_schedule = load_channel_schedule()
    cta_library = load_cta_library()
    now_for_slot = datetime.now(timezone.utc)
    funnel_stage = _normalize_funnel_stage_override(funnel_stage_override) or stage_for_slot(
        slot,
        history=history,
        funnel_config=funnel_config,
        schedule=channel_schedule,
        now_utc=now_for_slot,
    )
    preview_stage = funnel_stage
    products = load_products()
    business_profile = _build_business_profile(products)
    forced_product = _pick_product_by_id(products, product_id_override) if product_id_override else None
    if forced_product:
        # Operator explicitly requested this product (e.g. dashboard override) — always honor it.
        product = forced_product
        preferred_pillars = _preferred_pillars_for_stage(preview_stage)
        pillar, topic, topic_hash = _pick_topic_for_product(
            queue,
            history,
            product,
            preview_stage,
            preferred_pillars=preferred_pillars,
        )
        content_bucket = "product_education"
        want_product = True
    elif recurring_series.get("product_required"):
        product = _pick_series_product(products, history)
        preferred_pillars = _preferred_pillars_for_stage(preview_stage)
        pillar, topic, topic_hash = _pick_topic_for_product(
            queue,
            history,
            product,
            preview_stage,
            preferred_pillars=preferred_pillars,
        )
        content_bucket = "product_education"
        want_product = True
    else:
        editorial_plan = select_editorial_plan(
            queue,
            history,
            products,
            preview_stage,
            no_product=no_product,
            pillar_override=pillar_override,
        )
        product = editorial_plan["product"]
        pillar = editorial_plan["pillar"]
        topic = editorial_plan["topic"]
        topic_hash = editorial_plan["topic_hash"]
        content_bucket = editorial_plan["content_bucket"]
        want_product = editorial_plan["want_product"]

    marketing_strategy = _load_latest_marketing_strategy()
    brand_profile = load_brand_profile()
    selling_ideology = load_selling_ideology()
    structured_campaign = _load_latest_structured_campaign()
    stage_meta = funnel_config.get("stages", {}).get(funnel_stage, {}) if isinstance(funnel_config, dict) else {}

    hook_window = int(os.environ.get("ANTI_REPEAT_HOOK_WINDOW", "30"))
    cta_window = int(os.environ.get("ANTI_REPEAT_CTA_WINDOW", "30"))
    recent_hook_hashes = {
        str(p.get("hook_hash", ""))
        for p in history.get("posts", [])[-hook_window:]
        if isinstance(p, dict)
    }
    recent_cta_hashes = {
        str(p.get("cta_hash", ""))
        for p in history.get("posts", [])[-cta_window:]
        if isinstance(p, dict)
    }

    product_name = product.get("name", "") if product else ""
    product_id = product.get("id", "") if product else ""
    product_sku = product.get("sku", "") if product else ""
    product_price = product.get("price", "") if product else ""
    product_sale_price = product.get("sale_price", "") if product else ""
    product_metrics = ", ".join(product.get("metrics", [])[:5]) if product else ""
    product_categories = ", ".join(product.get("categories", [])[:3]) if product else ""
    product_facts = product.get("fact_snippet", "") if product else ""
    product_in_stock = product.get("in_stock", "") if product else ""
    product_stock = product.get("stock", "") if product else ""
    product_url = product.get("product_url", "") if product else ""
    talking_point = (
        _build_talking_point(topic=topic, funnel_stage=funnel_stage, product=product)
        if want_product
        else _build_talking_point_no_product(topic=topic, funnel_stage=funnel_stage, pillar=pillar)
    )
    consumer_moment = consumer_root["moment"]
    talking_point["pain_point"] = consumer_moment["friction"]
    talking_point["angle"] = consumer_moment["useful_discovery"]
    talking_point["first_step"] = consumer_moment["immediate_action"]
    talking_point["scenario"] = (
        f"{consumer_moment['person']} in {consumer_moment['setting']} while "
        f"{consumer_moment['activity'].lower()}"
    )

    marketing_context = ""
    selected_hook = (weekly_sequence.get("hook") or "").strip()
    selected_cta = (weekly_sequence.get("primary_cta") or "").strip()
    audience_segment = (weekly_sequence.get("segment") or structured_campaign.get("audience_segment") or "Prepared Buyer").strip()
    consumer_context = _product_consumer_context(product, audience_segment) if want_product else {}
    selected_consumer_persona = consumer_context.get("persona") if isinstance(consumer_context.get("persona"), dict) else {}
    if selected_consumer_persona:
        product["consumer_profile"] = consumer_context.get("profile", {})
        product["selected_consumer_persona"] = selected_consumer_persona
    consumer_profile_context = (
        "MANDATORY CONSUMER-LIFE ROOT:\n"
        "- Build the post around this complete moment before product, copy form, or visual treatment.\n"
        f"- Consumer world: {json.dumps(consumer_root['world'], sort_keys=True)}\n"
        f"- Reviewed moment: {json.dumps(consumer_moment, sort_keys=True)}\n"
        f"- Required receipt: {json.dumps(consumer_root['consumer_receipt'], sort_keys=True)}\n"
        "- Deliver the useful discovery and immediate action; do not turn this into a generic technical lesson.\n"
        + _product_consumer_prompt_context(consumer_context)
    )
    campaign_id = str(structured_campaign.get("campaign_id", "")).strip()
    destination_url = str(structured_campaign.get("destination_url", SITE_URL)).strip() or SITE_URL

    brand_name = str(brand_profile.get("brand_name") or "Infenergy Power").strip() or "Infenergy Power"
    brand_positioning = str(brand_profile.get("positioning") or INFENERGY_BUSINESS_GOALS["positioning"]).strip()
    brand_mission = str(brand_profile.get("mission") or "").strip()
    brand_personality_name = str(brand_profile.get("personality_name") or "Calm Strength").strip() or "Calm Strength"
    brand_personality_traits = _dedupe_str_list(brand_profile.get("personality_traits", []))
    brand_tone_rules = _dedupe_str_list(brand_profile.get("tone_rules", []))
    brand_voice_rules_db = _dedupe_str_list(brand_profile.get("voice_rules", []))
    brand_approved_phrases = _dedupe_str_list(brand_profile.get("approved_phrases", []))
    brand_words_to_use = _dedupe_str_list(brand_profile.get("words_to_use", []))
    brand_words_to_avoid = _dedupe_str_list(brand_profile.get("words_to_avoid", []))
    brand_forbidden_phrases = _dedupe_str_list(brand_profile.get("forbidden_phrases", []))
    brand_core_values = _dedupe_str_list(brand_profile.get("core_values", []))
    brand_trust_close = str(brand_profile.get("trust_close") or "").strip()
    brand_audience_summary = str(brand_profile.get("audience_summary") or "").strip()

    ideology_framework_mode = str(selling_ideology.get("framework_mode") or "mixed_with_campaign_overlay").strip()
    ideology_primary_conversion = str(selling_ideology.get("primary_conversion") or "direct_checkout").strip()
    ideology_tone_blend = str(selling_ideology.get("tone_blend") or "calm_protective_plus_assertive_urgent").strip()
    ideology_value_lens = str(selling_ideology.get("value_lens") or "benefit_first").strip()
    ideology_message_filter = str(selling_ideology.get("message_filter") or "no_nonconverting_copy").strip()
    ideology_cta_mode = str(selling_ideology.get("cta_mode") or "checkout_first").strip()
    ideology_proof_rule = str(selling_ideology.get("proof_rule") or "scenario_plus_spec_evidence").strip()
    ideology_core_promise = str(selling_ideology.get("core_promise") or "reliable_peace_of_mind").strip()
    ideology_banned_phrases = _dedupe_str_list(selling_ideology.get("banned_phrases", []))
    ideology_pillars = _dedupe_str_list(selling_ideology.get("pillar_messages", []))

    preferred_hooks = []
    if selected_hook:
        preferred_hooks.append(selected_hook)

    if marketing_strategy:
        founder_manifesto = marketing_strategy.get("brand_profile", {}).get("founder_manifesto", {})
        if not isinstance(founder_manifesto, dict):
            founder_manifesto = {}
        approved_sales = founder_manifesto.get("approved_sales_verbiage", {}) if isinstance(founder_manifesto.get("approved_sales_verbiage", {}), dict) else {}
        value_narrative = marketing_strategy.get("copy", {}).get("value_narrative", {})
        if not isinstance(value_narrative, dict):
            value_narrative = {}
        voice_rules = marketing_strategy.get("voice", {}).get("voice_rules", [])
        segments = marketing_strategy.get("audience", {}).get("segments", [])
        core_offers = marketing_strategy.get("offer", {}).get("core_offers", [])
        social_hooks = marketing_strategy.get("copy", {}).get("social_hooks", [])
        cta_bank = marketing_strategy.get("copy", {}).get("cta_bank", [])
        preferred_hooks.extend([h for h in social_hooks if isinstance(h, str)])
        combined_voice_rules = _dedupe_str_list((voice_rules if isinstance(voice_rules, list) else []) + brand_tone_rules + brand_voice_rules_db)
        combined_approved_phrases = _dedupe_str_list(brand_approved_phrases + (approved_sales.get("core_phrases", []) if isinstance(approved_sales.get("core_phrases", []), list) else []))
        selected_cta = choose_cta_for_stage(
            stage=funnel_stage,
            preferred=selected_cta or (cta_bank[0] if cta_bank else ""),
            cta_library=cta_library,
            recent_cta_hashes=recent_cta_hashes,
        )

        marketing_context = (
            "MARKETING TEAM DIRECTIVES:\n"
            f"- Brand name: {brand_name}\n"
            f"- Voice rules: {combined_voice_rules[:7]}\n"
            f"- Priority audience segments: {segments[:3]}\n"
            f"- Audience summary: {brand_audience_summary}\n"
            f"- Core offers: {core_offers[:4]}\n"
            f"- Proven hook styles: {social_hooks[:3]}\n"
            f"- Preferred CTAs: {cta_bank[:3]}\n"
            f"- Founder mission: {brand_mission or founder_manifesto.get('mission', '')}\n"
            f"- Brand values: {brand_core_values or founder_manifesto.get('core_values', [])}\n"
            f"- Brand personality: {brand_personality_name} | {brand_personality_traits}\n"
            f"- Approved sales language: {combined_approved_phrases[:8]}\n"
            f"- Words to emphasize: {brand_words_to_use[:10]}\n"
            f"- Words/phrases to avoid: {brand_words_to_avoid[:10]}\n"
            f"- Value narrative: {value_narrative}\n"
            f"- This slot hook direction: {selected_hook}\n"
            f"- This slot CTA direction: {selected_cta}\n"
            f"- Funnel stage: {funnel_stage}\n"
            f"- Funnel objective: {stage_meta.get('objective', '')}\n"
            "Use this as strategy guidance while still tailoring to the specific topic and product.\n"
        )
    else:
        preferred_hooks.extend([f"Most people misunderstand {topic.lower()}", "The hidden cost most buyers miss"])
        selected_cta = choose_cta_for_stage(
            stage=funnel_stage,
            preferred=selected_cta,
            cta_library=cta_library,
            recent_cta_hashes=recent_cta_hashes,
        )

    selected_cta = _ensure_explicit_cta(selected_cta, funnel_stage)
    recent_hooks_for_diversity = _recent_unique_values(history, "selected_hook", limit=8)

    hook_choice = select_hook(
        topic=topic,
        product_name=product_name or "INF Energy Power solution",
        audience_segment=audience_segment,
        recent_hook_hashes=recent_hook_hashes,
        recent_hooks=recent_hooks_for_diversity,
        preferred_hooks=preferred_hooks,
    )
    selected_hook = str(hook_choice.get("hook", selected_hook)).strip() or selected_hook
    hook_type = str(hook_choice.get("hook_type", "question")).strip() or "question"

    slot_guidance = {
        "morning": (
            "MORNING — EDUCATION. Open with a surprising or counterintuitive fact. "
            "Teach one genuinely useful concept the reader can act on today. "
            "Use real numbers, comparisons, or analogies. No fluff. End with a thought-provoking question."
        ),
        "midday": (
            "MIDDAY — PROOF. Lead with a specific, believable result. "
            "Include at least one concrete number (dollar amount, percentage, timeframe). "
            "Tell a mini-story: situation → problem → solution → outcome. "
            "Make the reader feel 'that could be me.' End with a credibility statement."
        ),
        "evening": (
            "EVENING — CTA. Create genuine urgency around a real reason to act now "
            "(limited slots, seasonal incentives, rising utility rates). "
            "Be direct and specific about the next step. Tell them exactly what happens when they reach out. "
            "One clear CTA only. No vague 'learn more.'"
        ),
    }.get(slot, "educational")

    recent_hooks = _recent_unique_values(history, "selected_hook", limit=8)
    recent_products = _recent_unique_values(history, "product_name", limit=6)
    recent_ctas = _recent_unique_values(history, "selected_cta", limit=8)
    recent_topics = _recent_unique_values(history, "topic", limit=8)
    recent_laws = _recent_unique_values(history, "logic_principle", limit=6)
    recent_structures = _recent_unique_values(history, "copy_framework", limit=6)

    # ------------------------------------------------------------------
    # PHASE 0 - Conversion Strategist (spec Sections 42, 19, 44)
    # Builds the StrategicBrief that owns awareness stage, logic law,
    # emotional driver, copy structure, objection, transformation, and CTA.
    # Every downstream phase must respect this brief.
    # ------------------------------------------------------------------
    _strategist_product = {
        "product_id": product.get("id", "") if product else "",
        "product_name": product.get("name", "") if product else "",
        "product_type": (product.get("categories", [""])[0] if product and product.get("categories") else ""),
        "features": (product.get("features") if product and product.get("features") else (product.get("metrics", []) if product else [])),
        "mechanisms": [],
        "benefits": (product.get("benefits", []) if product else []),
        "verified_facts": (product.get("metrics", []) if product else []),
        "landing_page_url": (product.get("product_url") if product and product.get("product_url") else SITE_URL),
    }
    if funnel_stage == "CONVERSION" and want_product:
        _campaign_goal = "purchase"
    elif funnel_stage in ("DESIRE", "TRUST"):
        _campaign_goal = "consideration"
    else:
        _campaign_goal = "awareness"
    try:
        _strategist_output = conversion_strategist.plan(
            funnel_stage=funnel_stage,
            product=_strategist_product,
            audience_hint=consumer_moment["person"],
            campaign_goal=_campaign_goal,
            platform_priority=["facebook", "instagram", "linkedin"],
            recent={
                "hooks": recent_hooks,
                "ctas": recent_ctas,
                "topics": recent_topics,
                "laws": recent_laws,
                "structures": recent_structures,
            },
            explicit={
                "problem": consumer_moment["friction"],
                "desire": consumer_moment["useful_discovery"],
                "cta": consumer_moment["immediate_action"],
            },
            data_dir=DATA_DIR,
        )
    except Exception as _strategist_err:  # pragma: no cover - never block generation
        _strategist_output = {
            "agent": "conversion_strategist",
            "error": str(_strategist_err),
            "brief": None,
        }

    run_context = build_run_context(
        slot=slot,
        topic=topic,
        funnel_stage=funnel_stage,
        stage_objective=str(stage_meta.get("objective", "")),
        audience_segment=audience_segment,
        campaign_id=campaign_id,
        destination_url=destination_url,
        product_id=product_id,
        product_name=product_name,
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        recent_hooks=recent_hooks,
        recent_topics=recent_topics,
        recent_ctas=recent_ctas,
    )
    run_context["consumer_root"] = consumer_root
    run_context["consumer_receipt"] = consumer_root["consumer_receipt"]
    if isinstance(_strategist_output, dict):
        run_context["strategic_brief"] = _strategist_output.get("brief")
        run_context["conversion_strategist"] = {
            "summary": _strategist_output.get("summary", ""),
            "hook_targets": _strategist_output.get("hook_targets", {}),
            "structure_beats": _strategist_output.get("structure_beats", []),
            "law_narrative_template": _strategist_output.get("law_narrative_template", []),
            "law_guardrails": _strategist_output.get("law_guardrails", []),
            "objection_reframe": _strategist_output.get("objection_reframe", {}),
            "downstream_instructions": _strategist_output.get("downstream_instructions", {}),
            "experiment": _strategist_output.get("experiment", {}),
            "winning_hints_applied": _strategist_output.get("winning_hints_applied", {}),
            "losing_hints_applied": _strategist_output.get("losing_hints_applied", {}),
            "error": _strategist_output.get("error"),
        }
    human_connection = _compile_human_connection_context(
        audience_segment=audience_segment,
        topic=topic,
        selected_hook=selected_hook,
        funnel_stage=funnel_stage,
    )
    run_context["human_connection"] = human_connection
    gate_records: list[dict] = []
    if isinstance(_strategist_output, dict) and _strategist_output.get("brief"):
        gate_records.append(
            build_gate_record(
                gate_id="phase0_conversion_strategist",
                passed=True,
                severity="warning",
                reasons=[],
                details={
                    "brief_id": _strategist_output["brief"].get("brief_id", ""),
                    "summary": _strategist_output.get("summary", ""),
                },
            )
        )
    else:
        gate_records.append(
            build_gate_record(
                gate_id="phase0_conversion_strategist",
                passed=False,
                severity="warning",
                reasons=[_strategist_output.get("error", "brief_not_produced")] if isinstance(_strategist_output, dict) else ["brief_not_produced"],
                details={},
            )
        )
    conference_model = os.environ.get("GEMINI_CONFERENCE_MODEL", "").strip()

    phase2_enabled = os.environ.get("ENABLE_PHASE2_CREATIVE_STACK", "true").strip().lower() not in {"0", "false", "no"}
    phase2_stack = _default_phase2_stack(
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        audience_segment=audience_segment,
        topic=topic,
        funnel_stage=funnel_stage,
    )
    if phase2_enabled:
        phase2_candidates = [preferred_model, conference_model if "conference_model" in locals() else "", "gemini-2.5-pro", "gemini-2.5-flash"]
        phase2_candidates = [m for m in phase2_candidates if m]
        phase2_raw = _run_phase2_creative_stack(
            phase2_candidates,
            topic=topic,
            funnel_stage=funnel_stage,
            stage_objective=str(stage_meta.get("objective", "")),
            selected_hook=selected_hook,
            selected_cta=selected_cta,
            audience_segment=audience_segment,
            product_name=product_name,
            product_categories=product_categories,
            product_metrics=product_metrics,
            recent_hooks=recent_hooks,
            recent_topics=recent_topics,
        )
        validated_ideation, ideation_errors = validate_agent_output("ideation_divergence", phase2_raw.get("ideation_divergence", {}))
        validated_psycho, psycho_errors = validate_agent_output("audience_psychographics", phase2_raw.get("audience_psychographics", {}))
        validated_narrative, narrative_errors = validate_agent_output("narrative_architect", phase2_raw.get("narrative_architect", {}))
        validated_voice, voice_errors = validate_agent_output("platform_voice_calibrator", phase2_raw.get("platform_voice_calibrator", {}))
        validated_hook_test, hook_test_errors = validate_agent_output("hook_stress_test", phase2_raw.get("hook_stress_test", {}))

        gate_records.append(build_gate_record(gate_id="phase2_ideation_divergence_schema", passed=len(ideation_errors) == 0, severity="error", reasons=ideation_errors, details={"enabled": True}))
        gate_records.append(build_gate_record(gate_id="phase2_audience_psychographics_schema", passed=len(psycho_errors) == 0, severity="error", reasons=psycho_errors, details={"enabled": True}))
        gate_records.append(build_gate_record(gate_id="phase2_narrative_architect_schema", passed=len(narrative_errors) == 0, severity="error", reasons=narrative_errors, details={"enabled": True}))
        gate_records.append(build_gate_record(gate_id="phase2_platform_voice_calibrator_schema", passed=len(voice_errors) == 0, severity="error", reasons=voice_errors, details={"enabled": True}))
        gate_records.append(build_gate_record(gate_id="phase2_hook_stress_test_schema", passed=len(hook_test_errors) == 0, severity="error", reasons=hook_test_errors, details={"enabled": True}))

        if not ideation_errors:
            phase2_stack["ideation_divergence"] = validated_ideation
        if not psycho_errors:
            phase2_stack["audience_psychographics"] = validated_psycho
        if not narrative_errors:
            phase2_stack["narrative_architect"] = validated_narrative
        if not voice_errors:
            phase2_stack["platform_voice_calibrator"] = validated_voice
        if not hook_test_errors:
            phase2_stack["hook_stress_test"] = validated_hook_test
    else:
        gate_records.append(build_gate_record(gate_id="phase2_ideation_divergence_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))
        gate_records.append(build_gate_record(gate_id="phase2_audience_psychographics_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))
        gate_records.append(build_gate_record(gate_id="phase2_narrative_architect_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))
        gate_records.append(build_gate_record(gate_id="phase2_platform_voice_calibrator_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))
        gate_records.append(build_gate_record(gate_id="phase2_hook_stress_test_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))

    selected_hook = str(phase2_stack.get("hook_stress_test", {}).get("recommended_hook", "")).strip() or selected_hook
    selected_cta = str(phase2_stack.get("audience_psychographics", {}).get("cta_framing", "")).strip() or selected_cta
    audience_segment = str(phase2_stack.get("audience_psychographics", {}).get("primary_segment", "")).strip() or audience_segment

    # Phase B: re-rank hook candidates with the conversion hook_engine (spec Section 22).
    try:
        from conversion import hook_engine as _hook_engine
        from conversion import personas as _personas_mod

        _hook_candidates: list[str] = []
        for _concept in (phase2_stack.get("ideation_divergence", {}) or {}).get("concepts", []) or []:
            _hc = str((_concept or {}).get("hook_candidate", "")).strip()
            if _hc:
                _hook_candidates.append(_hc)
        for _hc in (phase2_stack.get("hook_stress_test", {}) or {}).get("candidate_hooks", []) or []:
            _hc = str(_hc).strip()
            if _hc:
                _hook_candidates.append(_hc)
        _stress_rec = str((phase2_stack.get("hook_stress_test", {}) or {}).get("recommended_hook", "")).strip()
        if _stress_rec:
            _hook_candidates.append(_stress_rec)
        if selected_hook:
            _hook_candidates.append(selected_hook)
        # De-dup while preserving order
        _seen: set[str] = set()
        _dedup: list[str] = []
        for _c in _hook_candidates:
            if _c and _c not in _seen:
                _seen.add(_c)
                _dedup.append(_c)
        _hook_candidates = _dedup
        _brief_for_hook = (run_context.get("strategic_brief") or {}) if isinstance(run_context.get("strategic_brief"), dict) else {}
        _audience_id = str(_brief_for_hook.get("audience_id", "")).strip()
        _audience_kw = _personas_mod.audience_keywords(_audience_id) if _audience_id else []
        _awareness_stage = str(_brief_for_hook.get("awareness_stage", "")).strip()
        _winner_hook, _winner_scores, _all_scored = _hook_engine.pick_best(
            _hook_candidates,
            audience_keywords=_audience_kw,
            product_name=product_name or None,
            recent_hooks=recent_hooks,
            platform="facebook",
            min_total=0.0,
        )
        if _winner_hook:
            if _winner_hook != selected_hook:
                if isinstance(phase2_stack.get("hook_stress_test", {}), dict):
                    phase2_stack["hook_stress_test"]["recommended_hook"] = _winner_hook
                    phase2_stack["hook_stress_test"]["hook_engine_score"] = _winner_scores.get("total", 0)
                selected_hook = _winner_hook
            phase2_stack["hook_engine"] = {
                "selected_hook": selected_hook,
                "total_score": _winner_scores.get("total", 0),
                "component_scores": {k: v for k, v in _winner_scores.items() if k != "total"},
                "audience_keywords": _audience_kw,
                "awareness_stage": _awareness_stage,
                "candidate_count": len(_hook_candidates),
                "top_alternates": [
                    {"hook": item["hook"], "total": item["scores"].get("total", 0)}
                    for item in (_all_scored[1:4] if len(_all_scored) > 1 else [])
                ],
            }
            gate_records.append(
                build_gate_record(
                    gate_id="phase2_hook_engine_rank",
                    passed=True,
                    severity="warning",
                    reasons=[],
                    details={
                        "total_score": _winner_scores.get("total", 0),
                        "candidates": len(_hook_candidates),
                        "awareness_stage": _awareness_stage,
                    },
                )
            )
        else:
            gate_records.append(
                build_gate_record(
                    gate_id="phase2_hook_engine_rank",
                    passed=False,
                    severity="warning",
                    reasons=["no_candidates_scored"],
                    details={"candidates": len(_hook_candidates)},
                )
            )
    except Exception as _hook_engine_err:  # pragma: no cover - never block generation
        gate_records.append(
            build_gate_record(
                gate_id="phase2_hook_engine_rank",
                passed=False,
                severity="warning",
                reasons=[str(_hook_engine_err)],
                details={},
            )
        )

    diversified_hook, diversity_note = _enforce_hook_diversity(selected_hook, phase2_stack, recent_hooks)
    if diversified_hook != selected_hook:
        selected_hook = diversified_hook
        if isinstance(phase2_stack.get("hook_stress_test", {}), dict):
            phase2_stack["hook_stress_test"]["recommended_hook"] = selected_hook
            reason = str(phase2_stack["hook_stress_test"].get("reason", "")).strip()
            phase2_stack["hook_stress_test"]["reason"] = (f"{reason} {diversity_note}").strip()
        gate_records.append(
            build_gate_record(
                gate_id="phase2_hook_family_diversity",
                passed=True,
                severity="warning",
                reasons=[],
                details={"note": diversity_note},
            )
        )
    run_context["draft_direction"]["selected_hook"] = selected_hook
    run_context["draft_direction"]["selected_cta"] = selected_cta
    run_context["audience_segment"] = audience_segment

    conference_candidates = [conference_model, preferred_visual_director_model, preferred_model, "gemini-2.5-pro", "gemini-2.5-flash"]
    conference_candidates = [m for m in conference_candidates if m]

    phase3_enabled = os.environ.get("ENABLE_PHASE3_SAFETY_STACK", "true").strip().lower() not in {"0", "false", "no"}
    phase3_stack = _default_phase3_safety_stack(
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        recent_topics=recent_topics,
    )
    if phase3_enabled:
        phase3_candidates = [preferred_model, conference_model, "gemini-2.5-pro", "gemini-2.5-flash"]
        phase3_candidates = [m for m in phase3_candidates if m]
        preview_for_safety = (
            f"Topic: {topic}\n"
            f"Hook: {selected_hook}\n"
            f"CTA: {selected_cta}\n"
            f"Funnel stage: {funnel_stage}\n"
            f"Objective: {stage_meta.get('objective', '')}\n"
        )
        phase3_raw = _run_phase3_safety_stack(
            phase3_candidates,
            topic=topic,
            funnel_stage=funnel_stage,
            selected_hook=selected_hook,
            selected_cta=selected_cta,
            recent_hooks=recent_hooks,
            recent_topics=recent_topics,
            content_preview=preview_for_safety,
        )
        precision, precision_errors = validate_agent_output("precision_claims_verifier", phase3_raw.get("precision_claims_verifier", {}))
        compliance, compliance_errors = validate_agent_output("compliance_policy_sentinel", phase3_raw.get("compliance_policy_sentinel", {}))
        novelty, novelty_errors = validate_agent_output("semantic_novelty", phase3_raw.get("semantic_novelty", {}))

        gate_records.append(build_gate_record(gate_id="phase3_precision_claims_schema", passed=len(precision_errors) == 0, severity="error", reasons=precision_errors, details={"enabled": True}))
        gate_records.append(build_gate_record(gate_id="phase3_compliance_sentinel_schema", passed=len(compliance_errors) == 0, severity="error", reasons=compliance_errors, details={"enabled": True}))
        gate_records.append(build_gate_record(gate_id="phase3_semantic_novelty_schema", passed=len(novelty_errors) == 0, severity="error", reasons=novelty_errors, details={"enabled": True}))

        if not precision_errors:
            phase3_stack["precision_claims_verifier"] = precision
        if not compliance_errors:
            phase3_stack["compliance_policy_sentinel"] = compliance
        if not novelty_errors:
            phase3_stack["semantic_novelty"] = novelty

        if not bool(phase3_stack.get("precision_claims_verifier", {}).get("passed", True)):
            gate_records.append(
                build_gate_record(
                    gate_id="phase3_precision_claims_pass",
                    passed=False,
                    severity="error",
                    reasons=list(phase3_stack.get("precision_claims_verifier", {}).get("issues", [])) or ["precision_claims_failed"],
                    details={},
                )
            )

        risk_level = str(phase3_stack.get("compliance_policy_sentinel", {}).get("risk_level", "low")).strip().lower()
        if risk_level == "high":
            gate_records.append(
                build_gate_record(
                    gate_id="phase3_compliance_risk",
                    passed=False,
                    severity="error",
                    reasons=list(phase3_stack.get("compliance_policy_sentinel", {}).get("required_actions", [])) or ["high_compliance_risk"],
                    details={"risk_level": risk_level},
                )
            )

        novelty_min = float(os.environ.get("NOVELTY_MIN_SCORE", "0.62"))
        novelty_score = float(phase3_stack.get("semantic_novelty", {}).get("novelty_score", 1.0) or 1.0)
        if novelty_score < novelty_min:
            gate_records.append(
                build_gate_record(
                    gate_id="phase3_semantic_novelty_min",
                    passed=False,
                    severity="error",
                    reasons=list(phase3_stack.get("semantic_novelty", {}).get("rewrite_guidance", [])) or ["low_semantic_novelty"],
                    details={"novelty_score": novelty_score, "min": novelty_min},
                )
            )
    else:
        gate_records.append(build_gate_record(gate_id="phase3_precision_claims_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))
        gate_records.append(build_gate_record(gate_id="phase3_compliance_sentinel_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))
        gate_records.append(build_gate_record(gate_id="phase3_semantic_novelty_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))

    phase4_enabled = os.environ.get("ENABLE_PHASE4_OPTIMIZATION_STACK", "true").strip().lower() not in {"0", "false", "no"}
    phase4_stack = _default_phase4_optimization_stack(
        selected_cta=selected_cta,
        funnel_stage=funnel_stage,
    )
    if phase4_enabled:
        phase4_candidates = [preferred_model, conference_model, preferred_visual_director_model, "gemini-2.5-pro", "gemini-2.5-flash"]
        phase4_candidates = [m for m in phase4_candidates if m]
        phase4_raw = _run_phase4_optimization_stack(
            phase4_candidates,
            topic=topic,
            funnel_stage=funnel_stage,
            selected_hook=selected_hook,
            selected_cta=selected_cta,
            audience_segment=audience_segment,
        )
        visual_opt, visual_opt_errors = validate_agent_output("visual_strategy", phase4_raw.get("visual_strategy", {}))
        cta_opt, cta_opt_errors = validate_agent_output("cta_optimization", phase4_raw.get("cta_optimization", {}))
        gate_records.append(build_gate_record(gate_id="phase4_visual_strategy_schema", passed=len(visual_opt_errors) == 0, severity="error", reasons=visual_opt_errors, details={"enabled": True}))
        gate_records.append(build_gate_record(gate_id="phase4_cta_optimization_schema", passed=len(cta_opt_errors) == 0, severity="error", reasons=cta_opt_errors, details={"enabled": True}))
        if not visual_opt_errors:
            phase4_stack["visual_strategy"] = visual_opt
        if not cta_opt_errors:
            phase4_stack["cta_optimization"] = cta_opt
    else:
        gate_records.append(build_gate_record(gate_id="phase4_visual_strategy_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))
        gate_records.append(build_gate_record(gate_id="phase4_cta_optimization_schema", passed=True, severity="warning", reasons=[], details={"enabled": False}))

    selected_cta = str(phase4_stack.get("cta_optimization", {}).get("recommended_cta", "")).strip() or selected_cta
    selected_cta = _ensure_explicit_cta(selected_cta, funnel_stage)
    if isinstance(phase4_stack.get("cta_optimization", {}), dict):
        phase4_stack["cta_optimization"]["recommended_cta"] = selected_cta
    run_context["draft_direction"]["selected_cta"] = selected_cta

    pregen_enabled = os.environ.get("ENABLE_PREGEN_CONFERENCE", "true").strip().lower() not in {"0", "false", "no"}
    pre_generation_conference = {}
    if pregen_enabled:
        pre_generation_conference_raw = _run_pre_generation_conference(
            conference_candidates,
            topic=topic,
            funnel_stage=funnel_stage,
            stage_objective=str(stage_meta.get("objective", "")),
            selected_hook=selected_hook,
            selected_cta=selected_cta,
            product_name=product_name,
            product_categories=product_categories,
            product_metrics=product_metrics,
            recent_hooks=recent_hooks,
            recent_topics=recent_topics,
            recent_ctas=recent_ctas,
        )
        pre_generation_conference, pregen_errors = validate_agent_output("pre_generation_conference", pre_generation_conference_raw)
        if pregen_errors:
            pre_generation_conference = _default_pre_generation_conference(selected_hook, selected_cta)
            gate_records.append(
                build_gate_record(
                    gate_id="pre_generation_conference_schema",
                    passed=True,
                    severity="warning",
                    reasons=[],
                    details={"enabled": True, "fallback_applied": True, "source_errors": pregen_errors},
                )
            )
        else:
            gate_records.append(
                build_gate_record(
                    gate_id="pre_generation_conference_schema",
                    passed=True,
                    severity="error",
                    reasons=[],
                    details={"enabled": True, "fallback_applied": False},
                )
            )
        selected_hook = str(pre_generation_conference.get("recommended_hook", "")).strip() or selected_hook
        selected_cta = str(pre_generation_conference.get("recommended_cta", "")).strip() or selected_cta
        selected_cta = _ensure_explicit_cta(selected_cta, funnel_stage)
        if isinstance(pre_generation_conference, dict):
            pre_generation_conference["recommended_cta"] = selected_cta
    else:
        gate_records.append(
            build_gate_record(
                gate_id="pre_generation_conference_schema",
                passed=True,
                severity="warning",
                reasons=[],
                details={"enabled": False},
            )
        )

    segment_constraints = _apply_segment_constraints_to_stacks(phase2_stack, phase4_stack, audience_segment)

    pregen_context = ""
    if isinstance(pre_generation_conference, dict) and pre_generation_conference:
        pregen_context = (
            "PRE-GENERATION TEAM MEETING SUMMARY:\n"
            f"- Primary angle: {str(pre_generation_conference.get('primary_angle', ''))}\n"
            f"- Visual focus: {str(pre_generation_conference.get('visual_focus', ''))}\n"
            f"- Collective actions: {pre_generation_conference.get('collective_actions', [])}\n"
            f"- Risk checks: {pre_generation_conference.get('risk_checks', [])}\n"
            f"- Platform notes: {pre_generation_conference.get('platform_notes', {})}\n"
        )

    phase2_context = (
        "PHASE 2 CREATIVE INTELLIGENCE OUTPUTS:\n"
        f"- Ideation winner angle: {phase2_stack.get('ideation_divergence', {}).get('winner_angle', '')}\n"
        f"- Ideation novelty rationale: {phase2_stack.get('ideation_divergence', {}).get('novelty_rationale', '')}\n"
        f"- Psychographics emotional driver: {phase2_stack.get('audience_psychographics', {}).get('emotional_driver', '')}\n"
        f"- Psychographics core objection: {phase2_stack.get('audience_psychographics', {}).get('core_objection', '')}\n"
        f"- Psychographics trust trigger: {phase2_stack.get('audience_psychographics', {}).get('trust_trigger', '')}\n"
        f"- Narrative sequence: {phase2_stack.get('narrative_architect', {}).get('narrative_sequence', [])}\n"
        f"- Narrative must include: {phase2_stack.get('narrative_architect', {}).get('must_include', [])}\n"
        f"- Platform voice notes: {phase2_stack.get('platform_voice_calibrator', {})}\n"
        f"- Hook stress-test reason: {phase2_stack.get('hook_stress_test', {}).get('reason', '')}\n"
    )
    phase3_context = (
        "PHASE 3 PRECISION AND SAFETY OUTPUTS:\n"
        f"- Precision passed: {phase3_stack.get('precision_claims_verifier', {}).get('passed', True)}\n"
        f"- Precision issues: {phase3_stack.get('precision_claims_verifier', {}).get('issues', [])}\n"
        f"- Compliance risk: {phase3_stack.get('compliance_policy_sentinel', {}).get('risk_level', 'low')}\n"
        f"- Compliance actions: {phase3_stack.get('compliance_policy_sentinel', {}).get('required_actions', [])}\n"
        f"- Novelty score: {phase3_stack.get('semantic_novelty', {}).get('novelty_score', 1.0)}\n"
        f"- Novelty guidance: {phase3_stack.get('semantic_novelty', {}).get('rewrite_guidance', [])}\n"
    )
    phase4_context = (
        "PHASE 4 VISUAL AND CTA OPTIMIZATION OUTPUTS:\n"
        f"- Visual objective: {phase4_stack.get('visual_strategy', {}).get('visual_objective', '')}\n"
        f"- Visual composition adjustments: {phase4_stack.get('visual_strategy', {}).get('composition_adjustments', [])}\n"
        f"- Visual platform focus: {phase4_stack.get('visual_strategy', {}).get('platform_focus', {})}\n"
        f"- CTA recommendation: {phase4_stack.get('cta_optimization', {}).get('recommended_cta', selected_cta)}\n"
        f"- CTA alternates: {phase4_stack.get('cta_optimization', {}).get('alternates', [])}\n"
        f"- CTA friction note: {phase4_stack.get('cta_optimization', {}).get('friction_note', '')}\n"
    )

    segment_context = (
        "AUDIENCE SEGMENT EXECUTION DIRECTIVES:\n"
        f"- Segment key: {segment_constraints.get('segment_key', '')}\n"
        f"- Narrative requirement: {segment_constraints.get('narrative_requirement', '')}\n"
        f"- Visual requirement: {segment_constraints.get('visual_requirement', '')}\n"
        f"- Platform emphasis: {segment_constraints.get('platform_focus', '')}\n"
    )

    ideology_context = (
        "SELLING IDEOLOGY DIRECTIVES:\n"
        f"- Framework mode: {ideology_framework_mode}\n"
        f"- Primary conversion: {ideology_primary_conversion}\n"
        f"- Tone blend: {ideology_tone_blend}\n"
        f"- Value lens: {ideology_value_lens}\n"
        f"- Message filter: {ideology_message_filter}\n"
        f"- CTA mode: {ideology_cta_mode}\n"
        f"- Proof rule: {ideology_proof_rule}\n"
        f"- Core promise: {ideology_core_promise}\n"
        f"- Pillars: {ideology_pillars}\n"
        f"- Banned phrases and patterns: {ideology_banned_phrases}\n"
    )
    from production_policy import resolve as resolve_creative_policy
    policy_format = os.environ.get("POST_CONTENT_FORMAT", "educational_story_carousel")
    policy_objective = os.environ.get("POST_CONTENT_OBJECTIVE", "audience_growth")
    creative_policy = resolve_creative_policy(policy_format, objective=policy_objective)
    # Persisted sales defaults remain readable for migration, but cannot override
    # the owner's audience-growth mandate in a non-conversion post.
    ideology_context = "RESOLVED CREATIVE DIRECTIVES (supersede legacy selling defaults):\n" + json.dumps(creative_policy, ensure_ascii=False) + "\n"

    # Phase B: build the strategic brief context that instructs the copywriter to obey
    # the conversion logic engine's decisions (awareness, law, emotion, copy structure).
    _brief = run_context.get("strategic_brief") if isinstance(run_context.get("strategic_brief"), dict) else None
    _strategist_meta = run_context.get("conversion_strategist") if isinstance(run_context.get("conversion_strategist"), dict) else None
    if _brief:
        _persuasion = _brief.get("persuasion", {}) if isinstance(_brief.get("persuasion"), dict) else {}
        _downstream = (_strategist_meta or {}).get("downstream_instructions", {}) if _strategist_meta else {}
        strategic_brief_context = (
            "CONVERSION STRATEGIST BRIEF (MANDATORY — every choice below must be respected):\n"
            f"- Audience persona: {_brief.get('audience_id', '')}\n"
            f"- Awareness stage: {_brief.get('awareness_stage', '')}\n"
            f"- Logic principle: {_brief.get('logic_principle', '')}\n"
            f"- Copy structure (REQUIRED framework): {_brief.get('copy_framework', '')}\n"
            f"- Structure beats to follow in order: {(_strategist_meta or {}).get('structure_beats', [])}\n"
            f"- Primary emotional driver: {_brief.get('emotional_driver_primary', '')}\n"
            f"- Emotion cues to weave in: {(_strategist_meta or {}).get('hook_targets', {}).get('emotion_cues', [])}\n"
            f"- Core problem to name: {_persuasion.get('problem', '')}\n"
            f"- Objection to reframe: {_persuasion.get('objection', '')}\n"
            f"- Objection reframe pattern: {(_strategist_meta or {}).get('objection_reframe', {}).get('reframe_pattern', '')}\n"
            f"- Desired transformation (from -> to): {_persuasion.get('transformation_from', '')} -> {_persuasion.get('transformation_to', '')}\n"
            f"- Required proof to include: {_persuasion.get('proof', '')}\n"
            f"- Law narrative template (respect ordering): {(_strategist_meta or {}).get('law_narrative_template', [])}\n"
            f"- Law guardrails (do not violate): {(_strategist_meta or {}).get('law_guardrails', [])}\n"
            f"- Must include phrases/ideas: {_downstream.get('must_include', [])}\n"
            f"- Must avoid phrases/ideas: {_downstream.get('must_avoid', [])}\n"
            f"- Pinned CTA (use verbatim or a near-identical variant): {_downstream.get('cta_pinned', '')}\n"
            "- The final caption structure MUST follow the beats above in order.\n"
            "- Every claim MUST trace back to a verifiable product fact or clearly-labeled reasonable inference.\n"
        )
    else:
        strategic_brief_context = "CONVERSION STRATEGIST BRIEF: not available for this run (using default flow).\n"

    product_intelligence = _build_product_intelligence_handoff(
        product=product,
        topic=topic,
        funnel_stage=funnel_stage,
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        audience_segment=audience_segment,
        talking_point=talking_point,
    )
    if not want_product:
        # Restore the no-product talking point: the handoff above runs against a generic
        # placeholder product for internal bookkeeping only and must not reshape visible copy.
        talking_point.update(_build_talking_point_no_product(topic=topic, funnel_stage=funnel_stage, pillar=pillar))
    phase2_stack["product_intelligence_agent"] = product_intelligence
    logical_strategy = _select_logical_emotional_strategy(product, audience_segment, funnel_stage)
    phase2_stack["logical_emotional_strategy"] = logical_strategy
    product_agent_context = (
        "PRODUCT INTELLIGENCE AGENT HANDOFF:\n"
        f"- Product summary: {product_intelligence.get('product_summary', '')}\n"
        f"- Best-fit audiences: {product_intelligence.get('best_fit_audiences', [])}\n"
        f"- Core benefits: {product_intelligence.get('core_benefits', [])}\n"
        f"- Proof points: {product_intelligence.get('proof_points', [])}\n"
        f"- Sales angle: {product_intelligence.get('sales_angle', '')}\n"
        f"- Preferred vocabulary: {product_intelligence.get('preferred_vocabulary', [])}\n"
        f"- Emotional outcomes: {product_intelligence.get('emotional_outcomes', [])}\n"
        f"- Messaging devices: {product_intelligence.get('messaging_devices', [])}\n"
        f"- Sales copy seed: {product_intelligence.get('sales_copy_seed', '')}\n"
        f"- Team handoff directives: {product_intelligence.get('handoff', {})}\n"
    )

    logical_emotional_context = (
        "LOGICAL-EMOTIONAL CREATIVE DIRECTIVE:\n"
        f"- Formal principle: {logical_strategy.get('principle_name', '')}\n"
        f"- Logic form: {logical_strategy.get('formal_logic', '')}\n"
        f"- Caption strategy: {logical_strategy.get('caption_strategy', '')}\n"
        f"- Visual concept: {logical_strategy.get('visual_concept', '')}\n"
        f"- Audience archetype: {logical_strategy.get('audience_archetype', '')}\n"
        f"- Emotional outcome: {logical_strategy.get('emotional_outcome', '')}\n"
        f"- Required logic hook: {logical_strategy.get('logic_hook', '')}\n"
        f"- Required proof bridge: {logical_strategy.get('logic_bridge', '')}\n"
        "- Keep the reasoning valid and grounded in supplied product facts. Never make an unsupported exclusivity claim.\n"
    )

    messaging_playbook_context = (
        "INFENERGY MESSAGING PLAYBOOK:\n"
        f"- Core outcome: {INFENERGY_BUSINESS_GOALS['core_outcome']}\n"
        f"- Action bias: {INFENERGY_BUSINESS_GOALS['action_bias']}\n"
        f"- Voice anchors: {INFENERGY_BUSINESS_GOALS['voice_anchors']}\n"
        f"- Talking-point lenses: {INFENERGY_BUSINESS_GOALS['talking_point_lenses']}\n"
        f"- Words to emphasize: {brand_words_to_use[:14]}\n"
        f"- Approved sales language: {brand_approved_phrases[:12]}\n"
        f"- Ideology pillars: {ideology_pillars[:10]}\n"
        "- Every message should help the customer feel more prepared, more in control, and clearer about the right next step.\n"
    )

    visual_plan = _build_visual_plan_with_gemini(
        visual_director_candidates,
        topic=topic,
        funnel_stage=funnel_stage,
        stage_objective=str(stage_meta.get("objective", "")),
        selected_hook=selected_hook,
        selected_cta=selected_cta,
        product_name=product_name,
        product_categories=product_categories,
        product_metrics=product_metrics,
        has_product_image=bool((product or {}).get("image_url")),
    )
    if not isinstance(visual_plan, dict):
        visual_plan = {}
    visual_plan, visual_errors = validate_agent_output("visual_director", visual_plan)
    if visual_errors:
        visual_plan = _build_default_visual_plan(topic, funnel_stage, selected_hook, selected_cta, product)
        gate_records.append(
            build_gate_record(
                gate_id="visual_director_schema",
                passed=True,
                severity="warning",
                reasons=[],
                details={"agent": "visual_director", "fallback_applied": True, "source_errors": visual_errors},
            )
        )
    else:
        gate_records.append(
            build_gate_record(
                gate_id="visual_director_schema",
                passed=True,
                severity="error",
                reasons=[],
                details={"agent": "visual_director", "fallback_applied": False},
            )
        )
    visual_adjustments = phase4_stack.get("visual_strategy", {}).get("composition_adjustments", [])
    if isinstance(visual_adjustments, list) and visual_adjustments:
        visual_plan["phase4_composition_adjustments"] = [str(x) for x in visual_adjustments if str(x).strip()]
    segment_visual_requirement = str(segment_constraints.get("visual_requirement", "")).strip()
    if segment_visual_requirement:
        existing = visual_plan.get("phase4_composition_adjustments", [])
        if not isinstance(existing, list):
            existing = []
        if segment_visual_requirement not in existing:
            existing.append(segment_visual_requirement)
        visual_plan["phase4_composition_adjustments"] = existing
        visual_plan["segment_preset"] = str(segment_constraints.get("segment_key", "")).strip()
    visual_plan = _apply_logical_visual_strategy(visual_plan, logical_strategy, product)
    visual_plan = _apply_strategic_brief_to_visual(visual_plan, run_context, product)
    visual_plan = _apply_recurring_series_visual_plan(visual_plan, recurring_series)
    human_connection_context = _human_connection_prompt_context(
        run_context.get("human_connection") if isinstance(run_context.get("human_connection"), dict) else {}
    )

    if want_product:
        product_directive = (
            "PRODUCT CONTEXT (ground your content in these details when relevant):\n"
            f"- Product name: {product_name or 'N/A'}\n"
            f"- SKU: {product_sku or 'N/A'}\n"
            f"- Regular price: {product_price or 'N/A'}\n"
            f"- Sale price: {product_sale_price or 'N/A'}\n"
            f"- Categories: {product_categories or 'N/A'}\n"
            f"- Key measurable specs: {product_metrics or 'N/A'}\n"
            f"- Product facts excerpt: {product_facts or 'N/A'}\n"
        )
        fb_caption_instruction = (
            "150-220 words. Start with the lived customer moment, then introduce the product only after its relevance is clear. "
            "Use verified, relevant facts without forcing a feature count, explain fit and limits where needed, then close with a direct buy/preparedness CTA. "
            "4-8 targeted hashtags on the last line only."
        )
        ig_caption_instruction = (
            "110-170 words. Tight, visual, scroll-stopping copy rooted in the lived moment. First line should feel like a headline. "
            "Introduce the product after relevance is clear, use verified facts only when relevant, and close with a direct tap-to-buy CTA. "
            "7-10 hashtags on the final line only."
        )
        li_text_instruction = (
            "170-260 words. Professional product sales copy with a preparedness/business-continuity angle. Begin with the practical "
            "decision, introduce the product after relevance is clear, use verified facts only when relevant, and end with a direct order or action CTA."
        )
    else:
        product_directive = (
            "PRODUCT CONTEXT: No specific product is attached to this post.\n"
            "- Do NOT name, imply, or promote any specific product or SKU.\n"
            "- Do NOT include a buy/order/checkout CTA or any price.\n"
            "- Write genuinely useful, standalone content about the topic/pillar at a category or brand level.\n"
            "- If this is a lead-generation pillar, close with a free readiness-assessment CTA instead of a purchase CTA.\n"
        )
        fb_caption_instruction = (
            "150-220 words. Valuable, non-salesy post about the topic itself. No product name, no price, no purchase CTA. "
            "Teach something real or invite genuine engagement, then close with the assigned first-step action (comment, save, "
            "share, or book a free assessment if this is a lead-generation post). 3-6 relevant hashtags on the last line only."
        )
        ig_caption_instruction = (
            "110-170 words. Tight, visual, scroll-stopping post about the topic itself. First line should feel like a headline. "
            "No product name, no price, no purchase CTA. Close with the assigned first-step action. 5-8 hashtags on the final line only."
        )
        li_text_instruction = (
            "170-260 words. Professional, insight-led post about the topic with a business/preparedness-authority angle. "
            "No product name, no price, no purchase CTA. Close with the assigned first-step action."
        )

    prompt = f"""You are an expert content strategist and copywriter for {brand_name} ({SITE_URL}), a {brand_positioning}.

{CONVERSION_COPY_BRIEF}

BRAND VOICE: {brand_personality_name}. Traits: {brand_personality_traits or ['Direct', 'credible', 'genuinely helpful']}.
VOICE RULES: {brand_tone_rules or brand_voice_rules_db or ['Speak like a trusted expert neighbor, not a salesperson.']}
APPROVED VERBIAGE: {brand_approved_phrases[:10]}
TRUST CLOSER: {brand_trust_close or 'Use practical, non-hype language grounded in verified facts.'}
WORDS TO USE: {brand_words_to_use[:12]}
WORDS TO AVOID: {brand_words_to_avoid[:12]}
FORBIDDEN CLAIM PATTERNS: {brand_forbidden_phrases[:10]}
BRAND MISSION: {brand_mission}
BRAND VALUES: {brand_core_values}
AUDIENCE: {brand_audience_summary or INFENERGY_BUSINESS_GOALS['audience_summary']}
TOPIC: {topic}
CONTENT DIRECTIVE: {slot_guidance}

{product_directive}

{consumer_profile_context}

{human_connection_context}

{marketing_context}

{pregen_context}

{phase2_context}

{phase3_context}

{phase4_context}

{strategic_brief_context}

{segment_context}

{ideology_context}

{product_agent_context}

{logical_emotional_context}

{messaging_playbook_context}

CAMPAIGN EXECUTION CONTEXT:
- Selected hook for this post: {selected_hook}
- Selected CTA for this post: {selected_cta}
- Funnel stage: {funnel_stage}
- Funnel objective: {stage_meta.get('objective', '')}
- Weekly plan row: {json.dumps(weekly_sequence) if weekly_sequence else 'none'}
- Recent hooks (avoid repetition): {recent_hooks}
- Recent products (avoid overusing): {recent_products}
- Recent CTAs (avoid repetition): {recent_ctas}
- Recent topics (avoid repetition): {recent_topics}

BUSINESS PROFILE (derived from published catalog):
- Focus: {business_profile.get('focus_statement', '')}
- Top categories: {business_profile.get('top_categories', [])}
- Keyword signals: {business_profile.get('keyword_signals', {})}
- Core offers: {business_profile.get('offers', [])}

POSITIONING GUARDRAILS:
- Do not frame this brand as a rooftop residential solar installation service.
- Only reference solar in the context of portable/foldable solar panels paired with portable power products.
- Forbidden phrasing examples: rooftop install, net metering savings, utility bill offset via home solar install, state solar tax-credit pitch.

COPY ACCURACY RULES:
- Identify the actual product type from the provided name, categories, specs, and facts before writing a single sentence.
- Describe the selected item as the correct type, not a generic preparedness product.
- If the item is a solar panel, describe it as portable or foldable solar charging equipment, not a battery, power bank, or standalone power station.
- If the item is a power bank or charger, describe it as small-device charging backup, not whole-home backup power.
- If the item is a jump starter, make vehicle or roadside use explicit and do not frame it like a home backup system.
- If the item is a water filter or straw, make clean-water preparedness explicit and do not frame it like an energy product.
- If the item is a fan or comfort device, make airflow, camping, outage comfort, or small-device support explicit and do not frame it like a large battery system.
- If the item is a power station, inverter, generator, or larger battery unit, make output, runtime, charging limits, and device matching explicit.
- Never use vague core-product descriptions like "preparedness product", "solution", "gear", or "tool" when a more precise category is available from context.
- Never use generic benefit phrases like "turn confusion into a clear product-fit decision" or "using real usage data" as the main product description unless they are immediately tied to concrete specs and the real product category.

QUALITY RULES — every piece must follow all of these:
1. Open with a named device, task, failure, use case, or verified measurement. Do not open with a belief, mood, rhetorical abstraction, or generic question.
2. Name one concrete customer pain point in the first two lines.
3. Add one proof anchor when a verified fact or measurable detail is relevant.
4. Use a specific number, stat, or real-world comparison only when it is explicitly verified and relevant; otherwise provide concrete decision-support specificity without inventing a claim.
5. Give one usable instruction tied to a named device, specification, plug, location, duration, or task.
6. Write like a human expert, not a marketing team. Never use words like "revolutionize", "game-changer", or "unlock your potential."
7. Never make unverifiable guarantees. Use language like "many homeowners", "up to", "in most cases" where appropriate.
8. Every post must end in an observable outcome or action: a device stays running, a specification is checked, a list is made, or a compatible product page is opened.
9. If product context is available, use concrete product facts only when verified and relevant to the reader's decision. Do not force a product-first narrative or a fixed fact count.
10. Do not invent model names, specs, prices, or warranties not present in the provided product context.
11. End with one frictionless next action that can be done today.

TALKING-POINT BRIEF FOR THIS RUN:
- Pain point: {talking_point.get('pain_point', '')}
- Proof anchor: {talking_point.get('proof_anchor', '')}
- Angle: {talking_point.get('angle', '')}
- First-step CTA: {talking_point.get('first_step', '')}

Return ONLY valid JSON with these exact keys (no markdown, no code fences):
{{
  "wp_title": "Specific, curiosity-driven SEO title under 65 characters — not generic",
  "wp_content": "Full blog post as clean HTML with <h2> subheadings. 450-550 words. Open strong, build a logical case, end with a clear next step. Include at least 2 specific data points or examples.",
  "wp_excerpt": "One punchy sentence under 160 characters that makes someone want to click",
    "fb_caption": "{fb_caption_instruction}",
    "ig_caption": "{ig_caption_instruction}",
    "li_text": "{li_text_instruction}"
}}"""

    content = _generate_json_with_gemini(prompt, model_candidates)
    copy_generation_source = "gemini" if content is not None else "deterministic_fallback"
    talking_point["copy_generation_source"] = copy_generation_source

    if content is None:
        content = (
            _build_fallback_content(slot, topic, product, marketing_strategy, talking_point=talking_point, strategic_brief=_brief)
            if want_product
            else _build_fallback_content_no_product(slot, topic, pillar, marketing_strategy, talking_point=talking_point, strategic_brief=_brief)
        )
        content["copy_generation_source"] = copy_generation_source
        content["topic"] = topic
        content["pillar"] = pillar
        content["topic_hash"] = topic_hash
        content["product_name"] = product_name
        content["product_id"] = product_id
        content["product_sku"] = product_sku
        content["product_price"] = product_price
        content["product_sale_price"] = product_sale_price
        content["product_metrics"] = product.get("metrics", []) if product else []
        content["product_categories"] = product.get("categories", []) if product else []
        content["product_facts"] = product_facts
        content["product_in_stock"] = product_in_stock
        content["product_stock"] = product_stock
        content["product_url"] = product_url
        content["product_image_url"] = product.get("image_url", "") if product else ""
        content["product_image_candidates"] = product.get("image_candidates", []) if product else []
        content["category_image_candidates"] = product.get("category_image_candidates", []) if product else []
        content["product_selection_decision"] = product.get("_rotation_decision", {}) if product else {}
        content["product_compatibility"] = assess_product_compatibility(consumer_moment, product)
        content["marketing_strategy_used"] = bool(marketing_strategy)
        content["marketing_bundle_used"] = bool(marketing_strategy)
        content["business_profile"] = business_profile
        content["selected_hook"] = selected_hook
        content["selected_cta"] = selected_cta
        content["selected_hook_type"] = hook_type
        content["hook_scores"] = hook_choice.get("component_scores", {})
        content["funnel_stage"] = funnel_stage
        content["funnel_stage_objective"] = stage_meta.get("objective", "")
        content["audience_segment"] = audience_segment
        content["campaign_id"] = campaign_id
        content["destination_url"] = destination_url
        content["product_intelligence_handoff"] = product_intelligence
        content["logical_emotional_strategy"] = logical_strategy
        content["sales_copy_seed"] = str(product_intelligence.get("sales_copy_seed", "")).strip()
        content["weekly_plan_used"] = bool(weekly_sequence)
        content["hook_hash"] = stable_text_hash(selected_hook)
        content["cta_hash"] = stable_text_hash(selected_cta)

        cta_ok, cta_reason = cta_is_valid_for_stage(funnel_stage, selected_cta, destination_url)
        if not cta_ok:
            fallback_cta = choose_cta_for_stage(
                stage=funnel_stage,
                preferred="",
                cta_library=cta_library,
                recent_cta_hashes=recent_cta_hashes,
            )
            content["selected_cta"] = fallback_cta
            selected_cta = fallback_cta
            content["cta_hash"] = stable_text_hash(fallback_cta)
            content.setdefault("quality_warnings", []).append(f"cta_adjusted:{cta_reason}")
        selected_cta = _ensure_explicit_cta(selected_cta, funnel_stage)
        content["selected_cta"] = selected_cta
        content["cta_hash"] = stable_text_hash(selected_cta)
        content["date"] = str(date.today())
        content["slot"] = slot
        for key in ("fb_caption", "ig_caption", "li_text"):
            platform_name = "facebook" if key == "fb_caption" else "instagram" if key == "ig_caption" else "linkedin"
            content[key] = _enforce_conversion_caption(
                str(content.get(key, "")),
                talking_point,
                platform=platform_name,
            )
        _enforce_product_led_copy(content, product)
        _enforce_product_sales_platform_copy(content, product, talking_point)
        _enforce_numeric_proof_requirements(content, funnel_stage, talking_point)
        for key in ("wp_content", "fb_caption", "ig_caption", "li_text"):
            cleaned, replaced = apply_claim_guardrails(str(content.get(key, "")))
            content[key] = cleaned
            if replaced:
                content.setdefault("claim_guardrail_replacements", []).extend(replaced)
        quality = score_generated_content(content)
        content["quality_score"] = quality.score
        content["quality_checks"] = quality.checks
        content["quality_warnings"] = quality.warnings

        post_id = uuid.uuid4().hex[:12]
        selected_hook = str(content.get("selected_hook", selected_hook))
        selected_cta = str(content.get("selected_cta", selected_cta))
        components = _build_post_components(
            topic,
            selected_hook,
            selected_cta,
            product,
            funnel_stage,
            product_intelligence=product_intelligence,
            logical_strategy=logical_strategy,
            resolved_pain_point=str(talking_point.get("pain_point", "")),
        )
        components["pillar"] = pillar
        platform_posts = _build_platform_posts(
            post_id=post_id,
            campaign_id=campaign_id,
            audience_segment=audience_segment,
            funnel_stage=funnel_stage,
            destination_url=destination_url,
            components=components,
            quality_score=float(content.get("quality_score", 0)),
            caption_overrides=_model_caption_overrides(content),
        )
        for platform_name in ("facebook", "instagram", "linkedin"):
            post_payload = platform_posts.get(platform_name, {})
            if isinstance(post_payload, dict):
                post_payload["caption"] = _enforce_conversion_caption(
                    str(post_payload.get("caption", "")),
                    talking_point,
                    platform=platform_name,
                )
        conversion_gate_posts = {
            platform: {"caption": str(package.get("caption", ""))}
            for platform, package in platform_posts.items()
            if isinstance(package, dict)
        }
        platform_posts = normalize_brand_content(platform_posts)
        platform_posts = _apply_platform_presentation_priority(platform_posts, components)
        conversion_gate_posts = {
            platform: {"caption": str(package.get("caption", ""))}
            for platform, package in platform_posts.items()
            if isinstance(package, dict)
        }
        content = normalize_brand_content(content)
        content["post_id"] = post_id
        content["consumer_root"] = consumer_root
        content["consumer_root_id"] = consumer_root["root_id"]
        content["consumer_world_id"] = consumer_root["world_id"]
        content["consumer_moment_id"] = consumer_root["moment_id"]
        content["consumer_receipt"] = consumer_root["consumer_receipt"]
        content["consumer_profile"] = consumer_context.get("profile", {})
        content["selected_consumer_persona"] = selected_consumer_persona
        content["platform_posts"] = platform_posts
        # See note above: category-template "situation" text is near-static per product
        # category and would guarantee false-positive scenario-duplicate blocks.
        content["scenario"] = _build_scenario_fingerprint(talking_point, components)
        content["educational_lesson"] = consumer_moment["useful_discovery"]
        content["fb_caption"] = platform_posts["facebook"]["caption"]
        content["ig_caption"] = platform_posts["instagram"]["caption"]
        content["li_text"] = platform_posts["linkedin"]["caption"]
        content["selected_cta"] = components["cta"]
        _apply_recurring_series_package(content, platform_posts, recurring_series)
        _apply_recurring_series_generation_contract(content, visual_plan, recurring_series, product)
        social_media_assets = _build_social_media_assets(components, platform_posts, visual_plan)
        content["social_media_assets"] = social_media_assets
        platform_posts["instagram"]["carousel_campaign"] = social_media_assets["asset_2_carousel_campaign"]
        content["on_image_headline"] = components["on_image_headline"]
        content["on_image_subline"] = components["on_image_subline"]
        content["generated_visuals"] = (
            {"deferred": True, "reason": "text_only_candidate_pool"}
            if _text_only_generation()
            else generate_visuals(content, visual_plan=visual_plan)
        )
        from social import reels

        strategy_lock = content.get("strategy_lock") if isinstance(content.get("strategy_lock"), dict) else {}
        instagram_decision = reels.choose_instagram_media(
            strategy_lock=strategy_lock,
            components=components,
            visual_plan=visual_plan,
        )
        content["instagram_media_decision"] = instagram_decision
        platform_posts["instagram"]["media_type"] = instagram_decision["selected_format"]
        platform_posts["instagram"]["instagram_media_decision"] = instagram_decision
        if recurring_series.get("id") == "infenergy_intervention":
            platform_posts["instagram"]["media_type"] = "CAROUSEL" if recurring_series.get("preferred_format") == "educational_story_carousel" else "STATIC"
            content["instagram_media_decision"]["selected_format"] = platform_posts["instagram"]["media_type"]
        if instagram_decision["selected_format"] == "REEL":
            reel_plan = reels.build_reel_plan(
                post_id=post_id,
                components=components,
                decision=instagram_decision,
                strategy_lock=strategy_lock,
            )
            content["reel_plan"] = reel_plan
            reel_gate = reels.validate_reel_plan(reel_plan)
            content["reel_pre_render_gate"] = reel_gate
            if reel_gate["status"] == "REEL_READY":
                try:
                    reel_artifacts = reels.render_reel(
                        reel_plan,
                        source_image=str((content.get("generated_visuals") or {}).get("instagram") or ""),
                    )
                    reel_artifacts["technical_qa"] = reels.technical_qa(reel_artifacts, reel_plan)
                    reel_artifacts["freeze_qa"] = reels.freeze_qa(reel_artifacts, reel_plan)
                    reel_artifacts["final_frame_qa"] = reels.final_frame_qa(reel_artifacts)
                    reel_artifacts["cover_qa"] = reels.cover_qa(reel_artifacts)
                    reel_artifacts["motion_qa"] = reels.motion_qa(reel_plan)
                    content["instagram_reel"] = reel_artifacts
                    platform_posts["instagram"]["reel"] = reel_artifacts
                except RuntimeError as exc:
                    if str(exc) not in {"ffmpeg_not_available", "ffprobe_not_available"}:
                        raise
                    content["instagram_media_decision"]["selected_format"] = "STATIC"
                    content["instagram_media_decision"]["fallback_reason"] = str(exc)
                    platform_posts["instagram"]["media_type"] = "STATIC"
            else:
                content["instagram_media_decision"]["selected_format"] = "STATIC"
                platform_posts["instagram"]["media_type"] = "STATIC"
        content["visual_plan"] = visual_plan
        content["pre_generation_conference"] = pre_generation_conference
        content["phase2_creative_stack"] = phase2_stack
        content["phase3_safety_stack"] = phase3_stack
        content["phase4_optimization_stack"] = phase4_stack
        phase7_packets = _build_phase7_conference_packets(
            run_context=run_context,
            pre_generation_conference=pre_generation_conference,
            phase2_stack=phase2_stack,
            phase3_stack=phase3_stack,
            phase4_stack=phase4_stack,
            conference_summary={},
        )
        phase7_packets_validated, phase7_errors = validate_agent_output("phase7_conference_packets", phase7_packets)
        gate_records.append(
            build_gate_record(
                gate_id="phase7_conference_packets_schema",
                passed=len(phase7_errors) == 0,
                severity="error",
                reasons=phase7_errors,
                details={"enabled": True},
            )
        )
        content["phase7_conference_packets"] = phase7_packets_validated if not phase7_errors else phase7_packets
        conv_passed, conv_reasons = _conversion_caption_gate(conversion_gate_posts, talking_point, want_product=want_product)
        gate_records.append(
            build_gate_record(
                gate_id="conversion_caption_enforcement",
                passed=conv_passed,
                severity="error",
                reasons=conv_reasons,
                details={"source": "fallback_builder"},
            )
        )
        content["creative_agents"] = {
            "copywriter": preferred_model or "gemini-2.5-flash",
            "product_intelligence_agent": "deterministic_profile_agent_v1",
            "visual_director": (preferred_visual_director_model or "gemini-2.5-pro"),
            "ideation_divergence": (preferred_model or "gemini-2.5-flash"),
            "audience_psychographics": (preferred_model or "gemini-2.5-flash"),
            "narrative_architect": (preferred_model or "gemini-2.5-flash"),
            "platform_voice_calibrator": (preferred_model or "gemini-2.5-flash"),
            "hook_stress_test": (preferred_model or "gemini-2.5-flash"),
            "precision_claims_verifier": (preferred_model or "gemini-2.5-flash"),
            "compliance_policy_sentinel": (preferred_model or "gemini-2.5-flash"),
            "semantic_novelty": (preferred_model or "gemini-2.5-flash"),
            "visual_strategy": (preferred_model or "gemini-2.5-flash"),
            "cta_optimization": (preferred_model or "gemini-2.5-flash"),
            "pre_generation_conference": (conference_model or preferred_visual_director_model or "gemini-2.5-pro"),
            "image_model": os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"),
        }
        gate_records.append(
            build_gate_record(
                gate_id="copywriter_payload_minimum",
                passed=True,
                severity="error",
                reasons=[],
                details={"source": "fallback_builder"},
            )
        )
        quality = score_generated_content(content)
        content["quality_score"] = quality.score
        content["quality_checks"] = quality.checks
        content["quality_warnings"] = quality.warnings
        for p in content["platform_posts"].values():
            p["quality_score"] = float(quality.score)
        content = _run_phase_c_conversion_gates(content, run_context, gate_records)
        content = _run_phase_d_brief_adherence(content, run_context, gate_records)
        content = _run_phase_f_visual_alignment(content, run_context, gate_records)
        content = _apply_control_plane_metadata(content, run_context, gate_records)
        content["consumer_copy_fidelity"] = assess_copy_fidelity(content)
        content = _sanitize_legacy_cta_in_payload(content)
        content["editorial_decision"] = {
            "content_bucket": content_bucket,
            "pillar": pillar,
            "product_mode": PILLAR_PRODUCT_MODE.get(pillar, "optional_product"),
            "want_product": want_product,
            "product_forced_override": bool(forced_product),
        }
        return content

    content["copy_generation_source"] = copy_generation_source
    content["topic"] = topic
    content["pillar"] = pillar
    content["topic_hash"] = topic_hash
    content["product_name"] = product_name
    content["product_id"] = product_id
    content["product_sku"] = product_sku
    content["product_price"] = product_price
    content["product_sale_price"] = product_sale_price
    content["product_metrics"] = product.get("metrics", []) if product else []
    content["product_facts"] = product_facts
    content["product_in_stock"] = product_in_stock
    content["product_stock"] = product_stock
    content["product_url"] = product_url
    content["product_image_url"] = product.get("image_url", "") if product else ""
    content["product_image_candidates"] = product.get("image_candidates", []) if product else []
    content["category_image_candidates"] = product.get("category_image_candidates", []) if product else []
    content["product_selection_decision"] = product.get("_rotation_decision", {}) if product else {}
    content["product_compatibility"] = assess_product_compatibility(consumer_moment, product)
    content["marketing_strategy_used"] = bool(marketing_strategy)
    content["marketing_bundle_used"] = bool(marketing_strategy)
    content["business_profile"] = business_profile
    content["selected_hook"] = selected_hook
    content["selected_cta"] = selected_cta
    content["selected_hook_type"] = hook_type
    content["hook_scores"] = hook_choice.get("component_scores", {})
    content["funnel_stage"] = funnel_stage
    content["funnel_stage_objective"] = stage_meta.get("objective", "")
    content["audience_segment"] = audience_segment
    content["campaign_id"] = campaign_id
    content["destination_url"] = destination_url
    content["product_intelligence_handoff"] = product_intelligence
    content["logical_emotional_strategy"] = logical_strategy
    content["sales_copy_seed"] = str(product_intelligence.get("sales_copy_seed", "")).strip()
    content["weekly_plan_used"] = bool(weekly_sequence)
    content["hook_hash"] = stable_text_hash(selected_hook)
    content["cta_hash"] = stable_text_hash(selected_cta)

    cta_ok, cta_reason = cta_is_valid_for_stage(funnel_stage, selected_cta, destination_url)
    if not cta_ok:
        fallback_cta = choose_cta_for_stage(
            stage=funnel_stage,
            preferred="",
            cta_library=cta_library,
            recent_cta_hashes=recent_cta_hashes,
        )
        content["selected_cta"] = fallback_cta
        selected_cta = fallback_cta
        content["cta_hash"] = stable_text_hash(fallback_cta)
        content.setdefault("quality_warnings", []).append(f"cta_adjusted:{cta_reason}")
    selected_cta = _ensure_explicit_cta(selected_cta, funnel_stage)
    content["selected_cta"] = selected_cta
    content["cta_hash"] = stable_text_hash(selected_cta)
    content["date"] = str(date.today())
    content["slot"] = slot
    for key in ("fb_caption", "ig_caption", "li_text"):
        platform_name = "facebook" if key == "fb_caption" else "instagram" if key == "ig_caption" else "linkedin"
        content[key] = _enforce_conversion_caption(
            str(content.get(key, "")),
            talking_point,
            platform=platform_name,
        )
    _enforce_product_led_copy(content, product)
    _enforce_product_sales_platform_copy(content, product, talking_point)
    _enforce_numeric_proof_requirements(content, funnel_stage, talking_point)
    for key in ("wp_content", "fb_caption", "ig_caption", "li_text"):
        cleaned, replaced = apply_claim_guardrails(str(content.get(key, "")))
        content[key] = cleaned
        if replaced:
            content.setdefault("claim_guardrail_replacements", []).extend(replaced)
    quality = score_generated_content(content)
    content["quality_score"] = quality.score
    content["quality_checks"] = quality.checks
    content["quality_warnings"] = quality.warnings

    conference_enabled = os.environ.get("ENABLE_AGENT_CONFERENCE", "true").strip().lower() not in {"0", "false", "no"}
    conference_summary = {}
    if conference_enabled:
        conference_summary_raw = _run_agent_conference(
            conference_candidates,
            topic=topic,
            funnel_stage=funnel_stage,
            selected_hook=str(content.get("selected_hook", selected_hook)),
            selected_cta=str(content.get("selected_cta", selected_cta)),
            content=content,
            visual_plan=visual_plan,
            product_name=product_name,
            product_metrics=product_metrics,
        )
        conference_summary, conference_errors = validate_agent_output("agent_conference", conference_summary_raw)
        if conference_errors:
            conference_summary = {}
            gate_records.append(
                build_gate_record(
                    gate_id="agent_conference_schema",
                    passed=True,
                    severity="warning",
                    reasons=[],
                    details={"enabled": True, "fallback_applied": True, "source_errors": conference_errors},
                )
            )
        else:
            gate_records.append(
                build_gate_record(
                    gate_id="agent_conference_schema",
                    passed=True,
                    severity="error",
                    reasons=[],
                    details={"enabled": True, "fallback_applied": False},
                )
            )
        content, visual_plan = _apply_conference_refinements(content, visual_plan, conference_summary)
        # Conference may have replaced gemini_image_prompt; re-assert the brief's
        # visual direction so alignment metadata reflects the final prompt.
        visual_plan = _apply_strategic_brief_to_visual(visual_plan, run_context, product)
        selected_cta = _ensure_explicit_cta(str(content.get("selected_cta", selected_cta)), funnel_stage)
        content["selected_cta"] = selected_cta
        for key in ("fb_caption", "ig_caption", "li_text"):
            platform_name = "facebook" if key == "fb_caption" else "instagram" if key == "ig_caption" else "linkedin"
            content[key] = _enforce_conversion_caption(
                str(content.get(key, "")),
                talking_point,
                platform=platform_name,
            )
        _enforce_product_led_copy(content, product)
        _enforce_product_sales_platform_copy(content, product, talking_point)
        _enforce_numeric_proof_requirements(content, funnel_stage, talking_point)
        # Re-apply guardrails after conference-driven refinements.
        for key in ("wp_content", "fb_caption", "ig_caption", "li_text"):
            cleaned, replaced = apply_claim_guardrails(str(content.get(key, "")))
            content[key] = cleaned
            if replaced:
                content.setdefault("claim_guardrail_replacements", []).extend(replaced)
    else:
        gate_records.append(
            build_gate_record(
                gate_id="agent_conference_schema",
                passed=True,
                severity="warning",
                reasons=[],
                details={"enabled": False},
            )
        )

    phase7_packets = _build_phase7_conference_packets(
        run_context=run_context,
        pre_generation_conference=pre_generation_conference,
        phase2_stack=phase2_stack,
        phase3_stack=phase3_stack,
        phase4_stack=phase4_stack,
        conference_summary=conference_summary,
    )
    phase7_packets_validated, phase7_errors = validate_agent_output("phase7_conference_packets", phase7_packets)
    gate_records.append(
        build_gate_record(
            gate_id="phase7_conference_packets_schema",
            passed=len(phase7_errors) == 0,
            severity="error",
            reasons=phase7_errors,
            details={"enabled": True},
        )
    )

    post_id = uuid.uuid4().hex[:12]
    selected_hook = str(content.get("selected_hook", selected_hook))
    selected_cta = str(content.get("selected_cta", selected_cta))
    components = _build_post_components(
        topic,
        selected_hook,
        selected_cta,
        product,
        funnel_stage,
        product_intelligence=product_intelligence,
        logical_strategy=logical_strategy,
        resolved_pain_point=str(talking_point.get("pain_point", "")),
    )
    components["pillar"] = pillar
    platform_posts = _build_platform_posts(
        post_id=post_id,
        campaign_id=campaign_id,
        audience_segment=audience_segment,
        funnel_stage=funnel_stage,
        destination_url=destination_url,
        components=components,
        quality_score=float(content.get("quality_score", 0)),
        caption_overrides=_model_caption_overrides(content),
    )
    for platform_name in ("facebook", "instagram", "linkedin"):
        post_payload = platform_posts.get(platform_name, {})
        if isinstance(post_payload, dict):
            post_payload["caption"] = _enforce_conversion_caption(
                str(post_payload.get("caption", "")),
                talking_point,
                platform=platform_name,
            )
    conversion_gate_posts = {
        platform: {"caption": str(package.get("caption", ""))}
        for platform, package in platform_posts.items()
        if isinstance(package, dict)
    }
    platform_posts = normalize_brand_content(platform_posts)
    platform_posts = _apply_platform_presentation_priority(platform_posts, components)
    conversion_gate_posts = {
        platform: {"caption": str(package.get("caption", ""))}
        for platform, package in platform_posts.items()
        if isinstance(package, dict)
    }
    content = normalize_brand_content(content)
    content["post_id"] = post_id
    content["platform_posts"] = platform_posts
    # See note above: category-template "situation" text is near-static per product
    # category and would guarantee false-positive scenario-duplicate blocks.
    content["scenario"] = _build_scenario_fingerprint(talking_point, components)
    content["educational_lesson"] = components.get("info", "")
    # Preserve publisher compatibility with existing flat keys.
    content["fb_caption"] = platform_posts["facebook"]["caption"]
    content["ig_caption"] = platform_posts["instagram"]["caption"]
    content["li_text"] = platform_posts["linkedin"]["caption"]
    content["selected_cta"] = components["cta"]
    _apply_recurring_series_package(content, platform_posts, recurring_series)
    _apply_recurring_series_generation_contract(content, visual_plan, recurring_series, product)
    social_media_assets = _build_social_media_assets(components, platform_posts, visual_plan)
    content["social_media_assets"] = social_media_assets
    platform_posts["instagram"]["carousel_campaign"] = social_media_assets["asset_2_carousel_campaign"]
    content["on_image_headline"] = components["on_image_headline"]
    content["on_image_subline"] = components["on_image_subline"]
    content["generated_visuals"] = (
        {"deferred": True, "reason": "text_only_candidate_pool"}
        if _text_only_generation()
        else generate_visuals(content, visual_plan=visual_plan)
    )
    content["visual_plan"] = visual_plan
    content["pre_generation_conference"] = pre_generation_conference
    content["phase2_creative_stack"] = phase2_stack
    content["phase3_safety_stack"] = phase3_stack
    content["phase4_optimization_stack"] = phase4_stack
    content["phase7_conference_packets"] = phase7_packets_validated if not phase7_errors else phase7_packets
    content["agent_conference"] = conference_summary
    conv_passed, conv_reasons = _conversion_caption_gate(conversion_gate_posts, talking_point, want_product=want_product)
    gate_records.append(
        build_gate_record(
            gate_id="conversion_caption_enforcement",
            passed=conv_passed,
            severity="error",
            reasons=conv_reasons,
            details={"source": "model_or_hybrid"},
        )
    )
    content["creative_agents"] = {
        "copywriter": preferred_model or "gemini-2.5-flash",
        "product_intelligence_agent": "deterministic_profile_agent_v1",
        "visual_director": (preferred_visual_director_model or "gemini-2.5-pro"),
        "ideation_divergence": (preferred_model or "gemini-2.5-flash"),
        "audience_psychographics": (preferred_model or "gemini-2.5-flash"),
        "narrative_architect": (preferred_model or "gemini-2.5-flash"),
        "platform_voice_calibrator": (preferred_model or "gemini-2.5-flash"),
        "hook_stress_test": (preferred_model or "gemini-2.5-flash"),
        "precision_claims_verifier": (preferred_model or "gemini-2.5-flash"),
        "compliance_policy_sentinel": (preferred_model or "gemini-2.5-flash"),
        "semantic_novelty": (preferred_model or "gemini-2.5-flash"),
        "visual_strategy": (preferred_model or "gemini-2.5-flash"),
        "cta_optimization": (preferred_model or "gemini-2.5-flash"),
        "pre_generation_conference": (conference_model or preferred_visual_director_model or "gemini-2.5-pro"),
        "conference": (conference_model or preferred_visual_director_model or "gemini-2.5-pro"),
        "image_model": os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"),
    }
    gate_records.append(
        build_gate_record(
            gate_id="copywriter_payload_minimum",
            passed=all(bool(str(content.get(k, "")).strip()) for k in ("wp_title", "wp_content", "fb_caption", "ig_caption", "li_text")),
            severity="error",
            reasons=[] if all(bool(str(content.get(k, "")).strip()) for k in ("wp_title", "wp_content", "fb_caption", "ig_caption", "li_text")) else ["missing_required_copy_field"],
            details={"fields": ["wp_title", "wp_content", "fb_caption", "ig_caption", "li_text"]},
        )
    )
    quality = score_generated_content(content)
    content["quality_score"] = quality.score
    content["quality_checks"] = quality.checks
    content["quality_warnings"] = quality.warnings
    for p in content["platform_posts"].values():
        p["quality_score"] = float(quality.score)
    content = _run_phase_c_conversion_gates(content, run_context, gate_records)
    content = _run_phase_d_brief_adherence(content, run_context, gate_records)
    content = _run_phase_f_visual_alignment(content, run_context, gate_records)
    content = _apply_control_plane_metadata(content, run_context, gate_records)
    content["consumer_copy_fidelity"] = assess_copy_fidelity(content)
    content = _sanitize_legacy_cta_in_payload(content)
    content["editorial_decision"] = {
        "content_bucket": content_bucket,
        "pillar": pillar,
        "product_mode": PILLAR_PRODUCT_MODE.get(pillar, "optional_product"),
        "want_product": want_product,
        "product_forced_override": bool(forced_product),
    }
    return content
