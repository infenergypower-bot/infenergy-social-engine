import json
import os
import time
import requests

from social_visuals import review_rendered_visual, strict_artifact_review_platform

GRAPH_BASE = "https://graph.facebook.com/v26.0"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _raise_with_body(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = ""
        try:
            body = resp.text[:1000]
        except Exception:
            body = "<unavailable>"
        raise requests.HTTPError(
            f"facebook_api_http_{resp.status_code}:{resp.reason} | response={body}",
            response=resp,
        ) from e


def _resolve_page_access_token(token: str, page_id: str) -> str:
    """Resolve a page access token from /me/accounts when a user token is provided."""
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/me/accounts",
            params={
                "fields": "id,name,access_token,tasks",
                "access_token": token,
            },
            timeout=20,
        )
        if not resp.ok:
            return token

        data = resp.json().get("data", [])
        if not isinstance(data, list) or not data:
            return token

        # Prefer the configured page id.
        for page in data:
            if str(page.get("id", "")) == str(page_id) and page.get("access_token"):
                return page["access_token"]

        # Fallback to first managed page token if explicit match is not found.
        first = data[0]
        if first.get("access_token"):
            return first["access_token"]
    except Exception:
        return token

    return token


def _post_with_retry(url: str, data: dict, timeout: int = 30) -> requests.Response:
    attempts = [0.7, 1.5, 3.0]
    last: requests.Response | None = None
    for idx, wait_s in enumerate(attempts, start=1):
        resp = requests.post(url, data=data, timeout=timeout)
        last = resp
        if resp.ok:
            return resp

        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp

        if idx < len(attempts):
            time.sleep(wait_s)

    return last if last is not None else requests.Response()


def _publish_feed_with_photo(
    *,
    page_id: str,
    token: str,
    message: str,
    image_path: str = "",
    image_url: str = "",
) -> dict:
    upload_data = {
        "published": "false",
        "access_token": token,
    }
    if image_path:
        with open(image_path, "rb") as image_file:
            upload = requests.post(
                f"{GRAPH_BASE}/{page_id}/photos",
                data=upload_data,
                files={"source": (os.path.basename(image_path), image_file, "image/png")},
                timeout=60,
            )
    else:
        upload = requests.post(
            f"{GRAPH_BASE}/{page_id}/photos",
            data={**upload_data, "url": image_url},
            timeout=60,
        )
    _raise_with_body(upload)
    media_id = str(upload.json().get("id") or "").strip()
    if not media_id:
        raise RuntimeError("facebook_unpublished_photo_missing_id")

    feed = _post_with_retry(
        f"{GRAPH_BASE}/{page_id}/feed",
        {
            "message": message,
            "attached_media": json.dumps([{"media_fbid": media_id}]),
            "access_token": token,
        },
        timeout=30,
    )
    _raise_with_body(feed)
    return feed.json()


def _publish_feed_with_photos(*, page_id: str, token: str, message: str, image_paths: list[str]) -> dict:
    media: list[dict[str, str]] = []
    for image_path in image_paths:
        with open(image_path, "rb") as image_file:
            upload = requests.post(
                f"{GRAPH_BASE}/{page_id}/photos",
                data={"published": "false", "access_token": token},
                files={"source": (os.path.basename(image_path), image_file, "image/png")},
                timeout=60,
            )
        _raise_with_body(upload)
        media_id = str(upload.json().get("id") or "").strip()
        if not media_id:
            raise RuntimeError("facebook_unpublished_photo_missing_id")
        media.append({"media_fbid": media_id})
    feed = _post_with_retry(
        f"{GRAPH_BASE}/{page_id}/feed",
        {"message": message, "attached_media": json.dumps(media), "access_token": token},
        timeout=30,
    )
    _raise_with_body(feed)
    return feed.json()


def _publish_feed_with_photo_urls(*, page_id: str, token: str, message: str, image_urls: list[str]) -> dict:
    media: list[dict[str, str]] = []
    for image_url in image_urls:
        upload = requests.post(
            f"{GRAPH_BASE}/{page_id}/photos",
            data={"published": "false", "url": image_url, "access_token": token},
            timeout=60,
        )
        _raise_with_body(upload)
        media_id = str(upload.json().get("id") or "").strip()
        if not media_id:
            raise RuntimeError("facebook_unpublished_photo_missing_id")
        media.append({"media_fbid": media_id})
    feed = _post_with_retry(
        f"{GRAPH_BASE}/{page_id}/feed",
        {"message": message, "attached_media": json.dumps(media), "access_token": token},
        timeout=30,
    )
    _raise_with_body(feed)
    return feed.json()


def _publish_reel(*, page_id: str, token: str, description: str, video_url: str) -> dict:
    start = _post_with_retry(
        f"{GRAPH_BASE}/{page_id}/video_reels",
        {"upload_phase": "start", "access_token": token},
        timeout=30,
    )
    _raise_with_body(start)
    start_payload = start.json()
    video_id = str(start_payload.get("video_id") or "").strip()
    upload_url = str(start_payload.get("upload_url") or "").strip()
    if not video_id or not upload_url:
        raise RuntimeError("facebook_reel_start_missing_upload_contract")

    upload = requests.post(
        upload_url,
        headers={"Authorization": f"OAuth {token}", "file_url": video_url},
        timeout=120,
    )
    _raise_with_body(upload)

    finish = _post_with_retry(
        f"{GRAPH_BASE}/{page_id}/video_reels",
        {
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": description,
            "access_token": token,
        },
        timeout=60,
    )
    _raise_with_body(finish)
    payload = finish.json()
    return {**payload, "id": str(payload.get("id") or video_id), "video_id": video_id, "media_type": "REEL"}


def publish(content: dict, wp_link: str, dry_run: bool = False) -> dict:
    facebook_package = (content.get("platform_posts") or {}).get("facebook") or {}
    message = str(facebook_package.get("final_caption") or content["fb_caption"])
    if not facebook_package.get("final_caption") and wp_link and wp_link not in message:
        message = f"{message}\n\n{wp_link}"
    image_path = str((content.get("generated_visuals") or {}).get("facebook", "")).strip()
    image_url = str(content.get("primary_publish_image_url", "")).strip()
    if not image_url.startswith("http"):
        image_url = str(content.get("product_image_url", "")).strip()
    require_image = _env_flag("FB_REQUIRE_IMAGE", True)
    generation = content.get("gemini_generation") if isinstance(content.get("gemini_generation"), dict) else {}
    owner_supplied_visual = content.get("owner_supplied_visual") is True and image_url.startswith("https://")
    require_gemini = not owner_supplied_visual and (generation.get("strict_provider") is True or _env_flag("LIVE_REQUIRE_GEMINI_VISUAL", False))

    media_type = str(facebook_package.get("media_type") or "").strip().upper()
    if media_type == "REEL":
        reel = content.get("instagram_reel") if isinstance(content.get("instagram_reel"), dict) else {}
        urls = reel.get("public_urls") if isinstance(reel.get("public_urls"), dict) else {}
        video_url = str(urls.get("video") or "").strip()
        if not video_url.startswith("https://"):
            raise RuntimeError("facebook_reel_public_video_missing")
        if dry_run:
            return {"id": "dry-run", "media_type": "REEL"}
        page_id = _required_env("META_PAGE_ID")
        token = _resolve_page_access_token(_required_env("META_PAGE_ACCESS_TOKEN"), page_id)
        return _publish_reel(page_id=page_id, token=token, description=message, video_url=video_url)

    if dry_run:
        print(f"[DRY RUN] Facebook: would post:\n{message[:150]}...\n")
        return {"id": "dry-run"}

    carousel_assets = [
        asset
        for asset in content.get("carousel_assets", []) or []
        if isinstance(asset, dict)
    ]
    carousel_paths = [
        str(asset.get("local_path") or "").strip()
        for asset in carousel_assets
        if os.path.exists(str(asset.get("local_path") or "").strip())
    ]
    carousel_urls = [
        str(asset.get("public_url") or "").strip()
        for asset in carousel_assets
        if str(asset.get("public_url") or "").strip().startswith("https://")
    ]
    if require_gemini:
        if carousel_assets and len(carousel_paths) != len(carousel_assets):
            raise RuntimeError("facebook_strict_gemini_carousel_incomplete")
        paths_to_publish = carousel_paths if len(carousel_paths) >= 2 else [image_path]
        review_platform = strict_artifact_review_platform(content, "facebook")
        for path in paths_to_publish:
            review = review_rendered_visual(path, review_platform)
            if review.get("verdict") != "PASS":
                issues = ",".join(str(issue) for issue in review.get("issues", [])) or "artifact_review_failed"
                raise RuntimeError(f"facebook_strict_gemini_artifact_invalid:{issues}")

    page_id = _required_env("META_PAGE_ID")
    token = _resolve_page_access_token(_required_env("META_PAGE_ACCESS_TOKEN"), page_id)

    if len(carousel_urls) >= 2 and owner_supplied_visual:
        data = _publish_feed_with_photo_urls(
            page_id=page_id,
            token=token,
            message=message,
            image_urls=carousel_urls,
        )
    elif len(carousel_paths) >= 2:
        data = _publish_feed_with_photos(
            page_id=page_id,
            token=token,
            message=message,
            image_paths=carousel_paths,
        )
    elif image_path and os.path.exists(image_path):
        data = _publish_feed_with_photo(
            page_id=page_id,
            token=token,
            message=message,
            image_path=image_path,
        )
    elif image_url.startswith("http"):
        data = _publish_feed_with_photo(
            page_id=page_id,
            token=token,
            message=message,
            image_url=image_url,
        )
    else:
        if require_image:
            raise RuntimeError(
                "Facebook image is required but no generated visual or product image URL was available"
            )
        resp = _post_with_retry(
            f"{GRAPH_BASE}/{page_id}/feed",
            {
                "message": message,
                "access_token": token,
            },
            timeout=30,
        )
        _raise_with_body(resp)
        data = resp.json()
    print(f"[Facebook] Posted: {data['id']}")
    return {"id": data["id"]}


def delete(post_id: str) -> dict:
    """Delete a previously published Facebook page post/photo by its object id."""
    page_id = _required_env("META_PAGE_ID")
    token = _resolve_page_access_token(_required_env("META_PAGE_ACCESS_TOKEN"), page_id)
    resp = requests.delete(
        f"{GRAPH_BASE}/{post_id}",
        params={"access_token": token},
        timeout=30,
    )
    _raise_with_body(resp)
    data = resp.json() if resp.content else {}
    print(f"[Facebook] Deleted: {post_id}")
    return {"id": post_id, "success": bool(data.get("success", True))}
