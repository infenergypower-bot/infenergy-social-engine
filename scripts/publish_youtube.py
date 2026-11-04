from __future__ import annotations

import os
import tempfile
import time
from typing import Any

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.upper() in {"REPLACE_ME", "SET_ME"}:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _access_token() -> str:
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": _required("YOUTUBE_CLIENT_ID"),
            "client_secret": _required("YOUTUBE_CLIENT_SECRET"),
            "refresh_token": _required("YOUTUBE_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    token = str(response.json().get("access_token") or "").strip()
    if not token:
        raise RuntimeError("youtube_oauth_refresh_returned_no_access_token")
    return token


def _video_url(content: dict[str, Any]) -> str:
    reel = content.get("instagram_reel") if isinstance(content.get("instagram_reel"), dict) else {}
    urls = reel.get("public_urls") if isinstance(reel.get("public_urls"), dict) else {}
    url = str(urls.get("video") or "").strip()
    if not url.startswith("https://"):
        raise RuntimeError("youtube_requires_public_https_video_url")
    return url


from publication_authorization import guarded as _editorial_guard

@_editorial_guard("youtube")
def publish(content: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    video_url = _video_url(content)
    title = str(content.get("title") or content.get("topic") or "Infenergy Story").strip()[:100]
    description = str(content.get("youtube_caption") or content.get("ig_caption") or content.get("fb_caption") or "").strip()[:5000]
    if dry_run:
        return {"id": "dry-run", "media_type": "VIDEO", "platform": "youtube"}
    token = _access_token()
    with requests.get(video_url, stream=True, timeout=90) as source:
        source.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            for chunk in source.iter_content(1024 * 1024):
                if chunk:
                    video.write(chunk)
            video.flush()
            video_size = os.path.getsize(video.name)
            metadata = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "categoryId": os.environ.get("YOUTUBE_CATEGORY_ID", "22").strip() or "22",
                    "tags": [tag.strip() for tag in os.environ.get("YOUTUBE_DEFAULT_TAGS", "Infenergy,Micro Mission").split(",") if tag.strip()][:20],
                },
                "status": {
                    "privacyStatus": os.environ.get("YOUTUBE_PRIVACY_STATUS", "unlisted").strip() or "unlisted",
                    "selfDeclaredMadeForKids": os.environ.get("YOUTUBE_MADE_FOR_KIDS", "false").strip().lower() == "true",
                },
            }
            session = requests.post(
                UPLOAD_URL,
                params={"part": "snippet,status", "uploadType": "resumable"},
                headers={
                    "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(video_size), "X-Upload-Content-Type": "video/mp4",
                },
                json=metadata, timeout=30,
            )
            session.raise_for_status()
            upload_url = str(session.headers.get("Location") or "").strip()
            if not upload_url:
                raise RuntimeError("youtube_resumable_session_returned_no_location")
            video.seek(0)
            for attempt in range(4):
                response = requests.put(
                    upload_url,
                    headers={"Content-Type": "video/mp4", "Content-Length": str(video_size)},
                    data=video, timeout=600,
                )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt == 3:
                    break
                video.seek(0)
                time.sleep(2 ** attempt)
    response.raise_for_status()
    payload = response.json()
    video_id = str(payload.get("id") or "").strip()
    if not video_id:
        raise RuntimeError("youtube_upload_returned_no_video_id")
    return {
        "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
        "media_type": "VIDEO", "status": "PROCESSING",
    }


def get_status(video_id: str) -> dict[str, Any]:
    response = requests.get(
        VIDEOS_URL,
        params={"part": "status,processingDetails", "id": video_id},
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=30,
    )
    response.raise_for_status()
    items = response.json().get("items") or []
    if not items:
        return {"platform": "youtube", "id": video_id, "status": "NOT_FOUND"}
    item = items[0]
    processing = str((item.get("processingDetails") or {}).get("processingStatus") or "unknown").upper()
    upload_status = str((item.get("status") or {}).get("uploadStatus") or "unknown").upper()
    return {"platform": "youtube", "id": video_id, "status": processing, "upload_status": upload_status, "url": f"https://www.youtube.com/watch?v={video_id}"}
