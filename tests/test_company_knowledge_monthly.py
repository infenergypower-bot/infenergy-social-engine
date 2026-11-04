from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from agents import carousel_slide_writer  # noqa: E402
from build_monthly_content import CONTENT_PLAN_120, _captions, _gemini_generation_plan, _load_editorial_slate, _load_product_brief, _plan_entry_thought, _render_assets, _weekly_brand_mix_thoughts, build_monthly_calendar, latest_monthly_calendar, prepare_monthly_gemini_prompts  # noqa: E402
from content_plan_120 import build_120_day_plan  # noqa: E402
from company_knowledge import agent_specialization, compact_generation_context, load_company_knowledge  # noqa: E402
from content_operations import daily_status  # noqa: E402
from dispatch_outbox import _refresh_current_news_package, pregenerate_upcoming  # noqa: E402
from social_visuals import _apply_v5_text_overlay  # noqa: E402


def _seed_knowledge(data_dir: Path) -> None:
    marketing = data_dir / "marketing"
    marketing.mkdir(parents=True)
    shutil.copyfile(ROOT / "data" / "marketing" / "infenergy_company_knowledge.json", marketing / "infenergy_company_knowledge.json")


def test_company_knowledge_has_complete_generation_contract():
    knowledge = load_company_knowledge(str(ROOT / "data"))
    context = compact_generation_context(knowledge)

    assert knowledge["brand"]["mission"]
    assert knowledge["brand"]["vision"]
    assert knowledge["consumer_benefits"]["lifestyle_transformations"]
    assert len(knowledge["thought_library"]) >= 30
    assert len(knowledge["agent_specializations"]) >= 13
    assert context["central_human_truth"]
    assert context["product_truth_policy"]["never_claim"]


def test_editorial_playbook_reaches_every_shared_agent_context():
    knowledge = load_company_knowledge(str(ROOT / "data"))
    playbook = knowledge["editorial_playbook"]

    assert compact_generation_context(knowledge)["editorial_playbook"] == playbook
    for agent_name in knowledge["agent_specializations"]:
        assert agent_specialization(knowledge, agent_name)["editorial_playbook"] == playbook


def test_elite_monthly_slate_meets_the_editorial_variety_contract():
    thoughts = _load_editorial_slate()["posts"]
    captions = [_captions(thought) for thought in thoughts]
    required_fields = {
        "editorial_mode", "content_type", "statement", "overlay_text", "expansion",
        "useful_detail", "action", "prompt", "linkedin_lens", "image_scene", "visual_execution",
    }

    assert len(thoughts) == 30
    assert all(required_fields <= thought.keys() for thought in thoughts)
    assert len({thought["editorial_mode"] for thought in thoughts}) >= 25
    assert sum(bool(thought.get("humor")) for thought in thoughts) == 4
    assert len({thought["statement"] for thought in thoughts}) == 30
    assert len({thought["action"] for thought in thoughts}) == 30
    assert len({thought["image_scene"] for thought in thoughts}) == 30
    for platform in ("facebook", "instagram", "linkedin"):
        assert len({caption[platform] for caption in captions}) == 30
    assert all(len({caption[platform] for platform in ("facebook", "instagram", "linkedin")}) == 3 for caption in captions)

    carousels = [thought for thought in thoughts if thought["format"] == "carousel"]
    assert len(carousels) == 9
    assert all(len(thought.get("slides", [])) == 4 for thought in carousels)
    assert all(len({slide["headline"] for slide in thought["slides"]}) == 4 for thought in carousels)


def test_elite_slate_starts_with_sourced_indiana_series_and_has_weekly_products():
    posts = _load_editorial_slate()["posts"]

    assert all(post.get("event_series") == "indiana-outage-2026-08" for post in posts[:7])
    assert all(post.get("source_note") for post in posts[:7])
    assert not any(post.get("humor") for post in posts[:7])
    assert sum(post["content_type"] == "product" for post in posts) == 8
    assert [sum(post["content_type"] == "product" for post in posts[index:index + 7]) for index in range(0, 28, 7)] == [2, 2, 2, 2]
    assert sum(post["visual_execution"] == "statement_graphic" for post in posts) == 8


def test_every_elite_prompt_contains_its_authored_scene_and_product_reference_rule():
    posts = _load_editorial_slate()["posts"]

    for post in posts:
        product_id = str(post.get("product_id") or "")
        product = _load_product_brief(str(ROOT / "data"), product_id) if product_id else None
        plan = _gemini_generation_plan(post, product)
        assert all(post["image_scene"] in prompt["gemini_image_prompt"] for prompt in plan["prompts"])
        assert all(prompt["v5_direction"]["gemini_rendered_text"] for prompt in plan["prompts"])
        if product:
            assert all(product["name"] in prompt["gemini_image_prompt"] for prompt in plan["prompts"])
            assert all("attached reference image" in prompt["gemini_image_prompt"] for prompt in plan["prompts"])


def test_company_knowledge_falls_back_to_packaged_contract(tmp_path):
    knowledge = load_company_knowledge(str(tmp_path / "empty-persistent-volume"))

    assert knowledge["knowledge_id"] == "infenergy-company-truth"
    assert len(knowledge["thought_library"]) >= 30


def test_thought_carousel_advances_one_idea_across_four_slides(tmp_path):
    payload = carousel_slide_writer.run(
        str(tmp_path),
        thought={
            "statement": "Preparedness over panic.",
            "expansion": "A clear plan creates room to think.",
            "prompt": "What will you protect first?",
        },
    )

    assert payload["content_mode"] == "company_thought"
    assert [slide["slide_role"] for slide in payload["slides"]] == [
        "thought",
        "meaning",
        "practical_application",
        "community_question",
    ]


def test_month_builder_persists_ready_assets_and_is_idempotent(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")

    first = build_monthly_calendar(
        data_dir=str(tmp_path),
        start_date="2026-09-01",
        days=2,
        enqueue=True,
    )

    assert first["status"] == "READY"
    assert first["queued"] == 2
    assert first["single_image_posts"] == 1
    assert first["carousel_posts"] == 1
    assert os.path.exists(first["calendar_path"])
    assert daily_status(str(tmp_path), "2026-09-01")["ready"] == 1
    assert daily_status(str(tmp_path), "2026-09-02")["ready"] == 1

    single = first["entries"][0]["package"]
    carousel = first["entries"][1]["package"]
    assert os.path.exists(single["generated_visuals"]["linkedin"])
    assert single["primary_publish_image_url"].startswith("https://example.test/media/")
    assert len(carousel["carousel_assets"]) == 6
    assert [asset["role"] for asset in carousel["carousel_assets"]] == ["COVER", "STORY", "STORY", "STORY", "STORY", "FINALE"]
    assert carousel["carousel_assets"][-1]["logo_url"].startswith("https://infenergypower.com/")
    assert all(os.path.exists(asset["local_path"]) for asset in carousel["carousel_assets"])
    assert carousel["platform_posts"]["linkedin"]["final_caption_qa"]["status"] == "PRESENTATION_READY"
    assert single["gemini_generation"]["required_image_count"] == 1
    assert carousel["gemini_generation"]["required_image_count"] == 6
    assert not carousel["gemini_generation"]["fallback_allowed"]
    prompts = carousel["gemini_generation"]["prompts"]
    assert len({prompt["prompt_sha256"] for prompt in prompts}) == 6
    assert prompts[0]["v5_direction"]["semantic_role"] == "COVER"
    assert prompts[-1]["v5_direction"]["semantic_role"] == "FINALE"
    assert prompts[-1]["v5_direction"]["official_logo_path"].endswith("infenergy_official_logo.png")
    assert all("Do not render any words" in prompt["gemini_image_prompt"] for prompt in prompts)
    assert all(prompt["v5_direction"]["text_overlay"] == {"enabled": False} for prompt in prompts)

    second = build_monthly_calendar(
        data_dir=str(tmp_path),
        start_date="2026-09-01",
        days=2,
        enqueue=True,
    )
    assert second["queued"] == 0
    assert second["skipped_existing"] == 2

    saved = json.loads(Path(first["calendar_path"]).read_text(encoding="utf-8"))
    assert saved["knowledge_digest"] == first["knowledge_digest"]
    assert len(saved["entries"]) == 2
    assert latest_monthly_calendar(str(tmp_path))["entries"][1]["thought_id"] == "E02"


def test_weekly_brand_mix_compiles_120_days_with_exact_weekly_roles(monkeypatch):
    monkeypatch.setattr("build_monthly_content._load_current_news", lambda limit: [
        {"title": f"Current power story {index}", "url": f"https://news.example/{index}", "published": "today"}
        for index in range(limit)
    ])
    monkeypatch.setattr("build_monthly_content._load_locked_canon_references", lambda: ["https://studio.example/api/assets/canon"])

    thoughts = _weekly_brand_mix_thoughts(_load_editorial_slate(), start=__import__("datetime").date(2026, 9, 1), days=120)

    assert len(thoughts) == 120
    expected_roles = ["product", "product", "product", "current_news", "superhero_quote", "micro_mission", "historical_mission"]
    for offset in range(0, 119, 7):
        assert [thought["weekly_role"] for thought in thoughts[offset:offset + 7]] == expected_roles
    mission = thoughts[5]
    assert len(mission["slides"]) == 9
    assert len(_gemini_generation_plan(mission)["prompts"]) == 10
    assert mission["reference_image_urls"] == ["https://studio.example/api/assets/canon"]
    assert thoughts[4]["canon_required"] is True
    assert thoughts[6]["source_note"].startswith("https://")


def test_120_day_entries_translate_to_runtime_generation_contracts():
    plan = build_120_day_plan(data_dir=str(ROOT / "data"), start_date="2026-09-02")
    tuesday = next(entry for entry in plan["entries"] if entry["format"] == "product_comic_strip_carousel")
    friday = next(entry for entry in plan["entries"] if entry["format"] == "product_story_page")
    sunday = next(entry for entry in plan["entries"] if entry["format"] == "infenergy_company_quote_visual")

    comic = _plan_entry_thought(tuesday)
    story = _plan_entry_thought(friday)
    message = _plan_entry_thought(sunday)

    assert comic["format"] == "carousel"
    assert comic["product_id"] == tuesday["product_id"]
    assert comic["editorial_mode"] == tuesday["entertainment_mode"]
    assert comic["generation_contract"]["product_integration"]["required"] is True
    assert [slide["headline"] for slide in comic["slides"]] == tuesday["story_sequence"]
    assert story["format"] == "single"
    assert story["weekly_role"] == "story_page"
    assert message["format"] == "single"
    assert message["statement"] == sunday["exact_visible_text"][0]
    assert message["expansion"] == sunday["support_statement"]
    assert message["generation_contract"]["verbatim_company_quote"] is True

    comic_generation = _gemini_generation_plan(comic, _load_product_brief(str(ROOT / "data"), comic["product_id"]))
    story_generation = _gemini_generation_plan(story, _load_product_brief(str(ROOT / "data"), story["product_id"]))
    message_generation = _gemini_generation_plan(message)
    assert comic_generation["required_image_count"] == 3
    assert comic_generation["aspect_ratio"] == "4:5"
    assert "sequential swipeable comic strip carousel" in comic_generation["prompts"][0]["gemini_image_prompt"]
    assert tuesday["entertainment_mode"] in comic_generation["prompts"][0]["gemini_image_prompt"]
    assert "square editorial" not in comic_generation["prompts"][0]["gemini_image_prompt"]
    assert "finished 1:1" not in comic_generation["prompts"][0]["gemini_image_prompt"]
    assert comic_generation["prompts"][0]["v5_direction"]["gemini_rendered_text"] == [line for line in [comic["slides"][0]["headline"], comic["slides"][0]["supporting"]] if line]
    assert story_generation["prompts"][0]["v5_direction"]["gemini_rendered_text"] == list(friday["visible_text"].values())
    assert story_generation["required_image_count"] == 1
    assert story_generation["aspect_ratio"] == "9:16"
    assert "Story page containing exactly THREE" in story_generation["prompts"][0]["gemini_image_prompt"]
    assert message_generation["aspect_ratio"] == "4:5"
    assert sunday["exact_visible_text"][0] in message_generation["prompts"][0]["v5_direction"]["gemini_rendered_text"]


def test_product_story_page_renders_three_designed_panels(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setattr("build_monthly_content._load_product_reference", lambda _url: Image.new("RGBA", (200, 200), "#ffffff"))
    thought = {
        "id": "PLAN120-D001",
        "pillar": "Freedom, Designed",
        "format": "single",
        "product_id": "03534566804e",
        "statement": "At 84%, you have plans. At 3%, every outlet has excellent real estate.",
        "expansion": "A clean mobile-power ritual protects momentum.",
        "consumer_receipt": {"who": "rec-league organizer", "where": "community gym", "doing": "recording scores"},
        "generation_contract": {
            "format": "product_story_page",
            "canvas_px": {"width": 1080, "height": 1920},
            "product_name": "PowerCharge Pro - White",
            "visible_text": {
                "headline": "At 84%, you have plans. At 3%, every outlet has excellent real estate.",
                "infenergy_line": "Pause. What job does the power actually need to do?",
                "resolution_line": "A clean mobile-power ritual protects momentum better than last-minute outlet hunting.",
            },
        },
    }

    assets = _render_assets(str(tmp_path), thought, 0)

    with Image.open(assets["primary"]["local_path"]) as rendered:
        assert rendered.size == (1080, 1920)
        assert len({rendered.getpixel((100, y)) for y in (240, 840, 1440)}) == 3
    assert len(assets["slides"]) == 1
    assert assets["review_issues"] == []


def test_product_story_page_caption_is_concise_and_non_repetitive():
    thought = {
        "statement": "At 84%, you have plans. At 3%, every outlet has excellent real estate.",
        "expansion": "Test the entire chain.",
        "useful_detail": "Keep essential tasks accessible.",
        "action": "Compare capacity, ports, and charging speed with the devices you carry.",
        "prompt": "Explore",
        "pillar": "Freedom, Designed",
        "hashtags": ["Infenergy", "PowerReadiness", "PracticalPower"],
        "consumer_receipt": {"who": "rec-league organizer", "where": "community gym", "doing": "recording scores"},
        "generation_contract": {
            "format": "product_story_page",
            "visible_text": {
                "headline": "At 84%, you have plans. At 3%, every outlet has excellent real estate.",
                "infenergy_line": "Pause. What job does the power actually need to do?",
                "resolution_line": "A clean mobile-power ritual protects momentum better than last-minute outlet hunting.",
            },
        },
    }

    captions = _captions(thought)

    assert captions["facebook"].count("At 84%") == 1
    assert "The friction:" not in captions["facebook"]
    assert "What matters next:" not in captions["facebook"]
    assert len(re.findall(r"(?<!\w)#[A-Za-z0-9_]+", captions["facebook"])) == 5


def test_120_day_plan_queues_existing_runtime_packages(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")

    calendar = build_monthly_calendar(
        data_dir=str(tmp_path),
        start_date="2026-09-02",
        days=7,
        enqueue=True,
        content_plan=CONTENT_PLAN_120,
    )

    assert calendar["queued"] == 7
    assert calendar["coverage_days"] == 7
    tuesday = next(entry for entry in calendar["entries"] if entry["package"]["generation_contract"].get("format") == "product_comic_strip_carousel")
    friday = next(entry for entry in calendar["entries"] if entry["package"]["generation_contract"].get("format") == "product_story_page")
    sunday = next(entry for entry in calendar["entries"] if entry["package"]["generation_contract"].get("format") == "infenergy_company_quote_visual")
    assert tuesday["package"]["gemini_generation"]["aspect_ratio"] == "4:5"
    assert tuesday["package"]["gemini_generation"]["required_image_count"] == 3
    assert friday["package"]["gemini_generation"]["aspect_ratio"] == "9:16"
    assert friday["package"]["generation_contract"]["product_integration"]["required"] is True
    assert sunday["package"]["thought_statement"] == sunday["package"]["generation_contract"]["exact_visible_text"][0]
    assert all(not entry["package"]["generated_visuals"] for entry in calendar["entries"])
    assert all(entry["package"]["copy_form"] == entry["package"]["generation_contract"]["copy_form"] for entry in calendar["entries"])
    assert all(entry["package"]["editorial_pillar"] == entry["package"]["generation_contract"]["editorial_pillar"] for entry in calendar["entries"])
    assert all(entry["package"]["copy_generation_source"] == "pending_strict_gemini" for entry in calendar["entries"])
    assert all(entry["package"]["fb_caption"] == "" for entry in calendar["entries"])
    assert all(entry["package"]["ig_caption"] == "" for entry in calendar["entries"])
    assert all(entry["package"]["li_text"] == "" for entry in calendar["entries"])
    assert all(
        post["final_caption_qa"]["status"] == "PENDING_GEMINI"
        for entry in calendar["entries"]
        for post in entry["package"]["platform_posts"].values()
    )
    assert all(
        label not in entry["package"]["fb_caption"]
        for entry in calendar["entries"]
        for label in ("Try this:", "Save this move:", "Practical next step:")
    )
    assert not (tmp_path / "public_media").exists()


def test_weekly_brand_mix_fails_closed_without_live_news_or_locked_canon(monkeypatch):
    monkeypatch.setattr("build_monthly_content._load_current_news", lambda limit: [])
    monkeypatch.setattr("build_monthly_content._load_locked_canon_references", lambda: ["https://studio.example/api/assets/canon"])
    with __import__("pytest").raises(RuntimeError, match="current_news_coverage_incomplete"):
        _weekly_brand_mix_thoughts(_load_editorial_slate(), start=__import__("datetime").date(2026, 9, 1), days=7)

    monkeypatch.setattr("build_monthly_content._load_current_news", lambda limit: [
        {"title": "Current story", "url": "https://news.example/current", "published": "today"},
    ])
    monkeypatch.setattr("build_monthly_content._load_locked_canon_references", lambda: [])
    with __import__("pytest").raises(RuntimeError, match="locked_infenergy_canon_unavailable"):
        _weekly_brand_mix_thoughts(_load_editorial_slate(), start=__import__("datetime").date(2026, 9, 1), days=7)


def test_replacement_validates_new_packages_before_canceling_inventory(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    cancellations = []
    monkeypatch.setattr("build_monthly_content._package", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("invalid_new_package")))
    monkeypatch.setattr("build_monthly_content.cancel_unpublished_inventory", lambda data_dir: cancellations.append(data_dir) or {"cancelled_outbox": 1})

    with __import__("pytest").raises(RuntimeError, match="invalid_new_package"):
        build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-09-01", days=1, enqueue=True, replace_unpublished=True)

    assert cancellations == []


def test_due_news_refresh_rebuilds_source_captions_and_prompts(monkeypatch):
    thought = {
        "id": "NEWS", "pillar": "outage_readiness", "kind": "current_event", "content_type": "current_event",
        "format": "single", "statement": "Old headline", "overlay_text": "Old headline", "expansion": "Explain the practical consequence.",
        "useful_detail": "Use verified context.", "action": "Make one plan.", "prompt": "What changes?", "linkedin_lens": "Operational relevance.",
        "instagram_hook": "Old headline", "hashtags": ["Infenergy"], "visual_motif": "A documentary outage scene", "image_scene": "A documentary outage scene",
        "visual_execution": "editorial_scene", "source_note": "https://old.example", "weekly_role": "current_news",
    }
    package = {
        "content_date": "2026-09-01", "weekly_role": "current_news", "generation_thought": thought,
        "platform_posts": {platform: {"caption": "old", "final_caption": "old"} for platform in ("facebook", "instagram", "linkedin")},
    }
    monkeypatch.setattr("dispatch_outbox._load_current_news", lambda limit: [
        {"title": "Fresh verified headline", "url": "https://news.example/fresh", "published": "today"},
    ])

    refreshed = _refresh_current_news_package(package)

    assert refreshed["thought_statement"] == "Fresh verified headline"
    assert refreshed["editorial_sources"] == ["https://news.example/fresh"]
    assert refreshed["news_freshness"]["status"] == "REFRESHED"
    assert "Fresh verified headline" in refreshed["platform_posts"]["instagram"]["final_caption"]
    assert "Fresh verified headline" in refreshed["gemini_generation"]["prompts"][0]["gemini_image_prompt"]


def test_future_news_waits_for_the_24_hour_freshness_window(monkeypatch, tmp_path):
    future = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(days=2)).isoformat()
    monkeypatch.setattr("dispatch_outbox.upcoming_ready_packages", lambda *args, **kwargs: [{
        "outbox_id": "news-1", "scheduled_at": future,
        "package": {"weekly_role": "current_news", "gemini_generation": {"strict_provider": True, "required_image_count": 1}},
    }])
    monkeypatch.setattr("dispatch_outbox._refresh_current_news_package", lambda package: __import__("pytest").fail("news refreshed too early"))

    result = pregenerate_upcoming(data_dir=str(tmp_path))

    assert result["status"] == "IDLE"


def test_weekly_brand_mix_survives_real_queue_and_prompt_preparation(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setattr("build_monthly_content._load_current_news", lambda limit: [
        {"title": "A current power story", "url": "https://news.example/current", "published": "today"},
    ])
    monkeypatch.setattr("build_monthly_content._load_locked_canon_references", lambda: ["https://studio.example/api/assets/canon"])

    calendar = build_monthly_calendar(
        data_dir=str(tmp_path), start_date="2026-09-01", days=7, enqueue=True, content_plan="weekly_brand_mix",
    )
    prepared = prepare_monthly_gemini_prompts(str(tmp_path))

    assert calendar["queued"] == 7
    assert calendar["coverage_days"] == 7
    assert calendar["product_posts"] == 3
    assert calendar["current_event_posts"] == 1
    assert calendar["superhero_posts"] == 1
    assert calendar["micro_mission_posts"] == 1
    assert calendar["historical_mission_posts"] == 1
    assert prepared["prepared_entries"] == 7
    assert prepared["prepared_prompts"] == 16
    saved = latest_monthly_calendar(str(tmp_path))
    assert len(saved["entries"][5]["package"]["carousel_assets"]) == 10
    assert saved["entries"][4]["package"]["gemini_generation"]["reference_image_urls"] == ["https://studio.example/api/assets/canon"]


def test_month_builder_replaces_an_unpublished_slot_package(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    first = build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-10-01", days=1, enqueue=True)

    knowledge_path = tmp_path / "marketing" / "infenergy_company_knowledge.json"
    knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
    knowledge["schema_version"] = "1.0.1"
    knowledge_path.write_text(json.dumps(knowledge), encoding="utf-8")
    replacement = build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-10-01", days=1, enqueue=True)

    assert first["entries"][0]["content_id"] != replacement["entries"][0]["content_id"]
    assert replacement["queued"] == 1
    assert daily_status(str(tmp_path), "2026-10-01")["ready"] == 1


def test_month_builder_can_cancel_unpublished_legacy_inventory(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-10-01", days=1, enqueue=True)

    knowledge_path = tmp_path / "marketing" / "infenergy_company_knowledge.json"
    stale_knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
    stale_knowledge["schema_version"] = "stale-persistent-copy"
    knowledge_path.write_text(json.dumps(stale_knowledge), encoding="utf-8")

    replacement = build_monthly_calendar(
        data_dir=str(tmp_path),
        start_date="2026-11-01",
        days=1,
        enqueue=True,
        replace_unpublished=True,
    )

    assert replacement["cancelled_legacy_outbox"] == 1
    assert replacement["knowledge_version"] == "3.0.0"
    assert replacement["knowledge_refresh"]["status"] == "REFRESHED_FROM_PACKAGED"
    assert Path(replacement["knowledge_refresh"]["backup_path"]).exists()
    assert json.loads(Path(replacement["knowledge_refresh"]["backup_path"]).read_text(encoding="utf-8"))["schema_version"] == "stale-persistent-copy"
    assert daily_status(str(tmp_path), "2026-10-01")["ready"] == 0
    assert daily_status(str(tmp_path), "2026-11-01")["ready"] == 1


def test_month_builder_requeues_cancelled_content_ids(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    first = build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-11-01", days=2, enqueue=True)

    replacement = build_monthly_calendar(
        data_dir=str(tmp_path),
        start_date="2026-11-01",
        days=2,
        enqueue=True,
        replace_unpublished=True,
    )

    assert first["queued"] == 2
    assert replacement["cancelled_legacy_outbox"] == 2
    assert replacement["queued"] == 2
    assert replacement["skipped_existing"] == 0
    assert daily_status(str(tmp_path), "2026-11-01")["ready"] == 1
    assert daily_status(str(tmp_path), "2026-11-02")["ready"] == 1


def test_existing_month_can_be_prepared_for_strict_gemini_without_rebuilding(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    original = build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-12-01", days=2, enqueue=True)

    result = prepare_monthly_gemini_prompts(str(tmp_path))
    saved = latest_monthly_calendar(str(tmp_path))

    assert result["prepared_entries"] == 2
    assert result["prepared_prompts"] == 7
    assert saved["gemini_prompt_status"] == "READY"
    assert saved["entries"][0]["outbox_id"] == original["entries"][0]["outbox_id"]
    assert saved["entries"][0]["package"]["gemini_generation"]["status"] == "PROMPTS_READY"


def test_prompt_preparation_ignores_preserved_entries_without_outbox_ids(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-12-01", days=1, enqueue=True)
    mixed = build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-12-01", days=2, enqueue=True)

    result = prepare_monthly_gemini_prompts(str(tmp_path))

    assert mixed["queued"] == 1
    assert mixed["skipped_existing"] == 1
    assert result["prepared_entries"] == 1
    assert result["prepared_prompts"] == 6


def test_month_builder_queues_a_duplicate_concept_when_the_requested_day_is_unplanned(tmp_path, monkeypatch):
    _seed_knowledge(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    preview = build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-12-10", days=1, enqueue=False)
    monkeypatch.setattr("build_monthly_content._existing_content_ids", lambda _data_dir: {preview["entries"][0]["content_id"]})

    calendar = build_monthly_calendar(data_dir=str(tmp_path), start_date="2026-12-10", days=1, enqueue=True)

    assert calendar["queued"] == 1
    assert calendar["coverage_days"] == 1
    assert daily_status(str(tmp_path), "2026-12-10")["ready"] == 1


def test_all_monthly_gemini_prompts_are_unique_and_contain_their_exact_text():
    from PIL import Image

    thoughts = _load_editorial_slate()["posts"]
    prompts = [
        prompt
        for thought in thoughts
        for prompt in _gemini_generation_plan(
            thought,
            _load_product_brief(str(ROOT / "data"), thought["product_id"]) if thought.get("product_id") else None,
        )["prompts"]
    ]

    assert len(prompts) == 75
    assert len({prompt["prompt_sha256"] for prompt in prompts}) == 75
    for prompt in prompts:
        direction = prompt["v5_direction"]
        assert direction["text_overlay"] == {"enabled": False}
        assert all(json.dumps(line, ensure_ascii=False)[1:-1] in prompt["gemini_image_prompt"] for line in direction["gemini_rendered_text"])
        assert "for an editorial overlay" not in prompt["gemini_image_prompt"]
