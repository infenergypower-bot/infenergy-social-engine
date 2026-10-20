import os
import hashlib
import sys
import time
import json
import hmac
import mimetypes
import threading
import subprocess
import traceback
import uuid
import urllib.request
import tempfile
from urllib.parse import urlparse, parse_qs, urlencode
import schedule
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone, timedelta
import glob
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import run_engine
import generate_posts
import inventory_db
import intelligence_packages
from build_monthly_content import build_monthly_calendar, latest_monthly_calendar, prepare_monthly_gemini_prompts
from content_operations import apply_growth_schedule_to_ready_inventory, classify_backlog, daily_status, enqueue_durable_package, evaluate_outbox_readiness, hold_outbox, init_content_operations, publishing_metrics, recent_outbox_activity, reconcile_confirmed_transactions, reconcile_ready_inventory, reconcile_stale_claims, release_outbox, today_schedule, why_not_published
from campaign_runtime import eligible_channels_for_slot, load_channel_schedule, load_funnel_config, stage_for_slot
from social.gemini_budget import budget_snapshot
from social_visuals import review_rendered_visual
from runtime_config import load_runtime_config, validate_startup_config

RUN_LOCK = threading.Lock()
RUN_START_LOCK = threading.Lock()
DEFERRED_RUN_LOCK = threading.Lock()
LAST_RUN = {
    "status": "idle",
    "slot": None,
    "started_at_utc": None,
    "finished_at_utc": None,
    "error": None,
}
LAST_DISPATCH = {
    "status": "idle",
    "trigger": None,
    "started_at_utc": None,
    "finished_at_utc": None,
    "exit_code": None,
    "result": None,
    "error": None,
}
STARTED_AT = datetime.now(timezone.utc)
VISUAL_REPO_BOOTSTRAP = {
    "status": "not_run",
    "time_utc": None,
    "summary": {},
    "error": None,
}
MONTHLY_SUMMARY_KEYS = (
    "status", "created_at_utc", "knowledge_id", "knowledge_version",
    "knowledge_digest", "campaign_id", "editorial_standard", "knowledge_refresh", "start_date", "end_date", "days", "coverage_days", "queued",
    "skipped_existing", "cancelled_legacy_outbox", "single_image_posts", "carousel_posts", "product_posts",
    "statement_graphics", "current_event_posts", "superhero_posts", "micro_mission_posts", "historical_mission_posts", "content_plan", "calendar_path",
)
CUSTOM_POST_LOCK = threading.Lock()
MONTHLY_GENERATION_LOCK = threading.Lock()


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))


def _monthly_generation_job_path() -> str:
    return os.path.join(_data_dir(), "social", "monthly_generation_job.json")


def _deferred_run_path() -> str:
    return os.path.join(_data_dir(), "social", "deferred_run.json")


def _deferred_run_status() -> dict:
    return _load_json(_deferred_run_path(), {"status": "IDLE"})


def _save_deferred_run_status(**updates) -> dict:
    current = _deferred_run_status()
    current.update(updates)
    current["updated_at_utc"] = _utc_now()
    _safe_write_json(_deferred_run_path(), current)
    return current


def _gemini_workflow_availability() -> dict:
    snapshot = budget_snapshot()
    required_images = max(0, int(os.environ.get("GEMINI_SOCIAL_WORKFLOW_IMAGE_CALLS", "2")))
    required_total = max(required_images, int(os.environ.get("GEMINI_SOCIAL_WORKFLOW_TOTAL_CALLS", "6")))
    reasons = []
    if snapshot["image"]["remaining"] < required_images:
        reasons.append("GEMINI_IMAGE_BUDGET_EXHAUSTED")
    if snapshot["total"]["remaining"] < required_total:
        reasons.append("GEMINI_TOTAL_BUDGET_EXHAUSTED")
    return {
        "available": not reasons,
        "reasons": reasons,
        "required": {"image_calls": required_images, "total_calls": required_total},
        "eligible_at_utc": snapshot["next_reset_at_utc"] if reasons else _utc_now(),
        "budget": snapshot,
    }


def _monthly_generation_status() -> dict:
    return _load_json(_monthly_generation_job_path(), {"status": "IDLE", "phase": "IDLE"})


def _save_monthly_generation_status(**updates) -> dict:
    current = _monthly_generation_status()
    current.update(updates)
    current["updated_at_utc"] = _utc_now()
    _safe_write_json(_monthly_generation_job_path(), current)
    return current


def _pregenerate_one_package() -> dict:
    env = os.environ.copy()
    env.update({"DATA_DIR": _data_dir(), "OUTBOX_ACTION": "pregenerate"})
    completed = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "scripts", "dispatch_outbox.py")],
        cwd=os.path.dirname(__file__), capture_output=True, text=True, env=env,
        timeout=int(os.environ.get("GEMINI_PREGEN_PROCESS_TIMEOUT_SEC", "600")), check=False,
    )
    output = (completed.stdout or "").strip().splitlines()
    if completed.returncode != 0 or not output:
        detail = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()[-2000:]
        return {"status": "RETRYABLE_FAILURE", "error": detail or f"pregeneration_exit_{completed.returncode}"}
    try:
        return json.loads(output[-1])
    except json.JSONDecodeError:
        return {"status": "RETRYABLE_FAILURE", "error": f"invalid_pregeneration_output:{output[-1][-500:]}"}


def run_manual_monthly_generation(job_id: str, *, days: int, start_date: str | None, replace_unpublished: bool, content_plan: str | None = None) -> None:
    if not MONTHLY_GENERATION_LOCK.acquire(blocking=False):
        _save_monthly_generation_status(job_id=job_id, status="DEFERRED", phase="WAITING", error="monthly_generation_already_running")
        return
    try:
        _save_monthly_generation_status(
            job_id=job_id, status="RUNNING", phase="BUILDING", days=days, start_date=start_date,
            replace_unpublished=replace_unpublished, content_plan=content_plan, pregenerated_packages=0, error=None, started_at_utc=_utc_now(),
        )
        calendar = build_monthly_calendar(
            data_dir=_data_dir(), start_date=start_date, days=days, enqueue=True,
            replace_unpublished=replace_unpublished, content_plan=content_plan,
        )
        _save_monthly_generation_status(
            phase="PREPARING", queued=int(calendar.get("queued") or 0), calendar_path=calendar.get("calendar_path"),
            single_image_posts=int(calendar.get("single_image_posts") or 0), carousel_posts=int(calendar.get("carousel_posts") or 0),
            product_posts=int(calendar.get("product_posts") or 0), current_event_posts=int(calendar.get("current_event_posts") or 0),
            superhero_posts=int(calendar.get("superhero_posts") or 0), micro_mission_posts=int(calendar.get("micro_mission_posts") or 0),
            historical_mission_posts=int(calendar.get("historical_mission_posts") or 0),
        )
        prepared = prepare_monthly_gemini_prompts(_data_dir())
        _save_monthly_generation_status(
            phase="PREGENERATING", prepared_entries=int(prepared.get("prepared_entries") or 0),
            prepared_prompts=int(prepared.get("prepared_prompts") or 0),
        )
        if int(calendar.get("queued") or 0) == 0:
            _save_monthly_generation_status(
                status="COMPLETE", phase="COMPLETE", pregenerated_packages=0,
                completed_at_utc=_utc_now(), error=None,
            )
            return
        pregenerated = 0
        queued = int(calendar.get("queued") or 0)
        package_limit = max(1, min(int(os.environ.get("GEMINI_MONTHLY_PREGEN_PACKAGE_LIMIT", "2")), 31))
        while pregenerated < package_limit:
            result = _pregenerate_one_package()
            result_status = str(result.get("status") or "")
            if result_status == "PREGENERATED":
                pregenerated += 1
                _save_monthly_generation_status(
                    phase="PREGENERATING", pregenerated_packages=pregenerated,
                    last_outbox_id=result.get("outbox_id"),
                )
                if pregenerated >= queued:
                    _save_monthly_generation_status(
                        status="COMPLETE", phase="COMPLETE", pregenerated_packages=pregenerated,
                        completed_at_utc=_utc_now(), error=None,
                    )
                    return
                continue
            if result_status == "IDLE":
                _save_monthly_generation_status(
                    status="COMPLETE", phase="COMPLETE", pregenerated_packages=pregenerated,
                    completed_at_utc=_utc_now(), error=None,
                )
                return
            _save_monthly_generation_status(
                status="RETRYABLE_FAILURE", phase="PREGENERATING", pregenerated_packages=pregenerated,
                error=str(result.get("error") or result.get("detail") or "pregeneration_failed"),
                failed_outbox_id=result.get("outbox_id"),
            )
            return
        _save_monthly_generation_status(
            status="PAUSED_BUDGET", phase="PREGENERATING", pregenerated_packages=pregenerated,
            error=f"monthly pregeneration paused after the configured {package_limit}-package limit",
        )
    except Exception as exc:
        _save_monthly_generation_status(
            status="RETRYABLE_FAILURE", phase="FAILED", error=f"{type(exc).__name__}:{exc}",
        )
    finally:
        MONTHLY_GENERATION_LOCK.release()


def _start_manual_monthly_generation(*, days: int, start_date: str | None, replace_unpublished: bool, content_plan: str | None = None) -> tuple[bool, dict]:
    current = _monthly_generation_status()
    if MONTHLY_GENERATION_LOCK.locked() or current.get("status") in {"ACCEPTED", "RUNNING"}:
        return False, current
    job_id = str(uuid.uuid4())
    pending = _save_monthly_generation_status(
        job_id=job_id, status="ACCEPTED", phase="QUEUED", days=days, start_date=start_date,
        replace_unpublished=replace_unpublished, content_plan=content_plan, pregenerated_packages=0, error=None, accepted_at_utc=_utc_now(),
    )
    threading.Thread(
        target=run_manual_monthly_generation,
        kwargs={"job_id": job_id, "days": days, "start_date": start_date, "replace_unpublished": replace_unpublished, "content_plan": content_plan},
        daemon=True, name=f"monthly-generation-{job_id[:8]}",
    ).start()
    return True, pending


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _meta_state_path() -> str:
    return os.path.join(_data_dir(), "marketing", "meta_token_state.json")


def _safe_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _custom_post_history_path() -> str:
    return os.path.join(_data_dir(), "custom_post_history.json")


def _valid_scored_story_urls(image_urls: list[str]) -> bool:
    return 2 <= len(image_urls) <= 30 and all(item.startswith("https://") for item in image_urls)


def _compose_scored_story(payload: dict) -> tuple[int, dict]:
    from social.reels import build_scored_story_plan, render_scored_story_reel, technical_qa

    image_urls = payload.get("image_urls") if isinstance(payload.get("image_urls"), list) else []
    image_urls = [str(item).strip() for item in image_urls if str(item).strip()]
    if not _valid_scored_story_urls(image_urls):
        return 400, {"error": "image_urls must contain 2 to 30 public HTTPS URLs"}
    if bool(payload.get("auto_narration", False)):
        return 400, {"error": "carousel_story_narration_is_disabled"}
    visual_readings = payload.get("visual_readings") if isinstance(payload.get("visual_readings"), list) else []
    if len(visual_readings) != len(image_urls):
        return 400, {"error": "visual_readings must contain one reading for every image URL"}
    media_dir = os.path.join(_data_dir(), "public_media")
    os.makedirs(media_dir, exist_ok=True)
    request_id = uuid.uuid4().hex
    assets = []
    try:
        for index, url in enumerate(image_urls):
            path = os.path.join(media_dir, f"studio_{request_id}_slide_{index + 1}.png")
            request = urllib.request.Request(url, headers={"User-Agent": "Infenergy-Scored-Story/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response, open(path, "wb") as destination:
                data = response.read(15 * 1024 * 1024 + 1)
                if len(data) > 15 * 1024 * 1024:
                    raise ValueError("slide exceeds 15 MB")
                destination.write(data)
            assets.append({"local_path": path, "public_url": url})
        music = payload.get("music") if isinstance(payload.get("music"), dict) else {}
        music_url = str(music.get("url") or "").strip()
        music_path = ""
        if music_url:
            if not music_url.startswith("https://"):
                raise ValueError("scored_story_music_requires_public_https_url")
            music_path = os.path.join(media_dir, f"studio_{request_id}_music.mp3")
            request = urllib.request.Request(music_url, headers={"User-Agent": "Infenergy-Scored-Story/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response, open(music_path, "wb") as destination:
                data = response.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError("music exceeds 20 MB")
                expected_sha256 = str(music.get("sha256") or "").strip().lower()
                if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
                    raise ValueError("scored_story_music_sha256_mismatch")
                destination.write(data)
        plan = build_scored_story_plan(
            post_id=str(payload.get("post_id") or f"studio-{request_id}"), carousel_assets=assets,
            slide_texts=[str(item) for item in payload.get("slide_texts", [])],
            emotions=[str(item) for item in payload.get("emotions", [])],
            motion_intensity=float(payload.get("motion_intensity", 0.55)),
            auto_narration=False,
            visual_readings=visual_readings,
        )
        if music_path:
            plan["music_path"] = music_path
            plan["music_score"].update({
                "source": "licensed_stock_music",
                "title": str(music.get("title") or "").strip(),
                "artist": str(music.get("artist") or "").strip(),
                "source_url": music_url,
                "source_page": str(music.get("source_page") or "").strip(),
                "license_url": str(music.get("license_url") or "").strip(),
                "sha256": str(music.get("sha256") or "").strip().lower(),
            })
        artifact = render_scored_story_reel(plan, data_dir=_data_dir())
        qa = technical_qa(artifact, plan)
        if qa["status"] != "PASS":
            return 422, {"error": "scored_story_technical_qa_failed", "technical_qa": qa}
        return 200, {"status": "RENDERED", "instagram_reel": artifact, "plan": plan, "technical_qa": qa}
    except Exception as exc:
        return 500, {"error": f"{type(exc).__name__}: {exc}"}


def _publish_custom_post(payload: dict) -> tuple[int, dict]:
    external_id = str(payload.get("external_id", "")).strip()
    caption = str(payload.get("caption", "")).strip()
    platform_captions = payload.get("platform_captions", {})
    image_url = str(payload.get("image_url", "")).strip()
    image_urls = payload.get("image_urls") if isinstance(payload.get("image_urls"), list) else []
    image_urls = list(dict.fromkeys(str(item).strip() for item in image_urls if str(item).strip()))
    reel = payload.get("reel") if isinstance(payload.get("reel"), dict) else {}
    reel_video_url = str(reel.get("video_url", "")).strip()
    reel_cover_url = str(reel.get("cover_url", "")).strip()
    instagram_story = bool(payload.get("instagram_story", False))
    source_system = str(payload.get("source_system", "")).strip().lower()
    iis_creative_id = str(payload.get("iis_creative_id", "")).strip()
    platforms = payload.get("platforms") if isinstance(payload.get("platforms"), list) else []
    platforms = list(dict.fromkeys(str(item).strip().lower() for item in platforms))
    allowed = {"facebook", "instagram", "linkedin", "youtube", "tiktok"}
    if not external_id or len(external_id) > 100:
        return 400, {"error": "external_id is required and must be at most 100 characters"}
    if not caption or len(caption) > 5000:
        return 400, {"error": "caption is required and must be at most 5000 characters"}
    if not isinstance(platform_captions, dict) or any(
        key not in allowed or not isinstance(value, str) or len(value.strip()) > 5000
        for key, value in platform_captions.items()
    ):
        return 400, {"error": "platform_captions must contain only supported platform strings up to 5000 characters"}
    if not image_url.startswith("https://"):
        return 400, {"error": "image_url must be a public HTTPS URL"}
    if image_urls and (not 2 <= len(image_urls) <= 10 or any(not item.startswith("https://") for item in image_urls)):
        return 400, {"error": "image_urls must contain 2 to 10 public HTTPS URLs"}
    if reel and (not reel_video_url.startswith("https://") or not reel_cover_url.startswith("https://")):
        return 400, {"error": "reel video_url and cover_url must be public HTTPS URLs"}
    if instagram_story and ("instagram" not in platforms or not reel):
        return 400, {"error": "instagram_story requires the instagram platform and a rendered Reel video"}
    if source_system == "iis" and not iis_creative_id:
        return 400, {"error": "iis_creative_id is required for IIS content"}
    if not platforms or any(item not in allowed for item in platforms):
        return 400, {"error": "platforms must contain facebook, instagram, linkedin, youtube, or tiktok"}
    if any(platform in {"youtube", "tiktok"} for platform in platforms) and not reel:
        return 400, {"error": "youtube and tiktok require a rendered Reel video"}

    dry_run = not bool(payload.get("live", False))
    if not dry_run:
        provenance_issues = _verify_iis_publish_package(source_system, iis_creative_id, image_url, image_urls, reel_video_url, reel_cover_url)
        if provenance_issues:
            return 422, {"status": "quality_repair_required", "error": "iis_publish_package_verification_failed", "issues": provenance_issues}
        preflight_issues = _custom_post_artifact_preflight(image_url, image_urls, reel_video_url, reel_cover_url, source_system)
        if preflight_issues:
            return 422, {"status": "quality_repair_required", "error": "custom_post_artifact_preflight_failed", "issues": preflight_issues}
    with CUSTOM_POST_LOCK:
        history = _load_json(_custom_post_history_path(), {})
        if not isinstance(history, dict):
            history = {}
        record = history.get(external_id)
        if not isinstance(record, dict):
            record = {"external_id": external_id, "created_at_utc": _utc_now(), "platforms": {}}
            history[external_id] = record
        results = record.setdefault("platforms", {})
        facebook_caption = str(platform_captions.get("facebook", "")).strip() or caption
        instagram_caption = str(platform_captions.get("instagram", "")).strip() or caption
        linkedin_caption = str(platform_captions.get("linkedin", "")).strip() or caption
        youtube_caption = str(platform_captions.get("youtube", "")).strip() or caption
        tiktok_caption = str(platform_captions.get("tiktok", "")).strip() or caption
        content = {
            "fb_caption": facebook_caption,
            "ig_caption": instagram_caption,
            "li_text": linkedin_caption,
            "youtube_caption": youtube_caption,
            "tiktok_caption": tiktok_caption,
            "wp_title": caption[:180],
            "topic": "IIS scheduled post",
            "primary_publish_image_url": image_url,
            "owner_supplied_visual": source_system == "iis",
            "source_system": source_system or "custom",
            "source_creative_id": iis_creative_id or None,
            "carousel_assets": [
                {"public_url": item, "render_engine": "owner"}
                for item in image_urls
            ],
            "platform_posts": {
                "facebook": {"final_caption": facebook_caption},
                "instagram": {"final_caption": instagram_caption},
                "linkedin": {"final_text": linkedin_caption},
                "youtube": {"final_caption": youtube_caption, "media_type": "VIDEO"},
                "tiktok": {"final_caption": tiktok_caption, "media_type": "VIDEO"},
            },
        }
        if reel:
            content["instagram_reel"] = {"public_urls": {"video": reel_video_url, "cover": reel_cover_url}}
            content["publish_instagram_story"] = instagram_story
            content["platform_posts"]["instagram"]["media_type"] = "REEL"
            content["platform_posts"]["facebook"]["media_type"] = "REEL"
        content["routing"] = {"platforms": platforms}
        content["unique_creative_required"] = True
        if not dry_run:
            unsupported = [platform for platform in platforms if platform not in {"facebook", "instagram", "linkedin"}]
            if unsupported:
                return 422, {"status": "blocked_configuration", "error": "durable_dispatch_platform_not_supported", "platforms": unsupported}
            outbox_id = enqueue_durable_package(
                _data_dir(),
                source_id=f"custom:{external_id}",
                package=content,
                actor="worker.custom_post",
            )
            record.update({"status": "queued", "outbox_id": outbox_id, "updated_at_utc": _utc_now()})
            _safe_write_json(_custom_post_history_path(), history)
            return 202, {
                "status": "queued",
                "external_id": external_id,
                "outbox_id": outbox_id,
                "diagnostics_url": f"/why-not-published?outbox_id={outbox_id}",
                "time_utc": _utc_now(),
            }
        for platform in platforms:
            previous = results.get(platform)
            if isinstance(previous, dict) and previous.get("status") == "processing":
                try:
                    from platform_publishing import get_status
                    platform_id = str((previous.get("result") or {}).get("id") or "").strip()
                    status_result = get_status(platform, platform_id)
                    status = str(status_result.get("status") or "UNKNOWN").upper()
                    if status in {"SUCCEEDED", "PUBLISH_COMPLETE", "PUBLISHED"}:
                        previous = {**previous, "status": "published", "status_result": status_result, "published_at_utc": _utc_now()}
                        results[platform] = previous
                    elif status in {"FAILED", "PROCESSING_FAILED", "REJECTED", "NOT_FOUND"}:
                        previous = {**previous, "status": "failed", "status_result": status_result, "error": str(status_result.get("fail_reason") or status), "updated_at_utc": _utc_now()}
                        results[platform] = previous
                    else:
                        previous["status_result"] = status_result
                        previous["updated_at_utc"] = _utc_now()
                        results[platform] = previous
                        _safe_write_json(_custom_post_history_path(), history)
                        continue
                except Exception as exc:
                    previous["status_error"] = str(exc)
                    previous["updated_at_utc"] = _utc_now()
                    results[platform] = previous
                    _safe_write_json(_custom_post_history_path(), history)
                    continue
            if isinstance(previous, dict) and previous.get("status") == "published":
                previous_result = previous.get("result") if isinstance(previous.get("result"), dict) else {}
                if str(previous_result.get("id") or "").strip().lower() == "skipped":
                    previous = {
                        "status": "failed",
                        "error": str(previous_result.get("reason") or "legacy skipped publisher result"),
                        "updated_at_utc": _utc_now(),
                    }
                    results[platform] = previous
            if isinstance(previous, dict) and previous.get("status") == "published":
                continue
            try:
                from platform_publishing import publish as publish_platform
                result = publish_platform(platform, content, dry_run=dry_run)
                if not isinstance(result, dict) or str(result.get("id") or "").strip().lower() == "skipped":
                    reason = str(result.get("reason") or "publisher did not confirm publication") if isinstance(result, dict) else "publisher returned an invalid result"
                    raise RuntimeError(reason)
                processing = str(result.get("status") or "").upper() in {"PROCESSING", "PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"}
                results[platform] = {
                    "status": "processing" if processing else "published", "result": result,
                    "published_at_utc": None if processing else _utc_now(), "updated_at_utc": _utc_now(), "dry_run": dry_run,
                }
            except Exception as exc:
                results[platform] = {"status": "failed", "error": str(exc), "updated_at_utc": _utc_now()}
            record["updated_at_utc"] = _utc_now()
            _safe_write_json(_custom_post_history_path(), history)
        failed = [platform for platform in platforms if results.get(platform, {}).get("status") == "failed"]
        processing = [platform for platform in platforms if results.get(platform, {}).get("status") == "processing"]
        response_status = "partial_failure" if failed else "processing" if processing else "published"
        response = {"status": response_status, "external_id": external_id, "platforms": results, "failed_platforms": failed, "processing_platforms": processing, "time_utc": _utc_now()}
        return (502 if failed else 202 if processing else 200), response


def _asset_url_identity(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    return f"{parsed.netloc.lower()}{parsed.path}"


def _approved_iis_image_identities(package: dict, studio_url: str) -> set[str]:
    delivery = package.get("delivery") if isinstance(package.get("delivery"), dict) else {}
    approved_images = {
        _asset_url_identity(item.get("url"))
        for item in delivery.get("carousel", []) or []
        if isinstance(item, dict)
    }
    image = package.get("image") if isinstance(package.get("image"), dict) else {}
    image_url = str(image.get("url") or "").strip()
    image_id = str(image.get("id") or "").strip()
    if image_url:
        approved_images.add(_asset_url_identity(image_url))
    if image_id:
        approved_images.add(_asset_url_identity(f"{studio_url}/api/assets/{image_id}"))
    return approved_images


def _verify_iis_publish_package(source_system: str, creative_id: str, image_url: str, image_urls: list[str], reel_video_url: str, reel_cover_url: str) -> list[str]:
    if source_system != "iis":
        return []
    studio_url = os.environ.get("IIS_STUDIO_URL", "https://infenergy-image-studio-production.up.railway.app").rstrip("/")
    try:
        response = requests.get(f"{studio_url}/api/export/{creative_id}", timeout=45)
        response.raise_for_status()
        package = response.json()
    except Exception as exc:
        return [f"approved_iis_package_unavailable:{type(exc).__name__}"]
    if str((package.get("creative") or {}).get("id") or "") != creative_id or str((package.get("qa") or {}).get("status") or "").upper() != "PASS":
        return ["iis_package_not_current_and_approved"]
    delivery = package.get("delivery") if isinstance(package.get("delivery"), dict) else {}
    approved_images = _approved_iis_image_identities(package, studio_url)
    submitted_images = {_asset_url_identity(url) for url in [image_url, *image_urls] if url}
    issues = []
    if not submitted_images or not submitted_images.issubset(approved_images):
        issues.append("submitted_images_not_in_approved_iis_package")
    approved_reel = delivery.get("reel") if isinstance(delivery.get("reel"), dict) else {}
    if reel_video_url and _asset_url_identity(reel_video_url) != _asset_url_identity(approved_reel.get("videoUrl")):
        issues.append("submitted_reel_not_in_approved_iis_package")
    if reel_cover_url and _asset_url_identity(reel_cover_url) != _asset_url_identity(approved_reel.get("coverUrl")):
        issues.append("submitted_reel_cover_not_in_approved_iis_package")
    return issues


def _custom_post_artifact_preflight(image_url: str, image_urls: list[str], reel_video_url: str, reel_cover_url: str, source_system: str = "") -> list[str]:
    issues = []
    carousel_profile = "iis_carousel" if source_system == "iis" else "custom_post"
    visual_urls = [(url, carousel_profile) for url in dict.fromkeys([image_url, *image_urls]) if url]
    if reel_cover_url:
        visual_urls.append((reel_cover_url, "iis_reel_cover" if source_system == "iis" else "custom_post"))
    with tempfile.TemporaryDirectory(prefix="custom-post-preflight-") as temp_dir:
        for index, (url, review_profile) in enumerate(visual_urls):
            try:
                response = requests.get(url, timeout=45, stream=True)
                response.raise_for_status()
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if "image" not in content_type and "octet-stream" not in content_type:
                    issues.append(f"visual_{index + 1}_invalid_content_type")
                    continue
                path = os.path.join(temp_dir, f"visual-{index + 1}.img")
                total = 0
                with open(path, "wb") as artifact:
                    for chunk in response.iter_content(1024 * 256):
                        total += len(chunk)
                        if total > 30 * 1024 * 1024:
                            raise RuntimeError("artifact_exceeds_30mb")
                        artifact.write(chunk)
                if total < 1024:
                    issues.append(f"visual_{index + 1}_empty_or_truncated")
                    continue
                review = review_rendered_visual(path, review_profile)
                if review.get("verdict") != "PASS":
                    details = ",".join(str(item) for item in review.get("issues", [])) or "visual_review_not_passed"
                    issues.append(f"visual_{index + 1}_{details}")
            except Exception as exc:
                issues.append(f"visual_{index + 1}_unavailable:{type(exc).__name__}")
    if reel_video_url:
        try:
            response = requests.head(reel_video_url, allow_redirects=True, timeout=30)
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if not response.ok or ("video" not in content_type and "octet-stream" not in content_type):
                issues.append("reel_video_unreachable_or_invalid")
        except Exception as exc:
            issues.append(f"reel_video_unavailable:{type(exc).__name__}")
    return issues


def _mask_token(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if len(token) <= 12:
        return "********"
    return f"{token[:6]}...{token[-6:]}"


def _load_meta_runtime_from_state() -> tuple[bool, str]:
    path = _meta_state_path()
    if not os.path.exists(path):
        return False, "state_file_not_found"
    try:
        data = _load_json(path, {})
        if not isinstance(data, dict):
            return False, "state_file_invalid"

        keys = [
            "META_LONG_LIVED_USER_TOKEN",
            "META_PAGE_ACCESS_TOKEN",
            "META_PAGE_ID",
            "META_IG_USER_ID",
            "META_TOKEN_EXPIRES_AT_UTC",
            "META_TOKEN_UPDATED_AT_UTC",
        ]
        loaded = 0
        for key in keys:
            value = str(data.get(key, "")).strip()
            if value:
                os.environ[key] = value
                loaded += 1
        if loaded == 0:
            return False, "state_file_has_no_runtime_keys"
        return True, f"loaded_{loaded}_keys"
    except Exception as e:
        return False, f"state_file_load_error:{e}"


def _meta_refresh_secret() -> str:
    return str(os.environ.get("META_REFRESH_TOKEN", "") or os.environ.get("MANUAL_RUN_TOKEN", "")).strip()


def _refresh_meta_tokens() -> tuple[bool, dict]:
    required = {
        "META_APP_ID": str(os.environ.get("META_APP_ID", "")).strip(),
        "META_APP_SECRET": str(os.environ.get("META_APP_SECRET", "")).strip(),
        "META_LONG_LIVED_USER_TOKEN": str(os.environ.get("META_LONG_LIVED_USER_TOKEN", "")).strip(),
        "META_PAGE_ID": str(os.environ.get("META_PAGE_ID", "")).strip(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        return False, {
            "ok": False,
            "error": "missing_required_env",
            "missing": missing,
        }

    app_id = required["META_APP_ID"]
    app_secret = required["META_APP_SECRET"]
    current_user_token = required["META_LONG_LIVED_USER_TOKEN"]
    page_id = required["META_PAGE_ID"]
    graph_version = str(os.environ.get("META_GRAPH_VERSION", "v20.0")).strip() or "v20.0"
    graph_base = f"https://graph.facebook.com/{graph_version}"

    try:
        exchange = requests.get(
            f"{graph_base}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": current_user_token,
            },
            timeout=30,
        )
        if not exchange.ok:
            return False, {
                "ok": False,
                "error": "exchange_failed",
                "status_code": exchange.status_code,
                "response": exchange.text[:1000],
            }
        exchange_payload = exchange.json() if exchange.content else {}
        new_user_token = str(exchange_payload.get("access_token", "")).strip()
        expires_in = int(exchange_payload.get("expires_in", 0) or 0)
        if not new_user_token:
            return False, {
                "ok": False,
                "error": "exchange_missing_access_token",
            }

        pages = requests.get(
            f"{graph_base}/me/accounts",
            params={
                "fields": "id,name,access_token,tasks,instagram_business_account",
                "access_token": new_user_token,
            },
            timeout=30,
        )
        if not pages.ok:
            return False, {
                "ok": False,
                "error": "me_accounts_failed",
                "status_code": pages.status_code,
                "response": pages.text[:1000],
            }

        rows = (pages.json() or {}).get("data") or []
        page = next((r for r in rows if str(r.get("id", "")).strip() == page_id), None)
        if not page:
            return False, {
                "ok": False,
                "error": "page_not_found",
                "page_id": page_id,
                "managed_pages": [
                    {"id": str(r.get("id", "")), "name": str(r.get("name", ""))}
                    for r in rows
                ],
            }

        new_page_token = str(page.get("access_token", "")).strip()
        if not new_page_token:
            return False, {
                "ok": False,
                "error": "page_token_missing",
                "page_id": page_id,
            }

        resolved_ig_id = ""
        ig_from_page = (page.get("instagram_business_account") or {}).get("id")
        if ig_from_page:
            resolved_ig_id = str(ig_from_page).strip()
        elif os.environ.get("META_IG_USER_ID", "").strip():
            resolved_ig_id = str(os.environ.get("META_IG_USER_ID", "")).strip()

        now_utc = datetime.now(timezone.utc)
        expires_at = ""
        if expires_in > 0:
            expires_at = (now_utc + timedelta(seconds=expires_in)).isoformat()

        os.environ["META_LONG_LIVED_USER_TOKEN"] = new_user_token
        os.environ["META_PAGE_ACCESS_TOKEN"] = new_page_token
        os.environ["META_PAGE_ID"] = page_id
        os.environ["META_TOKEN_UPDATED_AT_UTC"] = now_utc.isoformat()
        if expires_at:
            os.environ["META_TOKEN_EXPIRES_AT_UTC"] = expires_at
        if resolved_ig_id:
            os.environ["META_IG_USER_ID"] = resolved_ig_id

        state_payload = {
            "META_PAGE_ID": page_id,
            "META_IG_USER_ID": resolved_ig_id,
            "META_LONG_LIVED_USER_TOKEN": new_user_token,
            "META_PAGE_ACCESS_TOKEN": new_page_token,
            "META_TOKEN_UPDATED_AT_UTC": now_utc.isoformat(),
            "META_TOKEN_EXPIRES_AT_UTC": expires_at,
            "meta_graph_version": graph_version,
            "updated_at_utc": now_utc.isoformat(),
            "long_user_token_masked": _mask_token(new_user_token),
            "page_token_masked": _mask_token(new_page_token),
        }
        _safe_write_json(_meta_state_path(), state_payload)

        return True, {
            "ok": True,
            "page_id": page_id,
            "ig_user_id": resolved_ig_id,
            "expires_in": expires_in,
            "expires_at_utc": expires_at,
            "updated_at_utc": now_utc.isoformat(),
            "state_file": _meta_state_path(),
            "long_user_token": _mask_token(new_user_token),
            "page_token": _mask_token(new_page_token),
        }
    except Exception as e:
        return False, {
            "ok": False,
            "error": "exception",
            "detail": str(e),
        }


def _auto_refresh_meta_if_due() -> tuple[bool, str]:
    enabled = str(os.environ.get("META_AUTO_REFRESH_ENABLED", "true")).strip().lower()
    if enabled in ("0", "false", "no"):
        return False, "auto_refresh_disabled"

    force_every_run = str(os.environ.get("META_REFRESH_EVERY_RUN", "false")).strip().lower()
    if force_every_run in ("1", "true", "yes"):
        ok, payload = _refresh_meta_tokens()
        return ok, payload.get("error", "refresh_failed") if not ok else "refreshed_force_every_run"

    threshold_hours = int(str(os.environ.get("META_REFRESH_THRESHOLD_HOURS", "72") or "72"))
    expires_raw = str(os.environ.get("META_TOKEN_EXPIRES_AT_UTC", "")).strip()
    if not expires_raw:
        ok, payload = _refresh_meta_tokens()
        return ok, payload.get("error", "refreshed_without_known_expiry") if not ok else "refreshed"

    try:
        expiry = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        if expiry - now_utc > timedelta(hours=threshold_hours):
            return False, "not_due"
    except Exception:
        ok, payload = _refresh_meta_tokens()
        return ok, payload.get("error", "refreshed_on_invalid_expiry") if not ok else "refreshed"

    ok, payload = _refresh_meta_tokens()
    return ok, payload.get("error", "refresh_failed") if not ok else "refreshed"


def _load_history(limit: int = 20) -> list[dict]:
    history_path = os.path.join(_data_dir(), "post_history.json")
    history = _load_json(history_path, {"posts": []})
    posts = history.get("posts", []) if isinstance(history, dict) else []
    if not isinstance(posts, list):
        return []
    return posts[-limit:]


def _latest_file(pattern: str) -> str:
    paths = glob.glob(pattern)
    if not paths:
        return ""
    return max(paths, key=os.path.getmtime)


def _load_latest_campaign_plan() -> dict:
    paths = []
    for base in (_data_dir(), os.path.join(os.path.dirname(__file__), "data")):
        paths.extend(glob.glob(os.path.join(base, "marketing", "campaign_plan_*.json")))
    latest = max(paths, key=os.path.getmtime) if paths else ""
    if not latest:
        return {}
    data = _load_json(latest, {})
    if isinstance(data, dict):
        data["_artifact"] = latest
    return data if isinstance(data, dict) else {}


def _load_latest_structured_campaign() -> dict:
    paths = []
    for base in (_data_dir(), os.path.join(os.path.dirname(__file__), "data")):
        paths.extend(glob.glob(os.path.join(base, "marketing", "campaigns", "campaign_*.json")))
    latest = max(paths, key=os.path.getmtime) if paths else ""
    if not latest:
        return {}
    data = _load_json(latest, {})
    if isinstance(data, dict):
        data["_artifact"] = latest
    return data if isinstance(data, dict) else {}


def _quality_summary(posts: list[dict]) -> dict:
    scores = [p.get("quality_score") for p in posts if isinstance(p.get("quality_score"), (int, float))]
    avg = round(sum(scores) / len(scores), 2) if scores else None
    warning_count = sum(len(p.get("quality_warnings", []) or []) for p in posts if isinstance(p, dict))
    return {
        "samples": len(scores),
        "average_quality_score": avg,
        "quality_warning_count": warning_count,
    }


def _quality_report(posts: list[dict]) -> dict:
    scores = [float(p.get("quality_score")) for p in posts if isinstance(p.get("quality_score"), (int, float))]
    rejected = [p for p in posts if isinstance(p, dict) and str(p.get("status", "")).startswith("skipped_")]

    reason_counts: dict[str, int] = {}
    platform_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}

    for p in posts:
        if not isinstance(p, dict):
            continue
        stage = str(p.get("funnel_stage", "")).strip().upper()
        if stage:
            stage_counts[stage] = stage_counts.get(stage, 0) + 1

        for platform_record in p.get("platform_records", []) or []:
            if not isinstance(platform_record, dict):
                continue
            platform = str(platform_record.get("platform", "")).strip().lower()
            if platform:
                platform_counts[platform] = platform_counts.get(platform, 0) + 1
            err = str(platform_record.get("error") or "").strip()
            if err:
                reason_counts[err] = reason_counts.get(err, 0) + 1

        for r in p.get("duplicate_reasons", []) or []:
            key = str(r).strip()
            if key:
                reason_counts[key] = reason_counts.get(key, 0) + 1

        for r in p.get("validation_errors", []) or []:
            key = str(r).strip()
            if key:
                reason_counts[key] = reason_counts.get(key, 0) + 1

    recurring = sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "sample_size": len(posts),
        "scores": {
            "count": len(scores),
            "average": round(sum(scores) / len(scores), 2) if scores else None,
            "min": min(scores) if scores else None,
            "max": max(scores) if scores else None,
        },
        "rejected_posts": [
            {
                "post_id": p.get("post_id"),
                "date": p.get("date"),
                "slot": p.get("slot"),
                "status": p.get("status"),
                "duplicate_reasons": p.get("duplicate_reasons", []),
                "validation_errors": p.get("validation_errors", []),
            }
            for p in rejected[-50:]
        ],
        "rejection_reasons": [{"reason": k, "count": v} for k, v in recurring[:30]],
        "recurring_generation_problems": [{"problem": k, "count": v} for k, v in recurring[:15]],
        "platform_distribution": platform_counts,
        "funnel_stage_distribution": stage_counts,
    }


def _parse_preview_params(params: dict) -> dict:
    platform = str(params.get("platform", [""])[0]).strip().lower()
    slot = str(params.get("slot", ["morning"])[0]).strip().lower()
    funnel_stage = str(params.get("funnel_stage", [""])[0]).strip().upper()
    product_id = str(params.get("product_id", [""])[0]).strip()
    pipeline = str(params.get("pipeline", [""])[0]).strip().lower()
    pillar = str(params.get("pillar", [""])[0]).strip().lower()
    no_product = str(params.get("no_product", ["false"])[0]).strip().lower() in ("1", "true", "yes")
    if slot not in ("morning", "midday", "evening"):
        slot = "morning"
    if platform and platform not in ("facebook", "instagram", "linkedin", "wordpress"):
        platform = ""
    if funnel_stage and funnel_stage not in ("ATTENTION", "EDUCATION", "DESIRE", "TRUST", "CONVERSION"):
        funnel_stage = ""
    if pipeline not in ("legacy", "orchestrator", "best_of", "both"):
        pipeline = ""
    if pillar not in generate_posts.PILLAR_PRODUCT_MODE:
        pillar = ""
    return {
        "platform": platform,
        "slot": slot,
        "funnel_stage": funnel_stage,
        "product_id": product_id,
        "pipeline": pipeline,
        "pillar": pillar,
        "no_product": no_product,
    }


def _content_preview(preview_params: dict) -> dict:
    previous_text_only = os.environ.get("POST_TEXT_ONLY")
    previous_bucket = os.environ.get("CONTENT_BUCKET_OVERRIDE")
    os.environ["POST_TEXT_ONLY"] = "true"
    if preview_params.get("no_product"):
        os.environ["CONTENT_BUCKET_OVERRIDE"] = "no_product"
    try:
        generation_kwargs = {
            "funnel_stage_override": str(preview_params.get("funnel_stage", "")),
            "product_id_override": str(preview_params.get("product_id", "")),
            "pipeline_override": str(preview_params.get("pipeline", "")),
            "pillar_override": str(preview_params.get("pillar", "")),
        }
        if preview_params.get("no_product"):
            generation_kwargs["no_product"] = True
        content = generate_posts.generate(preview_params["slot"], **generation_kwargs)
    finally:
        if previous_text_only is None:
            os.environ.pop("POST_TEXT_ONLY", None)
        else:
            os.environ["POST_TEXT_ONLY"] = previous_text_only
        if previous_bucket is None:
            os.environ.pop("CONTENT_BUCKET_OVERRIDE", None)
        else:
            os.environ["CONTENT_BUCKET_OVERRIDE"] = previous_bucket
    platform = preview_params.get("platform", "")
    requested_stage = preview_params.get("funnel_stage", "")
    requested_product_id = preview_params.get("product_id", "")
    notes: list[str] = []

    if requested_stage:
        if str(content.get("funnel_stage", "")).upper() == requested_stage:
            notes.append("funnel_stage_override_applied")
        else:
            notes.append("funnel_stage_override_not_applied")

    if requested_product_id:
        matched = str(content.get("product_id", "")) == requested_product_id
        notes.append("requested_product_matched" if matched else "requested_product_not_matched")

    if platform:
        platform_posts = content.get("platform_posts", {})
        if isinstance(platform_posts, dict):
            selected = platform_posts.get(platform)
            content["platform_posts"] = {platform: selected} if isinstance(selected, dict) else {}

    content["preview_only"] = True
    content["preview_filters"] = preview_params
    content["preview_notes"] = notes
    return content


def _schedule_preview(days: int = 7) -> list[dict]:
    history = _load_json(os.path.join(_data_dir(), "post_history.json"), {"posts": []})
    schedule = load_channel_schedule()
    funnel_config = load_funnel_config()
    now = datetime.now(timezone.utc)
    out: list[dict] = []

    for offset in range(days):
        when = now + timedelta(days=offset)
        day_entry = {
            "date": when.date().isoformat(),
            "weekday": when.strftime("%A").lower(),
            "slots": {},
        }
        for slot in ("morning", "midday", "evening"):
            stage = stage_for_slot(slot, history=history, funnel_config=funnel_config, schedule=schedule, now_utc=when)
            eligibility = eligible_channels_for_slot(
                slot=slot,
                funnel_stage=stage,
                schedule=schedule,
                now_utc=when,
                manual_platforms=[],
            )
            day_entry["slots"][slot] = {
                "funnel_stage": stage,
                "eligible_channels": {
                    name: {"eligible": bool(values[0]), "reason": str(values[1])}
                    for name, values in eligibility.items()
                },
            }
        out.append(day_entry)
    return out


def _authorized(params: dict) -> tuple[bool, int, dict]:
    token = os.environ.get("MANUAL_RUN_TOKEN", "")
    provided = str(params.get("token", [""])[0])
    if not token:
        return False, 403, {"error": "MANUAL_RUN_TOKEN not configured"}
    if not hmac.compare_digest(provided, token):
        return False, 401, {"error": "invalid token"}
    return True, 200, {}


def _os_authorized(handler: BaseHTTPRequestHandler, params: dict) -> tuple[bool, int, dict]:
    """Authorize new OS APIs with a bearer token; query token remains a compatibility fallback."""
    valid_tokens = [
        token
        for token in (
            os.environ.get("INTELLIGENCE_OS_TOKEN", "").strip(),
            os.environ.get("MANUAL_RUN_TOKEN", "").strip(),
        )
        if token
    ]
    authorization = str(handler.headers.get("Authorization", "") if hasattr(handler, "headers") else "")
    provided = authorization[7:].strip() if authorization.lower().startswith("bearer ") else str(params.get("token", [""])[0])
    if not valid_tokens:
        return False, 403, {"error": "INTELLIGENCE_OS_TOKEN or MANUAL_RUN_TOKEN not configured"}
    if not any(hmac.compare_digest(provided, token) for token in valid_tokens):
        return False, 401, {"error": "invalid token"}
    return True, 200, {}


def _uptime_seconds() -> int:
    return int((datetime.now(timezone.utc) - STARTED_AT).total_seconds())


def _last_run_outcome() -> dict:
    return _load_json(os.path.join(_data_dir(), "social", "last_run_outcome.json"), {})


def _candidate_pool_depth() -> int:
    payload = _load_json(os.path.join(_data_dir(), "social", "candidate_pool.json"), {})
    candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
    return sum(1 for candidate in candidates if isinstance(candidate, dict) and candidate.get("status") == "available")


def _operational_intelligence_snapshot() -> dict:
    """Expose only operational facts with a local source; never infer provider budgets."""
    try:
        from social.living_intelligence import operational_status
        living = operational_status(_data_dir())
        state = _load_json(os.path.join(_data_dir(), "social", "living_intelligence.json"), {})
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": type(exc).__name__}
    target_depth = int(os.environ.get("CANDIDATE_POOL_TARGET_DEPTH", "6"))
    token_expiry = str(os.environ.get("META_TOKEN_EXPIRES_AT_UTC", "") or os.environ.get("META_TOKEN_EXPIRES_AT", "")).strip()
    token_status = {"status": "UNAVAILABLE", "value": None}
    if token_expiry:
        try:
            expiry = datetime.fromisoformat(token_expiry.replace("Z", "+00:00"))
            expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
            days_remaining = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
            token_status = {
                "status": "EXPIRED" if days_remaining < 0 else "WARNING_RENEW_WITHIN_7_DAYS" if days_remaining <= 7 else "HEALTHY",
                "value": token_expiry,
                "days_remaining": round(days_remaining, 2),
            }
        except ValueError:
            token_status = {"status": "INVALID_TIMESTAMP", "value": token_expiry}
    return {
        "status": "READY",
        "pool": {"available": _candidate_pool_depth(), "target": target_depth, "refill_needed": _candidate_pool_depth() < target_depth},
        "seasonal_lookahead": state.get("seasonal_lookahead", []),
        "visual_novelty": state.get("visual_novelty", {}),
        "exploration": state.get("exploration", {}),
        "token_expiry": token_status,
        "gemini": {"configured": bool(os.environ.get("GEMINI_API_KEY", "").strip()), "budget_status": "UNAVAILABLE_NO_SUPPORTED_BILLING_SOURCE"},
        "living": living,
    }


def _env_is_true(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _production_readiness_snapshot() -> dict:
    data_dir = _data_dir()
    channel_flags = run_engine.get_channel_config()
    overrides = {
        name: str(os.environ.get(name, "")).strip()
        for name in (
            "POST_PLATFORMS",
            "POST_PRODUCT_ID_OVERRIDE",
            "POST_FUNNEL_STAGE_OVERRIDE",
            "POST_PIPELINE_OVERRIDE",
            "MANUAL_DUPLICATE_MODE",
            "CHANNEL_READINESS_BLOCK_ON_RED",
            "SKIP_RECENT_SUCCESS_HOURS",
        )
    }
    return {
        "time_utc": _utc_now(),
        "global": {
            "dry_run": str(os.environ.get("SOCIAL_DRY_RUN", "true")).lower() == "true",
            "shadow_mode": _env_is_true("SOCIAL_SHADOW_MODE", False),
            "run_lock_active": RUN_LOCK.locked(),
            "scheduler_jobs_registered": len(schedule.jobs),
        },
        "channels": channel_flags,
        "platform_configuration_present": {
            "facebook": bool(os.environ.get("META_PAGE_ID", "").strip() and os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip()),
            "instagram": bool(os.environ.get("META_IG_USER_ID", "").strip() and os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip()),
            "linkedin": bool(os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()),
        },
        "linkedin_target_configuration": {
            "explicit_target_present": bool(os.environ.get("LINKEDIN_ORGANIZATION_URN", "").strip() or os.environ.get("LINKEDIN_AUTHOR_URN", "").strip()),
            "automatic_resolution_available": bool(os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()),
        },
        "gemini": {
            "api_key_present": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
            "text_model": str(os.environ.get("GEMINI_MODEL", "")).strip() or "provider_default",
            "image_model": str(os.environ.get("GEMINI_IMAGE_MODEL", "")).strip() or "gemini-2.5-flash-image",
        },
        "data": {
            "path": data_dir,
            "exists": os.path.isdir(data_dir),
            "writable": os.access(data_dir, os.W_OK),
            "history_present": os.path.isfile(os.path.join(data_dir, "post_history.json")),
            "receipts_present": os.path.isfile(os.path.join(data_dir, "social", "publish_receipts.json")),
        },
        "overrides": overrides,
    }


def _run_script(script_name: str) -> tuple[bool, str]:
    scripts_dir = os.path.join(os.path.dirname(__file__), "scripts")
    script_path = os.path.join(scripts_dir, script_name)
    try:
        runtime_data_dir = _data_dir()
        runtime_marketing_dir = os.path.join(runtime_data_dir, "marketing")
        env = os.environ.copy()
        env.setdefault("DATA_DIR", runtime_data_dir)
        env.setdefault("MARKETING_OUTPUT_DIR", runtime_marketing_dir)
        completed = subprocess.run(
            [sys.executable, script_path],
            cwd=os.path.dirname(__file__),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode == 0:
            return True, output[-3000:]
        return False, output[-3000:]
    except Exception as e:
        return False, str(e)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manual_run_credential(handler: BaseHTTPRequestHandler, params: dict) -> str:
    return str(handler.headers.get("X-Manual-Run-Token", "") or params.get("token", [""])[0])


def _auto_bootstrap_visual_repo() -> dict:
    global VISUAL_REPO_BOOTSTRAP
    try:
        summary = inventory_db.bootstrap_visual_repo_from_env(_data_dir())
        VISUAL_REPO_BOOTSTRAP = {
            "status": "ok",
            "time_utc": _utc_now(),
            "summary": summary,
            "error": None,
        }
    except Exception as e:
        VISUAL_REPO_BOOTSTRAP = {
            "status": "error",
            "time_utc": _utc_now(),
            "summary": {},
            "error": str(e),
        }
    return VISUAL_REPO_BOOTSTRAP


def _start_slot_thread(
    slot: str,
    force_live: bool = False,
    force_dry_run: bool = False,
    shadow_mode: bool = False,
    platforms_override: str = "",
    duplicate_mode: str = "",
    readiness_block_override: str = "",
    product_id_override: str = "",
    no_product: bool = False,
    funnel_stage_override: str = "",
    pipeline_override: str = "",
    deferred_job_id: str = "",
) -> bool:
    if not RUN_START_LOCK.acquire(blocking=False):
        return False

    def execute() -> None:
        try:
            run_slot(
                slot=slot,
                force_live=force_live,
                force_dry_run=force_dry_run,
                shadow_mode=shadow_mode,
                platforms_override=platforms_override,
                duplicate_mode=duplicate_mode,
                readiness_block_override=readiness_block_override,
                product_id_override=product_id_override,
                no_product=no_product,
                funnel_stage_override=funnel_stage_override,
                pipeline_override=pipeline_override,
                deferred_job_id=deferred_job_id,
            )
        finally:
            RUN_START_LOCK.release()

    thread = threading.Thread(
        target=execute,
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        RUN_START_LOCK.release()
        raise
    return True


def _start_or_defer_slot(*, idempotency_key: str = "", **run_kwargs) -> tuple[bool, dict]:
    request = {
        key: run_kwargs[key]
        for key in (
            "slot", "force_live", "force_dry_run", "shadow_mode", "platforms_override",
            "duplicate_mode", "readiness_block_override", "product_id_override", "no_product",
            "funnel_stage_override", "pipeline_override",
        )
    }
    fingerprint = idempotency_key or hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with DEFERRED_RUN_LOCK:
        current = _deferred_run_status()
        active_states = {"WAITING_FOR_BUDGET_RESET", "QUEUED", "RUNNING"}
        if current.get("idempotency_key") == fingerprint and current.get("status") in active_states:
            return True, {**current, "duplicate_request": True}
        if current.get("status") in active_states:
            return False, {**current, "conflict": "another_durable_run_is_active"}

        availability = _gemini_workflow_availability()
        job_id = uuid.uuid4().hex
        status = "QUEUED" if availability["available"] else "WAITING_FOR_BUDGET_RESET"
        durable = _save_deferred_run_status(
            job_id=job_id, idempotency_key=fingerprint, status=status, request=request,
            accepted_at_utc=_utc_now(), eligible_at_utc=availability["eligible_at_utc"],
            budget_requirements=availability["required"], reasons=availability["reasons"], error=None,
        )
        if not availability["available"]:
            return True, durable
        if not _start_slot_thread(**request, deferred_job_id=job_id):
            return False, _save_deferred_run_status(status="WAITING_FOR_WORKER", error="run_lock_busy")
        return True, _save_deferred_run_status(status="RUNNING", started_at_utc=_utc_now())


def resume_deferred_run(*, recover_stale: bool = False) -> dict:
    with DEFERRED_RUN_LOCK:
        current = _deferred_run_status()
        if current.get("status") in {"QUEUED", "RUNNING"}:
            if recover_stale and not RUN_START_LOCK.locked():
                current = _save_deferred_run_status(status="WAITING_FOR_BUDGET_RESET", error="recovered_after_process_restart")
            else:
                return current
        if current.get("status") not in {"WAITING_FOR_BUDGET_RESET", "WAITING_FOR_WORKER"}:
            return current
        availability = _gemini_workflow_availability()
        if not availability["available"]:
            return _save_deferred_run_status(
                status="WAITING_FOR_BUDGET_RESET", eligible_at_utc=availability["eligible_at_utc"],
                budget_requirements=availability["required"], reasons=availability["reasons"],
            )
        if RUN_START_LOCK.locked():
            return _save_deferred_run_status(status="WAITING_FOR_WORKER", error="run_lock_busy")
        request = current.get("request") if isinstance(current.get("request"), dict) else {}
        required_keys = {"slot", "force_live", "force_dry_run", "shadow_mode", "platforms_override", "duplicate_mode", "readiness_block_override", "product_id_override", "no_product", "funnel_stage_override", "pipeline_override"}
        if set(request) != required_keys:
            return _save_deferred_run_status(status="FAILED", error="invalid_deferred_request")
        job_id = str(current.get("job_id") or "")
        _save_deferred_run_status(status="QUEUED", started_at_utc=_utc_now(), reasons=[], error=None)
        if not _start_slot_thread(**request, deferred_job_id=job_id):
            return _save_deferred_run_status(status="WAITING_FOR_WORKER", error="run_lock_busy")
        return _save_deferred_run_status(status="RUNNING", error=None)


class HealthHandler(BaseHTTPRequestHandler):
    @staticmethod
    def _media_etag(stat_result: os.stat_result) -> str:
        return f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'

    def _serve_file(self, path: str, *, head_only: bool = False) -> None:
        stat_result = os.stat(path)
        etag = self._media_etag(stat_result)
        mime, _ = mimetypes.guess_type(path)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("ETag", etag)
            self.end_headers()
            return

        start = 0
        end = stat_result.st_size - 1
        range_header = str(self.headers.get("Range") or "").strip()
        if range_header:
            try:
                unit, requested = range_header.split("=", 1)
                if unit != "bytes" or "," in requested:
                    raise ValueError
                first, last = requested.split("-", 1)
                if first:
                    start = int(first)
                    end = int(last) if last else end
                else:
                    suffix_length = int(last)
                    if suffix_length <= 0:
                        raise ValueError
                    start = max(0, stat_result.st_size - suffix_length)
                end = min(end, stat_result.st_size - 1)
                if start < 0 or start > end:
                    raise ValueError
            except (TypeError, ValueError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{stat_result.st_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        content_length = end - start + 1
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("ETag", etag)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{stat_result.st_size}")
        self.end_headers()
        if head_only:
            return
        with open(path, "rb") as source:
            source.seek(start)
            remaining = content_length
            while remaining:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        self.do_GET()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/approved-assets/"):
            file_name = os.path.basename(parsed.path[len("/approved-assets/"):]).strip()
            media_path = os.path.join(os.path.dirname(__file__), "production_assets", file_name)
        elif parsed.path.startswith("/media/"):
            file_name = os.path.basename(parsed.path[len("/media/"):]).strip()
            media_path = os.path.join(_data_dir(), "public_media", file_name)
        else:
            self.send_error(404)
            return

        supported_media = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov"}
        if not file_name or os.path.splitext(file_name)[1].lower() not in supported_media or not os.path.isfile(media_path):
            self.send_error(404)
            return

        self._serve_file(media_path, head_only=True)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/auth/tiktok/connect":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                self._send_json(status_code, error_payload)
                return
            try:
                from tiktok_oauth import create_authorization
                authorization = create_authorization()
                if str(params.get("format", [""])[0]).lower() == "json":
                    self._send_json(200, authorization)
                else:
                    self.send_response(302)
                    self.send_header("Location", authorization["authorization_url"])
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            except Exception:
                self._send_json(503, {"error": "TikTok connection is not configured"})
            return

        if parsed.path == "/api/auth/tiktok/callback":
            params = parse_qs(parsed.query)
            state = str(params.get("state", [""])[0]).strip()
            try:
                from tiktok_oauth import TikTokOAuthError, complete_authorization, consume_state
                consume_state(state)
                provider_error = str(params.get("error", [""])[0]).strip()
                if provider_error:
                    location = "/os?" + urlencode({"view": "social", "tiktok": "error", "reason": "authorization_denied"})
                else:
                    complete_authorization(str(params.get("code", [""])[0]).strip())
                    location = "/os?" + urlencode({"view": "social", "tiktok": "connected"})
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (TikTokOAuthError, ValueError):
                self._send_json(400, {"error": "TikTok authorization could not be completed"})
            return

        if parsed.path == "/api/auth/tiktok/status":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                self._send_json(status_code, error_payload)
                return
            try:
                from tiktok_oauth import public_status
                self._send_json(200, public_status())
            except Exception:
                self._send_json(200, {"platform": "tiktok", "status": "ERROR", "connected": False})
            return

        if parsed.path == "/" and "text/html" in str(self.headers.get("Accept", "")).lower():
            self.send_response(302)
            self.send_header("Location", "/os")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if parsed.path in {"/os", "/os/"} or parsed.path.startswith("/os/assets/") or parsed.path.startswith("/api/os/"):
            params = parse_qs(parsed.query)
            if parsed.path.startswith("/api/os/"):
                authorized, status_code, error_payload = _os_authorized(self, params)
                if not authorized:
                    body = json.dumps(error_payload).encode("utf-8")
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            from social_engine.intelligence_os.web import handle
            status_code, content_type, body = handle("GET", parsed.path, None, _data_dir())
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path.startswith("/media/"):
            file_name = os.path.basename(parsed.path[len("/media/"):]).strip()
            if not file_name:
                body = b'{"error":"missing media file"}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            media_path = os.path.join(_data_dir(), "public_media", file_name)
            if not os.path.exists(media_path) or not os.path.isfile(media_path):
                body = b'{"error":"media not found"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self._serve_file(media_path)
            return

        if parsed.path.startswith("/approved-assets/"):
            file_name = os.path.basename(parsed.path[len("/approved-assets/"):]).strip()
            media_path = os.path.join(os.path.dirname(__file__), "production_assets", file_name)
            if not file_name or os.path.splitext(file_name)[1].lower() not in {".png", ".jpg", ".jpeg"} or not os.path.isfile(media_path):
                self.send_error(404)
                return
            self._serve_file(media_path)
            return

        if parsed.path in ("/", "/health", "/healthz"):
            startup = validate_startup_config()
            publishing = publishing_metrics(_data_dir())
            payload = {
                "status": "ok" if startup["status"] == "READY" and publishing["automation_status"] == "HEALTHY" else "degraded",
                "service": "infenergy-social-engine",
                "time_utc": _utc_now(),
                "uptime_seconds": _uptime_seconds(),
                "gemini_budget": budget_snapshot(),
                "deployment": startup["config"],
                "startup_validation": {key: startup[key] for key in ("status", "blockers", "warnings", "evaluated_at")},
                "publishing": publishing,
                "media": {
                    "public_directory": os.path.abspath(os.path.join(_data_dir(), "public_media")),
                    "working_directory": os.path.abspath(os.path.join(_data_dir(), "generated_visuals")),
                    "library_endpoint": "/media-library",
                    "browser_route": "/media/{filename}",
                },
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/media-library":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                self._send_json(status_code, error_payload)
                return
            media_dir = os.path.join(_data_dir(), "public_media")
            files = []
            if os.path.isdir(media_dir):
                for file_name in os.listdir(media_dir):
                    local_path = os.path.join(media_dir, file_name)
                    if os.path.isfile(local_path) and os.path.splitext(file_name)[1].lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                        files.append({
                            "file_name": file_name,
                            "url": f"/media/{file_name}",
                            "bytes": os.path.getsize(local_path),
                            "modified_at_utc": datetime.fromtimestamp(os.path.getmtime(local_path), timezone.utc).isoformat(),
                        })
            files.sort(key=lambda item: item["modified_at_utc"], reverse=True)
            self._send_json(200, {
                "status": "ok",
                "directory": os.path.abspath(media_dir),
                "working_directory": os.path.abspath(os.path.join(_data_dir(), "generated_visuals")),
                "count": len(files),
                "images": files[:200],
            })
            return

        if parsed.path == "/status":
            recent_posts = _load_history(limit=10)
            payload = {
                "status": "ok",
                "service": "infenergy-social-engine",
                "time_utc": _utc_now(),
                "uptime_seconds": _uptime_seconds(),
                "last_run": LAST_RUN,
                "last_dispatch": LAST_DISPATCH,
                "deferred_run": _deferred_run_status(),
                "recent_outbox": recent_outbox_activity(_data_dir()),
                "candidate_pool_depth": _candidate_pool_depth(),
                "dry_run": os.environ.get("SOCIAL_DRY_RUN", "true"),
                "shadow_mode": os.environ.get("SOCIAL_SHADOW_MODE", "false"),
                "recent_quality": _quality_summary(recent_posts),
                "visual_repo_bootstrap": VISUAL_REPO_BOOTSTRAP,
                "operational_intelligence": _operational_intelligence_snapshot(),
                "gemini_budget": budget_snapshot(),
                "deployment": load_runtime_config().public_dict(),
                "startup_validation": validate_startup_config(),
                "publishing_metrics": publishing_metrics(_data_dir()),
                "media": {
                    "public_directory": os.path.abspath(os.path.join(_data_dir(), "public_media")),
                    "working_directory": os.path.abspath(os.path.join(_data_dir(), "generated_visuals")),
                    "library_endpoint": "/media-library",
                    "browser_route": "/media/{filename}",
                },
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path in {"/why-not-published", "/can-this-post"}:
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                self._send_json(status_code, error_payload)
                return
            outbox_id = str((params.get("outbox_id") or [""])[0]).strip()
            if not outbox_id:
                self._send_json(400, {"error": "outbox_id_required"})
                return
            diagnostic = why_not_published(_data_dir(), outbox_id)
            if parsed.path == "/can-this-post" and diagnostic.get("found"):
                diagnostic["fresh_readiness"] = evaluate_outbox_readiness(
                    _data_dir(),
                    outbox_id,
                    dispatch_enabled=load_runtime_config().dispatch_enabled,
                    actor="worker.can_this_post",
                )
            self._send_json(200 if diagnostic.get("found") else 404, diagnostic)
            return

        if parsed.path in {"/today-schedule", "/publishing-backlog"}:
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                self._send_json(status_code, error_payload)
                return
            if parsed.path == "/today-schedule":
                day = str((params.get("date") or [""])[0]).strip() or None
                self._send_json(200, today_schedule(_data_dir(), day))
            else:
                self._send_json(200, classify_backlog(_data_dir()))
            return

        if parsed.path in {"/hold-outbox", "/release-outbox"}:
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                self._send_json(status_code, error_payload)
                return
            outbox_id = str((params.get("outbox_id") or [""])[0]).strip()
            if not outbox_id:
                self._send_json(400, {"error": "outbox_id_required"})
                return
            try:
                if parsed.path == "/hold-outbox":
                    reason = str((params.get("reason") or ["operator_hold"])[0]).strip() or "operator_hold"
                    hold_outbox(_data_dir(), outbox_id, reason)
                    result = {"status": "HELD", "outbox_id": outbox_id, "reason": reason}
                else:
                    release_outbox(_data_dir(), outbox_id, "operator_release", reset_attempts=True)
                    result = {"status": "RELEASED", "outbox_id": outbox_id}
                self._send_json(200, result)
            except ValueError as exc:
                self._send_json(409, {"error": str(exc), "outbox_id": outbox_id})
            return

        if parsed.path == "/gemini-visual-repo-bootstrap":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            bootstrap = _auto_bootstrap_visual_repo()
            payload = {
                "status": "ok" if bootstrap.get("status") == "ok" else "error",
                "time_utc": _utc_now(),
                "bootstrap": bootstrap,
                "repo": inventory_db.fetch_gemini_style_references(_data_dir(), active_only=False, limit=500),
                "settings": inventory_db.fetch_visual_generation_settings(_data_dir()),
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if bootstrap.get("status") == "ok" else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/agents/list":
            from agents.dispatcher import available_agents  # local import to avoid startup cost

            body = json.dumps({"status": "ok", "agents": available_agents()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/agents/conference":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            from business_intelligence import api as bi_api

            result = bi_api.run_agent_conference()
            body = json.dumps(result, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/agents/run":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            from agents.dispatcher import run_agent

            name = str(params.get("name", [""])[0]).strip()
            if not name:
                body = json.dumps({"error": "missing_name"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            result = run_agent(name, _data_dir(), params, query_params=True)
            code = 400 if isinstance(result, dict) and "error" in result else 200
            body = json.dumps(result, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/history":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(200, limit))
            posts = _load_history(limit=limit)
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "count": len(posts),
                "posts": posts,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/campaign":
            plan = _load_latest_campaign_plan()
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "campaign": plan,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/campaign-current":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            campaign = _load_latest_structured_campaign()
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "campaign": campaign,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/content-preview":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            preview_params = _parse_preview_params(params)
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "preview": _content_preview(preview_params),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/schedule-preview":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "days": _schedule_preview(days=7),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/production-readiness":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps({"status": "ok", "readiness": _production_readiness_snapshot()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/quality-report":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            limit = int(params.get("limit", ["200"])[0])
            limit = max(10, min(500, limit))
            posts = _load_history(limit=limit)
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "report": _quality_report(posts),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/inventory-db":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            sync_result = generate_posts.sync_inventory_database(force=False)
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "sync": sync_result,
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
                "brand_profile": generate_posts.load_brand_profile(),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/inventory-sync":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            force = str(params.get("force", ["false"])[0]).strip().lower() in ("1", "true", "yes", "on")
            sync_result = generate_posts.sync_inventory_database(force=force)
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "force": force,
                "sync": sync_result,
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/brand-profile-apply":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            result = generate_posts.apply_conference_brand_profile()
            payload = {
                "status": "ok" if result.get("ok") else "error",
                "time_utc": _utc_now(),
                "applied": bool(result.get("ok")),
                "brand_profile": result.get("brand_profile", {}),
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if result.get("ok") else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/selling-ideology":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "selling_ideology": generate_posts.load_selling_ideology(),
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/selling-ideology-apply":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            result = generate_posts.apply_conference_selling_ideology()
            payload = {
                "status": "ok" if result.get("ok") else "error",
                "time_utc": _utc_now(),
                "applied": bool(result.get("ok")),
                "selling_ideology": result.get("selling_ideology", {}),
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if result.get("ok") else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/gemini-visual-repo":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            refs = inventory_db.fetch_gemini_style_references(_data_dir(), active_only=False, limit=500)
            settings = inventory_db.fetch_visual_generation_settings(_data_dir())
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "repo": refs,
                "settings": settings,
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/gemini-visual-repo-seed":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            seeded = inventory_db.seed_gemini_style_idea_repo(_data_dir())
            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "seed": seeded,
                "repo": inventory_db.fetch_gemini_style_references(_data_dir(), active_only=False, limit=500),
                "settings": inventory_db.fetch_visual_generation_settings(_data_dir()),
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/gemini-visual-repo-apply":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            refs_json_raw = str(params.get("refs_json", [""])[0] or "").strip()
            active_style_keys_csv = str(params.get("active_style_keys", [""])[0] or "").strip()
            override_url = str(params.get("override_url", [""])[0] or "").strip()

            refs_payload = []
            if refs_json_raw:
                try:
                    parsed_refs = json.loads(refs_json_raw)
                    if isinstance(parsed_refs, list):
                        refs_payload = parsed_refs
                except Exception:
                    refs_payload = []

            upserted = inventory_db.upsert_gemini_style_references(_data_dir(), refs_payload) if refs_payload else 0
            active_style_keys = [k.strip() for k in active_style_keys_csv.split(",") if k.strip()]

            existing_settings = inventory_db.fetch_visual_generation_settings(_data_dir())
            inventory_db.upsert_visual_generation_settings(
                _data_dir(),
                {
                    "active_style_keys": active_style_keys or existing_settings.get("active_style_keys", []),
                    "visual_product_image_override_url": override_url or str(existing_settings.get("visual_product_image_override_url", "")),
                },
            )

            payload = {
                "status": "ok",
                "time_utc": _utc_now(),
                "upserted": upserted,
                "repo": inventory_db.fetch_gemini_style_references(_data_dir(), active_only=False, limit=500),
                "settings": inventory_db.fetch_visual_generation_settings(_data_dir()),
                "snapshot": inventory_db.get_inventory_snapshot(_data_dir()),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/run-marketing":
            token = os.environ.get("MANUAL_RUN_TOKEN", "")
            params = parse_qs(parsed.query)
            provided = params.get("token", [""])[0]
            if not token:
                body = b'{"error":"MANUAL_RUN_TOKEN not configured"}'
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if provided != token:
                body = b'{"error":"invalid token"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            ok, output = _run_script("run_marketing_team.py")
            payload = {
                "ok": ok,
                "message": "marketing team run complete" if ok else "marketing team run failed",
                "time_utc": _utc_now(),
                "output_tail": output,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/run-weekly":
            token = os.environ.get("MANUAL_RUN_TOKEN", "")
            params = parse_qs(parsed.query)
            provided = params.get("token", [""])[0]
            if not token:
                body = b'{"error":"MANUAL_RUN_TOKEN not configured"}'
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if provided != token:
                body = b'{"error":"invalid token"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            ok, output = _run_script("run_marketing_weekly.py")
            campaign_ok, campaign_output = _run_script("build_campaign_plan.py")
            current_campaign = _load_latest_structured_campaign()
            overall_ok = bool(ok and campaign_ok)
            payload = {
                "ok": overall_ok,
                "message": "weekly planner run complete" if overall_ok else "weekly planner run failed",
                "time_utc": _utc_now(),
                "output_tail": output,
                "campaign_build_ok": campaign_ok,
                "campaign_output_tail": campaign_output,
                "campaign_current": current_campaign,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if overall_ok else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/build-monthly-content":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                days = max(1, min(120, int(params.get("days", ["30"])[0])))
                start_date = str(params.get("start_date", [""])[0]).strip() or None
                result = build_monthly_calendar(
                    data_dir=_data_dir(),
                    start_date=start_date,
                    days=days,
                    enqueue=True,
                    replace_unpublished=str(params.get("replace_unpublished", ["false"])[0]).lower() in ("1", "true", "yes"),
                    content_plan=str(params.get("content_plan", [""])[0]).strip() or None,
                )
                payload = {
                    key: result.get(key)
                    for key in MONTHLY_SUMMARY_KEYS
                }
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(201)
            except Exception as exc:
                body = json.dumps({"error": f"{type(exc).__name__}:{exc}"}).encode("utf-8")
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/generate-monthly-content":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
            else:
                try:
                    days = max(1, min(120, int(params.get("days", ["30"])[0])))
                    start_date = str(params.get("start_date", [""])[0]).strip() or None
                    replace_unpublished = str(params.get("replace_unpublished", ["true"])[0]).lower() in ("1", "true", "yes")
                    content_plan = str(params.get("content_plan", [""])[0]).strip() or None
                    if content_plan not in (None, "weekly_brand_mix"):
                        raise ValueError(f"unsupported content plan: {content_plan}")
                    accepted, payload = _start_manual_monthly_generation(
                        days=days, start_date=start_date, replace_unpublished=replace_unpublished, content_plan=content_plan,
                    )
                    body = json.dumps({"accepted": accepted, **payload}, default=str).encode("utf-8")
                    self.send_response(202 if accepted else 409)
                except Exception as exc:
                    body = json.dumps({"error": f"{type(exc).__name__}:{exc}"}).encode("utf-8")
                    self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/monthly-generation-status":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            payload = _monthly_generation_status() if authorized else error_payload
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(200 if authorized else status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/monthly-content":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            calendar = latest_monthly_calendar(_data_dir())
            if not calendar:
                body = b'{"error":"monthly content not found"}'
                self.send_response(404)
            else:
                include_packages = str(params.get("detail", ["false"])[0]).lower() in ("1", "true", "yes")
                entries = calendar.get("entries", []) if isinstance(calendar.get("entries"), list) else []
                payload = {
                    key: calendar.get(key)
                    for key in MONTHLY_SUMMARY_KEYS
                }
                payload["entries"] = entries if include_packages else [
                    {key: entry.get(key) for key in ("date", "scheduled_at", "slot", "content_id", "thought_id", "format", "statement", "outbox_id")}
                    for entry in entries
                    if isinstance(entry, dict)
                ]
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/prepare-monthly-gemini":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _authorized(params)
            if not authorized:
                body = json.dumps(error_payload).encode("utf-8")
                self.send_response(status_code)
            else:
                try:
                    payload = prepare_monthly_gemini_prompts(_data_dir())
                    body = json.dumps(payload, default=str).encode("utf-8")
                    self.send_response(200)
                except Exception as exc:
                    body = json.dumps({"error": f"{type(exc).__name__}:{exc}"}).encode("utf-8")
                    self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/run-now":
            token = os.environ.get("MANUAL_RUN_TOKEN", "")
            params = parse_qs(parsed.query)
            provided = _manual_run_credential(self, params)
            slot = params.get("slot", ["morning"])[0]
            force_live = params.get("live", ["false"])[0].lower() in ("1", "true", "yes")
            force_dry_run = params.get("dry_run", ["false"])[0].lower() in ("1", "true", "yes")
            shadow_mode = params.get("shadow", ["false"])[0].lower() in ("1", "true", "yes")
            platforms_override = params.get("platforms", [""])[0]
            duplicate_mode = params.get("duplicate_mode", [""])[0].strip().lower()
            readiness_block_override = params.get("readiness_block", [""])[0].strip().lower()
            product_id_override = params.get("product_id", [""])[0].strip()
            no_product = params.get("no_product", ["false"])[0].strip().lower() in ("1", "true", "yes")
            funnel_stage_override = params.get("funnel_stage", [""])[0].strip().upper()
            pipeline_override = params.get("pipeline", [""])[0].strip().lower()
            idempotency_key = params.get("idempotency_key", [""])[0].strip()[:128]
            if duplicate_mode and duplicate_mode not in ("strict", "exact_only", "allow_all"):
                duplicate_mode = ""
            if readiness_block_override and readiness_block_override not in ("true", "false", "1", "0", "yes", "no"):
                readiness_block_override = ""
            if funnel_stage_override and funnel_stage_override not in ("ATTENTION", "EDUCATION", "TRUST", "DESIRE", "CONVERSION"):
                funnel_stage_override = ""
            if pipeline_override in ("both", "combined", "compare"):
                pipeline_override = "best_of"
            if pipeline_override and pipeline_override not in ("legacy", "orchestrator", "best_of"):
                pipeline_override = ""
            if slot not in ("morning", "midday", "evening"):
                slot = "morning"

            if not token:
                body = b'{"error":"MANUAL_RUN_TOKEN not configured"}'
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if provided != token:
                body = b'{"error":"invalid token"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            started, run_request = _start_or_defer_slot(
                idempotency_key=idempotency_key,
                slot=slot,
                force_live=force_live,
                force_dry_run=force_dry_run,
                platforms_override=platforms_override,
                duplicate_mode=duplicate_mode,
                readiness_block_override=readiness_block_override,
                product_id_override=product_id_override,
                no_product=no_product,
                funnel_stage_override=funnel_stage_override,
                pipeline_override=pipeline_override,
                shadow_mode=shadow_mode,
            )
            payload = {
                "accepted": started,
                "slot": slot,
                "force_live": force_live,
                "force_dry_run": force_dry_run,
                "shadow_mode": shadow_mode,
                "platforms": platforms_override,
                "duplicate_mode": duplicate_mode or "env_default",
                "readiness_block": readiness_block_override or "env_default",
                "product_id": product_id_override or "auto",
                "no_product": no_product,
                "funnel_stage": funnel_stage_override or "auto",
                "pipeline": pipeline_override or "env_default",
                "run_request": run_request,
                "message": "run accepted" if started else "run already in progress",
                "time_utc": _utc_now(),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(202 if started else 409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/delete-post":
            token = os.environ.get("MANUAL_RUN_TOKEN", "")
            params = parse_qs(parsed.query)
            provided = params.get("token", [""])[0]
            platform = params.get("platform", [""])[0].strip().lower()
            post_id = params.get("post_id", [""])[0].strip()

            if not token:
                body = b'{"error":"MANUAL_RUN_TOKEN not configured"}'
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if provided != token:
                body = b'{"error":"invalid token"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if platform not in ("facebook", "instagram", "linkedin") or not post_id:
                body = b'{"error":"platform must be facebook|instagram|linkedin and post_id is required"}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            payload = {"platform": platform, "post_id": post_id, "time_utc": _utc_now()}
            try:
                if platform == "facebook":
                    import publish_facebook
                    result = publish_facebook.delete(post_id)
                elif platform == "instagram":
                    import publish_instagram
                    result = publish_instagram.delete(post_id)
                else:
                    import publish_linkedin
                    result = publish_linkedin.delete(post_id)
                payload["success"] = True
                payload["result"] = result
                status = 200
            except Exception as e:
                payload["success"] = False
                payload["error"] = str(e)
                status = 502

            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/refresh-meta":
            params = parse_qs(parsed.query)
            provided = str(params.get("token", [""])[0]).strip()
            secret = _meta_refresh_secret()

            if not secret:
                payload = {"error": "META_REFRESH_TOKEN or MANUAL_RUN_TOKEN not configured"}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if provided != secret:
                payload = {"error": "invalid token"}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            ok, payload = _refresh_meta_tokens()
            payload["time_utc"] = _utc_now()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def _handle_intelligence_os_request(self, method: str) -> bool:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/os/"):
            return False
        params = parse_qs(parsed.query)
        authorized, status_code, error_payload = _os_authorized(self, params)
        if not authorized:
            body = json.dumps(error_payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1_000_000:
                raise ValueError("request body must be at most 1 MB")
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            from social_engine.intelligence_os.web import handle
            status_code, content_type, body = handle(method, parsed.path, payload, _data_dir())
        except (ValueError, json.JSONDecodeError) as exc:
            status_code, content_type, body = 400, "application/json", json.dumps({"error": str(exc)}).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_PATCH(self):
        if not self._handle_intelligence_os_request("PATCH"):
            self.send_error(404)

    def do_POST(self):
        if not hasattr(self, "path"):
            self.do_GET()
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/tiktok/disconnect":
            params = parse_qs(parsed.query)
            authorized, status_code, error_payload = _os_authorized(self, params)
            if not authorized:
                self._send_json(status_code, error_payload)
                return
            try:
                from tiktok_oauth import disconnect
                self._send_json(200, disconnect())
            except Exception:
                self._send_json(502, {"error": "TikTok disconnect could not be completed"})
            return
        if self._handle_intelligence_os_request("POST"):
            return
        if parsed.path not in {"/custom-post", "/scored-story-reel"}:
            self.do_GET()
            return
        params = parse_qs(parsed.query)
        authorized, status_code, error_payload = _authorized(params)
        if not authorized:
            body = json.dumps(error_payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("request body must be between 1 byte and 1 MB")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            status_code, response = _compose_scored_story(payload) if parsed.path == "/scored-story-reel" else _publish_custom_post(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            status_code, response = 400, {"error": str(exc)}
        except Exception as exc:
            status_code, response = 500, {"error": str(exc)}
        body = json.dumps(response, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, request_handler_class):
        super().__init__(server_address, request_handler_class)
        configured = int(os.environ.get("HTTP_MAX_CONCURRENT_REQUESTS", "8"))
        self._request_slots = threading.BoundedSemaphore(max(1, min(configured, 64)))

    def process_request(self, request, client_address) -> None:
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = BoundedThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health endpoint listening on 0.0.0.0:{port}")


def run_slot(
    slot: str,
    force_live: bool = False,
    force_dry_run: bool = False,
    shadow_mode: bool = False,
    platforms_override: str = "",
    duplicate_mode: str = "",
    readiness_block_override: str = "",
    product_id_override: str = "",
    no_product: bool = False,
    funnel_stage_override: str = "",
    pipeline_override: str = "",
    deferred_job_id: str = "",
) -> None:
    with RUN_LOCK:
        if deferred_job_id:
            with DEFERRED_RUN_LOCK:
                current = _deferred_run_status()
                if current.get("job_id") == deferred_job_id:
                    _save_deferred_run_status(status="RUNNING", started_at_utc=_utc_now(), error=None)
        LAST_RUN["status"] = "running"
        LAST_RUN["slot"] = slot
        LAST_RUN["started_at_utc"] = _utc_now()
        LAST_RUN["finished_at_utc"] = None
        LAST_RUN["error"] = None

        print(f"\n[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] Starting {slot} run...")
        # A scheduled slot convenes an informed decision before generation; the
        # heartbeat itself never publishes or generates copy.
        try:
            from social.living_intelligence import heartbeat
            heartbeat(_data_dir(), level="LIGHT_HEARTBEAT", website_url=os.environ.get("FIRST_PARTY_SITE_URL", ""))
        except Exception as exc:
            print(f"[INTELLIGENCE] heartbeat unavailable: {exc}")
        previous_dry_run = os.environ.get("SOCIAL_DRY_RUN", "true")
        previous_platforms = os.environ.get("POST_PLATFORMS", "")
        previous_duplicate_mode = os.environ.get("MANUAL_DUPLICATE_MODE", "")
        previous_readiness_block = os.environ.get("CHANNEL_READINESS_BLOCK_ON_RED", "")
        previous_product_override = os.environ.get("POST_PRODUCT_ID_OVERRIDE", "")
        previous_bucket_override = os.environ.get("CONTENT_BUCKET_OVERRIDE", "")
        previous_funnel_stage_override = os.environ.get("POST_FUNNEL_STAGE_OVERRIDE", "")
        previous_pipeline_override = os.environ.get("POST_PIPELINE_OVERRIDE", "")
        previous_shadow_mode = os.environ.get("SOCIAL_SHADOW_MODE", "")
        previous_pool_runtime = os.environ.get("CANDIDATE_POOL_RUNTIME_ENABLED", "")
        os.environ["POST_SLOT"] = slot
        os.environ["POST_PLATFORMS"] = platforms_override
        os.environ["CANDIDATE_POOL_RUNTIME_ENABLED"] = "false"
        if duplicate_mode:
            os.environ["MANUAL_DUPLICATE_MODE"] = duplicate_mode
        if readiness_block_override:
            os.environ["CHANNEL_READINESS_BLOCK_ON_RED"] = "true" if readiness_block_override in ("1", "true", "yes") else "false"
        if product_id_override:
            os.environ["POST_PRODUCT_ID_OVERRIDE"] = product_id_override
        if no_product:
            os.environ["CONTENT_BUCKET_OVERRIDE"] = "no_product"
        if funnel_stage_override:
            os.environ["POST_FUNNEL_STAGE_OVERRIDE"] = funnel_stage_override
        if pipeline_override:
            os.environ["POST_PIPELINE_OVERRIDE"] = pipeline_override
        if force_live:
            os.environ["SOCIAL_DRY_RUN"] = "false"
        elif force_dry_run:
            os.environ["SOCIAL_DRY_RUN"] = "true"
        if shadow_mode:
            os.environ["SOCIAL_SHADOW_MODE"] = "true"

        _auto_bootstrap_visual_repo()

        refresh_ok, refresh_reason = _auto_refresh_meta_if_due()
        if refresh_ok:
            print("[META] Token refresh completed before run")
        elif refresh_reason not in ("not_due", "auto_refresh_disabled"):
            print(f"[META] Token refresh skipped/failed: {refresh_reason}")
        try:
            timeout_sec = int(os.environ.get("RUN_SLOT_TIMEOUT_SEC", "900"))
            scripts_dir = os.path.join(os.path.dirname(__file__), "scripts")
            run_engine_path = os.path.join(scripts_dir, "run_engine.py")
            env = os.environ.copy()
            env["POST_TEXT_ONLY"] = "true"

            completed = subprocess.run(
                [sys.executable, run_engine_path],
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_sec,
                check=False,
            )

            output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
            if output:
                print(output[-4000:])

            if completed.returncode != 0:
                outcome = _last_run_outcome()
                if outcome.get("slot") == slot and outcome.get("status") == "published":
                    LAST_RUN["status"] = "published"
                    LAST_RUN["error"] = f"partial_platform_failure: {output[-1500:]}"
                    return
                raise RuntimeError(f"run_engine exit={completed.returncode} output_tail={output[-1500:]}")

            outcome = _last_run_outcome()
            if outcome.get("slot") == slot and outcome.get("status") in {"published", "queued_for_durable_dispatch", "blocked_no_publish", "skipped_no_eligible_platforms"}:
                LAST_RUN["status"] = outcome["status"]
                LAST_RUN["error"] = str(outcome.get("detail") or "") or None
            else:
                LAST_RUN["status"] = "generation_failed"
                LAST_RUN["error"] = "run_engine_completed_without_outcome"
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout or "")
            stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr or "")
            partial = f"{stdout}\n{stderr}".strip()
            LAST_RUN["status"] = "generation_failed"
            LAST_RUN["error"] = f"run_timeout_after_{timeout_sec}s"
            if partial:
                print(partial[-4000:])
            print(f"[ERROR] {slot} run timed out after {timeout_sec}s")
        except BaseException as e:
            LAST_RUN["status"] = "generation_failed"
            LAST_RUN["error"] = str(e)
            print(f"[ERROR] {slot} run failed: {e}")
            traceback.print_exc()
        finally:
            os.environ["SOCIAL_DRY_RUN"] = previous_dry_run
            os.environ["POST_PLATFORMS"] = previous_platforms
            if previous_duplicate_mode:
                os.environ["MANUAL_DUPLICATE_MODE"] = previous_duplicate_mode
            elif "MANUAL_DUPLICATE_MODE" in os.environ:
                del os.environ["MANUAL_DUPLICATE_MODE"]
            if previous_readiness_block:
                os.environ["CHANNEL_READINESS_BLOCK_ON_RED"] = previous_readiness_block
            elif "CHANNEL_READINESS_BLOCK_ON_RED" in os.environ:
                del os.environ["CHANNEL_READINESS_BLOCK_ON_RED"]
            if previous_product_override:
                os.environ["POST_PRODUCT_ID_OVERRIDE"] = previous_product_override
            elif "POST_PRODUCT_ID_OVERRIDE" in os.environ:
                del os.environ["POST_PRODUCT_ID_OVERRIDE"]
            if previous_bucket_override:
                os.environ["CONTENT_BUCKET_OVERRIDE"] = previous_bucket_override
            elif "CONTENT_BUCKET_OVERRIDE" in os.environ:
                del os.environ["CONTENT_BUCKET_OVERRIDE"]
            if previous_funnel_stage_override:
                os.environ["POST_FUNNEL_STAGE_OVERRIDE"] = previous_funnel_stage_override
            elif "POST_FUNNEL_STAGE_OVERRIDE" in os.environ:
                del os.environ["POST_FUNNEL_STAGE_OVERRIDE"]
            if previous_pipeline_override:
                os.environ["POST_PIPELINE_OVERRIDE"] = previous_pipeline_override
            elif "POST_PIPELINE_OVERRIDE" in os.environ:
                del os.environ["POST_PIPELINE_OVERRIDE"]
            if previous_shadow_mode:
                os.environ["SOCIAL_SHADOW_MODE"] = previous_shadow_mode
            elif "SOCIAL_SHADOW_MODE" in os.environ:
                del os.environ["SOCIAL_SHADOW_MODE"]
            if previous_pool_runtime:
                os.environ["CANDIDATE_POOL_RUNTIME_ENABLED"] = previous_pool_runtime
            elif "CANDIDATE_POOL_RUNTIME_ENABLED" in os.environ:
                del os.environ["CANDIDATE_POOL_RUNTIME_ENABLED"]
            LAST_RUN["finished_at_utc"] = _utc_now()
            if deferred_job_id:
                with DEFERRED_RUN_LOCK:
                    current = _deferred_run_status()
                    if current.get("job_id") == deferred_job_id:
                        error_text = str(LAST_RUN.get("error") or "")
                        if "GEMINI_" in error_text and "BUDGET" in error_text:
                            availability = _gemini_workflow_availability()
                            _save_deferred_run_status(
                                status="WAITING_FOR_BUDGET_RESET", eligible_at_utc=availability["eligible_at_utc"],
                                reasons=availability["reasons"], error=error_text,
                            )
                        else:
                            successful_handoff = LAST_RUN.get("status") in {"published", "queued_for_durable_dispatch"}
                            _save_deferred_run_status(
                                status="COMPLETE" if successful_handoff else "FAILED",
                                finished_at_utc=LAST_RUN["finished_at_utc"], outcome_status=LAST_RUN.get("status"),
                                error=LAST_RUN.get("error"),
                            )


# All times in UTC — currently mapped to Central Time (CT)
# 8am CT = 13:00 UTC | 12pm CT = 17:00 UTC | 6pm CT = 23:00 UTC
# Change POST_SCHEDULE_MORNING/MIDDAY/EVENING env vars to override
morning_utc = os.environ.get("POST_SCHEDULE_MORNING", "13:00")
midday_utc  = os.environ.get("POST_SCHEDULE_MIDDAY",  "17:00")
evening_utc = os.environ.get("POST_SCHEDULE_EVENING", "23:00")
intelligence_light_utc = os.environ.get("INTELLIGENCE_SCHEDULE_LIGHT", "11:00")
intelligence_standard_utc = os.environ.get("INTELLIGENCE_SCHEDULE_STANDARD", "03:00")
intelligence_deep_utc = os.environ.get("INTELLIGENCE_SCHEDULE_DEEP", "02:00")


def run_intelligence_heartbeat(level: str) -> None:
    """Scheduled observation is independent from and incapable of publishing."""
    from social.living_intelligence import heartbeat
    result = heartbeat(_data_dir(), level=level, website_url=os.environ.get("FIRST_PARTY_SITE_URL", ""), publication_records=_load_history(limit=200))
    print(f"[INTELLIGENCE] {level}: {len(result.get('observations', []))} observations")


def run_candidate_batch() -> None:
    ok, output = _run_script("build_candidate_pool.py")
    status = "ok" if ok else "failed"
    print(f"[CANDIDATE_POOL] {status}: {output[-1000:]}")


def run_intelligence_enrichment() -> None:
    try:
        coverage = intelligence_packages.compile_packages(_data_dir())
        print(f"[ENRICHMENT] packages={len(coverage['packages'])} reserve={coverage['ready_reserve_available']} counts={coverage['counts']}")
    except Exception as exc:
        print(f"[ENRICHMENT] failed: {type(exc).__name__}: {exc}")


def run_content_factory() -> None:
    """Fill missing today/tomorrow slots independently from publication clocks."""
    if not RUN_LOCK.acquire(blocking=False):
        print("[FACTORY] Deferred because another content operation is active")
        return
    try:
        data_dir = _data_dir()
        init_content_operations(data_dir)
        reconcile_ready_inventory(data_dir)
        reconcile_stale_claims(data_dir)
        reconcile_confirmed_transactions(data_dir)
        today = datetime.now(timezone.utc).date()
        for content_date in (today, today + timedelta(days=1)):
            existing = {item["slot"]: item["status"] for item in daily_status(data_dir, content_date).get("slots", [])}
            for slot in ("morning", "midday", "evening"):
                completed_states = {"READY", "DUE", "CLAIMED", "PUBLISHING", "PUBLISHED"}
                if not _env_is_true("ENFORCE_SOCIAL_DELIVERY", False):
                    completed_states.add("EXTERNAL_ACTION_REQUIRED")
                if existing.get(slot) in completed_states:
                    continue
                env = os.environ.copy()
                env.update({
                    "DATA_DIR": data_dir, "POST_SLOT": slot, "POST_CONTENT_DATE": content_date.isoformat(),
                    "POST_TEXT_ONLY": "true", "CONTENT_FACTORY_ONLY": "true", "CONTENT_OPERATIONS_ENABLED": "true",
                    "CANDIDATE_POOL_RUNTIME_ENABLED": "false", "SOCIAL_DRY_RUN": "false", "SOCIAL_SHADOW_MODE": "false",
                    "POST_PLATFORMS": "",
                })
                completed = subprocess.run(
                    [sys.executable, os.path.join(os.path.dirname(__file__), "scripts", "run_engine.py")],
                    cwd=os.path.dirname(__file__), capture_output=True, text=True, env=env,
                    timeout=int(os.environ.get("CONTENT_FACTORY_SLOT_TIMEOUT_SEC", "900")), check=False,
                )
                output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
                print(f"[FACTORY] {content_date} {slot}: exit={completed.returncode} {output[-1000:]}")
            if content_date == today and int(daily_status(data_dir, today).get("missing") or 0) > 0:
                return
    except Exception as exc:
        print(f"[FACTORY] error: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        RUN_LOCK.release()


def dispatch_scheduled_slot(slot: str) -> None:
    """Publish due prepared content without generating or rewriting it."""
    if not RUN_LOCK.acquire(blocking=False):
        print(f"[DISPATCH] {slot} deferred because another content operation is active")
        return
    recovery_needed = False
    LAST_DISPATCH.update({
        "status": "running", "trigger": slot, "started_at_utc": _utc_now(),
        "finished_at_utc": None, "exit_code": None, "result": None, "error": None,
    })
    try:
        env = os.environ.copy()
        env["DATA_DIR"] = _data_dir()
        completed = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "scripts", "dispatch_outbox.py")],
            cwd=os.path.dirname(__file__), capture_output=True, text=True, env=env,
            timeout=int(os.environ.get("DISPATCH_TIMEOUT_SEC", "360")), check=False,
        )
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        LAST_DISPATCH["exit_code"] = completed.returncode
        print(f"[DISPATCH] {slot}: exit={completed.returncode} {output[-2000:]}")
        stdout_lines = (completed.stdout or "").strip().splitlines()
        if completed.returncode == 0 and stdout_lines:
            try:
                result = json.loads(stdout_lines[-1])
                LAST_DISPATCH["result"] = result
                LAST_DISPATCH["status"] = "complete"
                if int(result.get("processed") or 0) > 0 and int(result.get("published") or 0) == 0:
                    print(f"[DELIVERY_WATCHDOG] {slot}: zero confirmed publications; requesting replacement inventory")
                    recovery_needed = True
            except (TypeError, ValueError, json.JSONDecodeError):
                LAST_DISPATCH["status"] = "error"
                LAST_DISPATCH["error"] = "dispatcher_output_was_not_valid_json"
                print(f"[DELIVERY_WATCHDOG] {slot}: dispatcher output was not valid JSON")
        elif completed.returncode != 0:
            LAST_DISPATCH["status"] = "error"
            LAST_DISPATCH["error"] = output[-1500:] or f"dispatcher_exit_{completed.returncode}"
        else:
            LAST_DISPATCH["status"] = "error"
            LAST_DISPATCH["error"] = "dispatcher_produced_no_output"
    except Exception as exc:
        LAST_DISPATCH["status"] = "error"
        LAST_DISPATCH["error"] = f"{type(exc).__name__}:{exc}"
        print(f"[DISPATCH] {slot} failed: {LAST_DISPATCH['error']}")
    finally:
        LAST_DISPATCH["finished_at_utc"] = _utc_now()
        RUN_LOCK.release()
    if recovery_needed:
        _start_factory_thread()


def pregenerate_scheduled_content() -> None:
    if not RUN_LOCK.acquire(blocking=False):
        print("[PREGENERATE] Deferred because another content operation is active")
        return
    try:
        env = os.environ.copy()
        env.update({"DATA_DIR": _data_dir(), "OUTBOX_ACTION": "pregenerate"})
        completed = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "scripts", "dispatch_outbox.py")],
            cwd=os.path.dirname(__file__), capture_output=True, text=True, env=env,
            timeout=int(os.environ.get("GEMINI_PREGEN_PROCESS_TIMEOUT_SEC", "600")), check=False,
        )
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        print(f"[PREGENERATE] exit={completed.returncode} {output[-2000:]}")
    except subprocess.TimeoutExpired:
        print("[PREGENERATE] process timeout; package remains READY")
    finally:
        RUN_LOCK.release()


def _start_factory_thread() -> None:
    threading.Thread(target=run_content_factory, daemon=True, name="content-factory").start()


def _start_dispatch_thread(slot: str) -> None:
    threading.Thread(target=dispatch_scheduled_slot, args=(slot,), daemon=True, name=f"dispatch-{slot}").start()


def _start_pregeneration_thread() -> None:
    threading.Thread(target=pregenerate_scheduled_content, daemon=True, name="content-pregeneration").start()


def run_delivery_watchdog() -> dict:
    today = datetime.now(timezone.utc).date()
    summary = daily_status(_data_dir(), today)
    terminal_failures = [
        slot for slot in summary.get("slots", [])
        if slot.get("status") in {"FAILED", "EXTERNAL_ACTION_REQUIRED"}
    ]
    needs_inventory = int(summary.get("missing") or 0) > 0 or bool(terminal_failures)
    result = {
        "date": today.isoformat(),
        "published": int(summary.get("published") or 0),
        "ready": int(summary.get("ready") or 0),
        "missing": int(summary.get("missing") or 0),
        "terminal_failures": len(terminal_failures),
        "recovery_requested": needs_inventory,
    }
    autonomous_recovery = (
        _env_is_true("AUTONOMOUS_AI_ENABLED", False)
        and _env_is_true("CONTENT_FACTORY_ENABLED", False)
    )
    result["autonomous_recovery_enabled"] = autonomous_recovery
    if needs_inventory and autonomous_recovery:
        print(f"[DELIVERY_WATCHDOG] coverage gap: {json.dumps(result, sort_keys=True)}")
        _start_factory_thread()
    return result


def register_scheduled_jobs() -> None:
    schedule.clear()
    schedule.every().minute.do(resume_deferred_run)
    if os.environ.get("CONTENT_DISPATCH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        schedule.every().day.at(morning_utc).do(_start_dispatch_thread, "morning")
        schedule.every().day.at(midday_utc).do(_start_dispatch_thread, "midday")
        schedule.every().day.at(evening_utc).do(_start_dispatch_thread, "evening")
        schedule.every(5).minutes.do(_start_dispatch_thread, "due_sweep")
        schedule.every(10).minutes.do(run_delivery_watchdog)
    if _env_is_true("CONTENT_PREGENERATION_ENABLED", False):
        primary = str(os.environ.get("CONTENT_PREGENERATION_PRIMARY_UTC", "05:15")).strip()
        retry = str(os.environ.get("CONTENT_PREGENERATION_RETRY_UTC", "11:15")).strip()
        schedule.every().day.at(primary).do(_start_pregeneration_thread)
        schedule.every().day.at(retry).do(_start_pregeneration_thread)
    autonomous_ai = os.environ.get("AUTONOMOUS_AI_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    if autonomous_ai:
        if os.environ.get("CONTENT_FACTORY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
            schedule.every(30).minutes.do(_start_factory_thread)
        schedule.every(6).hours.do(run_intelligence_enrichment)
        schedule.every().day.at(intelligence_light_utc).do(run_intelligence_heartbeat, "LIGHT_HEARTBEAT")
        schedule.every().day.at("10:00").do(run_candidate_batch)
        schedule.every().monday.at(intelligence_standard_utc).do(run_intelligence_heartbeat, "STANDARD_HEARTBEAT")
        schedule.every().sunday.at(intelligence_deep_utc).do(run_intelligence_heartbeat, "DEEP_HEARTBEAT")
        if os.environ.get("INTELLIGENCE_OS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
            from social_engine.intelligence_os.foundation import heartbeat
            schedule.every(15).minutes.do(heartbeat, _data_dir())

def main() -> None:
    startup = validate_startup_config()
    print(json.dumps({"event": "startup_configuration", **startup}, sort_keys=True))
    if startup["blockers"]:
        raise RuntimeError(f"STARTUP_CONFIGURATION_BLOCKED:{','.join(startup['blockers'])}")
    if os.environ.get("INTELLIGENCE_OS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        from social_engine.intelligence_os.foundation import bootstrap
        intelligence_os = bootstrap(_data_dir())
        print(f"Infenergy Intelligence OS ready: {len(intelligence_os.registry.list())} capabilities")
    register_scheduled_jobs()

    start_health_server()

    loaded, load_reason = _load_meta_runtime_from_state()
    if loaded:
        print(f"Loaded Meta runtime state from disk ({load_reason})")
    else:
        print(f"Meta runtime state load: {load_reason}")

    bootstrap = _auto_bootstrap_visual_repo()
    if bootstrap.get("status") == "ok":
        print(f"Visual repo bootstrap ok: {bootstrap.get('summary', {})}")
    else:
        print(f"Visual repo bootstrap failed: {bootstrap.get('error')}")

    if os.environ.get("AUTONOMOUS_AI_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
        run_intelligence_enrichment()

    init_content_operations(_data_dir())
    growth_schedule_result = apply_growth_schedule_to_ready_inventory(_data_dir())
    print(f"Growth schedule applied: {growth_schedule_result['updated']} ready packages")

    print("=== INF Energy Social Engine — Railway Worker ===")
    print(f"Scheduled (UTC): morning={morning_utc}  midday={midday_utc}  evening={evening_utc}")
    print(f"Dry run: {os.environ.get('SOCIAL_DRY_RUN', 'true')}")
    print("Manual run endpoint: /run-now?slot=morning&token=... (requires MANUAL_RUN_TOKEN)")
    print("Meta refresh endpoint: /refresh-meta?token=... (uses META_REFRESH_TOKEN or MANUAL_RUN_TOKEN)")
    print("Waiting for next scheduled run...\n")

    resume_deferred_run(recover_stale=True)
    if _env_is_true("CONTENT_PREGENERATION_ENABLED", False) and _env_is_true("CONTENT_PREGENERATION_ON_STARTUP", False):
        pregenerate_scheduled_content()
    if os.environ.get("CONTENT_DISPATCH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        run_delivery_watchdog()
        _start_dispatch_thread("startup_sweep")

    if os.environ.get("RUN_FACTORY_ON_STARTUP", "false").lower() in {"1", "true", "yes", "on"}:
        _start_factory_thread()

    if os.environ.get("RUN_ON_STARTUP", "false").lower() == "true":
        print("RUN_ON_STARTUP=true, launching startup run for morning slot")
        _start_slot_thread("morning")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
