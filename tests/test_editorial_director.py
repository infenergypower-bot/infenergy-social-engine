from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, ROOT)
sys.path.insert(0, SCRIPTS)

import generate_posts  # noqa: E402

QUEUE = generate_posts.DEFAULT_TOPIC_QUEUE
PRODUCTS = [
    {"id": "PF-1", "name": "PowerFlex", "sku": "PF-1", "categories": ["Backup Power"], "metrics": ["500W", "avg 2h recharge"]},
    {"id": "PF-2", "name": "PowerFlex Mini", "sku": "PF-2", "categories": ["Backup Power"], "metrics": ["200W", "avg 1h recharge"]},
]


def _post(product_name: str = "", pillar: str = "preparedness_education", funnel_stage: str = "EDUCATION") -> dict:
    return {"product_name": product_name, "pillar": pillar, "funnel_stage": funnel_stage}


def _history(posts: list[dict]) -> dict:
    return {"posts": posts}


class EditorialDirectorTests(unittest.TestCase):
    def test_product_selection_randomizes_after_excluding_recent_product(self) -> None:
        history = _history([{
            "product_id": "PF-1",
            "product_name": "PowerFlex",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "status": "published",
        }])

        with patch("generate_posts.random.choice", side_effect=lambda pool: pool[-1]) as choose:
            selected = generate_posts._pick_product(PRODUCTS, history)

        self.assertEqual(selected["id"], "PF-2")
        self.assertEqual(selected["_rotation_decision"]["selection_reason"], "random_recent_exclusion")
        self.assertEqual(len(choose.call_args.args[0]), 1)

    def test_intervention_series_uses_least_recently_used_catalog_product(self) -> None:
        history = _history([{
            "product_id": "PF-1",
            "product_name": "PowerFlex",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "status": "published",
        }])

        selected = generate_posts._pick_series_product(PRODUCTS, history)

        self.assertEqual(selected["id"], "PF-2")
        self.assertEqual(selected["_rotation_decision"]["selection_reason"], "least_recently_used_catalog")

    def test_intervention_series_format_controls_visual_and_platform_contracts(self) -> None:
        series = {
            "id": "infenergy_intervention",
            "preferred_format": "product_micro_mission_comic",
            "story_pattern": "mistake_to_intervention_to_resolution",
            "originality_dimensions": ["scenario", "persona"],
        }
        visual_plan = generate_posts._apply_recurring_series_visual_plan({"gemini_image_prompt": "A grounded kitchen scene."}, series)
        content = {}
        platform_posts = {"facebook": {}, "instagram": {}, "linkedin": {}}
        generate_posts._apply_recurring_series_package(content, platform_posts, series)

        self.assertEqual(visual_plan["creative_route"], "PRODUCT_MICRO_MISSION_COMIC")
        self.assertIn("complete multi-panel comic page", visual_plan["gemini_image_prompt"])
        self.assertEqual(content["series_format"], "product_micro_mission_comic")
        self.assertEqual(platform_posts["instagram"]["media_type"], "STATIC")
        self.assertTrue(all(post["series_id"] == "infenergy_intervention" for post in platform_posts.values()))

    def test_intervention_series_reaches_orchestrator_pipeline_with_rotated_product(self) -> None:
        series = {"id": "infenergy_intervention", "product_required": True, "preferred_format": "cinematic_brand_poster"}
        captured = {}

        def route(*args, **kwargs):
            captured.update(kwargs)
            return {"status": "generated"}

        with patch.object(generate_posts, "select_weekly_sequence", return_value={"series": series}), patch.object(
            generate_posts, "load_products", return_value=PRODUCTS
        ), patch.object(generate_posts, "load_history", return_value=_history([])), patch.object(
            generate_posts, "_route_generate_orchestrator", side_effect=route
        ):
            result = generate_posts.generate("midday", pipeline_override="orchestrator")

        self.assertEqual(result["status"], "generated")
        self.assertEqual(captured["product_id_override"], "PF-1")
        self.assertEqual(captured["recurring_series"]["id"], "infenergy_intervention")

    def test_orchestrator_series_strategy_changes_copy_and_concept_brief(self) -> None:
        captured = {}
        series = {
            "id": "infenergy_intervention",
            "preferred_format": "product_micro_mission_comic",
            "story_pattern": "mistake_to_intervention_to_resolution",
            "originality_dimensions": ["scenario", "persona", "setting"],
        }

        with patch.object(generate_posts, "run_social_intelligence", side_effect=lambda **kwargs: captured.update(kwargs) or []), patch.object(
            generate_posts, "_living_strategy_for_generation", return_value=({
                "audience":"households", "customer_moment":"a familiar energy mistake",
                "human_need":"stay connected", "angle":"learn through a story",
                "positioning":"preparedness", "non_price_edge":"practical fit",
                "benefit":"less interruption", "human_outcome":"capability", "claim_limits":"verified product facts only",
            }, {})
        ):
            generate_posts._route_generate_orchestrator("midday", recurring_series=series)

        strategy = captured["approved_strategy"]
        self.assertEqual(strategy["series_id"], "infenergy_intervention")
        self.assertEqual(strategy["preferred_format"], "product_micro_mission_comic")
        self.assertEqual(strategy["originality_dimensions"], ["scenario", "persona", "setting"])
        self.assertIn("fresh avoidable energy mistake", strategy["angle"])

    def test_intervention_carousel_emits_strict_multi_slide_generation_contract(self) -> None:
        content = {
            "selected_hook": "Your phone is at one percent",
            "wp_excerpt": "Preparation protects the routines that matter.",
            "selected_cta": "Stay prepared",
            "pillar": "everyday_power",
        }
        series = {"id": "infenergy_intervention", "preferred_format": "educational_story_carousel"}
        product = {"name": "PowerFlex", "image_url": "https://example.test/powerflex.png"}

        generate_posts._apply_recurring_series_generation_contract(content, {"visual_objective": "A commuter loses phone access"}, series, product)

        generation = content["gemini_generation"]
        self.assertTrue(generation["strict_provider"])
        self.assertFalse(generation["fallback_allowed"])
        self.assertEqual(generation["required_image_count"], len(generation["prompts"]))
        self.assertGreaterEqual(generation["required_image_count"], 6)
        self.assertEqual(generation["prompts"][0]["role"], "COVER")
        self.assertEqual(generation["prompts"][-1]["role"], "FINALE")
        self.assertIn(product["image_url"], generation["reference_image_urls"])

    def test_bootstrap_history_biases_to_no_product(self) -> None:
        # Fewer than 3 recent posts: bucket should default to no_product to seed a healthy mix.
        history = _history([_post("PowerFlex"), _post("PowerFlex")])
        self.assertEqual(generate_posts._decide_content_bucket(history), "no_product")

    def test_max_two_consecutive_product_posts_forces_no_product(self) -> None:
        history = _history([_post(), _post("PowerFlex"), _post("PowerFlex"), _post("PowerFlex")])
        self.assertEqual(generate_posts._consecutive_product_posts(history), 3)
        self.assertEqual(generate_posts._decide_content_bucket(history), "no_product")

    def test_weekly_mix_snapshot_ratios(self) -> None:
        posts = (
            [_post("PowerFlex", pillar="product_education")] * 3
            + [_post("PowerFlex", pillar="readiness_assessment_lead_gen")]
            + [_post("", pillar="preparedness_education")] * 6
        )
        snapshot = generate_posts._weekly_mix_snapshot(_history(posts), window=14)
        self.assertEqual(snapshot["total"], 10)
        self.assertAlmostEqual(snapshot["non_product_ratio"], 0.6)
        self.assertAlmostEqual(snapshot["product_education_ratio"], 0.3)
        self.assertAlmostEqual(snapshot["conversion_ratio"], 0.1)

    def test_deficit_in_product_education_selects_that_bucket(self) -> None:
        # Non-product and conversion targets already met; product_education is under target.
        # Interleaved so the trailing max-2-consecutive-product-posts guardrail doesn't fire.
        posts = [
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
        ]
        history = _history(posts)
        self.assertEqual(generate_posts._decide_content_bucket(history), "product_education")


    def test_select_editorial_plan_no_product_bucket_has_no_product(self) -> None:
        history = _history([_post("PowerFlex")] * 2)
        plan = generate_posts.select_editorial_plan(QUEUE, history, PRODUCTS, "EDUCATION")
        self.assertEqual(plan["content_bucket"], "no_product")
        self.assertIsNone(plan["product"])
        self.assertFalse(plan["want_product"])
        self.assertIn(plan["pillar"], QUEUE["pillars"])
        self.assertTrue(plan["topic"])

    def test_forced_no_product_overrides_history_that_requests_a_product(self) -> None:
        posts = [
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
        ]

        plan = generate_posts.select_editorial_plan(
            QUEUE,
            _history(posts),
            PRODUCTS,
            "DESIRE",
            no_product=True,
        )

        self.assertEqual(plan["content_bucket"], "no_product")
        self.assertIsNone(plan["product"])
        self.assertFalse(plan["want_product"])

    def test_forced_no_product_honors_requested_editorial_pillar(self) -> None:
        plan = generate_posts.select_editorial_plan(
            QUEUE,
            _history([]),
            PRODUCTS,
            "ATTENTION",
            no_product=True,
            pillar_override="community_engagement",
        )

        self.assertEqual(plan["pillar"], "community_engagement")
        self.assertIsNone(plan["product"])

    def test_modular_power_system_profile_uses_concrete_outage_job(self) -> None:
        profile = generate_posts._product_copy_profile({
            "id": "SOREIN-SYSTEM",
            "name": "Sorein Modular Power System",
            "categories": [],
            "metrics": ["1,800W", "1,000W"],
        })

        self.assertEqual(
            profile["benefit"],
            "powers compatible must-run devices during outages and off-grid use",
        )
        self.assertNotIn("supports a more reliable preparedness setup", profile["benefit"])

    def test_select_editorial_plan_product_bucket_has_product(self) -> None:
        posts = [
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
            _post("PowerFlex", pillar="readiness_assessment_lead_gen"),
            _post("", pillar="preparedness_education"),
            _post("", pillar="preparedness_education"),
        ]
        history = _history(posts)
        plan = generate_posts.select_editorial_plan(QUEUE, history, PRODUCTS, "EDUCATION")
        self.assertEqual(plan["content_bucket"], "product_education")
        self.assertIsNotNone(plan["product"])
        self.assertTrue(plan["want_product"])


    def test_build_talking_point_no_product_never_names_a_product(self) -> None:
        for pillar in QUEUE["pillars"]:
            talking_point = generate_posts._build_talking_point_no_product("Sample topic text", "EDUCATION", pillar)
            combined = " ".join(str(v) for v in talking_point.values()).lower()
            for product in PRODUCTS:
                self.assertNotIn(product["name"].lower(), combined)
            self.assertNotIn("buy now", combined)
            self.assertNotIn("order now", combined)

    def test_fallback_content_no_product_has_no_product_name_or_price(self) -> None:
        talking_point = generate_posts._build_talking_point_no_product(
            "Why resilience planning matters", "EDUCATION", "preparedness_education"
        )
        content = generate_posts._build_fallback_content_no_product(
            "morning", "Why resilience planning matters", "preparedness_education", {}, talking_point=talking_point
        )
        for key in ("wp_title", "wp_content", "wp_excerpt", "fb_caption", "ig_caption", "li_text"):
            self.assertTrue(str(content.get(key, "")).strip(), msg=f"{key} should not be empty")
        full_text = " ".join(str(content.get(k, "")) for k in ("wp_content", "fb_caption", "ig_caption", "li_text")).lower()
        for product in PRODUCTS:
            self.assertNotIn(product["name"].lower(), full_text)
        self.assertNotIn("$", full_text)

    def test_topic_queue_migration_detects_stale_off_brand_queue(self) -> None:
        stale = {
            "pillars": ["solar_savings", "energy_independence", "battery_storage"],
            "topics": {"solar_savings": ["Federal and state solar tax credits available right now"]},
        }
        self.assertTrue(generate_posts._topic_queue_needs_migration(stale))

    def test_topic_queue_migration_accepts_current_queue(self) -> None:
        self.assertFalse(generate_posts._topic_queue_needs_migration(QUEUE))

    def test_topic_queue_migration_rejects_empty_or_missing(self) -> None:
        self.assertTrue(generate_posts._topic_queue_needs_migration({}))
        self.assertTrue(generate_posts._topic_queue_needs_migration({"pillars": []}))
        self.assertTrue(generate_posts._topic_queue_needs_migration(None))

    def test_generate_honors_product_id_override_regardless_of_bucket(self) -> None:
        os.environ["GEMINI_API_KEY"] = ""
        history = _history([_post("PowerFlex")] * 2)  # would otherwise bias to no_product
        import json
        import tempfile

        # Isolate history/products for this run via monkeypatched loaders.
        original_load_history = generate_posts.load_history
        original_load_products = generate_posts.load_products
        try:
            generate_posts.load_history = lambda: history
            generate_posts.load_products = lambda: PRODUCTS
            content = generate_posts.generate("morning", product_id_override="PF-1", pipeline_override="legacy")
        finally:
            generate_posts.load_history = original_load_history
            generate_posts.load_products = original_load_products

        self.assertEqual(content.get("product_id"), "PF-1")
        decision = content.get("editorial_decision", {})
        self.assertTrue(decision.get("want_product"))
        self.assertTrue(decision.get("product_forced_override"))


if __name__ == "__main__":
    unittest.main()
