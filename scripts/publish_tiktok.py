from __future__ import annotations

import os
from typing import Any

import requests

from tiktok_oauth import access_token, creator_info

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
PUBLISH_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
UPLOAD_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.upper() in {"REPLACE_ME", "SET_ME"}:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _access_token() -> str:
    return access_token()


def _video_url(content: dict[str, Any]) -> str:
    reel = content.get("instagram_reel") if isinstance(content.get("instagram_reel"), dict) else {}
    urls = reel.get("public_urls") if isinstance(reel.get("public_urls"), dict) else {}
    url = str(urls.get("video") or "").strip()
    if not url.startswith("https://"):
        raise RuntimeError("tiktok_requires_public_https_video_url")
    return url


from publication_authorization import guarded as _editorial_guard

@_editorial_guard("tiktok")
def publish(content: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    video_url = _video_url(content)
    title = str(content.get("tiktok_caption") or content.get("ig_caption") or content.get("fb_caption") or "Infenergy Story").strip()[:2200]
    if dry_run:
        return {"id": "dry-run", "media_type": "VIDEO", "platform": "tiktok"}
    token = _access_token()
    publish_mode = os.environ.get("TIKTOK_PUBLISH_MODE", "direct").strip().lower()
    if publish_mode not in {"direct", "draft"}:
        raise RuntimeError("tiktok_publish_mode_must_be_direct_or_draft")
    request_url = PUBLISH_URL if publish_mode == "direct" else UPLOAD_URL
    request_body: dict[str, Any] = {
        "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
    }
    if publish_mode == "direct":
        settings = creator_info(token)
        privacy_level = os.environ.get("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY").strip() or "SELF_ONLY"
        allowed_privacy = settings.get("privacy_level_options") if isinstance(settings.get("privacy_level_options"), list) else []
        if allowed_privacy and privacy_level not in allowed_privacy:
            raise RuntimeError("tiktok_privacy_level_not_available_for_creator")
        request_body["post_info"] = {
            "title": title,
            "privacy_level": privacy_level,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        }
    response = requests.post(
        request_url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
        json=request_body,
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if error and str(error.get("code") or "ok") != "ok":
        raise RuntimeError(f"tiktok_publish_error:{error.get('code')}:{error.get('message')}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    publish_id = str(data.get("publish_id") or "").strip()
    if not publish_id:
        raise RuntimeError("tiktok_publish_returned_no_publish_id")
    return {"id": publish_id, "publish_id": publish_id, "media_type": "VIDEO", "status": "PROCESSING", "publish_mode": publish_mode}


def get_status(publish_id: str) -> dict[str, Any]:
    response = requests.post(
        STATUS_URL,
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json; charset=UTF-8"},
        json={"publish_id": publish_id},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if error and str(error.get("code") or "ok") != "ok":
        raise RuntimeError(f"tiktok_status_error:{error.get('code')}:{error.get('message')}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "platform": "tiktok", "id": publish_id,
        "status": str(data.get("status") or "UNKNOWN").upper(),
        "fail_reason": data.get("fail_reason"), "public_post_ids": data.get("publicaly_available_post_id") or [],
    }
