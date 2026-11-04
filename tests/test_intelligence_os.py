from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from social_engine.intelligence_os.foundation import bootstrap
from social_engine.intelligence_os.foundation import heartbeat
from social_engine.intelligence_os.db import connect, encode
from social_engine.intelligence_os.intelligence import AutomationService, ResearchIntelligence, classify_source
from social_engine.intelligence_os.knowledge import WorldModel
from social_engine.intelligence_os.models import CopilotMaster, MasterModelUnavailable, ModelStatus
from social_engine.intelligence_os.operations import JobService
from social_engine.intelligence_os.web import handle
from social.visual_provider import VisualResult


def test_bootstrap_registers_foundation_and_preserves_default_deny(tmp_path):
    service = bootstrap(str(tmp_path))

    capabilities = {item["id"] for item in service.registry.list()}

    assert "system.health" in capabilities
    assert "social.schedule" in capabilities
    assert "social.schedule_job_campaign" in capabilities
    assert "creative.carousel.generate" in capabilities
    assert "creative.command.produce" in capabilities
    assert "creative.post.compose" in capabilities
    assert "agents.run" in capabilities
    assert "content.plan_120_days" in capabilities
    blocked = service.execute_capability(
        "goals.create",
        {"name": "Grow qualified audience", "description": "Increase relevant reach."},
    )
    assert blocked["status"] == "WAITING_APPROVAL"
    assert blocked["approval_id"]


def test_command_center_produces_six_card_canonical_typography_story(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    captured = {}
    asset_ids = [f"asset-{index}" for index in range(1, 7)]

    class StudioProvider:
        base_url = "https://studio.test"

        def generate(self, **kwargs):
            captured.update(kwargs)
            return VisualResult(
                provider="entertainment_studio",
                kind="generated_image",
                asset_path="https://studio.test/api/assets/asset-1",
                provider_meta={
                    "creative_result": {
                        "status": "APPROVED",
                        "assets": asset_ids,
                        "creativeId": "studio-creative-1",
                        "versionId": "studio-version-1",
                        "sequenceNodes": [{"id": "studio-node-1", "position": 1, "title": "Hook", "assetId": asset_ids[0]}],
                        "generationMetadata": {
                            "canonReferenceAssetIds": ["canon-face", "canon-suit"],
                            "candidateEvaluations": [
                                {"assetId": f"candidate-{index}", "rank": index, "selected": index == 1, "qaStatus": "PASS" if index == 1 else "FAIL"}
                                for index in range(1, 5)
                            ],
                            "creativeQA": {
                                "status": "PASS",
                                "testsPerformed": ["CANON_QA", "DIALOGUE_QA", "TEXT_QA", "STORY_QA", "READING_ORDER_QA", "BRAND_QA", "VISUAL_QA", "CONTINUITY_QA", "ORIGINALITY_QA", "EMOTIONAL_QA"],
                                "scores": {"overall": 1.0}, "failures": [], "repairPlan": [], "attempts": 1,
                            },
                            "platformVariants": [{"platform": "instagram", "qa": {"status": "PASS"}}],
                        },
                        "qualityDecision": {"decision": "AUTO_APPROVE"},
                    },
                },
            )

    monkeypatch.setattr("social.visual_provider.default_provider", lambda: StudioProvider())
    response = service.command(
        "Create a six-card Micro Mission with the superhero and text that says DEAD BATTERIES for Instagram"
    )

    creative = response["creative"]
    contract = creative["creative_contract"]
    request = captured["art_direction"]["creative_request"]
    sequence = captured["art_direction"]["sequence_briefs"]
    assert response["status"] == "DELIVERED"
    assert creative["asset_count"] == 6
    assert contract["characters"] == ["Infenergy"]
    assert contract["exact_visible_text"] == ["DEAD BATTERIES"]
    assert [item["role"] for item in sequence] == [
        "COVER", "STORY", "STORY", "STORY", "STORY", "FINALE",
    ]
    assert request["canonRequired"] is True
    assert request["textMode"] == "HEADLINE"
    assert request["qualityGovernance"]["blocking"] is True
    assert request["productionStrategy"]["candidate_count"] == 4
    assert creative["canon_reference_asset_ids"] == ["canon-face", "canon-suit"]
    assert creative["quality_decision"]["decision"] == "AUTO_APPROVE"
    assert creative["studio_creative_id"] == "studio-creative-1"
    assert creative["studio_version_id"] == "studio-version-1"
    assert creative["package"]["studio_ancestry"]["sequence_nodes"][0]["id"] == "studio-node-1"
    persisted = service.get_creative(creative["creative_id"])
    assert persisted["status"] == "DELIVERED"
    assert persisted["package"]["creative_contract"]["request_id"] == contract["request_id"]
    assert persisted["preflight"]["passed"] is True
    content_memory = json.loads((tmp_path / "social" / "content_memory.json").read_text(encoding="utf-8"))
    assert content_memory["records"][-1]["creative_id"] == creative["creative_id"]
    assert creative["package"]["carousel_assets"] == [
        {"public_url": f"https://studio.test/api/assets/{asset_id}", "local_path": ""}
        for asset_id in asset_ids
    ]
    assert "Produced and validated 6 finished assets" in response["message"]


def test_command_center_does_not_claim_delivery_when_provider_falls_back(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    monkeypatch.setattr(
        "social.visual_provider.default_provider",
        lambda: type("Fallback", (), {"generate": lambda self, **kwargs: VisualResult(provider="template_render", kind="template_recipe")})(),
    )

    response = service.command("Create today's Infenergy quote about momentum")

    assert response["status"] == "GENERATION_FAILED"
    assert response["creative"]["failure"] == "finished_asset_provider_unavailable"
    assert "No finished deliverable" not in response["message"]
    assert "Produced and validated" not in response["message"]


def test_command_center_develops_quotes_but_preserves_owner_text():
    from social.command_center import compile_command

    developed = compile_command("Create today's Infenergy quote about momentum")
    supplied = compile_command("Create an Infenergy quote post that says OWNER WORDS")

    assert developed["exact_visible_text"] == ["MOMENTUM IS ENERGY WITH DIRECTION."]
    assert developed["quote_development"]["theme"] == "momentum"
    assert developed["characters"] == ["Infenergy"]
    assert developed["integrated_typography"] is True
    assert supplied["exact_visible_text"] == ["OWNER WORDS"]
    assert supplied["quote_development"] == {}


def test_command_center_single_image_overrides_negated_carousel_terms():
    from social.command_center import compile_command

    contract = compile_command(
        'Create one single Instagram image of Infenergy interacting with the exact words '
        '"THE SUN IS FREE. YOUR ENERGY BUDGET STILL NEEDS A MANAGER." '
        "One picture only, not a carousel, not multiple cards, and not a Micro Mission."
    )

    assert contract["deliverable"] == "social_visual"
    assert contract["format"] == "typography"
    assert contract["card_count"] == 1
    assert contract["creative_mode"] == "CINEMATIC_STORY"
    assert contract["exact_visible_text"] == ["THE SUN IS FREE. YOUR ENERGY BUDGET STILL NEEDS A MANAGER."]
    assert contract["integrated_typography"] is True


def test_command_center_honors_generation_control_directives():
    from social.command_center import compile_command

    contract = compile_command(
        "Create a finished Infenergy social post. Concept: Prepared wherever life moves. "
        "Format: Carousel Topic: Emergency preparedness Platform: LinkedIn"
    )

    assert contract["deliverable"] == "carousel"
    assert contract["format"] == "carousel"
    assert contract["topic"] == "Emergency preparedness"
    assert contract["platform"] == "linkedin"


def test_command_center_resolves_callable_story_format_canons():
    from social.command_center import compile_command

    mission = compile_command("Create an Infenergy Micro Mission.")
    storypage = compile_command("Surprise me with a StoryPage.")
    typography = compile_command("Create the superhero with the text integration.")

    assert mission["content_format_identifier"] == "infenergy_micro_mission"
    assert mission["creative_mode"] == "MICRO_MISSION"
    assert mission["card_count"] == 8
    assert [mission["story_beats"][0]["role"], mission["story_beats"][-1]["role"]] == ["COVER", "FINALE"]
    assert "DIALOGUE_QA" in mission["quality_gates"]
    assert storypage["content_format_identifier"] == "infenergy_storypage"
    assert storypage["deliverable"] == "storypage"
    assert storypage["format"] == "storypage"
    assert storypage["aspect_ratio"] == "9:16"
    assert storypage["panel_count"] == 4
    assert storypage["content_format_contract"]["canon"]["one_image_only"] is True
    assert typography["content_format_identifier"] == "superhero_text_integration"
    assert typography["format"] == "typography"


def test_entertainment_studio_provider_transports_storypage_contract(monkeypatch):
    from social.command_center import compile_command
    from social.visual_provider import EntertainmentStudioVisualProvider, TemplateRenderProvider

    contract = compile_command("Create an Infenergy StoryPage about a station outage.")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": "APPROVED", "assets": ["page-asset"]}}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr("social.visual_provider.requests.post", fake_post)
    provider = EntertainmentStudioVisualProvider("https://studio.test", "token", fallback=TemplateRenderProvider())
    creative_request = {
        "requestedRoute": contract["creative_mode"],
        "contentFormatIdentifier": contract["content_format_identifier"],
        "composition": {"aspectRatio": contract["aspect_ratio"]},
    }
    result = provider.generate(
        art_direction={"creative_request": creative_request, "visual_message": "The Last Platform", "visual_format": contract["format"], "sequence_briefs": contract["story_beats"]},
        positive_prompt="story", negative_prompt="drift", platform="instagram",
    )

    production = captured["json"]["production"]
    assert result.kind == "generated_image"
    assert production["kind"] == "storypage"
    assert production["aspectRatio"] == "9:16"
    assert len(production["sequenceBriefs"]) == 4
    assert production["sequenceBriefs"][2]["dialogue"] == "Wrong path. Right rhythm."
    assert production["sequenceBriefs"][2]["heroPanel"] is True


def test_entertainment_studio_provider_rejects_generic_sequence_fallbacks():
    from social.visual_provider import EntertainmentStudioVisualProvider

    provider = EntertainmentStudioVisualProvider("https://studio.test", "token")
    try:
        provider.generate(
            art_direction={
                "creative_request": {"requestedRoute": "MICRO_MISSION"},
                "visual_message": "A concrete mission",
                "sequence_briefs": [{"title": "Opening", "prompt": ""}, {"title": "Result", "prompt": "The lights return."}],
            },
            positive_prompt="mission",
            negative_prompt="",
            platform="instagram",
        )
    except RuntimeError as exc:
        assert "incomplete_sequence_briefs" in str(exc)
    else:
        raise AssertionError("Incomplete sequence briefs must fail closed")


def test_flagship_transports_generation_topic_to_studio(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    captured = {}

    class StudioProvider:
        def generate(self, **kwargs):
            captured.update(kwargs)
            return VisualResult(provider="template_render", kind="template_recipe")

    monkeypatch.setattr("social.visual_provider.default_provider", lambda: StudioProvider())
    service.command(
        "Create a finished Infenergy social post. Concept: Prepared wherever life moves. "
        "Format: Carousel Topic: Emergency preparedness Platform: LinkedIn"
    )

    request = captured["art_direction"]["creative_request"]
    assert request["format"] == "carousel"
    assert request["dominantIdea"] == "Emergency preparedness"


def test_flagship_resolves_and_transports_named_product_evidence(tmp_path, monkeypatch):
    marketing = tmp_path / "marketing"
    marketing.mkdir()
    (marketing / "product_consumer_profiles.json").write_text(json.dumps({
        "profiles": {
            "PCP-5IN1": {
                "schema_version": "1.0", "product_id": "PCP-5IN1", "product_name": "PowerCharge Pro",
                "market_role": "portable charging continuity", "core_customer_truth": "Device access keeps responsibilities moving.",
                "personas": [
                    {"persona_id": "travelers", "name": "Travelers", "use_case": "travel", "why_it_matters": "Travel depends on device access."},
                    {"persona_id": "commuters", "name": "Commuters", "use_case": "commuting", "why_it_matters": "Commuting depends on device access."},
                ],
            },
        },
    }), encoding="utf-8")
    product_briefs = tmp_path / "product_briefs"
    product_briefs.mkdir()
    (product_briefs / "PCP-5IN1.json").write_text(json.dumps({
        "product_id": "PCP-5IN1", "verified_facts": ["10,000mAh"],
        "source_image_url": "https://example.test/powercharge.png", "updated_at_utc": "2026-09-01T00:00:00+00:00",
    }), encoding="utf-8")
    service = bootstrap(str(tmp_path))
    captured = {}

    class StudioProvider:
        base_url = "https://studio.test"

        def generate(self, **kwargs):
            captured.update(kwargs)
            request = kwargs["art_direction"]["creative_request"]
            assets = [f"asset-{index}" for index in range(1, max(2, len(kwargs["art_direction"]["sequence_briefs"]) + 1))]
            candidate_count = request["productionStrategy"]["candidate_count"]
            return VisualResult(
                provider="entertainment_studio", kind="generated_image", asset_path="https://studio.test/api/assets/asset-1",
                provider_meta={"creative_result": {
                    "status": "APPROVED", "assets": assets,
                    "generationMetadata": {
                        "canonReferenceAssetIds": ["canon-product"],
                        "candidateEvaluations": [
                            {"assetId": f"candidate-{index}", "selected": index == 1, "qaStatus": "PASS" if index == 1 else "FAIL"}
                            for index in range(1, candidate_count + 1)
                        ],
                        "creativeQA": {"status": "PASS", "testsPerformed": request["qualityGovernance"]["requiredGates"]},
                    },
                }},
            )

    monkeypatch.setattr("social.visual_provider.default_provider", lambda: StudioProvider())

    grounded = service.execute_capability(
        "creative.command.produce", {"command": "Create a PowerCharge Pro post for commuters"}, dry_run=True,
    )["result"]
    generic = service.execute_capability(
        "creative.command.produce", {"command": "Create today's Infenergy quote about momentum"}, dry_run=True,
    )["result"]
    delivered = service.command("Create a PowerCharge Pro post for commuters")

    assert grounded["product_context"]["profile"]["product_id"] == "PCP-5IN1"
    assert grounded["product_context"]["persona"]["persona_id"] == "commuters"
    assert grounded["product_context"]["evidence"]["verified_facts"] == ["10,000mAh"]
    assert generic["product_context"] == {}
    assert delivered["status"] == "DELIVERED"
    assert captured["art_direction"]["creative_request"]["referencePackage"]["product_asset_urls"] == [
        "https://example.test/powercharge.png",
    ]


def test_approved_carousel_agent_accepts_structured_parameters(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    pending = service.execute_capability(
        "agents.run",
        {
            "name": "carousel_slide_writer",
            "params": {
                "product": {"name": "PowerFlex", "metrics": ["400W"]},
                "thought": {
                    "statement": "Prepare before the storm",
                    "expansion": "Protect one essential routine.",
                },
                "slide_count": 4,
            },
        },
    )

    approved = service.approve_and_execute(pending["approval_id"])

    assert approved["execution"]["status"] == "COMPLETED"
    assert approved["execution"]["result"]["output"]["product_name"] == "PowerFlex"
    assert len(approved["execution"]["result"]["output"]["slides"]) == 4


def test_approved_carousel_agent_normalizes_objective_alias(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    pending = service.execute_capability(
        "agents.run",
        {
            "name": "carousel_slide_writer",
            "params": {
                "objective": "Rebuild the existing Tropical Storm Edouard drafts",
                "platform": "instagram_feed",
                "slide_count": 6,
            },
        },
    )

    approved = service.approve_and_execute(pending["approval_id"])

    assert approved["execution"]["status"] == "COMPLETED"
    output = approved["execution"]["result"]["output"]
    assert approved["execution"]["result"]["delegated_capability"] == "creative.carousel.generate"
    assert output["package"]["objective"] == "Rebuild the existing Tropical Storm Edouard drafts"
    assert len(output["package"]["carousel_slides"]) == 6
    assert len(output["assets"]) == 6
    assert output["all_assets_valid"] is True
    assert all(item["valid"] for item in output["asset_validation"])
    assert output["next_action"].startswith("Call social.schedule")
    assert approved["execution"]["rollback_available"] is True
    assert all(
        operation["capability"] != "social.schedule"
        for transaction in service.transactions.list()
        for operation in transaction["operations"]
    )


def test_carousel_generation_fails_closed_when_an_asset_is_invalid(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    invalid_asset = {"local_path": str(tmp_path / "missing.png"), "public_url": ""}
    monkeypatch.setattr(
        "build_monthly_content._render_assets",
        lambda data_dir, thought, index: {"primary": invalid_asset, "slides": [invalid_asset, invalid_asset]},
    )

    with pytest.raises(RuntimeError, match="carousel_asset_validation_failed:1,2"):
        service.execute_capability(
            "creative.carousel.generate",
            {"objective": "Validate every frame", "slide_count": 2},
        )

    assert service.transactions.list()[0]["status"] == "FAILED"


def test_approved_schedule_is_transactional_and_rollbackable(tmp_path):
    service = bootstrap(str(tmp_path))
    arguments = {
        "content_date": "2026-08-25",
        "slot": "morning",
        "scheduled_at": "2026-08-25T13:00:00+00:00",
        "package": {
            "post_id": "approved-post-1",
            "fb_caption": "A useful approved post.",
            "ig_caption": "A useful approved post.",
            "li_text": "A useful approved post.",
            "platform_posts": {},
        },
    }
    pending = service.execute_capability("social.schedule", arguments, operation_id="schedule-request")
    service.policies.create_policy(
        capability="social.schedule",
        rule="Approved social packages may be scheduled.",
        approval_level="EXECUTE_WITH_APPROVAL",
        created_by="owner",
    )
    approval_id = service.policies.request_approval("social.schedule", "owner", arguments)
    service.policies.decide_approval(approval_id, approved=True, decided_by="owner")

    result = service.execute_capability(
        "social.schedule",
        arguments,
        operation_id="schedule-approved",
        approval_id=approval_id,
    )

    assert pending["status"] == "WAITING_APPROVAL"
    assert result["status"] == "COMPLETED"
    assert result["rollback_available"] is True
    rolled_back = service.transactions.rollback(result["transaction_id"])
    assert rolled_back["status"] == "ROLLED_BACK"


@pytest.mark.parametrize(("content_date", "expected_offset"), [
    ("2026-09-01", "-05:00"),
    ("2026-12-01", "-06:00"),
])
def test_schedule_localizes_creative_studio_wall_time_to_central(
    tmp_path, content_date, expected_offset,
):
    service = bootstrap(str(tmp_path))
    service.policies.create_policy(
        capability="social.schedule",
        rule="Test autonomous scheduling.",
        approval_level="AUTONOMOUS",
        created_by="owner",
    )

    result = service.execute_capability(
        "social.schedule",
        {
            "content_date": content_date,
            "slot": "midday",
            "scheduled_at": f"{content_date}T13:00:00",
            "package": {"post_id": f"central-{content_date}", "platforms": ["facebook"]},
        },
    )
    from content_operations import daily_status
    scheduled_at = next(
        item["scheduled_at"] for item in daily_status(str(tmp_path), content_date)["slots"]
        if item["slot"] == "midday"
    )

    assert result["status"] == "COMPLETED"
    assert scheduled_at == f"{content_date}T13:00:00{expected_offset}"


def test_carousel_generation_executes_without_approval_and_schedules_with_one(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")

    generated = service.execute_capability(
        "creative.carousel.generate",
        {
            "objective": "The last percent is not a plan.",
            "platform": "instagram_feed",
            "platforms": ["facebook", "instagram"],
            "slide_count": 8,
        },
    )

    assert generated["status"] == "COMPLETED"
    assert generated["result"]["slide_count"] == 8
    assert len(generated["result"]["assets"]) == 8
    assert all(item["local_path"] for item in generated["result"]["assets"])
    assert generated["result"]["all_assets_valid"] is True
    assert len(generated["result"]["asset_validation"]) == 8
    assert all(item["valid"] for item in generated["result"]["asset_validation"])

    pending = service.execute_capability(
        "social.schedule",
        {
            "content_date": "2026-08-27",
            "slot": "midday",
            "package": generated["result"]["package"],
        },
    )
    assert pending["status"] == "WAITING_APPROVAL"
    approved = service.approve_and_execute(pending["approval_id"])
    assert approved["execution"]["status"] == "COMPLETED"
    assert approved["approval"]["status"] == "CONSUMED"
    assert approved["execution"]["result"]["outbox_id"]


def test_calendar_returns_exact_persisted_scheduled_post(tmp_path):
    service = bootstrap(str(tmp_path))
    service.policies.create_policy(
        capability="social.schedule",
        rule="Test autonomous scheduling.",
        approval_level="AUTONOMOUS",
        created_by="owner",
    )
    result = service.execute_capability(
        "social.schedule",
        {
            "content_date": "2026-09-03",
            "slot": "midday",
            "scheduled_at": "2026-09-03T13:00:00-05:00",
            "package": {
                "post_id": "calendar-proof",
                "title": "Exact scheduled post",
                "platforms": ["facebook", "instagram"],
                "platform_posts": {
                    "facebook": {"final_caption": "Exact Facebook caption."},
                    "instagram": {"final_caption": "Exact Instagram caption."},
                },
            },
        },
    )

    status, content_type, response = handle(
        "POST", "/api/os/calendar", {"start_date": "2026-09-01", "days": 7}, str(tmp_path)
    )
    calendar = json.loads(response)
    post = next(day["posts"][0] for day in calendar["days"] if day["posts"])

    assert status == 200
    assert content_type.startswith("application/json")
    assert calendar["scheduled_count"] == 1
    assert post["outbox_id"] == result["result"]["outbox_id"]
    assert post["scheduled_at"] == "2026-09-03T13:00:00-05:00"
    assert post["platforms"] == ["facebook", "instagram"]
    assert post["package"]["platform_posts"]["facebook"]["final_caption"] == "Exact Facebook caption."


def test_content_operations_separates_upcoming_from_confirmed_history(tmp_path):
    from content_operations import begin_platform_transaction, complete_platform_transaction

    service = bootstrap(str(tmp_path))
    service.policies.create_policy(
        capability="social.schedule",
        rule="Test autonomous scheduling.",
        approval_level="AUTONOMOUS",
        created_by="owner",
    )
    future = service.execute_capability("social.schedule", {
        "content_date": "2026-09-12",
        "slot": "midday",
        "scheduled_at": "2026-09-12T17:00:00+00:00",
        "package": {
            "post_id": "future-post",
            "content_type": "product_story",
            "platforms": ["facebook", "instagram"],
            "primary_publish_image_url": "https://example.test/shared.png",
            "media_asset": {"url": "https://example.test/shared.png", "sha256": "shared-sha", "status": "READY"},
        },
    })["result"]
    past = service.execute_capability("social.schedule", {
        "content_date": "2026-09-08",
        "slot": "morning",
        "scheduled_at": "2026-09-08T13:00:00+00:00",
        "package": {
            "post_id": "published-post",
            "content_type": "micro_mission",
            "platforms": ["facebook"],
            "primary_publish_image_url": "https://example.test/published.png",
        },
    })["result"]
    begin_platform_transaction(
        str(tmp_path), outbox_id=past["outbox_id"], platform="facebook", payload={"message": "Published copy"}
    )
    complete_platform_transaction(
        str(tmp_path), outbox_id=past["outbox_id"], platform="facebook",
        state="CONFIRMED_SUCCESS", external_id="fb-verified-123", provider_response={"id": "fb-verified-123"},
    )

    status, content_type, response = handle("POST", "/api/os/content-operations", {
        "now_utc": "2026-09-10T12:00:00+00:00", "upcoming_days": 30, "history_days": 30,
    }, str(tmp_path))
    workspace = json.loads(response)

    assert status == 200
    assert content_type.startswith("application/json")
    assert [item["outbox_id"] for item in workspace["upcoming"]] == [future["outbox_id"]]
    assert workspace["upcoming"][0]["prepare_by"] == "2026-09-12T13:00:00+00:00"
    assert workspace["upcoming"][0]["operationally_visible"] is False
    assert [item["outbox_id"] for item in workspace["published"]] == [past["outbox_id"]]
    assert workspace["published"][0]["confirmed_publications"][0]["external_id"] == "fb-verified-123"
    assert workspace["summary"] == {"upcoming": 1, "published": 1, "media": 3, "needs_attention": 0}
    assert workspace["facets"]["platforms"] == ["facebook", "instagram"]


def test_conversational_remake_uses_exact_master_model_and_preserves_version(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    service.policies.create_policy(
        capability="publication.master_remake", rule="Test owner-authorized remake.",
        approval_level="AUTONOMOUS", created_by="owner",
    )
    service.policies.create_policy(
        capability="social.schedule", rule="Test autonomous scheduling.",
        approval_level="AUTONOMOUS", created_by="owner",
    )
    scheduled = service.execute_capability("social.schedule", {
        "content_date": "2026-09-12", "slot": "midday", "scheduled_at": "2026-09-12T17:00:00+00:00",
        "package": {"post_id": "remake-me", "title": "Original", "master_copy": "Original copy", "platforms": ["facebook"], "platform_posts": {"facebook": {"final_caption": "Original caption"}}, "primary_publish_image_url": "https://example.test/original.png"},
    })["result"]

    async def fake_converse(self, prompt, *, session_id, system_message, tools=None):
        assert self.model == "gpt-5.6-sol"
        assert "Make it more direct" in prompt
        return {"content": json.dumps({"title": "Remade", "master_copy": "Direct copy", "platform_posts": {"facebook": {"final_caption": "Direct caption"}}, "creative_direction": "Keep the approved image."}), "model": self.model, "provider": "github-copilot-sdk", "session_id": session_id}

    monkeypatch.setattr(CopilotMaster, "converse", fake_converse)
    result = service.execute_capability("publication.master_remake", {
        "content_date": "2026-09-12", "outbox_id": scheduled["outbox_id"], "instruction": "Make it more direct",
    })
    status, _, response = handle("POST", "/api/os/content-operations", {"now_utc": "2026-09-10T12:00:00+00:00"}, str(tmp_path))
    remade = json.loads(response)["upcoming"][0]["package"]

    assert result["status"] == "COMPLETED"
    assert result["result"]["model"] == "gpt-5.6-sol"
    assert remade["title"] == "Remade"
    assert remade["version_history"][0]["package"]["title"] == "Original"
    assert remade["last_remake"]["model"] == "gpt-5.6-sol"


def test_ready_post_editor_uses_registered_type_and_preserves_prior_package(tmp_path):
    service = bootstrap(str(tmp_path))
    for capability in ("social.schedule", "publication.edit_ready"):
        service.policies.create_policy(capability=capability, rule="Test autonomous edit.", approval_level="AUTONOMOUS", created_by="owner")
    scheduled = service.execute_capability("social.schedule", {
        "content_date": "2026-09-12", "slot": "midday", "scheduled_at": "2026-09-12T17:00:00+00:00",
        "package": {"post_id": "edit-me", "title": "Original", "post_type": "statement", "platforms": ["facebook"], "platform_posts": {"facebook": {"final_caption": "Original caption"}}, "primary_publish_image_url": "https://example.test/original.png"},
    })["result"]

    result = service.execute_capability("publication.edit_ready", {
        "content_date": "2026-09-12", "outbox_id": scheduled["outbox_id"],
        "changes": {"title": "Edited", "post_type": "product_education", "platform_posts": {"facebook": {"final_caption": "Edited caption"}}},
    })
    _, _, response = handle("POST", "/api/os/content-operations", {"now_utc": "2026-09-10T12:00:00+00:00"}, str(tmp_path))
    edited = json.loads(response)["upcoming"][0]["package"]

    assert result["status"] == "COMPLETED"
    assert edited["post_type"] == "product_education"
    assert edited["post_type_label"] == "Product education"
    assert edited["platform_posts"]["facebook"]["final_caption"] == "Edited caption"
    assert edited["version_history"][0]["package"]["title"] == "Original"


def test_post_composer_dry_run_preserves_selected_models_and_type(tmp_path):
    service = bootstrap(str(tmp_path))

    result = service.execute_capability("creative.post.compose", {
        "post_type": "product_education",
        "copy_provider": "gpt-5.6-sol",
        "visual_format": "single_image",
        "brief": "Explain the Black Warrior without inventing specifications.",
        "platforms": ["facebook", "instagram", "linkedin"],
    }, dry_run=True)

    assert result["status"] == "DRY_RUN_COMPLETE"
    assert result["result"]["post_type"] == "product_education"
    assert result["result"]["copy_provider"] == "gpt-5.6-sol"
    assert result["result"]["image_provider"] == "gemini"


@pytest.mark.parametrize(
    ("post_type", "visual_format", "slide_count"),
    [
        ("infenergy_micro_mission", "carousel", 8),
        ("infenergy_storypage", "single_image", 1),
        ("superhero_text_integration", "single_image", 1),
    ],
)
def test_post_composer_enforces_branded_story_contracts(tmp_path, post_type, visual_format, slide_count):
    service = bootstrap(str(tmp_path))

    result = service.execute_capability("creative.post.compose", {
        "post_type": post_type,
        "copy_provider": "gemini",
        "visual_format": "carousel" if visual_format == "single_image" else "single_image",
        "slide_count": 3,
        "brief": "Tell a complete product story without inventing specifications.",
        "platforms": ["instagram"],
    }, dry_run=True)["result"]

    assert result["content_format_identifier"] == post_type
    assert result["visual_format"] == visual_format
    assert result["slide_count"] == slide_count
    assert result["visible_text_required"] is True


def test_composed_creative_copy_can_be_saved_before_scheduling(tmp_path):
    service = bootstrap(str(tmp_path))
    creative = service.create_creative(title="Generated post", idea="A useful brief")
    package = {
        "composer": {"brief": "A useful brief", "visual_format": "single_image"},
        "title": "Generated post",
        "platform_posts": {"facebook": {"final_caption": "Original"}},
    }
    with connect(str(tmp_path)) as connection:
        connection.execute(
            "UPDATE os_creatives SET status='DELIVERED', platforms_json=?, package_json=? WHERE id=?",
            (encode(["facebook"]), encode(package), creative["id"]),
        )
        connection.commit()

    saved = service.save_composed_creative(creative["id"], {
        "title": "Owner-edited post",
        "platform_posts": {"facebook": {"final_caption": "Owner-edited copy"}},
    })

    assert saved["title"] == "Owner-edited post"
    assert saved["package"]["platform_posts"]["facebook"]["final_caption"] == "Owner-edited copy"
    assert saved["package"]["fb_caption"] == "Owner-edited copy"


def test_composed_creative_save_endpoint_and_exact_lifecycle_actions(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    creative = service.create_creative(title="Generated post", idea="A useful brief")
    package = {
        "composer": {"brief": "A useful brief", "visual_format": "single_image"},
        "title": "Generated post",
        "platform_posts": {"facebook": {"final_caption": "Original"}},
    }
    schedule = {
        "content_date": "2026-09-12", "scheduled_at": "2026-09-12T17:00:00+00:00",
        "slot": "midday", "outbox_id": "composer-outbox",
    }
    with connect(str(tmp_path)) as connection:
        connection.execute(
            "UPDATE os_creatives SET status='DELIVERED', platforms_json=?, package_json=? WHERE id=?",
            (encode(["facebook"]), encode(package), creative["id"]),
        )
        connection.commit()
    status, _, response = handle("POST", f"/api/os/creatives/{creative['id']}/save-composed", {
        "title": "Saved title", "platform_posts": {"facebook": {"final_caption": "Saved caption"}},
    }, str(tmp_path))
    assert status == 200
    assert json.loads(response)["creative"]["package"]["fb_caption"] == "Saved caption"
    with connect(str(tmp_path)) as connection:
        connection.execute(
            "UPDATE os_creatives SET status='SCHEDULED', schedule_json=? WHERE id=?",
            (encode(schedule), creative["id"]),
        )
        connection.commit()

    calls = []

    def execute(capability, arguments, **kwargs):
        calls.append((capability, arguments))
        if capability == "publication.reschedule":
            return {"status": "COMPLETED", "result": {"content_date": "2026-09-13", "scheduled_at": "2026-09-13T18:00:00+00:00", "slot": "evening"}}
        return {"status": "COMPLETED", "result": {"processed": 1, "published": 1}}

    monkeypatch.setattr(service, "execute_capability", execute)
    rescheduled = service.reschedule_creative(
        creative["id"], scheduled_at="2026-09-13T18:00:00+00:00", slot="evening",
    )
    published = service.publish_creative_now(creative["id"])

    assert rescheduled["creative"]["schedule"]["content_date"] == "2026-09-13"
    assert published["execution"]["result"]["published"] == 1
    assert calls == [
        ("publication.reschedule", {"outbox_id": "composer-outbox", "scheduled_at": "2026-09-13T18:00:00+00:00", "slot": "evening"}),
        ("publication.dispatch", {"outbox_id": "composer-outbox", "publish_now": True}),
    ]


def test_creative_idea_persists_and_title_can_be_renamed(tmp_path):
    service = bootstrap(str(tmp_path))
    created = service.create_creative(title="Untitled creative")

    saved = service.update_creative(created["id"], {
        "title": "The One Percent Heist",
        "idea": "The last percent is not a plan.",
        "slide_count": 7,
        "platform": "instagram_feed",
        "platforms": ["facebook", "instagram"],
    })
    restored = bootstrap(str(tmp_path)).get_creative(created["id"])

    assert saved["title"] == "The One Percent Heist"
    assert restored["idea"] == "The last percent is not a plan."
    assert restored["slide_count"] == 7
    assert service.list_creatives()[0]["id"] == created["id"]


def test_one_click_creative_workflow_checks_approves_and_schedules(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    creative = service.create_creative(
        title="The One Percent Heist",
        idea="The last percent is not a plan.",
        platform="instagram_feed",
        platforms=["facebook", "instagram"],
        slide_count=6,
    )

    result = service.prepare_and_schedule_creative(
        creative["id"],
        content_date="2026-08-29",
        scheduled_at="2026-08-29T12:30:00",
        slot="midday",
    )

    saved = result["creative"]
    assert saved["status"] == "SCHEDULED"
    assert saved["preflight"]["passed"] is True
    assert saved["schedule"]["outbox_id"]
    assert len(saved["package"]["carousel_slides"]) == 6
    assert saved["package"]["carousel_slides"][0]["on_image_headline"] == "The last percent is not a plan"
    assert all("cheap watts" not in slide["on_image_headline"].lower() for slide in saved["package"]["carousel_slides"])
    assert all("backup power kit" not in slide["on_image_subline"].lower() for slide in saved["package"]["carousel_slides"])
    assert saved["package"]["platform_posts"]["facebook"]["final_caption"] != saved["package"]["platform_posts"]["instagram"]["final_caption"]
    assert service.policies.list_approvals(actor="owner", status="PENDING") == []


def test_schedule_rejects_silent_replacement_and_can_choose_slot_time(tmp_path):
    service = bootstrap(str(tmp_path))
    service.policies.create_policy(
        capability="social.schedule",
        rule="Test autonomous scheduling.",
        approval_level="AUTONOMOUS",
        created_by="owner",
    )
    arguments = {
        "content_date": "2026-08-28",
        "slot": "evening",
        "package": {"post_id": "first"},
    }
    first = service.execute_capability("social.schedule", arguments)
    assert first["status"] == "COMPLETED"

    with pytest.raises(ValueError, match="slot_already_occupied"):
        service.execute_capability(
            "social.schedule",
            {**arguments, "package": {"post_id": "second"}},
        )

    replaced = service.execute_capability(
        "social.schedule",
        {**arguments, "package": {"post_id": "second"}, "replace_existing": True},
    )
    assert replaced["status"] == "COMPLETED"

def test_completed_campaign_is_loaded_with_one_approval_and_no_publication(tmp_path):
    service = bootstrap(str(tmp_path))
    job = service.jobs.create(job_type="CAMPAIGN", objective="Build dated campaign", plan=["produce"])
    service.jobs.transition(job["id"], "COMPLETED", progress=1, result={
        "campaign": {"name": "Blackout House"},
        "episodes": [
            {"date": "2026-09-21", "title": "Day one", "script": ["Opening"]},
            {"date": "2026-09-22", "title": "Day two", "script": ["Follow-up"]},
        ],
    })
    arguments = {
        "job_id": job["id"], "start_date": "2026-09-21", "end_date": "2026-09-22",
        "slot": "midday", "scheduled_time": "17:00:00+00:00",
        "platforms": ["facebook", "instagram", "linkedin"],
    }

    pending = service.execute_capability("social.schedule_job_campaign", arguments)
    approved = service.approve_and_execute(pending["approval_id"])

    assert approved["approval"]["status"] == "CONSUMED"
    assert approved["execution"]["status"] == "COMPLETED"
    assert approved["execution"]["result"]["scheduled_count"] == 2
    assert approved["execution"]["result"]["platform_adaptation_count"] == 6
    assert approved["execution"]["result"]["publication_enabled"] is False
    assert approved["execution"]["rollback_available"] is True


def test_date_keyed_posts_load_multiple_slots_with_one_approval(tmp_path):
    service = bootstrap(str(tmp_path))
    job = service.jobs.create(job_type="CAMPAIGN", objective="Build companion posts", plan=["produce"])
    posts = []
    for content_date in ("2026-09-21", "2026-09-22"):
        for slot in ("morning", "evening"):
            posts.append({
                "content_date": content_date, "slot": slot, "hook": f"{slot} hook",
                "script": f"{slot} copy", "adaptations": {"facebook": "fb", "instagram": "ig", "linkedin": "li"},
            })
    service.jobs.transition(job["id"], "COMPLETED", progress=1, result={
        "campaign": {"name": "Blackout House"}, "posts": posts,
    })
    pending = service.execute_capability("social.schedule_job_campaign", {
        "job_id": job["id"], "start_date": "2026-09-21", "end_date": "2026-09-22",
        "slots": ["morning", "evening"],
        "schedule_times": {"morning": "13:00:00+00:00", "evening": "23:00:00+00:00"},
        "platforms": ["facebook", "instagram", "linkedin"],
    })

    approved = service.approve_and_execute(pending["approval_id"])

    assert approved["execution"]["status"] == "COMPLETED"
    assert approved["execution"]["result"]["scheduled_count"] == 4
    assert approved["execution"]["result"]["platform_adaptation_count"] == 12
    assert approved["execution"]["result"]["slots"] == ["morning", "evening"]
    calendar = service.execute_capability(
        "social.calendar.get", {"start_date": "2026-09-21", "days": 1}
    )["result"]["days"][0]
    loaded = {item["slot"]: item["status"] for item in calendar["slots"]}
    assert loaded["morning"] == "READY"
    assert loaded["evening"] == "READY"


def test_campaign_schedule_approval_supersedes_overlapping_variants(tmp_path):
    service = bootstrap(str(tmp_path))
    job_id = "job-123"
    single = service.execute_capability("social.schedule", {
        "content_date": "2026-09-21", "slot": "midday", "scheduled_at": "2026-09-21T17:00:00+00:00",
        "package": {"source_job_id": job_id},
    })
    automation = service.execute_capability("automations.create", {
        "name": "Legacy loader", "trigger": {"type": "once"},
        "steps": [{"capability": "social.schedule", "arguments": {"package": {"source_job_id": job_id}}}],
    })
    canonical = service.execute_capability("social.schedule_job_campaign", {
        "job_id": job_id, "start_date": "2026-09-21", "end_date": "2026-10-20",
    })

    assert service.policies.get_approval(single["approval_id"])["status"] == "SUPERSEDED"
    assert service.policies.get_approval(automation["approval_id"])["status"] == "SUPERSEDED"
    assert service.policies.get_approval(canonical["approval_id"])["status"] == "PENDING"


def test_default_deny_approval_executes_exact_request_once(tmp_path):
    service = bootstrap(str(tmp_path))
    arguments = {"name": "Approved goal", "description": "A verified one-time goal"}
    pending = service.execute_capability("goals.create", arguments)

    approved = service.approve_and_execute(pending["approval_id"])
    replay = service.approve_and_execute(pending["approval_id"])

    assert approved["execution"]["status"] == "COMPLETED"
    assert approved["approval"]["status"] == "CONSUMED"
    assert approved["execution"]["result"]["goal"]["description"] == arguments["description"]
    assert replay["approval"]["status"] == "CONSUMED"
    assert replay["execution"]["idempotent_replay"] is True
    assert replay["execution"]["transaction_id"] == approved["execution"]["transaction_id"]


def test_approval_cannot_authorize_different_payload(tmp_path):
    service = bootstrap(str(tmp_path))
    pending = service.execute_capability("goals.create", {"name": "Approved", "description": "Approved description"})
    service.policies.decide_approval(pending["approval_id"], approved=True, decided_by="owner")

    mismatch = service.execute_capability(
        "goals.create", {"name": "Different", "description": "Different description"}, approval_id=pending["approval_id"]
    )

    assert mismatch["status"] == "WAITING_APPROVAL"
    assert mismatch["approval_id"] != pending["approval_id"]


def test_duplicate_pending_approval_is_reused_and_stale_variants_are_superseded(tmp_path):
    service = bootstrap(str(tmp_path))
    first = service.execute_capability("goals.create", {"name": "One", "description": "Same request"})
    duplicate = service.execute_capability("goals.create", {"name": "One", "description": "Same request"})
    variant = service.execute_capability("goals.create", {"name": "Two", "description": "Stale variant"})

    assert duplicate["approval_id"] == first["approval_id"]
    assert variant["approval_id"] != first["approval_id"]
    service.approve_and_execute(variant["approval_id"])

    assert service.policies.get_approval(variant["approval_id"])["status"] == "CONSUMED"
    assert service.policies.get_approval(first["approval_id"])["status"] == "SUPERSEDED"


def test_natural_language_approval_executes_without_model_retry_loop(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    pending = service.execute_capability("goals.create", {"name": "Proceed", "description": "Proceed once"})

    async def model_must_not_run(*args, **kwargs):
        raise AssertionError("approval intent must be handled deterministically")

    monkeypatch.setattr(service.master, "converse", model_must_not_run)
    result = service.command("I approve this. Go ahead.")

    assert result["status"] == "COMPLETED"
    assert result["approval"]["id"] == pending["approval_id"]
    assert result["approval"]["status"] == "CONSUMED"
    assert "exactly once" in result["message"]


def test_approved_job_continues_to_persisted_deliverables(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    pending = service.execute_capability(
        "research.mission.create", {"question": "Build the complete Blackout House research brief"}
    )

    async def complete_job(prompt, **kwargs):
        job = service.jobs.list(1)[0]
        completed = service.execute_capability(
            "jobs.complete",
            {
                "job_id": job["id"],
                "result": {
                    "campaign": "Blackout House",
                    "scripts": ["Episode one script"],
                    "captions": ["Episode one caption"],
                    "calendar": [{"day": 1, "title": "The first hour"}],
                },
            },
        )
        assert completed["status"] == "COMPLETED"
        return {
            "content": "Blackout House deliverables were built and persisted.",
            "model": "gpt-5.6-sol", "provider": "github-copilot-sdk",
            "session_id": kwargs["session_id"],
        }

    monkeypatch.setattr(service.master, "converse", complete_job)
    monkeypatch.setattr(service, "_copilot_tools", lambda actor: [])
    result = service.command("I approve this—start building.")
    job = service.jobs.list(1)[0]

    assert result["status"] == "COMPLETED"
    assert result["approval"]["id"] == pending["approval_id"]
    assert job["status"] == "COMPLETED"
    assert job["progress"] == 1.0
    assert job["result"]["campaign"] == "Blackout House"
    assert all(step["status"] == "COMPLETED" for step in job["steps"])


def test_approved_job_survives_continuation_provider_failure(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    conversation = service.create_conversation(owner_id="owner", title="Recovery")
    pending = service.execute_capability(
        "research.mission.create", {"question": "Build the complete Blackout House research brief"}
    )
    approved = service.approve_and_execute(pending["approval_id"])

    async def fail_with_non_json_upstream(*args, **kwargs):
        raise ValueError("Unexpected token 'u', upstream error is not valid JSON")

    monkeypatch.setattr(service.master, "converse", fail_with_non_json_upstream)
    monkeypatch.setattr(service, "_copilot_tools", lambda actor: [])
    result = service.continue_approved_job(conversation["id"], approved)

    assert result["status"] == "CONTINUATION_FAILED"
    assert result["approval"]["status"] == "CONSUMED"
    assert result["execution"]["status"] == "COMPLETED"
    assert result["execution"]["result"]["job"]["id"] == service.jobs.list(1)[0]["id"]
    assert "Send `continue`" in result["message"]


def test_120_day_capability_runs_real_builder_and_completes_job(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    captured = {}
    builder_started = threading.Event()
    release_builder = threading.Event()

    def build_calendar(**kwargs):
        captured.update(kwargs)
        builder_started.set()
        assert release_builder.wait(2)
        return {
            "calendar_path": str(tmp_path / "calendar.json"), "queued": 120,
            "cancelled_outbox": 90, "single_image_posts": 103, "carousel_posts": 17,
            "product_posts": 52, "current_event_posts": 17, "superhero_posts": 17,
            "micro_mission_posts": 17, "historical_mission_posts": 17,
        }

    monkeypatch.setattr("build_monthly_content.build_monthly_calendar", build_calendar)
    monkeypatch.setattr(
        "build_monthly_content.prepare_monthly_gemini_prompts",
        lambda data_dir: {"prepared_entries": 120, "prepared_prompts": 222},
    )
    pending = service.execute_capability(
        "content.plan_120_days",
        {
            "objective": "Build the consumer campaign", "start_date": "2026-08-27",
            "replace_unpublished": True, "content_plan": "weekly_brand_mix",
        },
    )
    result = service.approve_and_execute(pending["approval_id"])

    job = result["execution"]["result"]["job"]
    assert result["execution"]["status"] == "COMPLETED"
    assert result["execution"]["result"]["dispatched"] is True
    assert result["approval"]["status"] == "CONSUMED"
    assert builder_started.wait(1)
    assert job["status"] == "RUNNING"
    release_builder.set()
    for _ in range(100):
        job = service.jobs.get(job["id"])
        if job["status"] == "COMPLETED":
            break
        time.sleep(0.01)
    assert job["status"] == "COMPLETED"
    assert job["progress"] == 1.0
    assert job["result"]["queued"] == 120
    assert all(step["status"] == "COMPLETED" for step in job["steps"])
    assert captured == {
        "data_dir": str(tmp_path), "start_date": "2026-08-27", "days": 120,
        "enqueue": True, "replace_unpublished": True, "content_plan": "weekly_brand_mix",
    }


def test_120_day_content_plan_uses_deterministic_assets_without_gemini(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    gemini_called = threading.Event()

    monkeypatch.setattr(
        "build_monthly_content.build_monthly_calendar",
        lambda **kwargs: {"calendar_path": str(tmp_path / "calendar.json"), "queued": 120},
    )
    monkeypatch.setattr(
        "build_monthly_content.prepare_monthly_gemini_prompts",
        lambda data_dir: gemini_called.set(),
    )
    pending = service.execute_capability(
        "content.plan_120_days",
        {"content_plan": "content_plan_120", "replace_unpublished": True},
    )
    result = service.approve_and_execute(pending["approval_id"])
    job_id = result["execution"]["result"]["job"]["id"]

    for _ in range(100):
        job = service.jobs.get(job_id)
        if job["status"] == "COMPLETED":
            break
        time.sleep(0.01)

    assert job["status"] == "COMPLETED"
    assert job["result"]["prepared_entries"] == 0
    assert job["result"]["prepared_prompts"] == 0
    assert gemini_called.is_set() is False


def test_120_day_approval_route_returns_while_builder_runs(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    conversation = service.create_conversation()
    pending = service.execute_capability(
        "content.plan_120_days",
        {"objective": "Build the next 120 days", "replace_unpublished": True},
    )
    builder_started = threading.Event()
    release_builder = threading.Event()

    def build_calendar(**kwargs):
        builder_started.set()
        assert release_builder.wait(2)
        return {"calendar_path": str(tmp_path / "calendar.json"), "queued": 120}

    def unexpected_continuation(*args, **kwargs):
        raise AssertionError("asynchronous capabilities must not start model continuation in the approval request")

    monkeypatch.setattr("build_monthly_content.build_monthly_calendar", build_calendar)
    monkeypatch.setattr("build_monthly_content.prepare_monthly_gemini_prompts", lambda data_dir: {})
    monkeypatch.setattr(
        "social_engine.intelligence_os.service.IntelligenceOS.continue_approved_job",
        unexpected_continuation,
    )
    try:
        status, _, response = handle(
            "POST",
            f"/api/os/approvals/{pending['approval_id']}",
            {"approved": True, "execute": True, "decided_by": "owner", "conversation_id": conversation["id"]},
            str(tmp_path),
        )
        result = json.loads(response)

        assert status == 200
        assert builder_started.wait(1)
        assert result["approval"]["status"] == "CONSUMED"
        assert result["execution"]["result"]["job"]["status"] == "RUNNING"
    finally:
        release_builder.set()


def test_job_control_approval_route_returns_while_continuation_runs(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    conversation = service.create_conversation()
    job = service.jobs.create(job_type="campaign", objective="Build campaign", plan=["build"])
    pending = service.execute_capability("jobs.control", {"job_id": job["id"], "action": "continue"})
    continuation_started = threading.Event()
    release_continuation = threading.Event()

    def continue_job(self, conversation_id, approved, actor="owner"):
        continuation_started.set()
        assert release_continuation.wait(2)
        return {"status": "COMPLETED"}

    monkeypatch.setattr(
        "social_engine.intelligence_os.service.IntelligenceOS.continue_approved_job",
        continue_job,
    )
    try:
        status, _, response = handle(
            "POST",
            f"/api/os/approvals/{pending['approval_id']}",
            {"approved": True, "execute": True, "decided_by": "owner", "conversation_id": conversation["id"]},
            str(tmp_path),
        )
        result = json.loads(response)

        assert status == 200
        assert continuation_started.wait(1)
        assert result["approval"]["status"] == "CONSUMED"
        assert result["execution"]["result"]["job"]["status"] == "RUNNING"
        assert result["continuation_dispatched"] is True
    finally:
        release_continuation.set()


def test_scored_story_capability_returns_instagram_schedule_package(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    artifact = {
        "reel_artifact_path": str(tmp_path / "story.mp4"),
        "cover_path": str(tmp_path / "cover.jpg"),
        "final_freeze_frame_path": str(tmp_path / "final.jpg"),
        "static_derivative_path": str(tmp_path / "static.jpg"),
        "public_urls": {"video": "https://media.example/story.mp4", "cover": "https://media.example/cover.jpg"},
    }
    monkeypatch.setattr("social.reels.render_scored_story_reel", lambda plan, data_dir: artifact)
    monkeypatch.setattr("social.reels.technical_qa", lambda artifact, plan: {"status": "PASS", "reasons": []})
    package = {
        "post_id": "mission-1", "ig_caption": "The mission begins.",
        "carousel_assets": [{"local_path": "one.png"}, {"local_path": "two.png"}],
        "carousel_slides": [{"on_image_headline": "Darkness"}, {"on_image_headline": "Restore power"}],
        "platform_posts": {"facebook": {"content_format": "carousel"}},
    }
    pending = service.execute_capability("creative.scored_story_reel.generate", {"package": package, "emotions": ["eerie", "triumph"]})
    result = service.approve_and_execute(pending["approval_id"])["execution"]["result"]
    assert result["status"] == "RENDERED"
    assert result["package"]["instagram_reel"] == artifact
    assert result["package"]["platform_posts"]["instagram"]["media_type"] == "REEL"
    assert result["package"]["platform_posts"]["facebook"]["content_format"] == "carousel"
    assert result["platform_support"]["facebook"].endswith("VIDEO_UPLOAD_NOT_ENABLED")


def test_schedule_dry_run_does_not_write_social_slot(tmp_path):
    service = bootstrap(str(tmp_path))
    service.policies.create_policy(
        capability="social.schedule",
        rule="Testing only.",
        approval_level="AUTONOMOUS",
        created_by="owner",
    )
    result = service.execute_capability(
        "social.schedule",
        {
            "content_date": "2026-08-26",
            "slot": "midday",
            "scheduled_at": "2026-08-26T17:00:00+00:00",
            "package": {"post_id": "dry-run-post"},
        },
        dry_run=True,
    )

    calendar = service.execute_capability(
        "social.calendar.get", {"start_date": "2026-08-26", "days": 1}
    )
    assert result["status"] == "DRY_RUN_COMPLETE"
    assert calendar["result"]["days"][0]["slots"] == []


def test_idempotent_replay_preserves_transaction_response_contract(tmp_path):
    service = bootstrap(str(tmp_path))
    arguments = {"premise": "A validation scenario"}

    first = service.execute_capability(
        "scenario.create", arguments, dry_run=True, operation_id="same-operation"
    )
    replay = service.execute_capability(
        "scenario.create", arguments, dry_run=True, operation_id="same-operation"
    )

    assert replay["idempotent_replay"] is True
    assert replay["transaction_id"] == first["transaction_id"]
    assert replay["status"] == first["status"]
    assert replay["result"] == first["result"]
    assert replay["rollback_available"] == first["rollback_available"]


def test_world_model_preserves_temporal_assertion_history(tmp_path):
    world = WorldModel(str(tmp_path))
    competitor = world.upsert_entity("CompetitorProduct", "Example 1000")
    first = world.assert_fact(
        competitor["id"], "price_usd", 999,
        classification="OBSERVED_FACT", valid_from="2026-03-01T00:00:00+00:00"
    )
    second = world.assert_fact(
        competitor["id"], "price_usd", 799,
        classification="OBSERVED_FACT", valid_from="2026-08-23T00:00:00+00:00"
    )

    march = world.get_entity(competitor["id"], as_of="2026-03-20T00:00:00+00:00")
    august = world.get_entity(competitor["id"], as_of="2026-08-24T00:00:00+00:00")

    assert march["assertions"][0]["value"] == 999
    assert august["assertions"][0]["value"] == 799
    assert second["supersedes_id"] == first["id"]


def test_job_checkpoints_and_steering_survive_reload(tmp_path):
    jobs = JobService(str(tmp_path))
    job = jobs.create(job_type="RESEARCH", objective="Research portable power", plan=["plan", "research", "synthesize"])
    jobs.transition(job["id"], "RUNNING")
    jobs.checkpoint(job["id"], 0, status="COMPLETED", checkpoint={"sources": 3})
    jobs.steer(job["id"], "Prioritize primary sources", "owner")

    restored = JobService(str(tmp_path)).get(job["id"])

    assert restored["status"] == "RUNNING"
    assert restored["steps"][0]["checkpoint"] == {"sources": 3}
    assert restored["steering"][0]["instruction"] == "Prioritize primary sources"


def test_master_conversation_fails_closed_without_configured_model(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    unavailable = ModelStatus(
        provider="github-copilot-sdk",
        configured_model="gpt-5.6-sol",
        authenticated=True,
        available=False,
        available_models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
        reason="configured_master_model_not_in_authenticated_model_list",
        checked_at=datetime.now(timezone.utc).isoformat(),
    )

    async def blocked(*args, **kwargs):
        raise MasterModelUnavailable(unavailable)

    monkeypatch.setattr(service.master, "converse", blocked)
    result = service.command("What matters today?")

    assert result["status"] == "BLOCKED"
    assert result["model_status"]["available_models"] == [{"id": "gpt-5.4", "name": "GPT-5.4"}]
    assert "No strategic-model downgrade" in result["message"]


def test_master_continues_durable_conversation_context(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    prompts = []

    async def converse(prompt, **kwargs):
        prompts.append({"prompt": prompt, "system_message": kwargs["system_message"]})
        return {
            "content": "Completed the next useful step.",
            "model": "gpt-5.6-sol",
            "provider": "github-copilot-sdk",
            "session_id": kwargs["session_id"],
        }

    monkeypatch.setattr(service.master, "converse", converse)
    first = service.command("My audience is campers and off-grid travelers.")
    second = service.command(
        "Build a week of content for them.", conversation_id=first["conversation_id"]
    )

    assert second["status"] == "COMPLETED"
    assert "My audience is campers and off-grid travelers." in prompts[1]["prompt"]
    assert "Build a week of content for them." in prompts[1]["prompt"]
    assert "CURRENT OPERATING STATE" in prompts[1]["prompt"]
    assert "outcome-driven master intelligence" in prompts[1]["system_message"]
    assert "do not restart discovery" in prompts[1]["prompt"]


def test_master_persists_recovered_session_id(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    conversation = service.create_conversation()

    async def converse(prompt, **kwargs):
        return {
            "content": "Recovered and completed.",
            "model": "gpt-5.6-sol",
            "provider": "github-copilot-sdk",
            "session_id": "infenergy-recovered-session",
            "session_recovered": True,
        }

    monkeypatch.setattr(service.master, "converse", converse)
    monkeypatch.setattr(service, "_copilot_tools", lambda actor: [])
    result = service.command("Continue the work", conversation_id=conversation["id"])

    assert result["status"] == "COMPLETED"
    assert service.get_conversation(conversation["id"])["copilot_session_id"] == "infenergy-recovered-session"


def test_master_timeout_is_preserved_as_resumable_conversation_state(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))

    async def time_out(*args, **kwargs):
        raise TimeoutError("Timeout after 300.0s waiting for session.idle")

    monkeypatch.setattr(service.master, "converse", time_out)
    result = service.command("Complete a substantial research and planning operation.")
    conversation = service.get_conversation(result["conversation_id"])

    assert result["status"] == "TIMED_OUT"
    assert "No completion is being claimed" in result["message"]
    assert conversation["messages"][-1]["metadata"]["timed_out"] is True
    assert conversation["messages"][-2]["content"] == "Complete a substantial research and planning operation."


def test_copilot_master_uses_autopilot_and_extended_wait(tmp_path, monkeypatch):
    import copilot

    calls = {}

    class FakeSession:
        async def send_and_wait(self, prompt, **kwargs):
            calls.update({"prompt": prompt, **kwargs})
            return SimpleNamespace(data=SimpleNamespace(content="Verified completion"))

        async def disconnect(self):
            return None

    class FakeClient:
        async def start(self):
            return None

        async def resume_session(self, session_id, **kwargs):
            calls["session"] = kwargs
            return FakeSession()

        async def stop(self):
            return None

    available = ModelStatus(
        provider="github-copilot-sdk", configured_model="gpt-5.6-sol",
        authenticated=True, available=True, available_models=[{"id": "gpt-5.6-sol"}],
        reason="available", checked_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setenv("INFENERGY_COMMAND_TIMEOUT_SECONDS", "300")
    monkeypatch.setattr(copilot, "CopilotClient", FakeClient)
    master = CopilotMaster(str(tmp_path))

    async def available_status():
        return available

    monkeypatch.setattr(master, "status_async", available_status)
    result = asyncio.run(master.converse("Finish the operation", session_id="test-session", system_message="Operate."))

    assert result["content"] == "Verified completion"
    assert calls["agent_mode"] == "autopilot"
    assert calls["timeout"] == 300.0


@pytest.mark.parametrize(
    "missing_error",
    [
        "CAPError: 400 The resource you requested was not found.",
        "JsonRpcError: JSON-RPC Error -32603: Request session.resume failed with message: Session not found: infenergy-stale",
    ],
)
def test_copilot_master_replaces_missing_session_resource_once(tmp_path, monkeypatch, missing_error):
    calls = []

    class FakeSession:
        session_id = "sdk-created-session"

        async def send_and_wait(self, prompt, **kwargs):
            return SimpleNamespace(data=SimpleNamespace(content="Recovered completion"))

        async def disconnect(self):
            return None

    class FakeClient:
        async def start(self):
            return None

        async def resume_session(self, session_id, **kwargs):
            calls.append(("resume", session_id))
            raise Exception(missing_error)

        async def create_session(self, **kwargs):
            calls.append(("create", kwargs.get("session_id")))
            return FakeSession()

        async def stop(self):
            return None

    copilot = ModuleType("copilot")
    copilot.CopilotClient = FakeClient
    copilot_session = ModuleType("copilot.session")
    copilot_session.PermissionDecisionUserNotAvailable = type("PermissionDecisionUserNotAvailable", (), {})
    monkeypatch.setitem(sys.modules, "copilot", copilot)
    monkeypatch.setitem(sys.modules, "copilot.session", copilot_session)

    available = ModelStatus(
        provider="github-copilot-sdk", configured_model="gpt-5.6-sol",
        authenticated=True, available=True, available_models=[{"id": "gpt-5.6-sol"}],
        reason="available", checked_at=datetime.now(timezone.utc).isoformat(),
    )
    master = CopilotMaster(str(tmp_path))

    async def available_status():
        return available

    monkeypatch.setattr(master, "status_async", available_status)
    result = asyncio.run(master.converse("Retry", session_id="missing-session", system_message="Operate."))

    assert result["content"] == "Recovered completion"
    assert result["session_recovered"] is True
    assert calls == [("resume", "missing-session"), ("create", None)]
    assert result["session_id"] == "sdk-created-session"


def test_copilot_master_does_not_retry_unrelated_json_rpc_error(tmp_path, monkeypatch):
    calls = []

    class FakeClient:
        async def start(self):
            return None

        async def resume_session(self, session_id, **kwargs):
            calls.append(("resume", session_id))
            raise Exception("JsonRpcError: JSON-RPC Error -32603: Request session.resume failed with message: Internal error")

        async def create_session(self, **kwargs):
            calls.append(("create", kwargs.get("session_id")))

        async def stop(self):
            return None

    copilot = ModuleType("copilot")
    copilot.CopilotClient = FakeClient
    copilot_session = ModuleType("copilot.session")
    copilot_session.PermissionDecisionUserNotAvailable = type("PermissionDecisionUserNotAvailable", (), {})
    monkeypatch.setitem(sys.modules, "copilot", copilot)
    monkeypatch.setitem(sys.modules, "copilot.session", copilot_session)

    available = ModelStatus(
        provider="github-copilot-sdk", configured_model="gpt-5.6-sol",
        authenticated=True, available=True, available_models=[{"id": "gpt-5.6-sol"}],
        reason="available", checked_at=datetime.now(timezone.utc).isoformat(),
    )
    master = CopilotMaster(str(tmp_path))

    async def available_status():
        return available

    monkeypatch.setattr(master, "status_async", available_status)
    with pytest.raises(Exception, match="Internal error"):
        asyncio.run(master.converse("Do not retry", session_id="active-session", system_message="Operate."))

    assert calls == [("resume", "active-session")]


def test_command_center_and_api_are_served(tmp_path):
    job = bootstrap(str(tmp_path)).jobs.create(
        job_type="campaign", objective="Visible completed campaign", plan=["produce"],
    )
    status, content_type, page = handle("GET", "/os/assets/advanced.html", None, str(tmp_path))
    api_status, api_type, payload = handle("GET", "/api/os/capabilities", None, str(tmp_path))
    job_status, job_type, job_payload = handle("GET", f"/api/os/jobs/{job['id']}", None, str(tmp_path))
    create_status, create_type, created_payload = handle(
        "POST", "/api/os/conversations", {"title": "Fresh objective"}, str(tmp_path)
    )
    creative_status, _, creative_payload = handle(
        "POST", "/api/os/creatives", {"title": "Saved idea", "idea": "A complete idea"}, str(tmp_path)
    )
    js_status, js_type, javascript = handle("GET", "/os/assets/app.js", None, str(tmp_path))
    css_status, css_type, stylesheet = handle("GET", "/os/assets/styles.css", None, str(tmp_path))

    assert status == 200
    assert content_type.startswith("text/html")
    assert b"Infenergy Intelligence OS" in page
    assert b'id="mobile-nav"' in page
    assert b'app.js?v=29' in page
    assert b'styles.css?v=20' in page
    assert b'id="post-composer-form"' in page
    assert b'id="composer-post-type"' in page
    assert b'value="infenergy_micro_mission"' in page
    assert b'value="infenergy_storypage"' in page
    assert b'value="superhero_text_integration"' in page
    assert b'Eight connected 4:5 story cards' in page
    assert b'id="composer-provider"' in page
    assert b'id="generation-form"' in page
    assert b'data-view="content-plan"' in page
    assert b'id="plan-audience"' in page
    assert b'id="plan-image-count">0 images' in page
    assert b'data-view="master"' in page
    assert b'id="master-capabilities"' in page
    assert b'id="master-form"' in page
    assert b'id="master-transactions"' in page
    assert b'data-master-preset="creative"' in page
    assert b'data-master-preset="undo"' in page
    assert b'data-view="creative"' in page
    assert b'id="new-creative"' in page
    assert b'id="creative-title"' in page
    assert b'id="creative-run"' in page
    assert b'id="login-form"' in page
    assert b'id="login-password"' in page
    assert b'id="logout"' in page
    assert b'id="job-search"' in page
    assert b"function operationOutput(source)" in javascript
    assert b"source?.creative" in javascript
    assert b"/^https?:\\/\\//i.test" in javascript
    assert b"result.status === 'DELIVERED'" in javascript


    assert b"renderDeliverables(result)" in javascript
    assert b"data-view-deliverables" in javascript
    assert b"Not scheduled \xc2\xb7 Not published" in javascript
    assert b"/api/os/transactions" in javascript
    assert b".deliverable-grid" in stylesheet
    assert b'id="social-calendar"' in page
    assert b"/api/os/calendar" in javascript
    assert b"View exact post" in javascript
    assert b"platform_schedule" in javascript
    assert b".calendar-platform-times" in stylesheet
    assert b"timeZoneName: 'short'" in javascript
    assert b".calendar-days" in stylesheet
    assert api_status == 200
    assert api_type.startswith("application/json")
    assert b"system.health" in payload
    assert job_status == 200
    assert job_type.startswith("application/json")
    assert json.loads(job_payload)["job"]["id"] == job["id"]
    assert create_status == 201
    assert create_type.startswith("application/json")
    assert json.loads(created_payload)["conversation"]["title"] == "Fresh objective"
    assert creative_status == 201
    creative = json.loads(creative_payload)["creative"]
    rename_status, _, renamed_payload = handle(
        "POST", f"/api/os/creatives/{creative['id']}", {"title": "Renamed idea"}, str(tmp_path)
    )
    assert rename_status == 200
    assert json.loads(renamed_payload)["creative"]["title"] == "Renamed idea"
    created = json.loads(created_payload)["conversation"]
    archive_status, _, archived_payload = handle(
        "POST", f"/api/os/conversations/{created['id']}/archive", {}, str(tmp_path)
    )
    assert archive_status == 200
    assert json.loads(archived_payload)["conversation"]["status"] == "ARCHIVED"
    assert js_status == 200
    assert js_type.startswith("text/javascript")
    assert b"function renderResearch" in javascript
    assert b"function renderSocial" in javascript
    assert b"function renderHealth" in javascript
    assert b"function activateView" in javascript
    assert b"function richText" in javascript
    assert b"/api/os/conversations" in javascript
    assert b"Approve & run" in javascript
    assert b"Run checks, approve & schedule" in javascript
    assert b"/api/os/creatives" in javascript
    assert b"/api/os/generation-requests" in javascript
    assert b"/api/os/capabilities" in javascript
    assert b"/api/os/transactions" in javascript
    assert b"/api/os/execute" in javascript
    assert b"data-master-approval" in javascript
    assert b"data-master-rollback" in javascript
    assert b"function renderMasterCapabilities" in javascript
    assert b"function renderMasterTransactions" in javascript
    assert b"syncConversation" in javascript
    assert b"sessionStorage.getItem('infenergyToken')" in javascript
    assert b"localStorage.getItem('infenergyToken')" not in javascript
    assert b"prompt('Enter the Infenergy owner token')" not in javascript
    assert b"View persisted deliverables" in javascript
    assert b"data-job-id" in javascript
    assert b"JSON.stringify(item" not in javascript
    assert css_status == 200
    assert css_type.startswith("text/css")
    assert b".provider-grid" in stylesheet
    assert b".slot-grid" in stylesheet
    assert b".mobile-view-picker" in stylesheet
    assert b".creative-grid" in stylesheet
    assert b".generation-day-grid" in stylesheet
    assert b".master-grid" in stylesheet
    assert b".master-capability" in stylesheet
    assert b".master-transaction-list" in stylesheet
    assert b"overflow-x: hidden" in stylesheet
    assert b".login-screen" in stylesheet
    assert b"grid-template-columns: minmax(0, 1fr)" in stylesheet


def test_generation_request_delegates_unspecified_fields_and_updates_only_one_day(tmp_path):
    service = bootstrap(str(tmp_path))
    request = service.create_generation_request(
        start_date="2026-09-01",
        days=3,
        controls={"topic": {"mode": "CUSTOM", "value": "Preparedness"}},
    )
    untouched_before = request["day_cards"][1]

    status, _, response = handle(
        "PATCH",
        f"/api/os/generation-requests/{request['id']}/days/2026-09-01",
        {
            "frequency": {"mode": "CUSTOM", "value": "2"},
            "controls": {"format": {"mode": "CUSTOM", "value": "Carousel"}},
        },
        str(tmp_path),
    )
    updated = json.loads(response)["request"]

    assert status == 200
    assert request["controls"]["topic"] == {"mode": "CUSTOM", "value": "Preparedness"}
    assert request["controls"]["style"] == {"mode": "AUTO", "value": ""}
    assert updated["day_cards"][0]["frequency"] == {"mode": "CUSTOM", "value": "2"}
    assert len(updated["day_cards"][0]["posts"]) == 2
    assert updated["day_cards"][0]["controls"]["format"] == {"mode": "CUSTOM", "value": "Carousel"}
    assert updated["day_cards"][0]["controls"]["tone"] == {"mode": "AUTO", "value": ""}
    assert updated["day_cards"][1] == untouched_before


def test_generation_request_hydrates_controls_window_and_persists_execution(tmp_path, monkeypatch):
    service = bootstrap(str(tmp_path))
    request = service.create_generation_request(
        start_date="2026-09-01", days=3, production_window_days=1,
        controls={"format": {"mode": "CUSTOM", "value": "Carousel"}},
        day_overrides={
            "2026-09-01": {"controls": {"platform": {"mode": "CUSTOM", "value": "LinkedIn"}}},
        },
    )
    captured = {}

    def execute(capability, payload, **kwargs):
        captured.update({"capability": capability, "payload": payload})
        return {
            "status": "COMPLETED", "transaction_id": "tx-1",
            "result": {"production_status": "DELIVERED", "creative_id": "creative-1"},
        }

    monkeypatch.setattr(service, "execute_capability", execute)
    produced = service.produce_generation_post(
        request["id"], "2026-09-01", "2026-09-01-1",
    )

    assert request["day_cards"][0]["posts"][0]["format"] == "Carousel"
    assert request["day_cards"][0]["posts"][0]["platforms"] == "LinkedIn"
    assert request["day_cards"][0]["production_eligible"] is True
    assert request["day_cards"][1]["production_eligible"] is False
    assert captured["capability"] == "creative.command.produce"
    assert "Format: Carousel" in captured["payload"]["command"]
    assert "Platform: LinkedIn" in captured["payload"]["command"]
    assert produced["version"]["transaction_id"] == "tx-1"
    assert produced["request"]["day_cards"][0]["posts"][0]["versions"][0]["creative_id"] == "creative-1"
    with pytest.raises(ValueError, match="unsupported_generation_regeneration_scope"):
        service.produce_generation_post(request["id"], "2026-09-01", "2026-09-01-1", regeneration_scope="visual")
    with pytest.raises(ValueError, match="outside_production_window"):
        service.produce_generation_post(request["id"], "2026-09-02", "2026-09-02-1")


def test_rolling_generation_request_rehydrates_current_production_window(tmp_path, monkeypatch):
    import social_engine.intelligence_os.service as service_module

    class SeptemberFirst(service_module.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 1)

    class SeptemberSecond(service_module.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 2)

    monkeypatch.setattr(service_module, "date", SeptemberFirst)
    service = bootstrap(str(tmp_path))
    request = service.create_generation_request(
        start_date="2026-09-01", days=3, production_window_days=1, rolling_production=True,
    )
    assert [card["production_eligible"] for card in request["day_cards"]] == [True, False, False]

    monkeypatch.setattr(service_module, "date", SeptemberSecond)
    advanced = service.get_generation_request(request["id"])
    assert [card["production_eligible"] for card in advanced["day_cards"]] == [False, True, False]


def test_delivered_creative_schedules_reviewed_package_without_regeneration(tmp_path, monkeypatch):
    from social_engine.intelligence_os.db import connect, encode

    service = bootstrap(str(tmp_path))
    creative = service.create_creative(title="Reviewed flagship", idea="Approved story")
    reviewed_package = {
        "post_id": "reviewed-1", "platforms": ["facebook", "instagram"],
        "platform_policy": {"platforms": ["facebook", "instagram"]},
        "generated_visuals": {"instagram": "https://studio.test/reviewed.png"},
    }
    with connect(str(tmp_path)) as connection:
        connection.execute(
            "UPDATE os_creatives SET status='DELIVERED', package_json=?, preflight_json=? WHERE id=?",
            (encode(reviewed_package), encode({"passed": True}), creative["id"]),
        )
        connection.commit()
    calls = []

    def execute(capability, payload, **kwargs):
        calls.append((capability, payload))
        assert capability != "creative.carousel.generate"
        return {"status": "COMPLETED", "result": {"outbox_id": "outbox-1", "decision_id": "decision-1"}}

    monkeypatch.setattr(service, "execute_capability", execute)
    scheduled = service.prepare_and_schedule_creative(
        creative["id"], content_date="2026-09-03", scheduled_at="2026-09-03T17:00:00+00:00",
    )

    assert calls == [("social.schedule", calls[0][1])]
    assert calls[0][1]["package"] == reviewed_package
    assert scheduled["creative"]["status"] == "SCHEDULED"
    assert scheduled["creative"]["schedule"]["outbox_id"] == "outbox-1"


def test_generation_post_schedules_latest_delivered_version_directly(tmp_path, monkeypatch):
    from social_engine.intelligence_os.db import connect, encode

    service = bootstrap(str(tmp_path))
    request = service.create_generation_request(
        start_date="2026-09-03", days=1,
        day_overrides={"2026-09-03": {"frequency": {"mode": "CUSTOM", "value": "1"}}},
    )
    post = request["day_cards"][0]["posts"][0]
    creative = service.create_creative(title="Reviewed day post", idea="Approved day story")
    reviewed_package = {
        "post_id": post["id"], "platforms": ["instagram"],
        "platform_policy": {"platforms": ["instagram"]},
        "generated_visuals": {"instagram": "https://studio.test/reviewed.png"},
    }
    with connect(str(tmp_path)) as connection:
        connection.execute(
            "UPDATE os_creatives SET status='DELIVERED', package_json=?, preflight_json=? WHERE id=?",
            (encode(reviewed_package), encode({"passed": True}), creative["id"]),
        )
        post["versions"] = [{
            "number": 1, "scope": "entire post", "status": "DELIVERED",
            "creative_id": creative["id"], "created_at": "2026-09-01T12:00:00+00:00",
        }]
        connection.execute(
            "UPDATE os_generation_requests SET day_cards_json=? WHERE id=?",
            (encode(request["day_cards"]), request["id"]),
        )
        connection.commit()
    calls = []

    def execute(capability, payload, **kwargs):
        calls.append((capability, payload))
        return {"status": "COMPLETED", "result": {"outbox_id": "outbox-day", "decision_id": "decision-day"}}

    monkeypatch.setattr(service, "execute_capability", execute)
    scheduled = service.schedule_generation_post(
        request["id"], "2026-09-03", post["id"], scheduled_at="2026-09-03T14:00:00+00:00",
    )

    assert [call[0] for call in calls] == ["social.schedule"]
    assert calls[0][1]["package"] == reviewed_package
    assert scheduled["version"]["status"] == "SCHEDULED"
    assert scheduled["version"]["schedule"]["outbox_id"] == "outbox-day"


def test_generation_request_api_supports_365_days_and_zero_or_multiple_posts(tmp_path):
    status, _, response = handle(
        "POST",
        "/api/os/generation-requests",
        {
            "start_date": "2026-09-01",
            "days": 365,
            "day_overrides": {
                "2026-09-01": {"frequency": {"mode": "CUSTOM", "value": "0"}},
                "2026-09-02": {"frequency": {"mode": "CUSTOM", "value": "3"}},
            },
        },
        str(tmp_path),
    )
    request = json.loads(response)["request"]

    assert status == 201
    assert request["horizon_days"] == 365
    assert request["end_date"] == "2027-08-31"
    assert len(request["day_cards"]) == 365
    assert request["day_cards"][0]["posts"] == []
    assert len(request["day_cards"][1]["posts"]) == 3


def test_content_plan_activation_queues_120_day_runtime_contract(monkeypatch, tmp_path):
    calls = []

    def build_monthly_calendar(**kwargs):
        calls.append(kwargs)
        return {"status": "READY", "queued": 120, "coverage_days": 120}

    monkeypatch.setattr("build_monthly_content.build_monthly_calendar", build_monthly_calendar)
    status, content_type, payload = handle(
        "POST",
        "/api/os/content-plan/activate",
        {"start_date": "2026-09-02", "days": 120},
        str(tmp_path),
    )

    assert status == 200
    assert content_type.startswith("application/json")
    assert json.loads(payload)["coverage_days"] == 120
    assert calls == [{
        "data_dir": str(tmp_path),
        "start_date": "2026-09-02",
        "days": 120,
        "enqueue": True,
        "replace_unpublished": True,
        "content_plan": "content_plan_120",
    }]


def test_120_day_plan_is_a_durable_approval_gated_job(tmp_path):
    service = bootstrap(str(tmp_path))
    arguments = {
        "objective": "Entertainment first; product when it fits; keep the future adaptive.",
        "locked_days": 7,
    }
    dry_run = service.execute_capability("content.plan_120_days", arguments, dry_run=True)
    pending = service.execute_capability("content.plan_120_days", arguments)

    assert dry_run["status"] == "DRY_RUN_COMPLETE"
    assert dry_run["required_approval"]["required"] is True
    assert pending["status"] == "WAITING_APPROVAL"


def test_source_quality_and_news_findings_retain_provenance(tmp_path, monkeypatch):
    rss = b"""<?xml version='1.0'?><rss><channel><item><title>Battery safety update</title><link>https://www.energy.gov/example</link><description>New official guidance.</description><source>Department of Energy</source><pubDate>Sun, 23 Aug 2026 12:00:00 GMT</pubDate></item></channel></rss>"""

    class Response:
        content = rss

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr("social_engine.intelligence_os.intelligence.requests.get", lambda *args, **kwargs: Response())
    source_type, credibility = classify_source("https://www.energy.gov/example")
    result = ResearchIntelligence(str(tmp_path)).search_news("battery safety", freshness_days=365)

    assert source_type == "government"
    assert credibility == pytest.approx(0.95)
    assert result["count"] == 1
    assert result["findings"][0]["source_id"]
    assert result["findings"][0]["expires_at"]


def test_automation_and_watch_are_durable_and_capability_based(tmp_path):
    automations = AutomationService(str(tmp_path))
    created = automations.create(
        name="Morning intelligence",
        trigger={"type": "schedule"},
        schedule={"every_minutes": 1440},
        steps=[{"capability": "research.news", "arguments": {"query": "portable power"}}],
        created_by="owner",
    )
    watch = automations.create_watch(
        subject="EcoFlow pricing",
        scope={"market": "US"},
        frequency="daily",
        source_policy={"prefer": ["official_company"]},
        materiality_threshold=0.8,
        condition={"price_change_percent": {"gte": 10}},
        actions=[{"capability": "attention.get"}],
    )

    assert AutomationService(str(tmp_path)).get(created["id"])["steps"][0]["capability"] == "research.news"
    assert automations.list_watches()[0]["id"] == watch["id"]
    assert automations.set_status(created["id"], "PAUSED")["status"] == "PAUSED"


def test_material_events_become_attention_items(tmp_path):
    service = bootstrap(str(tmp_path))
    service.events.emit(
        "PROVIDER_DOWN",
        source="test",
        payload={"error": "Publisher authentication failed", "confidence": 1.0},
        materiality=0.95,
    )

    result = heartbeat(str(tmp_path))

    assert result["attention_created"] == 1
    assert service.attention.list_open()[0]["title"] == "Provider Down"


def test_due_automation_runs_registered_capability_and_records_result(tmp_path):
    service = bootstrap(str(tmp_path))
    automations = AutomationService(str(tmp_path))
    created = automations.create(
        name="Immediate health check",
        trigger={"type": "schedule"},
        schedule={"next_run": "2000-01-01T00:00:00+00:00"},
        steps=[{"capability": "system.health", "arguments": {}}],
        created_by="owner",
    )

    runs = automations.run_due(service)

    assert runs[0]["automation_id"] == created["id"]
    assert runs[0]["status"] == "SUCCEEDED"
    assert runs[0]["outputs"][0]["capability"] == "system.health"
    assert automations.list_runs()[0]["status"] == "SUCCEEDED"


def test_creative_scoring_adapter_uses_preserved_quality_engine(tmp_path):
    service = bootstrap(str(tmp_path))
    result = service.execute_capability(
        "creative.score",
        {
            "content": {
                "selected_hook": "How prepared is your home for an outage?",
                "selected_cta": "Compare the right fit",
                "product_name": "Power Station",
                "funnel_stage": "EDUCATION",
                "platform_posts": {"facebook": {"caption": "Home outage planning starts with checking your real device load. Compare the Power Station against what you need to keep powered, then share what matters most in your home?"}},
            },
            "platforms": ["facebook"],
        },
    )

    assert result["status"] == "COMPLETED"
    assert result["result"]["evaluation"]["platform_results"]["facebook"]["total"] > 0
    assert result["result"]["production_mutated"] is False


def test_publication_operations_and_dispatch_preview_are_safe(tmp_path):
    service = bootstrap(str(tmp_path))
    operations = service.execute_capability("publication.operations.get", {})
    preview = service.execute_capability("publication.dispatch", {}, dry_run=True)

    assert operations["status"] == "COMPLETED"
    assert "readiness" in operations["result"]
    assert preview["status"] == "DRY_RUN_COMPLETE"
    assert preview["result"]["production_mutated"] is False


def test_publication_delete_requires_approval_and_previews_exact_id(tmp_path):
    service = bootstrap(str(tmp_path))

    pending = service.execute_capability(
        "publication.delete",
        {"platform": "facebook", "post_id": "page_post_duplicate"},
    )
    preview = service.execute_capability(
        "publication.delete",
        {"platform": "facebook", "post_id": "page_post_duplicate"},
        dry_run=True,
    )

    assert pending["status"] == "WAITING_APPROVAL"
    assert preview["status"] == "DRY_RUN_COMPLETE"
    assert preview["result"]["would_delete"] == {
        "platform": "facebook",
        "post_id": "page_post_duplicate",
    }
    assert preview["result"]["production_mutated"] is False


def test_copilot_tool_adapter_uses_sdk_invocation_contract(tmp_path):
    import asyncio
    from copilot.tools import ToolInvocation

    service = bootstrap(str(tmp_path))
    tool = next(item for item in service._copilot_tools("owner") if item.name == "system_health")
    result = asyncio.run(tool.handler(ToolInvocation(tool_name="system_health", arguments={})))

    assert result.result_type == "success"
    payload = json.loads(result.text_result_for_llm)
    assert payload["status"] == "COMPLETED"
    assert payload["result"]["status"] in {"OPERATIONAL", "DEGRADED"}
