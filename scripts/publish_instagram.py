import os
import time
import re
import mimetypes
import requests
from urllib.parse import urljoin, urlparse

from social_visuals import review_rendered_visual, strict_artifact_review_platform
from url_safety import is_safe_http_url

GRAPH_BASE = "https://graph.facebook.com/v26.0"
INSTAGRAM_CAPTION_MAX_LENGTH = 2200


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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


def _graph_error_text(resp: requests.Response) -> str:
    try:
        payload = resp.json() if resp.content else {}
    except Exception:
        payload = {}
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message", "")).strip()
        code = str(err.get("code", "")).strip()
        subcode = str(err.get("error_subcode", "")).strip()
        parts = [p for p in [message, f"code={code}" if code else "", f"subcode={subcode}" if subcode else ""] if p]
        if parts:
            return " | ".join(parts)
    return (resp.text or "").strip()[:1000]


def _wait_for_media_container(ig_user_id: str, creation_id: str, access_token: str, timeout_sec: int = 45) -> tuple[bool, str]:
    deadline = time.time() + timeout_sec
    last_status = ""
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{GRAPH_BASE}/{creation_id}",
                params={
                    "fields": "status_code,status",
                    "access_token": access_token,
                },
                timeout=20,
            )
            if not resp.ok:
                return False, f"container_status_http_{resp.status_code}: {_graph_error_text(resp)}"
            data = resp.json() if resp.content else {}
            status_code = str(data.get("status_code", "")).strip().upper()
            status = str(data.get("status", "")).strip().upper()
            last_status = status_code or status
            if status_code == "FINISHED" or status == "FINISHED":
                return True, "finished"
            if status_code in {"ERROR", "EXPIRED"} or status in {"ERROR", "EXPIRED"}:
                return False, last_status or "container_not_ready"
        except Exception as e:
            return False, f"container_status_exception:{e}"
        time.sleep(2)
    return False, f"container_not_ready_timeout:last_status={last_status or 'unknown'}"


def _is_valid_public_image(url: str) -> bool:
    if not url:
        return False
    u = str(url).strip().lower()
    if not u.startswith("http"):
        return False
    if any(x in u for x in ("placeholder", "no-image", "default")):
        return False
    return True


def _local_public_media_path(url: str) -> str:
    parsed = urlparse(url)
    public_hosts = {
        host
        for host in (
            _env("RAILWAY_PUBLIC_DOMAIN"),
            urlparse(_env("PUBLIC_BASE_URL")).hostname or "",
        )
        if host
    }
    if parsed.hostname not in public_hosts or not parsed.path.startswith("/media/"):
        return ""
    file_name = os.path.basename(parsed.path[len("/media/"):]).strip()
    if not file_name:
        return ""
    return os.path.join(_env("DATA_DIR", "/data"), "public_media", file_name)


def _is_reachable_image_url(url: str, timeout: int = 10) -> bool:
    if not is_safe_http_url(url):
        return False
    media_path = _local_public_media_path(url)
    if media_path:
        content_type = (mimetypes.guess_type(media_path)[0] or "").lower()
        return os.path.isfile(media_path) and os.path.getsize(media_path) > 0 and "image" in content_type
    try:
        head = requests.head(url, allow_redirects=True, timeout=timeout)
        if head.status_code < 400:
            ctype = (head.headers.get("Content-Type") or "").lower()
            if not ctype or "image" in ctype or "octet-stream" in ctype:
                return True
        get_resp = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
        try:
            if get_resp.status_code >= 400:
                return False
            ctype = (get_resp.headers.get("Content-Type") or "").lower()
            return "image" in ctype or "octet-stream" in ctype
        finally:
            get_resp.close()
    except Exception:
        return False


def _is_reachable_public_url(url: str, *, media_kind: str, timeout: int = 15) -> bool:
    if not is_safe_http_url(url):
        return False
    expected = "video" if media_kind == "video" else "image"
    media_path = _local_public_media_path(url)
    if media_path:
        content_type = (mimetypes.guess_type(media_path)[0] or "").lower()
        return os.path.isfile(media_path) and os.path.getsize(media_path) > 0 and expected in content_type
    try:
        response = requests.head(url, allow_redirects=True, timeout=timeout)
        if response.status_code >= 400 or int(response.headers.get("Content-Length") or "1") <= 0:
            return False
        content_type = (response.headers.get("Content-Type") or "").lower()
        return expected in content_type or "octet-stream" in content_type
    except Exception:
        return False


def _publish_reel(content: dict, *, dry_run: bool) -> dict:
    reel = content.get("instagram_reel") if isinstance(content.get("instagram_reel"), dict) else {}
    urls = reel.get("public_urls") if isinstance(reel.get("public_urls"), dict) else {}
    video_url = str(urls.get("video") or "").strip()
    cover_url = str(urls.get("cover") or "").strip()
    if not (_is_reachable_public_url(video_url, media_kind="video") and _is_reachable_public_url(cover_url, media_kind="image")):
        raise RuntimeError("instagram_reel_public_artifact_preflight_failed")
    if dry_run:
        print("[DRY RUN] Instagram: would publish Reel container")
        return {"id": "dry-run", "media_type": "REEL", "story_id": "dry-run" if content.get("publish_instagram_story") else None}
    ig_user_id = _required_env("META_IG_USER_ID")
    access_token = _required_env("META_PAGE_ACCESS_TOKEN")
    response = _post_with_retry(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        {
            "media_type": "REELS",
            "video_url": video_url,
            "cover_url": cover_url,
            "share_to_feed": "true",
            "caption": str(content.get("ig_caption") or ""),
            "access_token": access_token,
        },
        timeout=45,
    )
    if not response.ok:
        raise RuntimeError(f"instagram_reel_container_create_failed_http_{response.status_code}: {_graph_error_text(response)}")
    creation_id = str(response.json().get("id") or "").strip()
    if not creation_id:
        raise RuntimeError("instagram_reel_container_missing_id")
    ready, reason = _wait_for_media_container(ig_user_id, creation_id, access_token, timeout_sec=300)
    if not ready:
        raise RuntimeError(f"instagram_reel_container_not_ready:{reason}")
    published = _post_with_retry(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        {"creation_id": creation_id, "access_token": access_token},
        timeout=45,
    )
    if not published.ok:
        raise RuntimeError(f"instagram_reel_publish_failed_http_{published.status_code}: {_graph_error_text(published)}")
    media_id = str(published.json().get("id") or "").strip()
    if not media_id:
        raise RuntimeError("instagram_reel_publish_missing_id")
    print(f"[Instagram] Reel posted: {media_id}")
    result = {"id": media_id, "container_id": creation_id, "media_type": "REEL"}
    if content.get("publish_instagram_story"):
        try:
            story = _publish_story_video(video_url, ig_user_id=ig_user_id, access_token=access_token)
            result.update({"story_id": story["id"], "story_container_id": story["container_id"]})
        except Exception as exc:
            result["story_error"] = str(exc)
    return result


def _publish_story_video(video_url: str, *, ig_user_id: str, access_token: str) -> dict:
    response = _post_with_retry(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        {"media_type": "STORIES", "video_url": video_url, "access_token": access_token},
        timeout=45,
    )
    if not response.ok:
        raise RuntimeError(f"instagram_story_container_create_failed_http_{response.status_code}: {_graph_error_text(response)}")
    creation_id = str(response.json().get("id") or "").strip()
    if not creation_id:
        raise RuntimeError("instagram_story_container_missing_id")
    ready, reason = _wait_for_media_container(ig_user_id, creation_id, access_token, timeout_sec=300)
    if not ready:
        raise RuntimeError(f"instagram_story_container_not_ready:{reason}")
    published = _post_with_retry(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        {"creation_id": creation_id, "access_token": access_token},
        timeout=45,
    )
    if not published.ok:
        raise RuntimeError(f"instagram_story_publish_failed_http_{published.status_code}: {_graph_error_text(published)}")
    media_id = str(published.json().get("id") or "").strip()
    if not media_id:
        raise RuntimeError("instagram_story_publish_missing_id")
    print(f"[Instagram] Story posted: {media_id}")
    return {"id": media_id, "container_id": creation_id, "media_type": "STORY"}


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _bounded_caption(value: str, limit: int = INSTAGRAM_CAPTION_MAX_LENGTH) -> str:
    """Keep the API payload within Instagram's documented caption limit."""
    caption = str(value or "").strip()
    if len(caption) <= limit:
        return caption
    cutoff = max(1, limit - 3)
    boundary = caption.rfind(" ", 0, cutoff)
    return f"{caption[:boundary if boundary > 0 else cutoff].rstrip()}..."


def _extract_page_image_candidate(page_url: str, timeout: int = 15) -> str:
    if not page_url or not str(page_url).strip().lower().startswith("http"):
        return ""
    if not is_safe_http_url(page_url):
        return ""
    try:
        resp = requests.get(page_url, timeout=timeout)
        if resp.status_code >= 400:
            return ""
        html = resp.text or ""
        # Prefer OpenGraph image; fall back to Twitter image.
        patterns = [
            r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
            r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']twitter:image["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html, flags=re.IGNORECASE)
            if not m:
                continue
            candidate = str(m.group(1) or "").strip()
            if not candidate:
                continue
            candidate = urljoin(page_url, candidate)
            if _is_valid_public_image(candidate):
                return candidate
        return ""
    except Exception:
        return ""


def publish(content: dict, dry_run: bool = False) -> dict:
    media_type = str(((content.get("platform_posts") or {}).get("instagram") or {}).get("media_type") or "").strip().upper()
    if media_type == "REEL":
        return _publish_reel(content, dry_run=dry_run)

    ig_default_image = _env("IG_DEFAULT_IMAGE_URL")
    validate_urls = _env("IG_VALIDATE_IMAGE_URLS", "true").lower() in ("1", "true", "yes", "on")
    generated_image_path = str((content.get("generated_visuals") or {}).get("instagram", "")).strip()
    primary_publish_image_url = str(content.get("primary_publish_image_url", "")).strip()
    generation = content.get("gemini_generation") if isinstance(content.get("gemini_generation"), dict) else {}
    owner_supplied_visual = content.get("owner_supplied_visual") is True and primary_publish_image_url.startswith("https://")
    require_gemini = not owner_supplied_visual and (generation.get("strict_provider") is True or _env("LIVE_REQUIRE_GEMINI_VISUAL", "false").lower() in ("1", "true", "yes", "on"))
    render_engines = (content.get("generated_visuals") or {}).get("render_engines") or {}
    strict_public_urls = {
        str(asset.get("public_url") or "").strip()
        for asset in generation.get("assets", []) or []
        if isinstance(asset, dict) and asset.get("render_engine") == "gemini"
    }

    candidates = []
    review_platform = strict_artifact_review_platform(content, "instagram")
    if generated_image_path and os.path.exists(generated_image_path):
        if require_gemini:
            review = review_rendered_visual(generated_image_path, review_platform)
            if review.get("verdict") != "PASS":
                issues = ",".join(str(issue) for issue in review.get("issues", [])) or "artifact_review_failed"
                return {"id": "skipped", "reason": f"strict_gemini_artifact_invalid:{issues}"}
            if str(render_engines.get("instagram") or "") == "gemini" and primary_publish_image_url.startswith("http"):
                strict_public_urls.add(primary_publish_image_url)
    if _is_valid_public_image(primary_publish_image_url) and (not require_gemini or primary_publish_image_url in strict_public_urls):
        candidates.append(primary_publish_image_url)
    if not require_gemini:
        product_image = content.get("product_image_url", "")
        if _is_valid_public_image(product_image):
            candidates.append(product_image)
        for c in content.get("product_image_candidates", []) or []:
            if _is_valid_public_image(c):
                candidates.append(c)
        for c in content.get("category_image_candidates", []) or []:
            if _is_valid_public_image(c):
                candidates.append(c)
        if _is_valid_public_image(ig_default_image):
            candidates.append(ig_default_image)
        destination_url = str(content.get("destination_url") or "").strip()
        page_image_candidate = _extract_page_image_candidate(destination_url)
        if _is_valid_public_image(page_image_candidate):
            candidates.append(page_image_candidate)
    candidates = _dedupe_keep_order(candidates)

    image_url = ""
    if validate_urls:
        for candidate in candidates:
            if _is_reachable_image_url(candidate):
                image_url = candidate
                break
        if not image_url and candidates:
            if require_gemini:
                return {"id": "skipped", "reason": "strict_gemini_public_url_preflight_failed"}
            image_url = candidates[0]
            print("[Instagram] Warning: no candidate passed preflight, using first candidate anyway")
    else:
        image_url = candidates[0] if candidates else ""

    if owner_supplied_visual and image_url != primary_publish_image_url:
        raise RuntimeError("instagram_approved_primary_image_unavailable")

    if not image_url:
        reason = "required_gemini_visual_unavailable" if require_gemini else "no_valid_image_url_candidates"
        print(f"[Instagram] Skipped: {reason}")
        return {
            "id": "skipped",
            "reason": reason,
            "candidate_count": len(candidates),
        }

    caption = _bounded_caption(content["ig_caption"])
    carousel_assets = [asset for asset in content.get("carousel_assets", []) or [] if isinstance(asset, dict)]
    carousel_urls = []
    for asset in carousel_assets:
        public_url = str(asset.get("public_url") or "").strip()
        local_path = str(asset.get("local_path") or "").strip()
        if require_gemini:
            review = review_rendered_visual(local_path, review_platform)
            if asset.get("render_engine") != "gemini" or public_url not in strict_public_urls or review.get("verdict") != "PASS":
                return {"id": "skipped", "reason": "strict_gemini_carousel_artifact_invalid"}
        if _is_valid_public_image(public_url):
            carousel_urls.append(public_url)
    if require_gemini and carousel_assets and len(carousel_urls) != len(carousel_assets):
        return {"id": "skipped", "reason": "strict_gemini_carousel_incomplete"}

    if dry_run:
        print(f"[DRY RUN] Instagram: would post:\n{caption[:150]}...\n")
        return {"id": "dry-run"}

    # Step 1: create media container
    ig_user_id = _required_env("META_IG_USER_ID")
    access_token = _required_env("META_PAGE_ACCESS_TOKEN")
    if len(carousel_urls) >= 2:
        child_ids: list[str] = []
        for carousel_url in carousel_urls:
            child = _post_with_retry(
                f"{GRAPH_BASE}/{ig_user_id}/media",
                {"image_url": carousel_url, "is_carousel_item": "true", "access_token": access_token},
                timeout=30,
            )
            if not child.ok:
                raise RuntimeError(f"instagram_carousel_child_create_failed_http_{child.status_code}: {_graph_error_text(child)}")
            child_id = str(child.json().get("id") or "").strip()
            if not child_id:
                raise RuntimeError("instagram_carousel_child_missing_id")
            ready, ready_reason = _wait_for_media_container(ig_user_id, child_id, access_token)
            if not ready:
                raise RuntimeError(f"instagram_carousel_child_not_ready:{ready_reason}")
            child_ids.append(child_id)
        parent = _post_with_retry(
            f"{GRAPH_BASE}/{ig_user_id}/media",
            {
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "caption": caption,
                "access_token": access_token,
            },
            timeout=30,
        )
        if not parent.ok:
            raise RuntimeError(f"instagram_carousel_create_failed_http_{parent.status_code}: {_graph_error_text(parent)}")
        creation_id = str(parent.json().get("id") or "").strip()
        ready, ready_reason = _wait_for_media_container(ig_user_id, creation_id, access_token)
        if not ready:
            raise RuntimeError(f"instagram_carousel_not_ready:{ready_reason}")
        published = _post_with_retry(
            f"{GRAPH_BASE}/{ig_user_id}/media_publish",
            {"creation_id": creation_id, "access_token": access_token},
            timeout=30,
        )
        if not published.ok:
            raise RuntimeError(f"instagram_carousel_publish_failed_http_{published.status_code}: {_graph_error_text(published)}")
        data = published.json()
        print(f"[Instagram] Posted carousel: {data['id']}")
        return {"id": data["id"], "media_type": "CAROUSEL", "image_urls": carousel_urls}

    resp = _post_with_retry(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        {
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"instagram_media_create_failed_http_{resp.status_code}: {_graph_error_text(resp)}")
    creation_id = resp.json()["id"]

    ready, ready_reason = _wait_for_media_container(ig_user_id, creation_id, access_token)
    if not ready:
        raise RuntimeError(f"instagram_media_not_ready:{ready_reason}")

    # Step 2: publish the container
    resp2 = _post_with_retry(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        {
            "creation_id": creation_id,
            "access_token": access_token,
        },
        timeout=30,
    )
    if not resp2.ok:
        raise RuntimeError(f"instagram_media_publish_failed_http_{resp2.status_code}: {_graph_error_text(resp2)}")
    data = resp2.json()
    print(f"[Instagram] Posted: {data['id']}")
    return {"id": data["id"], "media_type": "IMAGE", "image_url": image_url, "container_id": creation_id}


def delete(media_id: str) -> dict:
    """Attempt to delete a published Instagram media object.

    Note: Meta's Instagram Graph API does not support deleting Business/Creator
    account media published through the API. This will typically fail with a
    Graph API error; callers should treat that as "must be removed manually in
    the Instagram app" rather than a bug.
    """
    access_token = _required_env("META_PAGE_ACCESS_TOKEN")
    resp = requests.delete(
        f"{GRAPH_BASE}/{media_id}",
        params={"access_token": access_token},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"instagram_delete_failed_http_{resp.status_code}: {_graph_error_text(resp)}")
    data = resp.json() if resp.content else {}
    print(f"[Instagram] Deleted: {media_id}")
    return {"id": media_id, "success": bool(data.get("success", True))}
