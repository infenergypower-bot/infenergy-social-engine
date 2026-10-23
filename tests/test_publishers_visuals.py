from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, ROOT)
sys.path.insert(0, SCRIPTS)

import publish_facebook  # noqa: E402
import publish_instagram  # noqa: E402
import publish_linkedin  # noqa: E402
from generate_posts import (  # noqa: E402
    _build_fallback_content,
    _enforce_conversion_caption,
    _enforce_product_led_copy,
    _enforce_product_sales_platform_copy,
    _model_caption_overrides,
    _product_copy_profile,
)
from run_engine import _live_visual_gate_errors  # noqa: E402
from social_visuals import (  # noqa: E402
    _build_gemini_image_prompt,
    _gemini_plate_quality,
    _has_scanline_corruption,
    _normalize_reference_image,
    _platform_visual_spec,
    generate_visuals,
    normalize_brand_text,
    review_rendered_visual,
)


class PublisherVisualTests(unittest.TestCase):
    def test_facebook_http_error_does_not_expose_request_token(self) -> None:
        response = Mock()
        response.status_code = 400
        response.reason = "Bad Request"
        response.text = '{"error":{"message":"Unsupported delete request"}}'
        response.raise_for_status.side_effect = publish_facebook.requests.HTTPError(
            "400 Client Error for url: https://graph.facebook.com/post?access_token=sentinel-secret"
        )

        with self.assertRaises(publish_facebook.requests.HTTPError) as raised:
            publish_facebook._raise_with_body(response)

        self.assertNotIn("sentinel-secret", str(raised.exception))
        self.assertIn("facebook_api_http_400:Bad Request", str(raised.exception))

    def test_normalize_brand_text(self) -> None:
        text = "INF Energy Power helps INF Energy customers. #InfEnergyPower"
        normalized = normalize_brand_text(text)
        self.assertNotIn("INF Energy", normalized)
        self.assertIn("Infenergy Power", normalized)
        self.assertIn("#InfenergyPower", normalized)

    def test_gemini_prompt_includes_verbatim_ad_copy(self) -> None:
        prompt = _build_gemini_image_prompt(
            {
                "funnel_stage": "TRUST",
                "product_name": "PowerFlex 2000",
                "product_metrics": ["2000 Wh"],
                "selected_hook": "Keep essentials running",
                "selected_cta": "Compare the verified specs.",
            },
            "instagram",
            {"gemini_image_prompt": "Add badges, labels, and a fake product render."},
        )
        self.assertIn("left 42% and bottom 16%", prompt)
        self.assertIn("Render exactly this on-image copy", prompt)
        self.assertIn("Compare the verified specs.", prompt)
        self.assertIn("2000 Wh", prompt)
        self.assertIn("do not invent a different or generic device", prompt)
        self.assertNotIn("ABSOLUTE EXCLUSIONS", prompt)
        self.assertIn("subordinate to every rule", prompt)

    def test_gemini_prompt_requires_consumer_scene_and_visual_evidence(self) -> None:
        prompt = _build_gemini_image_prompt(
            {
                "consumer_root": {
                    "moment": {
                        "person": "A market vendor serving a closing-time line",
                        "setting": "an outdoor market stall",
                        "activity": "Completing the final sales",
                        "visual_evidence": ["customer ready to tap", "phone hotspot at low battery"],
                    }
                }
            },
            "instagram",
            {"gemini_image_prompt": "A premium product scene."},
        )
        self.assertIn("MANDATORY LIVED SCENE", prompt)
        self.assertIn("market vendor", prompt)
        self.assertIn("phone hotspot at low battery", prompt)

    def test_gemini_plate_quality_rejects_wrong_aspect_ratio(self) -> None:
        from PIL import Image

        wrong_ratio = Image.new("RGB", (1200, 400), "#202830")
        accepted, reasons = _gemini_plate_quality(wrong_ratio, "instagram")
        self.assertFalse(accepted)
        self.assertIn("aspect_ratio", reasons)

        correct_ratio = Image.new("RGB", (1200, 1200), "#334455")
        accepted, reasons = _gemini_plate_quality(correct_ratio, "instagram")
        self.assertNotIn("aspect_ratio", reasons)

    def test_corrupted_scanlines_are_rejected_at_generation_and_artifact_qa(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1200, 1200), "#263746")
        draw = ImageDraw.Draw(image)
        for y, color in ((200, "#ff0000"), (400, "#00ff00"), (600, "#0000ff")):
            draw.rectangle((700, y, 1199, y + 12), fill=color)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image_path = image_file.name
        try:
            image.save(image_path, format="PNG")
            accepted, reasons = _gemini_plate_quality(image, "instagram")
            review = review_rendered_visual(image_path, "instagram")
        finally:
            os.unlink(image_path)

        self.assertTrue(_has_scanline_corruption(image))
        self.assertFalse(accepted)
        self.assertIn("scanline_corruption", reasons)
        self.assertEqual(review["verdict"], "REGENERATE_VISUAL")
        self.assertIn("rendered_scanline_corruption", review["issues"])

    def test_iis_carousel_reuses_studio_qa_for_branded_scanlines(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1080, 1350), "#263746")
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 100, 1000, 145), fill="#b7ff00")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image_path = image_file.name
        try:
            image.save(image_path, format="PNG")
            review = review_rendered_visual(image_path, "iis_carousel")
        finally:
            os.unlink(image_path)

        self.assertTrue(_has_scanline_corruption(image))
        self.assertEqual(review["verdict"], "PASS")

    def test_instagram_strict_gemini_mode_never_uses_catalog_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LIVE_REQUIRE_GEMINI_VISUAL": "true",
                "IG_VALIDATE_IMAGE_URLS": "false",
            },
            clear=False,
        ):
            result = publish_instagram.publish(
                {
                    "ig_caption": "Caption",
                    "generated_visuals": {},
                    "product_image_url": "https://example.com/catalog.png",
                }
            )

        self.assertEqual(result["id"], "skipped")
        self.assertEqual(result["reason"], "required_gemini_visual_unavailable")

    def test_instagram_strict_package_rejects_untracked_hosted_image(self) -> None:
        with patch.dict(os.environ, {"IG_VALIDATE_IMAGE_URLS": "false"}, clear=False):
            result = publish_instagram.publish(
                {
                    "ig_caption": "Caption",
                    "gemini_generation": {"strict_provider": True, "assets": []},
                    "primary_publish_image_url": "https://example.com/untracked.png",
                }
            )

        self.assertEqual(result["id"], "skipped")
        self.assertEqual(result["reason"], "required_gemini_visual_unavailable")

    def test_instagram_strict_mode_accepts_reviewed_hosted_gemini_artifact(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png") as image_file, patch.dict(
            os.environ,
            {"LIVE_REQUIRE_GEMINI_VISUAL": "true", "IG_VALIDATE_IMAGE_URLS": "false"},
            clear=False,
        ), patch.object(
            publish_instagram, "review_rendered_visual", return_value={"verdict": "PASS", "issues": []}
        ):
            result = publish_instagram.publish(
                {
                    "ig_caption": "Caption",
                    "generated_visuals": {
                        "instagram": image_file.name,
                        "render_engines": {"instagram": "gemini"},
                    },
                    "primary_publish_image_url": "https://example.com/media/reviewed.png",
                },
                dry_run=True,
            )

        self.assertEqual(result["id"], "dry-run")

    def test_live_gate_rejects_approved_photo_when_gemini_is_required(self) -> None:
        content = {
            "generated_visuals": {
                "facebook": "facebook.png",
                "instagram": "instagram.png",
                "render_engines": {
                    "facebook": "approved_product_photo",
                    "instagram": "approved_product_photo",
                },
            }
        }
        with patch.dict(os.environ, {"LIVE_REQUIRE_GEMINI_VISUAL": "true"}, clear=False):
            errors = _live_visual_gate_errors(
                content,
                {"facebook": True, "instagram": True, "linkedin": False},
                dry_run=False,
            )

        self.assertIn("facebook_visual_not_gemini", errors)
        self.assertIn("instagram_visual_not_gemini", errors)

    def test_instagram_strict_package_fails_closed_on_public_url_preflight(self) -> None:
        public_url = "https://example.com/media/reviewed.png"
        with patch.dict(os.environ, {"IG_VALIDATE_IMAGE_URLS": "true"}, clear=False), patch.object(
            publish_instagram, "_is_reachable_image_url", return_value=False
        ):
            result = publish_instagram.publish(
                {
                    "ig_caption": "Caption",
                    "gemini_generation": {
                        "strict_provider": True,
                        "assets": [{"render_engine": "gemini", "public_url": public_url}],
                    },
                    "primary_publish_image_url": public_url,
                }
            )

        self.assertEqual(result["id"], "skipped")
        self.assertEqual(result["reason"], "strict_gemini_public_url_preflight_failed")

    def test_facebook_strict_package_rejects_corrupted_local_artifact(self) -> None:
        with patch.object(
            publish_facebook,
            "review_rendered_visual",
            return_value={"verdict": "REGENERATE_VISUAL", "issues": ["rendered_scanline_corruption"]},
        ):
            with self.assertRaisesRegex(RuntimeError, "facebook_strict_gemini_artifact_invalid"):
                publish_facebook.publish(
                    {
                        "fb_caption": "Caption",
                        "gemini_generation": {"strict_provider": True},
                        "generated_visuals": {"facebook": "missing.png"},
                    },
                    "https://example.com",
                )

    def test_facebook_strict_carousel_uses_carousel_dimensions(self) -> None:
        with patch.object(
            publish_facebook,
            "review_rendered_visual",
            return_value={"verdict": "REGENERATE_VISUAL", "issues": ["rendered_scanline_corruption"]},
        ) as review:
            with self.assertRaisesRegex(RuntimeError, "facebook_strict_gemini_artifact_invalid"):
                publish_facebook.publish(
                    {
                        "fb_caption": "Caption",
                        "gemini_generation": {"strict_provider": True, "aspect_ratio": "4:5"},
                        "generated_visuals": {"facebook": "missing.png"},
                    },
                    "https://example.com",
                )

        review.assert_called_once_with("missing.png", "iis_carousel")

    def test_instagram_strict_carousel_uses_carousel_dimensions(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png") as image_file, patch.dict(
            os.environ,
            {"IG_VALIDATE_IMAGE_URLS": "false"},
            clear=False,
        ), patch.object(
            publish_instagram,
            "review_rendered_visual",
            return_value={"verdict": "PASS", "issues": []},
        ) as review:
            result = publish_instagram.publish(
                {
                    "ig_caption": "Caption",
                    "gemini_generation": {
                        "strict_provider": True,
                        "aspect_ratio": "4:5",
                        "assets": [{
                            "render_engine": "gemini",
                            "public_url": "https://example.com/media/reviewed.png",
                        }],
                    },
                    "generated_visuals": {
                        "instagram": image_file.name,
                        "render_engines": {"instagram": "gemini"},
                    },
                    "primary_publish_image_url": "https://example.com/media/reviewed.png",
                },
                dry_run=True,
            )

        self.assertEqual(result["id"], "dry-run")
        review.assert_called_once_with(image_file.name, "iis_carousel")

    def test_facebook_owner_supplied_visual_bypasses_generated_artifact_gate(self) -> None:
        upload_response = Mock(ok=True)
        upload_response.json.return_value = {"id": "photo-1"}
        feed_response = Mock(ok=True)
        feed_response.json.return_value = {"id": "post-1"}
        with patch.dict(os.environ, {"LIVE_REQUIRE_GEMINI_VISUAL": "true", "META_PAGE_ID": "page", "META_PAGE_ACCESS_TOKEN": "token"}, clear=False), \
            patch.object(publish_facebook, "_resolve_page_access_token", return_value="token"), \
            patch.object(publish_facebook.requests, "post", return_value=upload_response), \
            patch.object(publish_facebook, "_post_with_retry", return_value=feed_response), \
            patch.object(publish_facebook, "review_rendered_visual") as review:
            result = publish_facebook.publish(
                {
                    "fb_caption": "Caption",
                    "owner_supplied_visual": True,
                    "primary_publish_image_url": "https://example.com/owner.png",
                },
                "",
            )

        self.assertEqual(result["id"], "post-1")
        review.assert_not_called()

    def test_facebook_reel_uses_start_upload_and_finish_protocol(self) -> None:
        start_response = Mock(ok=True)
        start_response.json.return_value = {"video_id": "video-1", "upload_url": "https://upload.facebook.test/reel"}
        upload_response = Mock(ok=True)
        finish_response = Mock(ok=True)
        finish_response.json.return_value = {"success": True}
        with patch.object(
            publish_facebook, "_post_with_retry", side_effect=[start_response, finish_response]
        ) as post_with_retry, patch.object(
            publish_facebook.requests, "post", return_value=upload_response
        ) as upload:
            result = publish_facebook._publish_reel(
                page_id="page-1",
                token="token-1",
                description="Mission caption",
                video_url="https://media.example/reel.mp4",
            )

        self.assertEqual(result["id"], "video-1")
        self.assertEqual(result["media_type"], "REEL")
        self.assertEqual(post_with_retry.call_count, 2)
        self.assertEqual(post_with_retry.call_args_list[0].args[1]["upload_phase"], "start")
        self.assertEqual(post_with_retry.call_args_list[1].args[1]["upload_phase"], "finish")
        self.assertEqual(post_with_retry.call_args_list[1].args[1]["video_state"], "PUBLISHED")
        upload.assert_called_once_with(
            "https://upload.facebook.test/reel",
            headers={"Authorization": "OAuth token-1", "file_url": "https://media.example/reel.mp4"},
            timeout=120,
        )

    def test_instagram_owner_supplied_visual_bypasses_generated_artifact_gate(self) -> None:
        create_response = Mock(ok=True)
        create_response.json.return_value = {"id": "container-1"}
        publish_response = Mock(ok=True)
        publish_response.json.return_value = {"id": "post-1"}
        with patch.dict(os.environ, {"LIVE_REQUIRE_GEMINI_VISUAL": "true", "META_IG_USER_ID": "user", "META_PAGE_ACCESS_TOKEN": "token"}, clear=False), \
            patch.object(publish_instagram, "_is_reachable_image_url", return_value=True), \
            patch.object(publish_instagram, "_post_with_retry", side_effect=[create_response, publish_response]), \
            patch.object(publish_instagram, "_wait_for_media_container", return_value=(True, "finished")), \
            patch.object(publish_instagram, "review_rendered_visual") as review:
            result = publish_instagram.publish(
                {
                    "ig_caption": "Caption",
                    "owner_supplied_visual": True,
                    "primary_publish_image_url": "https://example.com/owner.png",
                }
            )

        self.assertEqual(result["id"], "post-1")
        self.assertEqual(result["media_type"], "IMAGE")
        self.assertEqual(result["image_url"], "https://example.com/owner.png")
        review.assert_not_called()

    def test_linkedin_fails_closed_when_approved_image_cannot_be_downloaded(self) -> None:
        with patch.object(publish_linkedin, "_resolve_author_urn", return_value="urn:li:organization:1"), \
            patch.object(publish_linkedin, "_is_reachable_image_url", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "linkedin_approved_primary_image_unavailable"):
                publish_linkedin.publish(
                    {
                        "li_text": "Caption",
                        "primary_publish_image_url": "https://example.com/approved.png",
                        "owner_supplied_visual": True,
                    },
                    "",
                )

    def test_instagram_story_uses_story_container_and_publish_protocol(self) -> None:
        create_response = Mock(ok=True)
        create_response.json.return_value = {"id": "story-container"}
        publish_response = Mock(ok=True)
        publish_response.json.return_value = {"id": "story-media"}
        with patch.object(
            publish_instagram, "_post_with_retry", side_effect=[create_response, publish_response]
        ) as post_with_retry, patch.object(
            publish_instagram, "_wait_for_media_container", return_value=(True, "finished")
        ):
            result = publish_instagram._publish_story_video(
                "https://media.example/story.mp4",
                ig_user_id="ig-user",
                access_token="token",
            )

        self.assertEqual(result["id"], "story-media")
        self.assertEqual(result["media_type"], "STORY")
        self.assertEqual(post_with_retry.call_args_list[0].args[1]["media_type"], "STORIES")
        self.assertEqual(post_with_retry.call_args_list[1].args[1]["creation_id"], "story-container")

    def test_iis_delivery_profiles_require_native_carousel_and_reel_dimensions(self) -> None:
        self.assertEqual(_platform_visual_spec("iis_carousel")["target"], (1080, 1350))
        self.assertEqual(_platform_visual_spec("iis_reel_cover")["target"], (1080, 1920))

    def test_style_reference_must_decode_as_an_image(self) -> None:
        from PIL import Image
        from io import BytesIO

        self.assertEqual(_normalize_reference_image(b"<html>not an image</html>"), (b"", ""))
        source = BytesIO()
        Image.new("RGB", (80, 60), "#223344").save(source, format="PNG")
        normalized, mime_type = _normalize_reference_image(source.getvalue())
        self.assertGreater(len(normalized), 0)
        self.assertEqual(mime_type, "image/jpeg")

    def test_linkedin_prefers_explicit_organization_urn(self) -> None:
        with patch.dict(os.environ, {"LINKEDIN_ORGANIZATION_URN": "urn:li:organization:12345"}, clear=False):
            author = publish_linkedin._resolve_author_urn()
        self.assertEqual(author, "urn:li:organization:12345")

    def test_facebook_attaches_generated_photo_to_feed_post(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"fakepng")
            image_path = tmp.name
        try:
            upload_response = Mock(ok=True)
            upload_response.json.return_value = {"id": "photo_123"}
            feed_response = Mock(ok=True)
            feed_response.json.return_value = {"id": "page123_feed_456"}

            with patch.dict(
                os.environ,
                {
                    "META_PAGE_ID": "page123",
                    "META_PAGE_ACCESS_TOKEN": "token123",
                },
                clear=False,
            ), patch.object(publish_facebook, "_resolve_page_access_token", return_value="page_token"), patch.object(
                publish_facebook.requests,
                "post",
                side_effect=[upload_response, feed_response],
            ) as mock_post:
                result = publish_facebook.publish(
                    {
                        "fb_caption": "Caption text",
                        "generated_visuals": {"facebook": image_path},
                    },
                    "https://example.com",
                    dry_run=False,
                )

            self.assertEqual(result["id"], "page123_feed_456")
            self.assertEqual(mock_post.call_count, 2)
            self.assertIn("/photos", mock_post.call_args_list[0].args[0])
            self.assertEqual(mock_post.call_args_list[0].kwargs["data"]["published"], "false")
            self.assertIn("/feed", mock_post.call_args_list[1].args[0])
            self.assertIn('"media_fbid": "photo_123"', mock_post.call_args_list[1].kwargs["data"]["attached_media"])
        finally:
            try:
                os.remove(image_path)
            except OSError:
                pass

    def test_facebook_attaches_hosted_photo_to_feed_post(self) -> None:
        upload_response = Mock(ok=True)
        upload_response.json.return_value = {"id": "photo_url_123"}
        feed_response = Mock(ok=True)
        feed_response.json.return_value = {"id": "page123_feed_789"}

        with patch.dict(
            os.environ,
            {
                "META_PAGE_ID": "page123",
                "META_PAGE_ACCESS_TOKEN": "token123",
                "FB_REQUIRE_IMAGE": "true",
            },
            clear=False,
        ), patch.object(publish_facebook, "_resolve_page_access_token", return_value="page_token"), patch.object(
            publish_facebook.requests,
            "post",
            side_effect=[upload_response, feed_response],
        ) as mock_post:
            result = publish_facebook.publish(
                {
                    "fb_caption": "Caption text",
                    "product_image_url": "https://example.com/product.png",
                },
                "https://example.com",
                dry_run=False,
            )

        self.assertEqual(result["id"], "page123_feed_789")
        self.assertEqual(mock_post.call_count, 2)
        self.assertIn("/photos", mock_post.call_args_list[0].args[0])
        self.assertEqual(mock_post.call_args_list[0].kwargs["data"]["published"], "false")
        self.assertEqual(mock_post.call_args_list[0].kwargs["data"]["url"], "https://example.com/product.png")
        self.assertIn("/feed", mock_post.call_args_list[1].args[0])
        self.assertIn('"media_fbid": "photo_url_123"', mock_post.call_args_list[1].kwargs["data"]["attached_media"])

    def test_facebook_attaches_all_carousel_slides(self) -> None:
        paths = []
        try:
            for _ in range(4):
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.write(b"fakepng")
                tmp.close()
                paths.append(tmp.name)
            uploads = []
            for index in range(4):
                response = Mock(ok=True)
                response.json.return_value = {"id": f"photo_{index}"}
                uploads.append(response)
            feed = Mock(ok=True)
            feed.json.return_value = {"id": "carousel_post"}
            with patch.dict(os.environ, {"META_PAGE_ID": "page123", "META_PAGE_ACCESS_TOKEN": "token123"}, clear=False), patch.object(
                publish_facebook, "_resolve_page_access_token", return_value="page_token"
            ), patch.object(publish_facebook.requests, "post", side_effect=[*uploads, feed]) as mock_post:
                result = publish_facebook.publish(
                    {
                        "fb_caption": "Carousel caption",
                        "carousel_assets": [{"local_path": path} for path in paths],
                        "generated_visuals": {"facebook": paths[0]},
                    },
                    "",
                )
            attached = mock_post.call_args_list[-1].kwargs["data"]["attached_media"]
            self.assertEqual(result["id"], "carousel_post")
            self.assertEqual(mock_post.call_count, 5)
            self.assertEqual(attached.count("media_fbid"), 4)
        finally:
            for path in paths:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def test_facebook_raises_when_image_required_but_missing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "META_PAGE_ID": "page123",
                "META_PAGE_ACCESS_TOKEN": "token123",
                "FB_REQUIRE_IMAGE": "true",
            },
            clear=False,
        ), patch.object(publish_facebook, "_resolve_page_access_token", return_value="page_token"):
            with self.assertRaises(RuntimeError):
                publish_facebook.publish(
                    {
                        "fb_caption": "Caption text",
                        "product_image_url": "",
                        "generated_visuals": {},
                    },
                    "https://example.com",
                    dry_run=False,
                )

    def test_instagram_bounds_caption_before_creating_media(self) -> None:
        create_response = Mock(ok=True)
        create_response.json.return_value = {"id": "container_123"}
        publish_response = Mock(ok=True)
        publish_response.json.return_value = {"id": "instagram_456"}
        long_caption = "Useful preparation advice " * 150

        with patch.dict(
            os.environ,
            {
                "META_IG_USER_ID": "ig123",
                "META_PAGE_ACCESS_TOKEN": "token123",
                "IG_VALIDATE_IMAGE_URLS": "false",
            },
            clear=False,
        ), patch.object(publish_instagram, "_wait_for_media_container", return_value=(True, "finished")), patch.object(
            publish_instagram,
            "_post_with_retry",
            side_effect=[create_response, publish_response],
        ) as mock_post:
            result = publish_instagram.publish(
                {
                    "ig_caption": long_caption,
                    "product_image_url": "https://example.com/product.png",
                }
            )

        self.assertEqual(result["id"], "instagram_456")
        caption = mock_post.call_args_list[0].args[1]["caption"]
        self.assertLessEqual(len(caption), publish_instagram.INSTAGRAM_CAPTION_MAX_LENGTH)
        self.assertTrue(caption.endswith("..."))

    def test_instagram_creates_native_carousel(self) -> None:
        responses = []
        for index in range(4):
            child = Mock(ok=True)
            child.json.return_value = {"id": f"child_{index}"}
            responses.append(child)
        parent = Mock(ok=True)
        parent.json.return_value = {"id": "parent_1"}
        published = Mock(ok=True)
        published.json.return_value = {"id": "instagram_carousel_1"}
        with patch.dict(
            os.environ,
            {"META_IG_USER_ID": "ig123", "META_PAGE_ACCESS_TOKEN": "token123", "IG_VALIDATE_IMAGE_URLS": "false"},
            clear=False,
        ), patch.object(publish_instagram, "_wait_for_media_container", return_value=(True, "finished")), patch.object(
            publish_instagram, "_post_with_retry", side_effect=[*responses, parent, published]
        ) as mock_post:
            result = publish_instagram.publish(
                {
                    "ig_caption": "Carousel caption",
                    "primary_publish_image_url": "https://example.com/slide-1.png",
                    "carousel_assets": [
                        {"public_url": f"https://example.com/slide-{index}.png"} for index in range(1, 5)
                    ],
                }
            )
        parent_data = mock_post.call_args_list[4].args[1]
        self.assertEqual(result["id"], "instagram_carousel_1")
        self.assertEqual(parent_data["media_type"], "CAROUSEL")
        self.assertEqual(parent_data["children"], "child_0,child_1,child_2,child_3")

    def test_generate_visuals_has_no_fallback_when_gemini_unavailable(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            visuals = generate_visuals(
                {
                    "post_id": "unit_test_card_no_fallback",
                    "funnel_stage": "TRUST",
                    "selected_hook": "Why energy resilience needs a better design standard",
                    "topic": "A stronger branded social card",
                    "selected_cta": "Review the full system.",
                    "product_name": "PowerFlex",
                    "product_image_url": "",
                }
            )
        self.assertNotIn("facebook", visuals)
        self.assertNotIn("instagram", visuals)
        self.assertNotIn("linkedin", visuals)
        self.assertNotIn("facebook_html", visuals)
        self.assertEqual(visuals["render_engines"]["facebook"], "failed")
        self.assertEqual(visuals["gemini_available"], "false")
        render = visuals["visual_generation"]["facebook"]
        self.assertTrue(render["visual_generation_attempted"])
        self.assertEqual(render["generation_status"], "failed")
        self.assertEqual(render["provider_error_class"], "AUTH_ERROR")
        self.assertEqual(render["artifact_path"], "")
        self.assertFalse(render["artifact_exists"])

    def test_generate_visuals_uses_approved_product_photo_when_gemini_fails(self) -> None:
        import hashlib
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "product.png")
            Image.new("RGB", (800, 600), "#223344").save(source_path, format="PNG")
            with open(source_path, "rb") as source_file:
                source_sha256 = hashlib.sha256(source_file.read()).hexdigest()
            with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False), patch("social_visuals.VISUAL_DIR", temp_dir):
                visuals = generate_visuals(
                    {
                        "post_id": "unit_test_product_photo_fallback",
                        "product_id": "TEST-POWER",
                        "product_image_url": source_path,
                        "product_image_approval": {
                            "product_id": "TEST-POWER",
                            "source_url": source_path,
                            "sha256": source_sha256,
                        },
                    },
                    {"creative_route": "PREMIUM_PRODUCT_HERO"},
                )

            self.assertEqual(visuals["render_engines"]["instagram"], "approved_product_photo")
            self.assertIn("instagram", visuals)
            self.assertEqual(
                visuals["visual_generation"]["instagram"].get("fallback_source"),
                "approved_product_photo",
            )
            self.assertEqual(visuals["artifact_reviews"]["instagram"]["verdict"], "PASS")

    def test_product_image_approval_requires_matching_canonical_source(self) -> None:
        import hashlib
        from social_visuals import build_product_image_approval

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "product.png")
            source_bytes = b"locked-product-image"
            with open(source_path, "wb") as source_file:
                source_file.write(source_bytes)
            content = {"product_id": "BW-1500W-60AH", "product_image_url": source_path}

            approval = build_product_image_approval(content, source_path)

            self.assertEqual(approval["product_id"], "BW-1500W-60AH")
            self.assertEqual(approval["source_url"], source_path)
            self.assertEqual(approval["sha256"], hashlib.sha256(source_bytes).hexdigest())
            self.assertEqual(build_product_image_approval(content, f"{source_path}.other"), {})

    def test_facebook_square_is_shared_with_instagram_with_independent_review(self) -> None:
        calls = []

        def render(content, platform, plan, output_path):
            calls.append(platform)
            with open(output_path, "wb") as output_file:
                output_file.write(platform.encode("ascii"))
            return True, "", {"image_provider_call_count": 1}

        with tempfile.TemporaryDirectory() as temp_dir, patch("social_visuals.VISUAL_DIR", temp_dir), patch(
            "social_visuals._generate_gemini_full_creative", side_effect=render
        ), patch("social_visuals.review_rendered_visual", side_effect=lambda path, platform: {"verdict": "PASS", "platform": platform}):
            visuals = generate_visuals({"post_id": "shared-square"}, {})

        self.assertEqual(calls, ["facebook", "linkedin"])
        self.assertEqual(visuals["render_engines"]["instagram"], "gemini_shared_square")
        self.assertEqual(visuals["artifact_reviews"]["instagram"]["platform"], "instagram")
        self.assertEqual(visuals["image_provider_call_count"], 2)
        self.assertEqual(visuals["image_provider_call_budget"], 2)

    def test_failed_facebook_square_does_not_consume_instagram_image_call(self) -> None:
        calls = []

        def render(content, platform, plan, output_path):
            calls.append(platform)
            if platform == "facebook":
                return False, "semantic_quality_rejected", {"image_provider_call_count": 1}
            with open(output_path, "wb") as output_file:
                output_file.write(platform.encode("ascii"))
            return True, "", {"image_provider_call_count": 1}

        with tempfile.TemporaryDirectory() as temp_dir, patch("social_visuals.VISUAL_DIR", temp_dir), patch(
            "social_visuals._generate_gemini_full_creative", side_effect=render
        ), patch("social_visuals.review_rendered_visual", side_effect=lambda path, platform: {"verdict": "PASS", "platform": platform}):
            visuals = generate_visuals({"post_id": "failed-shared-square"}, {})

        self.assertEqual(calls, ["facebook", "linkedin"])
        self.assertEqual(visuals["visual_generation"]["instagram"]["generation_status"], "shared_source_unavailable")
        self.assertEqual(visuals["image_provider_call_count"], 2)

    def test_live_visual_gate_rejects_missing_product_source_and_overlay(self) -> None:
        content = {
            "product_id": "SFT-20K",
            "generated_visuals": {
                "facebook": "card.png",
                "render_engines": {"facebook": "local_render"},
                "product_overlay_applied": {"facebook": False},
                "product_specific_source_present": "false",
            }
        }
        errors = _live_visual_gate_errors(content, {"facebook": True}, dry_run=False)
        self.assertIn("product_specific_image_source_missing", errors)
        self.assertIn("facebook_product_overlay_missing", errors)

    def test_live_visual_gate_accepts_approved_product_photo_fallback(self) -> None:
        content = {
            "product_id": "SFT-20K",
            "generated_visuals": {
                "facebook": "product-photo.png",
                "render_engines": {"facebook": "approved_product_photo"},
                "product_overlay_applied": {"facebook": True},
                "product_specific_source_present": "true",
            },
        }

        assert _live_visual_gate_errors(content, {"facebook": True}, dry_run=False) == []

    def test_live_visual_gate_skips_product_checks_when_no_product_anchored(self) -> None:
        # Educational/pillar content with no product_id must not be blocked by
        # product-specific image requirements — those only apply when a product
        # is actually anchored to the post.
        content = {
            "generated_visuals": {
                "facebook": "card.png",
                "render_engines": {"facebook": "local_render"},
                "product_overlay_applied": {"facebook": False},
                "product_specific_source_present": "false",
            }
        }
        errors = _live_visual_gate_errors(content, {"facebook": True}, dry_run=False)
        self.assertNotIn("product_specific_image_source_missing", errors)
        self.assertNotIn("facebook_product_overlay_missing", errors)
        self.assertEqual(errors, [])

    def test_generate_visuals_uses_approved_reference_for_non_product_fallback(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "editorial.png")
            Image.new("RGB", (1200, 1200), "#36505f").save(source_path, format="PNG")
            context = {"references": [{"reference_url": source_path}], "settings": {}}
            with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False), \
                    patch("social_visuals.VISUAL_DIR", temp_dir), \
                    patch("social_visuals._load_visual_repo_context", return_value=context):
                visuals = generate_visuals({"post_id": "organic-reference", "product_id": None})

            self.assertEqual(visuals["render_engines"]["facebook"], "approved_reference_image")
            self.assertEqual(visuals["visual_generation"]["facebook"]["fallback_source"], "approved_reference_image")
            self.assertEqual(visuals["artifact_reviews"]["facebook"]["verdict"], "PASS")

    def test_live_visual_gate_accepts_gemini_product_composite(self) -> None:
        content = {
            "generated_visuals": {
                "facebook": "card.png",
                "instagram": "card.png",
                "render_engines": {"facebook": "gemini", "instagram": "gemini"},
                "product_overlay_applied": {"facebook": True, "instagram": True},
                "product_specific_source_present": "true",
            }
        }
        errors = _live_visual_gate_errors(
            content,
            {"facebook": True, "instagram": True, "linkedin": False},
            dry_run=False,
        )
        self.assertEqual(errors, [])

    def test_model_platform_captions_are_preserved_for_packaging(self) -> None:
        overrides = _model_caption_overrides(
            {
                "fb_caption": "Facebook copy written for this product.",
                "ig_caption": "Instagram copy written for this product.",
                "li_text": "LinkedIn copy written for this product.",
                "selected_cta": "Compare the verified specifications.",
            }
        )
        self.assertEqual(overrides["facebook"]["caption"], "Facebook copy written for this product.")
        self.assertEqual(overrides["instagram"]["caption"], "Instagram copy written for this product.")
        self.assertEqual(overrides["linkedin"]["caption"], "LinkedIn copy written for this product.")

    def test_product_copy_guard_preserves_substantive_model_copy(self) -> None:
        product = {"name": "HoneyVolt Slim Power Bank", "metrics": ["10,000 mAh"]}
        original = {
            "fb_caption": "HoneyVolt Slim Power Bank keeps your phone available through a long travel day without turning preparedness into a lecture. Compare the published capacity before buying.",
            "ig_caption": "HoneyVolt Slim Power Bank belongs in the bag you already carry. Check the published capacity, charge before leaving, and keep your phone ready when outlets disappear.",
            "li_text": "HoneyVolt Slim Power Bank gives mobile teams a simple layer of charging continuity. Review its published capacity against the devices employees actually carry before standardizing a kit.",
        }
        content = dict(original)
        _enforce_product_sales_platform_copy(content, product, {})
        self.assertEqual(content, original)

    def test_conversion_guard_does_not_append_boilerplate_to_substantive_copy(self) -> None:
        caption = (
            "HoneyVolt Slim Power Bank belongs in the bag you already carry. "
            "Check its published capacity against your phone, charge it before leaving, "
            "and keep one dependable backup close when outlets disappear."
        )
        result = _enforce_conversion_caption(
            caption,
            {
                "pain_point": "Dead batteries expose weak preparation.",
                "proof_anchor": "Use published specifications to validate fit.",
                "first_step": "Comment with your top three devices.",
                "copy_generation_source": "gemini",
            },
            platform="facebook",
        )
        self.assertEqual(result, caption)

        fallback_result = _enforce_conversion_caption(
            caption,
            {
                "pain_point": "Dead batteries expose weak preparation.",
                "proof_anchor": "Use published specifications to validate fit.",
                "first_step": "Comment with your top three devices.",
                "copy_generation_source": "deterministic_fallback",
            },
            platform="facebook",
        )
        self.assertEqual(fallback_result, caption)

    def test_fallback_platform_copy_stays_concise_and_avoids_inventory_prefix(self) -> None:
        product = {
            "name": "SolarFlex Titan 20K",
            "sku": "SFT-20K",
            "sale_price": "69.99",
            "metrics": ["20,000mAh", "20W", "18W"],
            "categories": ["Emergency Power", "Outdoors & Camping"],
            "fact_snippet": "SolarFlex Titan 20K: Infinite Power, On The Go Harness the power of the sun.",
        }
        talking_point = {
            "pain_point": "A dead phone during an outage can cut off weather alerts and family check-ins.",
            "proof_anchor": "Compare capacity and charging output with the devices you rely on.",
            "first_step": "Review the product details and confirm it fits your emergency kit.",
        }
        content = _build_fallback_content("midday", "outage readiness", product, {}, talking_point)
        _enforce_product_led_copy(content, product)

        for key in ("fb_caption", "ig_caption", "li_text"):
            caption = content[key]
            self.assertIn(product["name"], caption)
            self.assertLessEqual(len(caption), 700)
            self.assertNotIn("Featured product:", caption)
            self.assertNotIn("Key specs:", caption)
            self.assertNotIn("to keeps", caption)
            self.assertNotIn("Use only published", caption)

    def test_water_filter_fallback_copy_uses_water_domain_language(self) -> None:
        product = {
            "name": "Wosfer Micron Water Filter Straw",
            "sku": "WFS-001",
            "sale_price": "24.99",
            "metrics": ["28mm", "30mm"],
            "categories": ["Water Filtration"],
        }
        talking_point = {
            "pain_point": "Your backup power setup cannot run what matters most.",
            "proof_anchor": "Use 28mm and 30mm to validate fit before buying.",
            "first_step": "Comment with your top 3 must-run devices.",
        }
        content = _build_fallback_content("midday", "outage readiness", product, {}, talking_point)

        for key in ("fb_caption", "ig_caption", "li_text"):
            caption = content[key]
            self.assertIn("water", caption.lower())
            self.assertNotIn("must-run devices", caption)
            self.assertNotIn("backup power", caption.lower())
            self.assertNotIn("Use 28mm and 30mm", caption)
        self.assertIn("#WaterFiltration", content["fb_caption"])
        self.assertIn("#WaterFiltration", content["ig_caption"])

    def test_multifunction_fan_is_not_classified_as_a_power_bank(self) -> None:
        product = {
            "name": "3-in-1 Portable Camping Fan with 12000mAh Battery, LED Light & Power Bank",
            "sku": "CAMP-FAN-12K",
            "sale_price": "39.99",
            "metrics": [],
            "categories": ["Camping Gear", "Portable Power"],
        }
        content = _build_fallback_content("midday", "outage readiness", product, {}, {})

        for key in ("fb_caption", "ig_caption", "li_text"):
            caption = content[key]
            self.assertIn("airflow", caption.lower())
            self.assertIn("runtime", caption.lower())
            self.assertNotIn("cheap power bank", caption.lower())
            self.assertNotIn("top 3 must-run devices", caption)
        self.assertIn("#PortableFan", content["fb_caption"])
        self.assertLessEqual(len(content["fb_caption"]), 700)

    def test_power_station_evidence_wins_over_solar_panel_category(self) -> None:
        profile = _product_copy_profile(
            {
                "name": "AFERIY AF-P210",
                "categories": ["Emergency Power", "Power Stations", "Solar Panels"],
                "metrics": ["1.5 hours", "120V", "100W"],
                "fact_snippet": "2400W home backup powerhouse with a 2048Wh battery and pure sine wave inverter.",
            }
        )

        self.assertEqual(profile["role"], "backup power station")
        self.assertIn("runtime", profile["proof_intro"])

    def test_instagram_publishes_native_media_without_wordpress(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"fakepng")
            image_path = tmp.name
        native_url = "https://social.example/media/generated.png"
        try:
            first = Mock()
            first.ok = True
            first.json.return_value = {"id": "creation_123"}
            first.raise_for_status.return_value = None
            second = Mock()
            second.ok = True
            second.json.return_value = {"id": "ig_123"}
            second.raise_for_status.return_value = None

            with patch.dict(
                os.environ,
                {
                    "META_IG_USER_ID": "ig_user_1",
                    "META_PAGE_ACCESS_TOKEN": "page_token_1",
                    "IG_VALIDATE_IMAGE_URLS": "false",
                },
                clear=False,
            ), patch.object(
                publish_instagram,
                "_post_with_retry",
                side_effect=[first, second],
            ) as mock_post, patch.object(
                publish_instagram,
                "_wait_for_media_container",
                return_value=(True, "finished"),
            ):
                result = publish_instagram.publish(
                    {
                        "ig_caption": "Caption text",
                        "generated_visuals": {"instagram": image_path},
                        "primary_publish_image_url": native_url,
                        "product_image_url": "https://example.com/fallback.png",
                    },
                    dry_run=False,
                )

            self.assertEqual(result["id"], "ig_123")
            self.assertEqual(mock_post.call_args_list[0].args[1]["image_url"], native_url)
            self.assertFalse(hasattr(publish_instagram, "publish_wordpress"))
        finally:
            try:
                os.remove(image_path)
            except OSError:
                pass

    def test_instagram_preflight_accepts_native_media_from_data_volume(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            media_dir = os.path.join(data_dir, "public_media")
            os.makedirs(media_dir)
            with open(os.path.join(media_dir, "generated.png"), "wb") as image_file:
                image_file.write(b"fakepng")

            with patch.dict(
                os.environ,
                {
                    "DATA_DIR": data_dir,
                    "RAILWAY_PUBLIC_DOMAIN": "social.example",
                },
                clear=False,
            ), patch.object(publish_instagram.requests, "head") as mock_head:
                reachable = publish_instagram._is_reachable_image_url(
                    "https://social.example/media/generated.png"
                )

            self.assertTrue(reachable)
            mock_head.assert_not_called()


if __name__ == "__main__":
    unittest.main()
