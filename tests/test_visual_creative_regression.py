from __future__ import annotations

import os
import io
import sys
from pathlib import Path
from unittest.mock import patch
import sys
import types
import google

from PIL import Image

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

import social_visuals  # noqa: E402
import run_engine  # noqa: E402
from social.visual_provider import EntertainmentStudioVisualProvider, default_provider  # noqa: E402


def test_v5_text_overlay_supports_bounded_headline_scale():
    image = Image.new("RGB", (1080, 1080), "#111820")

    rendered, error = social_visuals._apply_v5_text_overlay(image, {
        "text_overlay": {
            "enabled": True,
            "text": "THE MOMENT YOU FELT PORTABLE.",
            "headline_scale": 0.046,
            "safe_margin_ratio": 0.028,
        }
    })

    assert error == ""
    assert rendered.size == (1080, 1080)
    assert rendered.getpixel((1079, 0)) == (17, 24, 32)


def test_entertainment_studio_provider_accepts_shared_service_token(monkeypatch):
    monkeypatch.setenv("ENTERTAINMENT_STUDIO_URL", "https://studio.example")
    monkeypatch.delenv("ENTERTAINMENT_STUDIO_TOKEN", raising=False)
    monkeypatch.setenv("SOCIAL_ENGINE_TOKEN", "shared-token")

    provider = default_provider()

    assert isinstance(provider, EntertainmentStudioVisualProvider)
    assert provider.token == "shared-token"

    monkeypatch.setenv("ENTERTAINMENT_STUDIO_TOKEN", "caller-token")

    assert default_provider().token == "caller-token"


def test_every_gemini_prompt_path_enforces_infenergy_originality_canon():
    prompt = social_visuals._build_gemini_image_prompt(
        {"post_id": "originality-policy"},
        "instagram",
        {"v5_direction": {"route": "character"}, "gemini_image_prompt": "Create a cinematic Infenergy scene."},
    )

    assert "never an imitation of Batman or any existing superhero" in prompt
    assert "never place the Infenergy logo" in prompt
    assert "sky, clouds, moon" in prompt
    assert "physically attached, canon-accurate suit detail" in prompt


def test_failed_gemini_variants_never_publish_a_reference_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(social_visuals, "VISUAL_DIR", str(tmp_path))

    def fake_generate(content, platform, plan, file_path):
        if platform == "facebook":
            Image.new("RGB", (1200, 1200), "#50646f").save(file_path)
            return True, "", {"generation_status": "success", "artifact_path": file_path}
        return False, "semantic_quality_rejected:gibberish_or_garbled_text", {"generation_status": "failed"}

    monkeypatch.setattr(social_visuals, "_generate_gemini_full_creative", fake_generate)
    monkeypatch.setattr(social_visuals, "_load_visual_repo_context", lambda: {"references": [], "settings": {}})
    monkeypatch.setattr(social_visuals, "_resolve_product_source", lambda *args, **kwargs: "https://example.com/reference.jpg")

    visuals = social_visuals.generate_visuals({"post_id": "winner", "product_id": "CAMP-FAN-12K"}, {})

    assert visuals["render_engine"] == "mixed"
    assert Path(visuals["facebook"]).is_file()
    assert Path(visuals["instagram"]).is_file()
    assert "linkedin" not in visuals
    assert visuals["render_engines"] == {
        "facebook": "gemini",
        "instagram": "gemini_shared_square",
        "linkedin": "failed",
    }
    assert all(
        generation.get("fallback_source") != "approved_product_photo"
        for generation in visuals["visual_generation"].values()
    )


def test_final_creative_budget_is_exactly_one_image_call_per_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(social_visuals, "VISUAL_DIR", str(tmp_path))

    def fake_generate(content, platform, plan, file_path):
        Image.new("RGB", social_visuals._platform_visual_spec(platform)["target"], "#50646f").save(file_path)
        return True, "", {
            "generation_status": "success",
            "artifact_path": file_path,
            "image_provider_call_count": 1,
        }

    monkeypatch.setattr(social_visuals, "_generate_gemini_full_creative", fake_generate)
    monkeypatch.setattr(social_visuals, "_load_visual_repo_context", lambda: {"references": [], "settings": {}})
    monkeypatch.setattr(social_visuals, "_resolve_product_source", lambda *args, **kwargs: "")

    visuals = social_visuals.generate_visuals({"post_id": "budget"}, {})

    assert visuals["image_provider_call_count"] == 2
    assert visuals["image_provider_call_budget"] == 2
    assert visuals["visual_generation"]["facebook"]["image_provider_call_count"] == 1
    assert visuals["visual_generation"]["instagram"]["image_provider_call_count"] == 0
    assert visuals["visual_generation"]["linkedin"]["image_provider_call_count"] == 1


def _image(path, size=(1080, 1080)):
    image = Image.new("RGB", size, "#d9dde2")
    for x in range(size[0] // 3, (size[0] * 2) // 3):
        for y in range(size[1] // 3, (size[1] * 2) // 3):
            image.putpixel((x, y), (50, 66, 74))
    image.save(path)


def test_powerpulse_raw_fallback_is_packshot_only_without_explicit_route(tmp_path):
    artifact = tmp_path / "powerpulse.png"
    _image(artifact, social_visuals._platform_visual_spec("instagram")["target"])
    review = social_visuals._fallback_creative_review(
        {"product_id": "PPP-200", "product_name": "PowerPulse Pro 200"},
        {"creative_route": "PRODUCT_IN_CONTEXT"},
        str(artifact),
        "instagram",
    )

    assert review["creative_classification"] == "PACKSHOT_ONLY"
    assert review["verdict"] == "REGENERATE_VISUAL"
    assert review["recovery_action"] == "CHANGE_CREATIVE_ROUTE"


def test_packshot_can_only_pass_when_creative_director_explicitly_selects_it(tmp_path):
    artifact = tmp_path / "hero.png"
    _image(artifact, social_visuals._platform_visual_spec("facebook")["target"])
    review = social_visuals._fallback_creative_review(
        {"product_id": "PPP-200"},
        {"creative_route": "PREMIUM_PRODUCT_HERO"},
        str(artifact),
        "facebook",
    )

    assert review["creative_classification"] == "EXPLICIT_PRODUCT_HERO"
    assert review["verdict"] == "PASS"


def test_product_photo_fallback_requires_identity_approval_and_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(social_visuals, "VISUAL_DIR", str(tmp_path))
    monkeypatch.setattr(
        social_visuals,
        "_generate_gemini_full_creative",
        lambda *_args, **_kwargs: (False, "provider_unavailable", {"generation_status": "failed"}),
    )
    monkeypatch.setattr(social_visuals, "_load_visual_repo_context", lambda: {"references": [], "settings": {}})
    monkeypatch.setattr(social_visuals, "_resolve_product_source", lambda *args, **kwargs: "https://example.com/wrong-brand.jpg")
    monkeypatch.setattr(
        social_visuals,
        "_save_product_photo_fallback",
        lambda _source, path, platform: (_image(path, social_visuals._platform_visual_spec(platform)["target"]) is None),
    )

    visuals = social_visuals.generate_visuals(
        {
            "post_id": "wrong-brand",
            "product_id": "PPP-200",
            "product_image_url": "https://example.com/wrong-brand.jpg",
        },
        {"creative_route": "PREMIUM_PRODUCT_HERO"},
    )

    assert all(visuals["render_engines"][platform] == "failed" for platform in ("facebook", "instagram", "linkedin"))
    assert all(platform not in visuals for platform in ("facebook", "instagram", "linkedin"))
    assert all(
        "product_image_identity_not_approved" in visuals["artifact_reviews"][platform]["issues"]
        for platform in ("facebook", "instagram", "linkedin")
    )


def test_unapproved_product_reference_never_reaches_gemini(tmp_path, monkeypatch):
    calls = {"count": 0}

    class Models:
        def generate_content(self, **_kwargs):
            calls["count"] += 1
            raise AssertionError("unapproved product reference reached Gemini")

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = lambda **_kwargs: types.SimpleNamespace(models=Models())
    fake_types = types.ModuleType("google.genai.types")
    fake_types.HttpOptions = lambda **kwargs: kwargs
    fake_types.HttpRetryOptions = lambda **kwargs: kwargs
    fake_types.Part = types.SimpleNamespace(from_bytes=lambda **kwargs: kwargs)
    fake_genai.types = fake_types
    monkeypatch.setattr(google, "genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(social_visuals, "_GEMINI_IMAGE_UNAVAILABLE_REASON", "")
    monkeypatch.setattr(social_visuals, "_load_visual_repo_context", lambda: {"references": [], "settings": {}})
    monkeypatch.setattr(social_visuals, "_read_image_bytes_any", lambda _source: (b"wrong-brand", "image/png"))

    rendered, reason, metadata = social_visuals._generate_gemini_full_creative(
        {
            "post_id": "wrong-reference",
            "product_id": "PPP-200",
            "product_image_url": "https://example.com/wrong-brand.png",
        },
        "facebook",
        {},
        str(tmp_path / "wrong-reference.png"),
    )

    assert rendered is False
    assert reason == "product_reference_identity_not_approved"
    assert calls["count"] == 0
    assert metadata["product_reference_identity_review"]["identity_approved"] is False


def test_local_character_reference_reaches_gemini(tmp_path, monkeypatch):
    reference_path = tmp_path / "infenergy-canon.png"
    _image(reference_path, (512, 512))
    output_path = tmp_path / "story.png"
    image_buffer = io.BytesIO()
    Image.new("RGB", (1080, 1920), "#d8e0e4").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
    calls = []
    preflight_calls = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs["contents"])
            inline_data = types.SimpleNamespace(data=image_bytes)
            part = types.SimpleNamespace(inline_data=inline_data)
            return types.SimpleNamespace(candidates=[types.SimpleNamespace(content=types.SimpleNamespace(parts=[part]))])

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = lambda **_kwargs: types.SimpleNamespace(models=Models())
    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types.ImageConfig = lambda **kwargs: kwargs
    fake_types.HttpOptions = lambda **kwargs: kwargs
    fake_types.HttpRetryOptions = lambda **kwargs: kwargs
    fake_types.Part = types.SimpleNamespace(from_bytes=lambda **kwargs: kwargs)
    fake_genai.types = fake_types
    monkeypatch.setattr(google, "genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_IMAGE_REPAIR_ATTEMPTS", "1")
    monkeypatch.setattr(social_visuals, "_GEMINI_IMAGE_UNAVAILABLE_REASON", "")
    monkeypatch.setattr(social_visuals, "_load_visual_repo_context", lambda: {"references": [], "settings": {}})
    monkeypatch.setattr(social_visuals, "_gemini_plate_quality", lambda *_args: (True, []))
    monkeypatch.setattr(social_visuals, "_gemini_semantic_plate_quality", lambda *_args: (True, []))
    monkeypatch.setattr(social_visuals, "preflight_gemini_workflow", lambda **kwargs: preflight_calls.append(kwargs))

    rendered, reason, metadata = social_visuals._generate_gemini_full_creative(
        {"post_id": "micro-mission", "reference_image_urls": [str(reference_path)]},
        "iis_reel_cover",
        {"v5_direction": {"format": "story"}, "gemini_image_prompt": "Create one superhero story."},
        str(output_path),
    )

    assert rendered is True
    assert reason == "ok"
    assert metadata["content_reference_count"] == 1
    assert preflight_calls == [{"image_calls": 2, "reasoning_calls": 2}]
    assert any(isinstance(part, dict) and part.get("data") for part in calls[0][1:])


def test_gemini_repairs_are_limited_by_remaining_budget(tmp_path, monkeypatch):
    output_path = tmp_path / "budgeted.png"
    image_buffer = io.BytesIO()
    Image.new("RGB", (1080, 1350), "#d8e0e4").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
    preflight_calls = []

    class Models:
        def generate_content(self, **_kwargs):
            inline_data = types.SimpleNamespace(data=image_bytes)
            part = types.SimpleNamespace(inline_data=inline_data)
            return types.SimpleNamespace(candidates=[types.SimpleNamespace(content=types.SimpleNamespace(parts=[part]))])

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = lambda **_kwargs: types.SimpleNamespace(models=Models())
    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types.ImageConfig = lambda **kwargs: kwargs
    fake_types.HttpOptions = lambda **kwargs: kwargs
    fake_types.HttpRetryOptions = lambda **kwargs: kwargs
    fake_types.Part = types.SimpleNamespace(from_bytes=lambda **kwargs: kwargs)
    fake_genai.types = fake_types
    monkeypatch.setattr(google, "genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_IMAGE_REPAIR_ATTEMPTS", "2")
    monkeypatch.setattr(social_visuals, "_GEMINI_IMAGE_UNAVAILABLE_REASON", "")
    monkeypatch.setattr(social_visuals, "_load_visual_repo_context", lambda: {"references": [], "settings": {}})
    monkeypatch.setattr(social_visuals, "_gemini_plate_quality", lambda *_args: (True, []))
    monkeypatch.setattr(social_visuals, "_gemini_semantic_plate_quality", lambda *_args: (True, []))
    monkeypatch.setattr(social_visuals, "budget_snapshot", lambda: {
        "image": {"remaining": 1},
        "total": {"remaining": 2},
    })
    monkeypatch.setattr(social_visuals, "preflight_gemini_workflow", lambda **kwargs: preflight_calls.append(kwargs))

    rendered, reason, metadata = social_visuals._generate_gemini_full_creative(
        {"post_id": "budgeted"},
        "instagram",
        {"v5_direction": {"format": "feed"}, "gemini_image_prompt": "Create one clean energy scene."},
        str(output_path),
    )

    assert rendered is True
    assert reason == "ok"
    assert metadata["image_provider_call_count"] == 1
    assert preflight_calls == [{"image_calls": 1, "reasoning_calls": 1}]


def test_product_free_editorial_source_is_not_misclassified_as_packshot(tmp_path):
    artifact = tmp_path / "editorial.png"
    _image(artifact, social_visuals._platform_visual_spec("linkedin")["target"])
    review = social_visuals._fallback_creative_review(
        {"product_id": None},
        {"creative_route": "EDITORIAL_HUMAN_SCENE"},
        str(artifact),
        "linkedin",
    )

    assert review["creative_classification"] == "EDITORIAL_SOURCE_IMAGE"
    assert review["verdict"] == "PASS"


def test_gemini_image_quota_failure_short_circuits_following_calls(tmp_path, monkeypatch):
    calls = {"count": 0}

    class Models:
        def generate_content(self, **_kwargs):
            calls["count"] += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED monthly spending cap")

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = lambda **_kwargs: types.SimpleNamespace(models=Models())
    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types.ImageConfig = lambda **kwargs: kwargs
    fake_types.HttpOptions = lambda **kwargs: kwargs
    fake_types.HttpRetryOptions = lambda **kwargs: kwargs
    fake_types.Part = types.SimpleNamespace(from_bytes=lambda **kwargs: kwargs)
    fake_genai.types = fake_types
    monkeypatch.setattr(google, "genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(social_visuals, "_GEMINI_IMAGE_UNAVAILABLE_REASON", "")
    content = {"post_id": "quota-test", "selected_cta": "Learn more"}

    first = social_visuals._generate_gemini_full_creative(content, "facebook", {}, str(tmp_path / "first.png"))
    second = social_visuals._generate_gemini_full_creative(content, "instagram", {}, str(tmp_path / "second.png"))

    assert first[0] is False and second[0] is False
    assert calls["count"] == 1
    assert "resource_exhausted" in second[1].lower()


def test_gemini_text_render_uses_image_aware_typography_agent(tmp_path, monkeypatch):
    image_buffer = io.BytesIO()
    Image.new("RGB", (1080, 1080), "#d8e0e4").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
    calls = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs["contents"])
            inline_data = types.SimpleNamespace(data=image_bytes)
            part = types.SimpleNamespace(inline_data=inline_data)
            content = types.SimpleNamespace(parts=[part])
            return types.SimpleNamespace(candidates=[types.SimpleNamespace(content=content)])

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = lambda **_kwargs: types.SimpleNamespace(models=Models())
    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types.ImageConfig = lambda **kwargs: kwargs
    fake_types.HttpOptions = lambda **kwargs: kwargs
    fake_types.HttpRetryOptions = lambda **kwargs: kwargs
    fake_types.Part = types.SimpleNamespace(from_bytes=lambda **kwargs: kwargs)
    fake_genai.types = fake_types
    monkeypatch.setattr(google, "genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_IMAGE_REPAIR_ATTEMPTS", "1")
    monkeypatch.setattr(social_visuals, "_GEMINI_IMAGE_UNAVAILABLE_REASON", "")
    monkeypatch.setattr(social_visuals, "_load_visual_repo_context", lambda: {"references": [], "settings": {}})
    monkeypatch.setattr(social_visuals, "_gemini_plate_quality", lambda *_args: (True, []))
    monkeypatch.setattr(social_visuals, "_gemini_semantic_plate_quality", lambda *_args: (True, []))
    designer_pixels = []

    def design_receipt(_data_dir, pixels, headline, platform):
        designer_pixels.append(pixels)
        return {
            "status": "COMPLETE",
            "snapshot_path": "designer.json",
            "typography": {
                "zone": {"x": 0.08, "y": 0.1, "width": 0.42, "height": 0.2},
                "anchor": "left architecture edge",
                "alignment": "left",
                "max_width_ratio": 0.42,
                "canvas_height_ratio": 0.1,
                "line_break": "BLUEPRINTS DO NOT WAIT.",
                "color": "warm_white",
                "weight": "bold",
                "tracking": 0.01,
                "decoration": "restrained amber accent rule",
                "rationale": "Uses the open upper-left field.",
                "protected_regions": ["person", "product", "cable"],
            },
        }

    monkeypatch.setattr("agents.on_image_typography_designer.design_receipt", design_receipt)
    output_path = str(tmp_path / "two-pass.png")
    rendered, reason, metadata = social_visuals._generate_gemini_full_creative(
        {"post_id": "two-pass"},
        "instagram",
        {
            "gemini_image_prompt": "An architect charging a phone on a transit platform.",
            "v5_direction": {
                "gemini_rendered_text": ["BLUEPRINTS DO NOT WAIT."],
                "typography_render_instruction": "Use no blue shadow, glow, outline, or text box.",
                "text_overlay": {"enabled": False},
            },
        },
        output_path,
    )

    assert rendered is True and reason == "ok"
    assert len(calls) == 1
    base_prompt = calls[0] if isinstance(calls[0], str) else calls[0][0]
    assert "text-free base photograph" in base_prompt
    assert "BLUEPRINTS DO NOT WAIT." not in base_prompt
    assert designer_pixels == [image_bytes]
    assert metadata["image_provider_call_count"] == 1
    assert metadata["local_text_overlay_used"] is True
    assert metadata["agent_orchestration"]["on_image_typography_designer"]["status"] == "COMPLETE"


def test_final_file_qa_preserves_packshot_only_rejection(tmp_path):
    artifact = tmp_path / "powerpulse.png"
    _image(artifact, social_visuals._platform_visual_spec("facebook")["target"])
    content = {
        "generated_visuals": {
            "facebook": str(artifact),
            "artifact_reviews": {
                "facebook": {
                    "verdict": "REGENERATE_VISUAL",
                    "issues": ["packshot_only_without_explicit_route"],
                    "creative_classification": "PACKSHOT_ONLY",
                    "recovery_action": "CHANGE_CREATIVE_ROUTE",
                }
            },
        }
    }

    reviews = run_engine._ensure_final_artifact_qa(
        content,
        {"facebook": True, "instagram": False, "linkedin": False},
    )

    assert reviews["facebook"]["verdict"] == "REGENERATE_VISUAL"
    assert reviews["facebook"]["creative_classification"] == "PACKSHOT_ONLY"
    assert "packshot_only_without_explicit_route" in reviews["facebook"]["issues"]
