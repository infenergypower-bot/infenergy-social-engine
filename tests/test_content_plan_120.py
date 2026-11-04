from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

from PIL import Image


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from content_plan_120 import build_120_day_plan  # noqa: E402
from build_monthly_content import _gemini_generation_plan, _package, _plan_entry_thought, _render_assets  # noqa: E402
from social_engine.intelligence_os.web import handle  # noqa: E402


def _write_profiles(data_dir, count: int = 8) -> None:
    marketing = data_dir / "marketing"
    marketing.mkdir(parents=True)
    profiles = {
        f"PRODUCT-{index:02d}": {
            "product_name": f"Product {index}",
            "product_type": "portable_power",
            "market_role": f"role {index}",
            "primary_promise": f"promise {index}",
            "core_customer_truth": f"truth {index}",
            "primary_call_to_action": f"compare product {index}",
            "personas": [{"name": f"Persona {index}", "use_case": f"use case {index}"}],
        }
        for index in range(count)
    }
    (marketing / "product_consumer_profiles.json").write_text(
        json.dumps({"profiles": profiles}),
        encoding="utf-8",
    )


def test_plan_covers_catalog_and_never_creates_generation_artifacts(tmp_path):
    _write_profiles(tmp_path, count=12)

    plan = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )

    assert plan["status"] == "TEXT_PREVIEW_READY"
    assert plan["mode"] == "TEXT_ONLY"
    assert plan["concept_count"] == 120
    assert plan["catalog_products_used"] == plan["catalog_size"] == 12
    assert plan["catalog_coverage_complete"] is True
    assert plan["image_generation_enabled"] is False
    assert plan["image_count"] == 0
    assert not (tmp_path / "public_media").exists()
    assert not (tmp_path / "inventory.db").exists()
    assert len({entry["date"] for entry in plan["entries"]}) == 120
    assert plan["date_coverage"] == {
        "expected_days": 120,
        "planned_days": 120,
        "continuous": True,
    }
    assert all(entry["production_status"] == "CONCEPT_ONLY" for entry in plan["entries"])
    assert all(entry["image_status"] == "NOT_GENERATED" for entry in plan["entries"])
    assert all(entry["generation_prompts"] == [] for entry in plan["entries"])
    assert all(entry["media_assets"] == [] for entry in plan["entries"])
    assert all(entry["consumer_root_id"] for entry in plan["entries"])
    assert all(entry["consumer_receipt"]["useful_discovery"] for entry in plan["entries"])
    assert all(entry["consumer_story_contract"]["visual_evidence"] for entry in plan["entries"])
    assert len({entry["consumer_world_id"] for entry in plan["entries"]}) >= 12


def test_consumer_root_reaches_monthly_outbox_package(tmp_path):
    _write_profiles(tmp_path)
    entry = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-03",
        days=1,
    )["entries"][0]
    thought = _plan_entry_thought(entry)
    knowledge = {
        "knowledge_id": "test-knowledge",
        "schema_version": "test.v1",
        "agent_specializations": {},
    }
    package = _package(
        knowledge,
        thought,
        entry["date"],
        0,
        str(tmp_path),
        defer_images=True,
    )

    assert package["consumer_root_id"] == entry["consumer_root_id"]
    assert package["consumer_receipt"] == entry["consumer_receipt"]
    assert package["visual_plan"]["consumer_story_contract"] == entry["consumer_story_contract"]
    assert package["consumer_receipt_qa"]["passed"] is True
    assert package["gemini_copy"]["provider"] == "gemini"
    assert package["gemini_copy"]["strict_provider"] is True
    assert package["gemini_copy"]["fallback_allowed"] is False
    assert package["gemini_generation"]["provider"] == "gemini"
    assert package["gemini_generation"]["strict_provider"] is True
    assert package["gemini_generation"]["required_image_count"] >= 1
    assert package["gemini_generation"]["prompts"]


def test_product_package_carries_immutable_image_approval(tmp_path):
    _write_profiles(tmp_path)
    product_id = "PRODUCT-01"
    image_url = "https://example.com/product.png"
    product_dir = tmp_path / "product_briefs"
    product_dir.mkdir(parents=True)
    (product_dir / f"{product_id}.json").write_text(json.dumps({
        "product_id": product_id,
        "name": "Product One",
        "source_image_url": image_url,
        "verified_facts": ["10,000mAh battery capacity"],
        "visual_direction": "Show the exact product powering a phone during an outage.",
    }), encoding="utf-8")
    entry = build_120_day_plan(data_dir=str(tmp_path), start_date="2026-09-15", days=1)["entries"][0]
    thought = _plan_entry_thought(entry)
    thought["product_id"] = product_id
    approval = {"product_id": product_id, "source_url": image_url, "sha256": "approved-digest"}

    with patch("build_monthly_content.build_product_image_approval", return_value=approval) as build_approval:
        package = _package(
            {"knowledge_id": "test-knowledge", "schema_version": "test.v1", "agent_specializations": {}},
            thought,
            "2026-09-15",
            0,
            str(tmp_path),
            defer_images=True,
        )

    assert package["product_image_approval"] == approval
    build_approval.assert_called_once_with(package, image_url)


def test_company_voice_package_uses_strict_gemini_generation_contract(tmp_path):
    _write_profiles(tmp_path)
    entry = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-06",
        days=1,
    )["entries"][0]
    thought = _plan_entry_thought(entry)
    package = _package(
        {"knowledge_id": "test-knowledge", "schema_version": "test.v1", "agent_specializations": {}},
        thought,
        entry["date"],
        0,
        str(tmp_path),
        defer_images=True,
    )

    assert thought["source_note"] != "120-day Infenergy content plan"
    assert package["gemini_copy"]["provider"] == "gemini"
    assert package["gemini_copy"]["strict_provider"] is True
    assert package["gemini_generation"]["provider"] == "gemini"
    assert package["gemini_generation"]["strict_provider"] is True
    assert package["gemini_generation"]["required_image_count"] >= 1
    assert package["gemini_generation"]["prompts"]


def test_plan_package_never_renders_deterministic_placeholder_media(tmp_path):
    _write_profiles(tmp_path)
    entry = build_120_day_plan(data_dir=str(tmp_path), start_date="2026-09-06", days=1)["entries"][0]
    package = _package(
        {"knowledge_id": "test-knowledge", "schema_version": "test.v1", "agent_specializations": {}},
        _plan_entry_thought(entry),
        entry["date"],
        0,
        str(tmp_path),
    )

    assert package["generated_visuals"] == {}
    assert package["primary_publish_image_url"] == ""
    assert not (tmp_path / "public_media").exists()


def test_plan_thought_preserves_slot_and_deterministic_canvas_contract(tmp_path):
    _write_profiles(tmp_path)
    entries = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-01",
        days=4,
    )["entries"]
    entry = next(candidate for candidate in entries if candidate["format"] == "product_comic_strip_carousel")
    thought = _plan_entry_thought(entry)

    assets = _render_assets(str(tmp_path), thought, 0)

    assert thought["slot"] == entry["slot"] == "midday"
    assert len(assets["slides"]) == 3
    for asset in assets["slides"]:
        with Image.open(asset["local_path"]) as image:
            assert image.size == (1080, 1350)


def test_plan_thought_never_exposes_internal_public_labels(tmp_path):
    _write_profiles(tmp_path)
    entries = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-04",
        days=120,
    )["entries"]

    public_statements = [_plan_entry_thought(entry)["statement"].upper() for entry in entries]

    assert all("POV:" not in statement for statement in public_statements)
    assert all("FIELD TRUTH" not in statement for statement in public_statements)


def test_story_plan_requires_vertical_gemini_asset_and_preserves_morning_slot(tmp_path):
    _write_profiles(tmp_path)
    entries = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-01",
        days=4,
    )["entries"]
    entry = next(candidate for candidate in entries if candidate["format"] == "product_story_page")
    thought = _plan_entry_thought(entry)
    generation = _gemini_generation_plan(thought)

    assert thought["slot"] == entry["slot"] == "morning"
    assert generation["aspect_ratio"] == "9:16"
    assert generation["required_image_count"] == 1
    assert generation["strict_provider"] is True
    assert generation["fallback_allowed"] is False


def test_product_days_never_use_a_no_product_consumer_moment(tmp_path):
    _write_profiles(tmp_path)
    entries = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-03",
    )["entries"]

    assert all(
        entry["consumer_root"]["moment"]["product_fit"]["mode"] != "none"
        for entry in entries
        if entry["product"]
    )


def test_plan_preserves_intervention_cadence_and_continuous_format_rotation(tmp_path):
    _write_profiles(tmp_path)

    plan = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )
    interventions = [
        entry for entry in plan["entries"]
        if entry["series"] == "Infenergy Intervention"
    ]

    assert len(interventions) == 34
    assert all(
        (entry["weekday"], entry["slot"]) in {
            ("Tuesday", "midday"),
            ("Friday", "morning"),
        }
        for entry in interventions
    )
    assert [entry["installment"] for entry in interventions] == list(range(1, 35))
    friday_interventions = [entry for entry in interventions if entry["weekday"] == "Friday"]
    tuesday_interventions = [entry for entry in interventions if entry["weekday"] == "Tuesday"]
    assert all(entry["format"] == "product_comic_strip_carousel" for entry in tuesday_interventions)
    assert all(entry["format"] == "product_story_page" for entry in friday_interventions)
    assert all(entry["canon_required"] for entry in interventions)
    assert plan["weekly_comic_strip_carousel_count"] == len(tuesday_interventions) == 17
    assert plan["weekly_story_page_count"] == len(friday_interventions) == 17
    assert all(entry["format_label"] == "Comic strip carousel" for entry in tuesday_interventions)
    assert all(entry["delivery_label"] == "Product comic strip carousel" for entry in tuesday_interventions)
    assert all(entry["platform"] == "instagram_feed" for entry in tuesday_interventions)
    assert all(entry["aspect_ratio"] == "4:5" for entry in tuesday_interventions)
    assert all(entry["canvas_px"] == {"width": 1080, "height": 1350} for entry in tuesday_interventions)
    assert all(entry["canvas_count"] == 3 for entry in tuesday_interventions)
    assert all(entry["layout"] == "three_frame_comic_carousel" for entry in tuesday_interventions)
    assert all(entry["format_label"] == "Story page post" for entry in friday_interventions)
    assert all(entry["delivery_label"] == "Product Story page" for entry in friday_interventions)
    assert all(entry["platform"] == "instagram_story" for entry in friday_interventions)
    assert all(entry["aspect_ratio"] == "9:16" for entry in friday_interventions)
    assert all(entry["canvas_px"] == {"width": 1080, "height": 1920} for entry in friday_interventions)
    assert all(entry["canvas_count"] == 1 for entry in friday_interventions)
    assert all(entry["layout"] == "single_vertical_story_page" for entry in friday_interventions)
    assert all(entry["visible_text"]["headline"] for entry in interventions)
    assert all(entry["visible_text"]["infenergy_line"] for entry in interventions)
    assert all(entry["visible_text"]["resolution_line"] for entry in interventions)
    assert all(entry["panel_count"] == 3 for entry in interventions)
    assert all(entry["product_required"] is True for entry in interventions)
    assert all(entry["product_id"] == entry["product"]["product_id"] for entry in interventions)
    assert all(entry["product_reference_required"] is True for entry in interventions)
    assert all(entry["product_integration"]["required"] is True for entry in interventions)
    assert all("Removing the product" in entry["product_integration"]["plot_test"] for entry in interventions)
    assert all(entry["product_name"] in entry["story_sequence"][2] for entry in interventions)
    assert len({entry["entertainment_mode"] for entry in interventions}) == 6
    assert all(entry["entertainment_hook"] and entry["visual_reveal"] for entry in interventions)
    assert 0 < sum(entry["humor_enabled"] for entry in interventions) < len(interventions)
    assert all("never the customer" in entry["humor_guardrail"] for entry in interventions)
    assert all(len(entry["story_sequence"]) == 3 for entry in interventions)
    assert all(
        any("true swipeable carousel" in constraint for constraint in entry["delivery_constraints"])
        for entry in tuesday_interventions
    )
    assert all(
        any("affect the plot" in constraint for constraint in entry["delivery_constraints"])
        for entry in interventions
    )
    assert all(entry["image_status"] == "NOT_GENERATED" for entry in interventions)

    complete_weeks = {
        entry["week"] for entry in plan["entries"]
        if {candidate["weekday"] for candidate in plan["entries"] if candidate["week"] == entry["week"]}
        == {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
    }
    for week in complete_weeks:
        formats = {entry["format"] for entry in plan["entries"] if entry["week"] == week}
        assert {"product_comic_strip_carousel", "product_story_page", "infenergy_company_quote_visual"}.issubset(formats)


def test_plan_includes_required_hero_and_mission_formats_with_approved_copy_rotation(tmp_path):
    _write_profiles(tmp_path)

    entries = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-03",
    )["entries"]
    delivery_counts = {
        delivery_type: sum(entry.get("delivery_type") == delivery_type for entry in entries)
        for delivery_type in {
            "hero_text_still",
            "comic_strip_carousel",
            "story_page_post",
        }
    }
    copy_form_counts = {
        entry["copy_form"]: sum(candidate["copy_form"] == entry["copy_form"] for candidate in entries)
        for entry in entries
    }

    assert all(count > 0 for count in delivery_counts.values())
    assert delivery_counts["hero_text_still"] == 17
    assert delivery_counts["comic_strip_carousel"] == 17
    assert delivery_counts["story_page_post"] == 17
    assert len(copy_form_counts) == 10
    assert set(copy_form_counts.values()) == {12}
    assert all(entry.get("canon_required") for entry in entries if entry.get("delivery_type"))
    assert all(entry.get("hero_text_required") for entry in entries if entry.get("delivery_type") in {"hero_text_still", "comic_strip_carousel", "story_page_post"})
    assert all(entry.get("still_images_only") for entry in entries if entry.get("delivery_type") in {"hero_text_still", "comic_strip_carousel", "story_page_post"})

    weekly_formats = {
        entry["format"]: _gemini_generation_plan(
            _plan_entry_thought(entry),
            {"name": entry["product_name"], "visual_direction": "Use the verified reference.", "image_url": ""}
            if entry.get("product_name") else None,
        )
        for entry in entries[:7]
        if entry["format"] in {"product_comic_strip_carousel", "product_story_page", "infenergy_company_quote_visual"}
    }
    assert weekly_formats["product_comic_strip_carousel"]["required_image_count"] == 3
    assert weekly_formats["product_comic_strip_carousel"]["aspect_ratio"] == "4:5"
    assert weekly_formats["product_story_page"]["required_image_count"] == 1
    assert weekly_formats["product_story_page"]["aspect_ratio"] == "9:16"
    assert weekly_formats["infenergy_company_quote_visual"]["required_image_count"] == 1
    assert weekly_formats["infenergy_company_quote_visual"]["aspect_ratio"] == "4:5"


def test_weekly_company_quotes_are_verbatim_sourced_and_audience_matched(tmp_path):
    _write_profiles(tmp_path)
    plan = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )
    with open("data/marketing/infenergy_company_knowledge.json", "r", encoding="utf-8") as handle:
        knowledge = json.load(handle)
    statements = {
        message["id"]: message["statement"]
        for message in knowledge["super_message_library"]
    }
    quotes = [
        entry for entry in plan["entries"]
        if entry["format"] == "infenergy_company_quote_visual"
    ]

    assert plan["weekly_company_quote_count"] == len(quotes) == 17
    assert plan["company_super_message_bank_count"] == len(statements) == 52
    assert len(statements.values()) == len(set(statements.values()))
    assert all(len(statement.split()) <= 12 for statement in statements.values())
    assert all(entry["weekday"] == "Sunday" for entry in quotes)
    assert len({entry["company_source"]["message_id"] for entry in quotes}) == len(quotes)
    assert all(entry["verbatim_company_quote"] is True for entry in quotes)
    assert all(entry["company_source"]["knowledge_id"] == knowledge["knowledge_id"] for entry in quotes)
    assert all(
        entry["exact_visible_text"] == [statements[entry["company_source"]["message_id"]]]
        for entry in quotes
    )
    assert all(entry["company_source"]["support_thought_id"].startswith("T") for entry in quotes)
    assert all(len(entry["exact_visible_text"][0].split()) <= 12 for entry in quotes)
    assert all(entry["support_statement"] != entry["exact_visible_text"][0] for entry in quotes)
    expected_audiences = {
        "mobile_professional": "mobile_professional",
        "outdoor_enthusiast": "outdoor",
        "caregiver": "caregiver",
        "small_business_operator": "mobile_professional",
        "preparedness_buyer": "preparedness_builder",
    }
    assert all(
        entry["company_source"]["audience"] == expected_audiences[entry["audience_id"]]
        for entry in quotes
    )
    assert all(entry["aspect_ratio"] == "4:5" for entry in quotes)
    assert all(entry["canvas_px"] == {"width": 1080, "height": 1350} for entry in quotes)
    assert all(entry["canvas_count"] == 1 for entry in quotes)
    assert all(entry["layout"] == "single_frame_integrated_typography" for entry in quotes)
    assert all(entry["integrated_typography"] is True for entry in quotes)
    assert all(entry["canon_required"] is True for entry in quotes)
    assert all(entry["character"] == "Infenergy" for entry in quotes)
    assert all(entry["image_status"] == "NOT_GENERATED" for entry in quotes)


def test_plan_uses_bundled_verified_catalog_when_runtime_volume_is_empty(tmp_path):
    plan = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )

    assert plan["catalog_size"] > 0
    assert all(
        entry["product_id"] and entry["product_name"]
        for entry in plan["entries"]
        if entry["format"] == "product_micro_mission_comic"
    )


def test_plan_uses_bundled_company_messages_when_runtime_copy_is_stale(tmp_path):
    marketing_dir = tmp_path / "marketing"
    marketing_dir.mkdir()
    (marketing_dir / "infenergy_company_knowledge.json").write_text(
        json.dumps({"knowledge_id": "stale-volume-copy", "thought_library": []}),
        encoding="utf-8",
    )

    plan = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )

    quotes = [
        entry for entry in plan["entries"]
        if entry["format"] == "infenergy_company_quote_visual"
    ]
    assert len(quotes) == 17
    assert all(entry["company_source"]["knowledge_id"] == "infenergy-company-truth" for entry in quotes)


def test_plan_exposes_progressively_adaptive_horizons(tmp_path):
    _write_profiles(tmp_path)

    entries = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )["entries"]

    assert entries[13]["state"] == "LOCKED"
    assert entries[14]["state"] == "SHAPED"
    assert entries[29]["state"] == "SHAPED"
    assert entries[30]["state"] == "ADAPTIVE"
    assert entries[59]["state"] == "ADAPTIVE"
    assert entries[60]["state"] == "DIRECTIONAL"
    assert entries[89]["state"] == "DIRECTIONAL"
    assert entries[90]["state"] == "OPPORTUNITY"


def test_plan_is_driven_by_consumer_psychographics_and_funnel_strategy(tmp_path):
    _write_profiles(tmp_path)

    plan = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )
    entries = plan["entries"]

    assert {entry["audience_id"] for entry in entries} == {
        "caregiver",
        "mobile_professional",
        "outdoor_enthusiast",
        "preparedness_buyer",
        "small_business_operator",
    }
    assert {entry["creative_territory"] for entry in entries} == {
        "Care Without Chaos",
        "Freedom, Designed",
        "Never Miss the Moment",
        "Prepared, Not Precious",
        "Range Is a Feeling",
    }
    assert {stage: sum(entry["funnel_stage"] == stage for entry in entries) for stage in {
        "ATTENTION", "EDUCATION", "DESIRE", "TRUST", "CONVERSION",
    }} == {
        "ATTENTION": 24,
        "EDUCATION": 36,
        "DESIRE": 30,
        "TRUST": 18,
        "CONVERSION": 12,
    }
    assert all(entry["demographic_lens"] for entry in entries)
    assert all(entry["psychographic"] for entry in entries)
    assert all(entry["consumer_desire"] for entry in entries)
    assert all(entry["identity_signal"] for entry in entries)
    assert all(entry["transformation"]["from"] != entry["transformation"]["to"] for entry in entries)
    assert all(entry["brain_movement"] and entry["heart_after"] for entry in entries)
    assert all(entry["primary_platform"] and entry["platform_treatment"] for entry in entries)
    assert not {"Readiness Myth Lab", "One Honest Job", "Saturday Field Test"} & {
        entry["series"] for entry in entries
    }


def test_plan_includes_the_complete_editorial_post_type_taxonomy(tmp_path):
    _write_profiles(tmp_path)

    plan = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )

    expected_types = {
        "infenergy_micro_mission",
        "infenergy_storypage",
        "superhero_text_integration",
        "current_event",
        "product_education",
        "statement",
        "humor",
        "framework",
        "micro_story",
        "explainer",
        "drill",
        "myth",
    }
    assert set(plan["post_type_taxonomy"]) == expected_types
    assert set(plan["post_type_counts"]) == expected_types
    # The taxonomy also advertises the three owner-selectable hero formats.
    # This existing weekly plan assigns its original nine editorial post types.
    optional_hero_types={"infenergy_micro_mission","infenergy_storypage","superhero_text_integration"}
    planned_types=expected_types-optional_hero_types
    assert all(plan["post_type_counts"][kind]>0 for kind in planned_types)
    assert all(plan["post_type_counts"][kind]==0 for kind in optional_hero_types)
    assert {entry["post_type"] for entry in plan["entries"]} == planned_types
    assert all(entry["post_type_label"] for entry in plan["entries"])
    assert all(
        entry["title"].startswith("Myth Check:") and entry["hook"].startswith("Myth:")
        for entry in plan["entries"]
        if entry["post_type"] == "myth"
    )
    assert all(
        "verified, current source" in entry["story"]
        for entry in plan["entries"]
        if entry["post_type"] == "current_event"
    )
    assert all(
        entry["title"].startswith("The 3-Part Framework:")
        for entry in plan["entries"]
        if entry["post_type"] == "framework"
    )
    assert all(
        entry["title"].startswith("60-Second Drill:")
        for entry in plan["entries"]
        if entry["post_type"] == "drill"
    )


def test_plan_matches_specialized_products_to_relevant_arcs(tmp_path):
    marketing = tmp_path / "marketing"
    marketing.mkdir(parents=True)
    product_types = {
        "bike": "electric_bike",
        "fan": "portable_fan",
        "solar": "solar_panel",
        "system": "power_system_bundle",
        "water": "portable_water_filter",
    }
    profiles = {
        product_id: {
            "product_name": product_id.title(),
            "product_type": product_type,
            "market_role": f"verified {product_type} role",
            "personas": [{"name": "Matched customer", "use_case": product_type}],
        }
        for product_id, product_type in product_types.items()
    }
    (marketing / "product_consumer_profiles.json").write_text(
        json.dumps({"profiles": profiles}),
        encoding="utf-8",
    )

    entries = build_120_day_plan(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
    )["entries"]
    arcs_by_product = {product_id: set() for product_id in product_types}
    for entry in entries:
        product = entry.get("product")
        if product:
            arcs_by_product[product["product_id"]].add(entry["weekly_arc"])

    expected_arcs = {
        "bike": "Roadside Plot Twist",
        "fan": "Soft Life, Hard Backup",
        "solar": "Sun Chaser Math",
        "system": "Big Energy, Better Taste",
        "water": "Water Has Main-Character Stakes",
    }
    assert all(arc in arcs_by_product[product_id] for product_id, arc in expected_arcs.items())


def test_os_content_plan_route_is_text_only_and_read_only(tmp_path):
    _write_profiles(tmp_path)

    status, content_type, body = handle(
        "POST",
        "/api/os/content-plan",
        {"start_date": "2026-09-02", "days": 120},
        str(tmp_path),
    )
    result = json.loads(body)

    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert result["status"] == "TEXT_PREVIEW_READY"
    assert result["concept_count"] == 120
    assert result["image_count"] == 0
    assert not (tmp_path / "public_media").exists()
    assert not (tmp_path / "inventory.db").exists()
