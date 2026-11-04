from __future__ import annotations

import os
import sys
import unittest
import tempfile
import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, ROOT)
sys.path.insert(0, SCRIPTS)

import generate_posts  # noqa: E402
import run_engine  # noqa: E402
import anti_repeat  # noqa: E402
import worker  # noqa: E402
from agent_control_plane import evaluate_global_gates, validate_agent_output  # noqa: E402
from anti_repeat import check_duplicates  # noqa: E402
from build_utm_url import build_utm_url  # noqa: E402
from validate_product_claims import validate_generated_content  # noqa: E402
from social import memory_intelligence  # noqa: E402
from social.publish_decision import decide as decide_publication  # noqa: E402


def _base_content() -> dict:
    return {
        "date": "2026-08-06",
        "topic": "Why resilience planning matters",
        "pillar": "energy_independence",
        "topic_hash": "topic123",
        "selected_hook": "Most homes miss this outage planning step?",
        "selected_hook_type": "question",
        "selected_cta": "Save this checklist.",
        "hook_hash": "hook123",
        "cta_hash": "cta123",
        "funnel_stage": "EDUCATION",
        "audience_segment": "Prepared Buyer",
        "campaign_id": "camp_123",
        "destination_url": "https://example.com/product?ref=base",
        "product_id": "prod_1",
        "product_name": "PowerFlex",
        "product_sku": "PF-1",
        "wp_title": "What a stronger home power plan looks like",
        "wp_content": "<p>Useful 200W guidance repeated. </p>" * 40,
        "fb_caption": "Most homes miss this outage planning step? Use 200W planning logic. #Energy #Backup #Home",
        "ig_caption": "Plan smarter\nUse 200W guidance. #Energy #Backup #Home #Prep #Solar",
        "li_text": "A practical resilience model with 200W planning details.",
        "platform_posts": {
            "facebook": {
                "platform": "facebook",
                "cta": "Save this checklist.",
                "content_format": "community_story",
                "visual_direction": "single_image",
            },
            "instagram": {
                "platform": "instagram",
                "cta": "Save this checklist.",
                "content_format": "short_caption",
                "visual_direction": "carousel",
            },
            "linkedin": {
                "platform": "linkedin",
                "cta": "Review the framework.",
                "content_format": "authority_post",
                "visual_direction": "insight_graphic",
            },
        },
    }


class PhaseThirteenFourteenTests(unittest.TestCase):
    def test_future_inventory_routes_using_content_date(self) -> None:
        fallback = datetime(2026, 8, 19, 16, tzinfo=timezone.utc)
        routed = run_engine._routing_datetime("2026-08-20", fallback)

        self.assertEqual(routed.strftime("%A"), "Thursday")
        self.assertEqual(routed.date().isoformat(), "2026-08-20")

    def test_material_change_angle_persists_conditional_lesson_for_matching_future_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {"DATA_DIR": data_dir}, clear=False):
            strategy = {
                "strategy_version": 1,
                "strategy_red_team": {
                    "verdict": "CHANGE_ANGLE",
                    "evidence_requirements": ["runtime"],
                    "challenge_evidence": ["verified_runtime_evidence_missing"],
                },
            }
            lessons = run_engine._record_material_strategy_lessons({"product_id": "PPP-200"}, strategy)
            matching = memory_intelligence.strategy_lessons(
                product_id="PPP-200", condition="runtime_angle_without_verified_evidence", data_dir=data_dir
            )
            unrelated = memory_intelligence.strategy_lessons(
                product_id="OTHER", condition="runtime_angle_without_verified_evidence", data_dir=data_dir
            )

        self.assertEqual(len(lessons), 1)
        self.assertEqual(len(matching), 1)
        self.assertEqual(unrelated, [])
        self.assertEqual(matching[0]["source_decision"], "CHANGE_ANGLE")
        self.assertIn("reconsider", matching[0]["revalidation"])

    def test_publication_gate_failure_becomes_scoped_product_intelligence(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {"DATA_DIR": data_dir}, clear=False):
            lessons = run_engine._record_publication_gate_lessons(
                {"product_id": "SM-PRO"},
                ["wattage_not_verified:5kw", "novelty_angle_weak"],
            )
            matching = memory_intelligence.strategy_lessons(
                product_id="SM-PRO", condition="publication_gate:wattage_not_verified", data_dir=data_dir
            )

        self.assertEqual(len(lessons), 1)
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["evidence"], ["wattage_not_verified:5kw"])

    def test_selected_visual_render_temporarily_lifts_text_only_mode(self) -> None:
        observed = []

        def fake_generate_visuals(content, visual_plan=None):
            observed.append(os.environ.get("POST_TEXT_ONLY"))
            return {"facebook": "final.png"}

        with patch.dict(os.environ, {"POST_TEXT_ONLY": "true"}, clear=False), patch.object(
            run_engine.generate_posts, "generate_visuals", side_effect=fake_generate_visuals
        ):
            result = run_engine._render_selected_visuals({"visual_plan": {"route": "gemini"}})
            restored = os.environ.get("POST_TEXT_ONLY")

        self.assertEqual(observed, [None])
        self.assertEqual(restored, "true")
        self.assertEqual(result["facebook"], "final.png")

    def test_final_artifact_qa_is_persistable_for_active_facebook_only(self) -> None:
        content = {"generated_visuals": {"facebook": "C:/tmp/final.png"}}
        review = {"verdict": "PASS", "issues": [], "inspected_path": "C:/tmp/final.png", "dimensions": [1080, 1080]}
        with patch.object(run_engine, "review_rendered_visual", return_value=review) as inspect:
            result = run_engine._ensure_final_artifact_qa(content, {"facebook": True, "instagram": False, "linkedin": False, "wordpress": False})

        inspect.assert_called_once_with("C:/tmp/final.png", "facebook")
        self.assertEqual(result["facebook"], review)
        self.assertEqual(content["artifact_visual_qa"]["facebook"]["verdict"], "PASS")

    def test_final_artifact_qa_uses_carried_forward_review_path(self) -> None:
        content = {
            "generated_visuals": {
                "artifact_reviews": {"facebook": {"artifact_path": "C:/tmp/carried-forward.png"}},
            }
        }
        review = {"verdict": "PASS", "issues": [], "inspected_path": "C:/tmp/carried-forward.png", "dimensions": [1200, 1200]}
        with patch.object(run_engine, "review_rendered_visual", return_value=review) as inspect:
            result = run_engine._ensure_final_artifact_qa(content, {"facebook": True, "instagram": False, "linkedin": False, "wordpress": False})

        inspect.assert_called_once_with("C:/tmp/carried-forward.png", "facebook")
        for key, value in review.items():
            self.assertEqual(result["facebook"][key], value)
        self.assertEqual(result["facebook"]["artifact_path"], "C:/tmp/carried-forward.png")

    def test_final_artifact_qa_reads_a_real_file_before_pass(self) -> None:
        from PIL import Image

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image_path = image_file.name
        try:
            Image.new("RGB", (1200, 1200), "#123456").save(image_path, format="PNG")
            expected_size = os.path.getsize(image_path)
            content = {"generated_visuals": {"facebook": image_path}}
            result = run_engine._ensure_final_artifact_qa(
                content, {"facebook": True, "instagram": False, "linkedin": False, "wordpress": False}
            )
        finally:
            os.unlink(image_path)

        self.assertEqual(result["facebook"]["verdict"], "PASS")
        self.assertEqual(result["facebook"]["dimensions"], [1200, 1200])
        self.assertEqual(result["facebook"]["file_size"], expected_size)

    def test_visual_carry_forward_requires_passing_matching_strategy(self) -> None:
        content = _base_content()
        content["visual_plan"] = {"composition": "proof-led", "image_strategy": "gemini_generated"}
        visuals = {
            "facebook": "C:/tmp/passing.png",
            "artifact_reviews": {"facebook": {"verdict": "PASS", "artifact_path": "C:/tmp/passing.png"}},
        }
        visuals["strategy_fingerprint"] = run_engine._visual_strategy_fingerprint(content)
        channels = {"facebook": True, "instagram": False, "linkedin": False, "wordpress": False}

        self.assertTrue(run_engine._can_carry_forward_visuals(visuals, content, channels))
        content["visual_plan"] = {"composition": "comparison-led", "image_strategy": "gemini_generated"}
        self.assertFalse(run_engine._can_carry_forward_visuals(visuals, content, channels))

    def test_failed_visual_cannot_carry_forward(self) -> None:
        content = _base_content()
        visuals = {
            "facebook": "",
            "artifact_reviews": {"facebook": {"verdict": "REGENERATE_VISUAL", "artifact_path": ""}},
            "strategy_fingerprint": run_engine._visual_strategy_fingerprint(content),
        }
        channels = {"facebook": True, "instagram": False, "linkedin": False, "wordpress": False}
        self.assertFalse(run_engine._can_carry_forward_visuals(visuals, content, channels))

    def test_subthreshold_critic_persists_actionable_findings_not_only_wrapper(self) -> None:
        decision = decide_publication(
            legacy_score={"total": 96, "platform_results": {}},
            validation={"passed": True, "errors": []},
            duplicates={"ok": True, "reasons": []},
            orchestrator_quality={
                "overall": 79.2,
                "component_scores": {"specificity": 0.5, "novelty": 0.55},
                "critic_findings": ["specificity_weak", "novelty_angle_weak"],
                "critic_evidence": [{"component": "specificity", "score": 0.5}],
            },
        )

        self.assertEqual(decision["decision"], "do_not_publish")
        self.assertIn("specificity_weak", decision["reasons"])
        self.assertIn("critic_below_publish_threshold", decision["reasons"])
        self.assertEqual(decision["critic_component_scores"]["specificity"], 0.5)

    def test_facebook_readiness_does_not_depend_on_wordpress(self) -> None:
        class Response:
            ok = True

        with patch.object(run_engine.requests, "get", return_value=Response()), patch.dict(
            os.environ, {"META_PAGE_ACCESS_TOKEN": "token", "META_PAGE_ID": "page"}, clear=False
        ):
            readiness = run_engine._build_phase5_channel_readiness(
                {"facebook": True, "instagram": False, "linkedin": False, "wordpress": False}, dry_run=True
            )

        self.assertEqual(readiness["checks"]["wordpress"]["status"], "yellow")
        self.assertEqual(readiness["checks"]["facebook"]["status"], "green")
        self.assertEqual(readiness["overall"], "pass")

    def test_generate_without_gemini_keeps_unapproved_draft_available(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            content = generate_posts.generate("morning", pipeline_override="legacy")
        self.assertFalse(bool(content.get("orchestration_blocked")))
        self.assertEqual(content.get("copy_generation_source"), "deterministic_fallback")
        self.assertFalse(content.get("production_approval"))
        from publication_authorization import authorize
        with patch.dict(os.environ, {"EDITORIAL_APPROVAL_MODE":"required"}):
            with self.assertRaisesRegex(ValueError,"editorial_approval_required"):
                authorize(content,"facebook")

    def test_phase2_creative_stack_fallback_is_schema_valid(self) -> None:
        stack = generate_posts._run_phase2_creative_stack(
            [],
            topic="Why resilience planning matters",
            funnel_stage="EDUCATION",
            stage_objective="Teach a practical framework",
            selected_hook="Most homes miss this outage planning step?",
            selected_cta="Save this checklist.",
            audience_segment="Prepared Buyer",
            product_name="PowerFlex",
            product_categories="home backup",
            product_metrics="200W",
            recent_hooks=["Older hook"],
            recent_topics=["Older topic"],
        )

        for agent_name in (
            "ideation_divergence",
            "audience_psychographics",
            "narrative_architect",
            "platform_voice_calibrator",
            "hook_stress_test",
        ):
            _, errors = validate_agent_output(agent_name, stack.get(agent_name, {}))
            self.assertEqual(errors, [], msg=f"schema errors for {agent_name}: {errors}")

    def test_control_plane_schema_validation_rejects_bad_pregen_shape(self) -> None:
        payload = {
            "recommended_hook": "Fresh angle",
            "recommended_cta": "Book a call",
            # Missing required fields on purpose.
        }
        _, errors = validate_agent_output("pre_generation_conference", payload)
        self.assertGreater(len(errors), 0)

    def test_control_plane_global_gate_fails_on_error_gate(self) -> None:
        result = evaluate_global_gates(
            [
                {"gate_id": "a", "passed": True, "severity": "error", "reasons": []},
                {"gate_id": "b", "passed": False, "severity": "error", "reasons": ["bad_shape"]},
            ]
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "fail")

    def test_phase3_phase4_phase7_schema_payloads_validate(self) -> None:
        phase3 = {
            "precision_claims_verifier": {"passed": True, "issues": [], "required_fixes": []},
            "compliance_policy_sentinel": {"risk_level": "low", "blocked_terms": [], "required_actions": []},
            "semantic_novelty": {"novelty_score": 0.8, "signal": "high", "rewrite_guidance": []},
        }
        phase4 = {
            "visual_strategy": {
                "visual_objective": "Test objective",
                "composition_adjustments": ["Adjust focus"],
                "platform_focus": {"facebook": "a", "instagram": "b", "linkedin": "c"},
            },
            "cta_optimization": {
                "recommended_cta": "Book now",
                "alternates": ["Try this"],
                "friction_note": "shorten first step",
            },
        }
        phase7 = {
            "pre_generation_packet": {"topic": "x"},
            "pre_publish_packet": {"safety": "ok"},
            "post_run_packet": {"notes": "ok"},
        }

        for agent_name, payload in (
            ("precision_claims_verifier", phase3["precision_claims_verifier"]),
            ("compliance_policy_sentinel", phase3["compliance_policy_sentinel"]),
            ("semantic_novelty", phase3["semantic_novelty"]),
            ("visual_strategy", phase4["visual_strategy"]),
            ("cta_optimization", phase4["cta_optimization"]),
            ("phase7_conference_packets", phase7),
        ):
            _, errors = validate_agent_output(agent_name, payload)
            self.assertEqual(errors, [], msg=f"schema errors for {agent_name}: {errors}")

    def test_phase5_readiness_reports_disabled_channels(self) -> None:
        readiness = run_engine._build_phase5_channel_readiness(
            {
                "wordpress": False,
                "facebook": False,
                "instagram": False,
                "linkedin": False,
            },
            dry_run=True,
        )
        self.assertEqual(readiness["overall"], "pass")
        self.assertEqual(readiness["checks"]["facebook"]["status"], "yellow")
        self.assertEqual(readiness["checks"]["linkedin"]["status"], "yellow")

    def test_build_utm_url_preserves_query_and_term(self) -> None:
        result = build_utm_url(
            "https://example.com/product?ref=base&x=1",
            source="instagram",
            campaign="outage_readiness_2026_w32",
            content="power_station_reel_desire",
            term="remote_workers",
        )
        self.assertTrue(result["ok"])
        self.assertIn("ref=base", result["utm_url"])
        self.assertIn("x=1", result["utm_url"])
        self.assertIn("utm_medium=organic_social", result["utm_url"])
        self.assertIn("utm_term=remote_workers", result["utm_url"])

    def test_duplicate_detection_flags_recent_repeat(self) -> None:
        content = _base_content()
        history = {
            "posts": [
                {
                    "run_started_at_utc": "2099-01-01T00:00:00+00:00",
                    "topic_hash": "topic123",
                    "hook_hash": "hook123",
                    "cta_hash": "cta123",
                    "product_id": "prod_1",
                    "product_name": "PowerFlex",
                    "product_sku": "PF-1",
                    "opening_signature": "abc",
                }
            ]
        }
        result = check_duplicates(content, history)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reasons"], [])
        self.assertIn("duplicate_topic_within_window", result["observed_reasons"])
        self.assertIn("duplicate_hook_within_window", result["observed_reasons"])
        self.assertIn("duplicate_cta_within_window", result["observed_reasons"])
        self.assertIn("duplicate_product_within_window", result["observed_reasons"])

    def test_duplicate_policy_can_configure_product_as_a_blocker(self) -> None:
        content = _base_content()
        history = {
            "posts": [{
                "run_started_at_utc": "2099-01-01T00:00:00+00:00",
                "product_id": "prod_1",
                "product_name": "PowerFlex",
                "product_sku": "PF-1",
            }]
        }

        result = check_duplicates(
            content,
            history,
            windows={"blocking_signatures": ["exact_caption", "product"], "max_violations_allowed": 0},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reasons"], ["duplicate_product_within_window"])

    def test_runtime_duplicate_config_preserves_shipped_policy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            marketing_dir = os.path.join(data_dir, "marketing")
            os.makedirs(marketing_dir)
            with open(os.path.join(marketing_dir, "anti_repeat_config.json"), "w", encoding="utf-8") as handle:
                json.dump({"product_feature_days": 3}, handle)
            with patch.object(anti_repeat, "MARKETING_DIR", marketing_dir):
                config = anti_repeat.load_anti_repeat_windows()

        self.assertEqual(config["product_feature_days"], 3)
        self.assertEqual(config["disabled_signatures"], ["cta"])
        self.assertEqual(config["blocking_signatures"], ["exact_caption"])

    def test_production_readiness_snapshot_reports_effective_controls_without_secrets(self) -> None:
        with patch.dict(os.environ, {
            "SOCIAL_DRY_RUN": "false",
            "SOCIAL_SHADOW_MODE": "false",
            "ENABLE_FACEBOOK": "true",
            "ENABLE_INSTAGRAM": "true",
            "ENABLE_LINKEDIN": "true",
            "META_PAGE_ID": "page-id",
            "META_PAGE_ACCESS_TOKEN": "secret-page-token",
            "META_IG_USER_ID": "ig-id",
            "LINKEDIN_ACCESS_TOKEN": "secret-linkedin-token",
            "LINKEDIN_ORGANIZATION_URN": "urn:li:organization:1",
            "GEMINI_API_KEY": "secret-gemini-key",
            "DATA_DIR": "C:/test-data",
        }, clear=False), patch.object(worker, "_data_dir", return_value="C:/test-data"), patch.object(worker.os.path, "isdir", return_value=True), patch.object(worker.os, "access", return_value=True), patch.object(worker.os.path, "isfile", return_value=True):
            snapshot = worker._production_readiness_snapshot()

        self.assertFalse(snapshot["global"]["dry_run"])
        self.assertFalse(snapshot["global"]["shadow_mode"])
        self.assertTrue(snapshot["channels"]["facebook"])
        self.assertTrue(snapshot["platform_configuration_present"]["instagram"])
        self.assertTrue(snapshot["linkedin_target_configuration"]["explicit_target_present"])
        self.assertTrue(snapshot["gemini"]["api_key_present"])
        self.assertNotIn("secret-page-token", str(snapshot))
        self.assertNotIn("secret-linkedin-token", str(snapshot))
        self.assertNotIn("secret-gemini-key", str(snapshot))

    def test_duplicate_conflicts_consume_retry_budget_and_product_conflicts_reset_lock(self) -> None:
        reasons = ["duplicate_cta_within_window", "duplicate_product_within_window"]

        retryability = run_engine._retryability_classification(
            {"reasons": reasons},
            reasons,
        )

        self.assertEqual(retryability, "RETRYABLE_CONTENT")
        self.assertTrue(run_engine._duplicate_conflict_requires_fresh_product(reasons))
        self.assertFalse(run_engine._duplicate_conflict_requires_fresh_product(["duplicate_cta_within_window"]))

    def test_duplicate_detection_accepts_old_history_records(self) -> None:
        content = _base_content()
        history = {
            "posts": [
                {"date": "2026-01-01", "topic": "Older entry only", "wp_id": "123"},
                {"date": "2026-01-02", "slot": "midday"},
            ]
        }
        result = check_duplicates(content, history)
        self.assertIn("ok", result)
        self.assertIn("signatures", result)

    def test_claim_validator_rejects_unavailable_product(self) -> None:
        content = {
            "product_name": "PowerFlex",
            "product_metrics": ["200W"],
            "product_facts": "Published 200W spec.",
            "product_price": "499",
            "product_url": "https://example.com/product",
            "product_in_stock": "false",
            "wp_content": "<p>Uses verified 200W specs.</p>",
            "fb_caption": "",
            "ig_caption": "",
            "li_text": "",
        }
        result = validate_generated_content(content)
        self.assertFalse(result["passed"])
        self.assertIn("product_unavailable_or_out_of_stock", result["errors"])

    def test_missing_image_fallback_uses_category_defaults(self) -> None:
        urls = generate_posts._fallback_images_for_categories(["Solar Panels"])
        self.assertGreaterEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("https://"))

    def test_run_engine_quality_exhaustion_does_not_become_content_failure(self) -> None:
        save_calls: list[dict] = []
        first = deepcopy(_base_content())
        second = deepcopy(_base_content())
        second["topic_hash"] = "topic456"

        with patch.object(run_engine.generate_posts, "ensure_runtime_data", return_value=None), \
            patch.object(run_engine.generate_posts, "generate", side_effect=[first, second]) as mock_generate, \
            patch.object(run_engine.generate_posts, "load_history", return_value={"posts": []}), \
            patch.object(run_engine.generate_posts, "save_history", side_effect=lambda history: save_calls.append(history)), \
            patch.object(run_engine, "load_channel_schedule", return_value={}), \
            patch.object(
                run_engine,
                "eligible_channels_for_slot",
                return_value={
                    "wordpress": (False, "disabled_env"),
                    "facebook": (False, "not_scheduled"),
                    "instagram": (False, "not_scheduled"),
                    "linkedin": (False, "not_scheduled"),
                },
            ), \
            patch.object(run_engine, "validate_generated_content", return_value={"passed": True, "errors": [], "warnings": []}), \
            patch.object(
                run_engine,
                "score_content",
                side_effect=[
                    {"total": 78, "decision": "regenerate_once", "component_scores": {}},
                    {"total": 74, "decision": "reject", "component_scores": {}},
                ],
            ), \
            patch.object(run_engine, "load_anti_repeat_windows", return_value={}), \
            patch.object(run_engine, "check_duplicates", return_value={"ok": True, "reasons": [], "signatures": {}}), \
            patch.dict(os.environ, {"SOCIAL_DRY_RUN": "true", "POST_SLOT": "morning", "POST_CANDIDATE_COUNT": "2"}, clear=False):
            run_engine.main()

        self.assertEqual(mock_generate.call_count, 1)
        self.assertEqual(save_calls[-1]["posts"][-1]["status"], "skipped_no_eligible_platforms")
        self.assertEqual(len(save_calls[-1]["posts"][-1]["generation_attempts"]), 1)

    def test_run_engine_selects_highest_quality_candidate_from_seven(self) -> None:
        save_calls: list[dict] = []
        candidates = []
        for number in range(1, 8):
            candidate = deepcopy(_base_content())
            candidate["post_id"] = f"candidate-{number}"
            candidate["topic_hash"] = f"topic-{number}"
            candidates.append(candidate)
        scores = [82, 91, 87, 94, 89, 90, 86]
        decisions = [
            {"decision": "publish", "publishable": True, "reasons": [], "orchestrator_critic_score": 85}
            for _ in candidates
        ] + [{"decision": "publish", "publishable": True, "reasons": [], "orchestrator_critic_score": 85}]

        with patch.object(run_engine.generate_posts, "ensure_runtime_data", return_value=None), \
            patch.object(run_engine.generate_posts, "generate", side_effect=candidates) as mock_generate, \
            patch.object(run_engine.generate_posts, "load_history", return_value={"posts": []}), \
            patch.object(run_engine.generate_posts, "save_history", side_effect=lambda history: save_calls.append(history)), \
            patch.object(run_engine, "load_channel_schedule", return_value={}), \
            patch.object(run_engine, "eligible_channels_for_slot", return_value={
                "wordpress": (False, "disabled_env"), "facebook": (False, "not_scheduled"),
                "instagram": (False, "not_scheduled"), "linkedin": (False, "not_scheduled"),
            }), \
            patch.object(run_engine, "validate_generated_content", return_value={"passed": True, "errors": [], "warnings": []}), \
            patch.object(run_engine, "score_content", side_effect=[{"total": score, "decision": "approve", "component_scores": {}} for score in scores]), \
            patch.object(run_engine, "decide_publication", side_effect=decisions), \
            patch.object(run_engine, "load_anti_repeat_windows", return_value={}), \
            patch.object(run_engine, "check_duplicates", return_value={"ok": True, "reasons": [], "signatures": {}}), \
            patch.dict(os.environ, {"SOCIAL_DRY_RUN": "true", "POST_SLOT": "morning", "POST_CANDIDATE_COUNT": "7"}, clear=False):
            run_engine.main()

        self.assertEqual(mock_generate.call_count, 7)
        saved_post = save_calls[-1]["posts"][-1]
        self.assertEqual(saved_post["post_id"], "candidate-4")
        self.assertEqual(saved_post["candidate_selection"], {
            "candidate_count": 7,
            "publishable_count": 7,
            "selected_attempt": 4,
            "selection_reason": "highest_complete_content_package",
        })
        self.assertEqual(len(saved_post["generation_attempts"]), 7)

    def test_run_engine_revise_retries_with_critic_feedback_and_strategy_lock(self) -> None:
        save_calls: list[dict] = []
        strategy_lock = {"audience": "mobile professional", "angle": "match power to devices", "topic": "Power Stations"}
        first = deepcopy(_base_content())
        first["product_id"] = "PPP-200"
        first["copy"] = {"strategy_lock": strategy_lock}
        first["final_platform_copy_reviews"] = {"facebook": {"issues": ["primary_benefit_not_explicit"]}}
        first["generated_visuals"] = {"facebook": "/tmp/passing-visual.png"}
        second = deepcopy(first)
        second["topic_hash"] = "topic456"
        second["final_platform_copy_reviews"] = {"facebook": {"issues": []}}
        decisions = [
            {"decision": "revise", "publishable": False, "reasons": ["orchestrator_critic_requires_revision"], "orchestrator_critic_score": 76},
            {"decision": "publish", "publishable": True, "reasons": [], "orchestrator_critic_score": 84},
            {"decision": "publish", "publishable": True, "reasons": [], "orchestrator_critic_score": 84},
        ]

        with patch.object(run_engine.generate_posts, "ensure_runtime_data", return_value=None), \
            patch.object(run_engine.generate_posts, "generate", side_effect=[first, second]) as mock_generate, \
            patch.object(run_engine.generate_posts, "load_history", return_value={"posts": []}), \
            patch.object(run_engine.generate_posts, "save_history", side_effect=lambda history: save_calls.append(history)), \
            patch.object(run_engine, "load_channel_schedule", return_value={}), \
            patch.object(run_engine, "eligible_channels_for_slot", return_value={
                "wordpress": (False, "disabled_env"), "facebook": (False, "not_scheduled"),
                "instagram": (False, "not_scheduled"), "linkedin": (False, "not_scheduled"),
            }), \
            patch.object(run_engine, "validate_generated_content", return_value={"passed": True, "errors": [], "warnings": []}), \
            patch.object(run_engine, "score_content", return_value={"total": 90, "decision": "approve", "component_scores": {}}), \
            patch.object(run_engine, "decide_publication", side_effect=decisions), \
            patch.object(run_engine, "load_anti_repeat_windows", return_value={}), \
            patch.object(run_engine, "check_duplicates", return_value={"ok": True, "reasons": [], "signatures": {}}), \
            patch.dict(os.environ, {"SOCIAL_DRY_RUN": "true", "POST_SLOT": "morning", "POST_CANDIDATE_COUNT": "2"}, clear=False):
            run_engine.main()

        self.assertEqual(mock_generate.call_count, 2)
        retry_kwargs = mock_generate.call_args_list[1].kwargs
        self.assertEqual(retry_kwargs["approved_strategy"], strategy_lock)
        self.assertEqual(retry_kwargs["product_id_override"], "PPP-200")
        self.assertIn("orchestrator_critic_requires_revision", retry_kwargs["revision_feedback"])
        self.assertIn("primary_benefit_not_explicit", retry_kwargs["revision_feedback"])
        self.assertEqual(second["generated_visuals"], first["generated_visuals"])
        self.assertEqual(second["revision_reused_components"], ["generated_visuals"])
        attempts = save_calls[-1]["posts"][-1]["generation_attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["revision_scope"], "copy")
        self.assertEqual(attempts[0]["strategy_lock"], strategy_lock)
        self.assertEqual(attempts[1]["historical_feedback"], attempts[0]["current_candidate_findings"])
        self.assertEqual(attempts[1]["current_candidate_findings"], [])
        self.assertTrue(all(item["status"] == "resolved" for item in attempts[1]["issue_closure"]))
        self.assertEqual(attempts[0]["candidate"]["hook"], first["selected_hook"])

    def test_retryable_do_not_publish_revises_with_claim_safe_feedback(self) -> None:
        save_calls: list[dict] = []
        strategy_lock = {
            "audience": "mobile professional",
            "angle": "match power to devices",
            "topic": "Power Stations",
            "benefit": "keeps compatible daily devices charged away from outlets",
        }
        first = deepcopy(_base_content())
        first.update({
            "product_id": "PPP-200",
            "product_metrics": ["154Wh", "200W"],
            "product_facts": "154Wh capacity and 200W output.",
            "fb_caption": "A 40W fridge runs for 3.2 hours.",
            "copy": {"strategy_lock": strategy_lock, "body_text": "A 40W fridge runs for 3.2 hours."},
            "generated_visuals": {"facebook": "/tmp/passing-visual.png"},
        })
        second = deepcopy(first)
        second["topic_hash"] = "topic456"
        second["fb_caption"] = "Match compatible devices to verified output before leaving an outlet."
        second["copy"]["body_text"] = second["fb_caption"]
        decisions = [
            {"decision": "do_not_publish", "publishable": False, "reasons": ["wattage_not_verified:40w", "runtime_not_verified:3.2 hours", "runtime_claim_not_supported", "humanness below bar", "primary_benefit_not_explicit", "generic_or_ai_like_language"], "orchestrator_critic_score": 76},
            {"decision": "publish", "publishable": True, "reasons": [], "orchestrator_critic_score": 84},
            {"decision": "do_not_publish", "publishable": False, "reasons": ["test_final_stop"], "orchestrator_critic_score": 84},
        ]

        with patch.object(run_engine.generate_posts, "ensure_runtime_data", return_value=None), \
            patch.object(run_engine.generate_posts, "generate", side_effect=[first, second]) as mock_generate, \
            patch.object(run_engine.generate_posts, "load_history", return_value={"posts": []}), \
            patch.object(run_engine.generate_posts, "save_history", side_effect=lambda history: save_calls.append(history)), \
            patch.object(run_engine, "load_channel_schedule", return_value={}), \
            patch.object(run_engine, "eligible_channels_for_slot", return_value={
                "wordpress": (False, "disabled_env"), "facebook": (False, "not_scheduled"),
                "instagram": (False, "not_scheduled"), "linkedin": (False, "not_scheduled"),
            }), \
            patch.object(run_engine, "validate_generated_content", return_value={"passed": True, "errors": [], "warnings": []}), \
            patch.object(run_engine, "score_content", return_value={"total": 90, "decision": "approve", "component_scores": {}}), \
            patch.object(run_engine, "decide_publication", side_effect=decisions), \
            patch.object(run_engine, "load_anti_repeat_windows", return_value={}), \
            patch.object(run_engine, "check_duplicates", return_value={"ok": True, "reasons": [], "signatures": {}}), \
            patch.dict(os.environ, {"SOCIAL_DRY_RUN": "true", "POST_SLOT": "morning", "POST_CANDIDATE_COUNT": "2"}, clear=False):
            run_engine.main()

        self.assertEqual(mock_generate.call_count, 2)
        retry_kwargs = mock_generate.call_args_list[1].kwargs
        self.assertEqual(retry_kwargs["approved_strategy"], strategy_lock)
        self.assertEqual(retry_kwargs["product_id_override"], "PPP-200")
        self.assertIn("runtime_claim_not_supported", retry_kwargs["revision_feedback"])
        self.assertIn("humanness below bar", retry_kwargs["revision_feedback"])
        self.assertIn("primary_benefit_not_explicit", retry_kwargs["revision_feedback"])
        self.assertIn("generic_or_ai_like_language", retry_kwargs["revision_feedback"])
        self.assertEqual(second["generated_visuals"], first["generated_visuals"])
        self.assertNotIn("40W", first["fb_caption"])
        self.assertNotIn("3.2", first["fb_caption"])
        attempts = save_calls[-1]["posts"][-1]["generation_attempts"]
        self.assertEqual(attempts[0]["retryability_classification"], "RETRYABLE_CONTENT")
        self.assertEqual(attempts[1]["previous_decision"], "do_not_publish")
        self.assertEqual(attempts[1]["previous_current_findings"], attempts[0]["current_candidate_findings"])
        self.assertEqual(attempts[1]["historical_feedback"], attempts[0]["current_candidate_findings"])
        self.assertEqual(attempts[1]["current_candidate_findings"], [])
        self.assertTrue(attempts[0]["claim_corrections"])

    def test_terminal_do_not_publish_does_not_retry(self) -> None:
        save_calls: list[dict] = []
        content = deepcopy(_base_content())
        decision = {
            "decision": "do_not_publish",
            "publishable": False,
            "reasons": ["product_unavailable_or_out_of_stock"],
            "orchestrator_critic_score": 90,
        }

        with patch.object(run_engine.generate_posts, "ensure_runtime_data", return_value=None), \
            patch.object(run_engine.generate_posts, "generate", return_value=content) as mock_generate, \
            patch.object(run_engine.generate_posts, "load_history", return_value={"posts": []}), \
            patch.object(run_engine.generate_posts, "save_history", side_effect=lambda history: save_calls.append(history)), \
            patch.object(run_engine, "load_channel_schedule", return_value={}), \
            patch.object(run_engine, "eligible_channels_for_slot", return_value={
                "wordpress": (False, "disabled_env"), "facebook": (False, "not_scheduled"),
                "instagram": (False, "not_scheduled"), "linkedin": (False, "not_scheduled"),
            }), \
            patch.object(run_engine, "validate_generated_content", return_value={"passed": False, "errors": ["product_unavailable_or_out_of_stock"], "warnings": []}), \
            patch.object(run_engine, "score_content", return_value={"total": 90, "decision": "approve", "component_scores": {}}), \
            patch.object(run_engine, "decide_publication", return_value=decision), \
            patch.object(run_engine, "load_anti_repeat_windows", return_value={}), \
            patch.object(run_engine, "check_duplicates", return_value={"ok": True, "reasons": [], "signatures": {}}), \
            patch.dict(os.environ, {"SOCIAL_DRY_RUN": "true", "POST_SLOT": "morning", "POST_CANDIDATE_COUNT": "1"}, clear=False):
            run_engine.main()

        self.assertEqual(mock_generate.call_count, 1)
        attempt = save_calls[-1]["posts"][-1]["generation_attempts"][0]
        self.assertEqual(attempt["retryability_classification"], "TERMINAL")
        self.assertEqual(attempt["historical_feedback"], [])

    def test_run_engine_records_skipped_no_eligible_platforms(self) -> None:
        save_calls: list[dict] = []
        outcomes: list[dict] = []
        content = deepcopy(_base_content())
        content.pop("date")
        content.pop("pillar")
        content.pop("topic_hash")
        content["platform_posts"]["facebook"]["final_caption_qa"] = {
            "status": "REVISE_PRESENTATION",
            "reasons": ["planning_language"],
        }
        content["generated_visuals"] = {
            "render_engines": {"facebook": "approved_product_photo"},
            "artifact_reviews": {"facebook": {"verdict": "PASS", "issues": []}},
        }
        content["artifact_visual_qa"] = content["generated_visuals"]["artifact_reviews"]

        with patch.object(run_engine.generate_posts, "ensure_runtime_data", return_value=None), \
            patch.object(run_engine.generate_posts, "generate", return_value=content), \
            patch.object(run_engine.generate_posts, "load_history", return_value={"posts": []}), \
            patch.object(run_engine.generate_posts, "save_history", side_effect=lambda history: save_calls.append(history)), \
            patch.object(run_engine, "load_channel_schedule", return_value={}), \
            patch.object(
                run_engine,
                "eligible_channels_for_slot",
                return_value={
                    "wordpress": (False, "disabled_env"),
                    "facebook": (False, "not_scheduled"),
                    "instagram": (False, "not_scheduled"),
                    "linkedin": (False, "not_scheduled"),
                },
            ), \
            patch.object(run_engine, "validate_generated_content", return_value={"passed": True, "errors": [], "warnings": []}), \
            patch.object(run_engine, "score_content", return_value={"total": 90, "decision": "approve", "component_scores": {}}), \
            patch.object(run_engine, "load_anti_repeat_windows", return_value={}), \
            patch.object(run_engine, "check_duplicates", return_value={"ok": True, "reasons": [], "signatures": {}}), \
            patch.object(run_engine, "_write_run_outcome", side_effect=lambda status, **kwargs: outcomes.append({"status": status, **kwargs})), \
            patch.dict(os.environ, {"SOCIAL_DRY_RUN": "true", "POST_SLOT": "morning"}, clear=False):
            run_engine.main()

        saved_post = save_calls[-1]["posts"][-1]
        self.assertEqual(saved_post["status"], "skipped_no_eligible_platforms")
        self.assertEqual(saved_post["date"], saved_post["run_started_at_utc"][:10])
        self.assertIn("platform_records", saved_post)
        self.assertEqual(len(saved_post["platform_records"]), 4)
        self.assertEqual(saved_post["generated_visuals"]["render_engines"]["facebook"], "approved_product_photo")
        self.assertEqual(
            saved_post["platform_posts"]["facebook"]["final_caption_qa"]["reasons"],
            ["planning_language"],
        )
        self.assertEqual(saved_post["artifact_visual_qa"]["facebook"]["verdict"], "PASS")
        self.assertEqual(outcomes, [{"status": "skipped_no_eligible_platforms", "slot": "morning", "detail": "no_eligible_platforms"}])

    def test_run_engine_blocks_when_orchestration_control_plane_fails(self) -> None:
        save_calls: list[dict] = []
        content = deepcopy(_base_content())
        content["orchestration_blocked"] = True

        with patch.object(run_engine.generate_posts, "ensure_runtime_data", return_value=None), \
            patch.object(run_engine.generate_posts, "generate", return_value=content), \
            patch.object(run_engine.generate_posts, "load_history", return_value={"posts": []}), \
            patch.object(run_engine.generate_posts, "save_history", side_effect=lambda history: save_calls.append(history)), \
            patch.object(run_engine, "load_channel_schedule", return_value={}), \
            patch.object(
                run_engine,
                "eligible_channels_for_slot",
                return_value={
                    "wordpress": (False, "disabled_env"),
                    "facebook": (False, "not_scheduled"),
                    "instagram": (False, "not_scheduled"),
                    "linkedin": (False, "not_scheduled"),
                },
            ), \
            patch.object(run_engine, "validate_generated_content", return_value={"passed": True, "errors": [], "warnings": []}), \
            patch.object(run_engine, "score_content", return_value={"total": 90, "decision": "approve", "component_scores": {}}), \
            patch.object(run_engine, "load_anti_repeat_windows", return_value={}), \
            patch.object(run_engine, "check_duplicates", return_value={"ok": True, "reasons": [], "signatures": {}}), \
            patch.dict(
                os.environ,
                {"SOCIAL_DRY_RUN": "true", "POST_SLOT": "morning", "ORCHESTRATION_HARD_BLOCK": "true"},
                clear=False,
            ):
            run_engine.main()

        saved_post = save_calls[-1]["posts"][-1]
        self.assertEqual(saved_post["status"], "skipped_validation_or_quality")
        self.assertIn("orchestration_control_plane_blocked", saved_post.get("validation_errors", []))


if __name__ == "__main__":
    unittest.main()
