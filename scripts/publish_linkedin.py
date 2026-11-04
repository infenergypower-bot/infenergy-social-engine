import os
import time
import tempfile
import requests
from datetime import datetime, timezone

from PIL import Image, ImageFilter, ImageOps

from url_safety import is_safe_http_url

LI_API = "https://api.linkedin.com/v2"
DEFAULT_LINKEDIN_VERSION = datetime.now(timezone.utc).strftime("%Y%m")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _headers(include_json: bool = False) -> dict:
    token = _required_env("LINKEDIN_ACCESS_TOKEN")
    linkedin_version = _env("LINKEDIN_VERSION", DEFAULT_LINKEDIN_VERSION)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": linkedin_version,
    }
    if include_json:
        headers["Content-Type"] = "application/json"
    return headers


def _raise_with_body(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = ""
        try:
            body = resp.text[:1000]
        except Exception:
            body = "<unavailable>"
        raise requests.HTTPError(f"{e} | response={body}") from e


def _response_body(resp: requests.Response) -> str:
    try:
        return resp.text[:1000]
    except Exception:
        return "<unavailable>"


def _file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _is_valid_public_image(url: str) -> bool:
    u = str(url or "").strip().lower()
    if not u.startswith("http"):
        return False
    if any(x in u for x in ("placeholder", "no-image", "default")):
        return False
    return True


def _is_reachable_image_url(url: str, timeout: int = 10) -> bool:
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


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _download_image_to_temp(url: str) -> str:
    if not is_safe_http_url(url):
        raise RuntimeError("LinkedIn fallback image URL is not permitted (unsafe host)")
    resp = requests.get(url, allow_redirects=True, timeout=30)
    _raise_with_body(resp)
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "image" not in ctype and "octet-stream" not in ctype:
        raise RuntimeError(f"LinkedIn fallback image URL is not an image content type: {ctype}")
    fd, temp_path = tempfile.mkstemp(prefix="li-image-", suffix=".png")
    os.close(fd)
    with open(temp_path, "wb") as f:
        f.write(resp.content)
    return temp_path


def _normalize_linkedin_image(image_path: str) -> str:
    """Normalize image to LinkedIn-friendly dimensions to avoid awkward feed rendering."""
    if not image_path or not os.path.exists(image_path):
        return image_path

    try:
        target_w = int(_env("LINKEDIN_IMAGE_WIDTH", "1200") or "1200")
        target_h = int(_env("LINKEDIN_IMAGE_HEIGHT", "1200") or "1200")
        target_w = max(400, min(3000, target_w))
        target_h = max(400, min(3000, target_h))

        with Image.open(image_path) as img:
            base = img.convert("RGB")
            canvas_size = (target_w, target_h)

            # Fill canvas with a blurred version of the source, then place contained foreground.
            bg = ImageOps.fit(base, canvas_size, method=Image.Resampling.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=22))

            fg = ImageOps.contain(base, canvas_size, method=Image.Resampling.LANCZOS)
            x = (target_w - fg.width) // 2
            y = (target_h - fg.height) // 2
            bg.paste(fg, (x, y))

            fd, normalized_path = tempfile.mkstemp(prefix="li-image-normalized-", suffix=".jpg")
            os.close(fd)
            bg.save(normalized_path, format="JPEG", quality=92, optimize=True)
            return normalized_path
    except Exception as e:
        print(f"[LinkedIn] Warning: image normalization failed, using original image: {e}")
        return image_path


def _organization_author_urn() -> str:
    urn = _env("LINKEDIN_ORGANIZATION_URN")
    if urn:
        return urn

    headers = _headers()
    resp = requests.get(
        f"{LI_API}/organizationalEntityAcls",
        headers=headers,
        params={
            "q": "roleAssignee",
            "role": "ADMINISTRATOR",
            "state": "APPROVED",
        },
        timeout=20,
    )
    if not resp.ok:
        return ""

    elements = resp.json().get("elements", [])
    if not isinstance(elements, list):
        return ""
    for row in elements:
        target = str(row.get("organizationalTarget", "")).strip()
        if target.startswith("urn:li:organization:"):
            return target
    return ""


def _resolve_author_urn() -> str:
    org_urn = _organization_author_urn()
    if org_urn:
        return org_urn

    urn = _env("LINKEDIN_AUTHOR_URN")
    if urn:
        return urn

    headers = _headers()
    errors = []

    # First try OpenID userinfo (works when openid/profile scopes are granted).
    resp_userinfo = requests.get(f"{LI_API}/userinfo", headers=headers, timeout=15)
    if resp_userinfo.ok:
        sub = resp_userinfo.json().get("sub", "").strip()
        if sub:
            return f"urn:li:person:{sub}"
    else:
        errors.append(f"userinfo={resp_userinfo.status_code}:{_response_body(resp_userinfo)}")

    # Fallback to legacy /me endpoint (works with r_liteprofile).
    resp_me = requests.get(f"{LI_API}/me", headers=headers, timeout=15)
    if resp_me.ok:
        person_id = resp_me.json().get("id", "").strip()
        if person_id:
            return f"urn:li:person:{person_id}"
    else:
        errors.append(f"me={resp_me.status_code}:{_response_body(resp_me)}")

    details = " | ".join(errors) if errors else "No author identifier returned"
    raise RuntimeError(
        "Unable to resolve LinkedIn author URN automatically. "
        "Set LINKEDIN_ORGANIZATION_URN for business-page posting or LINKEDIN_AUTHOR_URN for fallback personal posting. "
        f"Diagnostics: {details}"
    )


def _register_image_upload(owner: str) -> tuple[str, str]:
    headers = _headers(include_json=True)
    payload = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "owner": owner,
            "serviceRelationships": [
                {
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent",
                }
            ],
        }
    }
    resp = requests.post(
        f"{LI_API}/assets?action=registerUpload",
        headers=headers,
        json=payload,
        timeout=30,
    )
    _raise_with_body(resp)
    value = resp.json().get("value", {})
    asset = str(value.get("asset", "")).strip()
    upload_url = str(
        value.get("uploadMechanism", {})
        .get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {})
        .get("uploadUrl", "")
    ).strip()
    if not asset or not upload_url:
        raise RuntimeError("LinkedIn image upload registration did not return an asset URN and upload URL")
    return asset, upload_url


def _upload_image_asset(owner: str, image_path: str) -> str:
    asset, upload_url = _register_image_upload(owner)
    upload_headers = {"Authorization": f"Bearer {_required_env('LINKEDIN_ACCESS_TOKEN')}"}
    resp = requests.put(
        upload_url,
        headers=upload_headers,
        data=_file_bytes(image_path),
        timeout=60,
    )
    _raise_with_body(resp)
    return asset


def _post_with_retry(url: str, *, headers: dict, json_payload: dict, timeout: int = 30) -> requests.Response:
    attempts = [0.7, 1.5, 3.0]
    last: requests.Response | None = None
    for idx, wait_s in enumerate(attempts, start=1):
        resp = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
        last = resp
        if resp.ok:
            return resp
        if resp.status_code >= 500 and url.rstrip('/').endswith('/ugcPosts'):
            raise requests.Timeout("publication_outcome_unknown_after_server_error")

        status = resp.status_code
        body = _response_body(resp).lower()
        if status not in (429, 500, 502, 503, 504):
            return resp

        if "throttle" not in body and "rate" not in body and status != 429 and status < 500:
            return resp

        if idx < len(attempts):
            time.sleep(wait_s)

    return last if last is not None else requests.Response()

def _is_duplicate_error(resp: requests.Response) -> bool:
    if resp.status_code not in (400, 409, 422):
        return False
    body = _response_body(resp).lower()
    return "duplicate" in body or "already exists" in body or "same content" in body


def delete(post_urn: str) -> dict:
    """Delete a previously published LinkedIn UGC post by its URN (e.g. urn:li:share:123 or urn:li:ugcPost:123)."""
    from urllib.parse import quote

    urn = str(post_urn or "").strip()
    if urn.startswith("urn:li:share:"):
        urn = "urn:li:ugcPost:" + urn.split(":")[-1]
    resp = requests.delete(
        f"{LI_API}/ugcPosts/{quote(urn, safe='')}",
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code not in (200, 204) and not resp.ok:
        raise RuntimeError(f"linkedin_delete_failed_http_{resp.status_code}: {_response_body(resp)}")
    print(f"[LinkedIn] Deleted: {urn}")
    return {"id": urn, "success": True}


from publication_authorization import guarded as _editorial_guard

@_editorial_guard("linkedin")
def publish(content: dict, wp_link: str, dry_run: bool = False) -> dict:
    text = content['li_text'] if content.get("production_approval") else f"{content['li_text']}\n\n{wp_link}"
    image_path = str((content.get("generated_visuals") or {}).get("linkedin", "")).strip()
    temp_download_path = ""
    temp_normalized_path = ""
    carousel_normalized_paths = []

    if dry_run:
        print(f"[DRY RUN] LinkedIn: would post:\n{text[:150]}...\n")
        return {"id": "dry-run"}

    author = content["destination_accounts"]["linkedin"] if content.get("production_approval") else _resolve_author_urn()
    primary_publish_image_url = str(content.get("primary_publish_image_url", "")).strip()

    if not (image_path and os.path.exists(image_path)):
        candidates: list[str] = []
        if _is_valid_public_image(primary_publish_image_url):
            candidates.append(primary_publish_image_url)
        product_image = str(content.get("product_image_url", "")).strip()
        if _is_valid_public_image(product_image):
            candidates.append(product_image)
        for c in content.get("product_image_candidates", []) or []:
            candidate = str(c or "").strip()
            if _is_valid_public_image(candidate):
                candidates.append(candidate)
        for c in content.get("category_image_candidates", []) or []:
            candidate = str(c or "").strip()
            if _is_valid_public_image(candidate):
                candidates.append(candidate)

        for candidate in _dedupe_keep_order(candidates):
            if not _is_reachable_image_url(candidate):
                continue
            try:
                temp_download_path = _download_image_to_temp(candidate)
                image_path = temp_download_path
                print(f"[LinkedIn] Using fallback downloaded image URL: {candidate}")
                break
            except Exception as e:
                print(f"[LinkedIn] Warning: failed to download fallback image URL: {e}")

    if content.get("owner_supplied_visual") is True and not (image_path and os.path.exists(image_path)):
        raise RuntimeError("linkedin_approved_primary_image_unavailable")

    attached_asset = ""
    share_content = {
        "shareCommentary": {"text": text},
        "shareMediaCategory": "NONE",
    }
    if image_path and os.path.exists(image_path):
        normalized_path = image_path if content.get("production_approval") else _normalize_linkedin_image(image_path)
        if normalized_path != image_path:
            temp_normalized_path = normalized_path
        asset = _upload_image_asset(author, normalized_path)
        attached_asset = asset
        share_content = {
            "shareCommentary": {"text": text},
            "shareMediaCategory": "IMAGE",
            "media": [
                {
                    "status": "READY",
                    "media": asset,
                    "description": {"text": str(content.get("li_text", ""))[:256]},
                    "title": {"text": str(content.get("wp_title", "Infenergy update"))[:180]},
                }
            ],
        }

    if content.get("production_approval") and len(content.get("carousel_assets") or []) > 1:
        media = list(share_content.get("media") or [])
        for image in content["carousel_assets"][1:]:
            original = image["local_path"]
            normalized = original  # Keep the exact approved pixels for this version.
            if normalized != original: carousel_normalized_paths.append(normalized)
            media.append({"status":"READY", "media":_upload_image_asset(author, normalized)})
        share_content["media"] = media

    payload = {
        "author": author,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": share_content,
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        },
    }

    try:
        headers = _headers(include_json=True)
        resp = _post_with_retry(
            f"{LI_API}/ugcPosts",
            headers=headers,
            json_payload=payload,
            timeout=30,
        )
        if _is_duplicate_error(resp) and not content.get("production_approval"):
            # Retry once with a minimal variant to avoid duplicate-content rejection.
            variant = content.get("topic", "power update")
            payload["specificContent"]["com.linkedin.ugc.ShareContent"]["shareCommentary"]["text"] = (
                f"{text}\n\nUpdate: {variant}"
            )
            resp = _post_with_retry(
                f"{LI_API}/ugcPosts",
                headers=headers,
                json_payload=payload,
                timeout=30,
            )
        _raise_with_body(resp)
        post_id = resp.headers.get("x-restli-id", "")
        if not post_id:
            try: post_id = str(resp.json().get("id") or "")
            except (ValueError, AttributeError): pass
        if not post_id: raise requests.Timeout("publication_outcome_unknown_missing_receipt")
        print(f"[LinkedIn] Posted: {post_id}")
        return {
            "id": post_id,
            "media_type": "IMAGE" if attached_asset else "NONE",
            "image_url": primary_publish_image_url if attached_asset else "",
            "asset": attached_asset,
        }
    finally:
        for path in carousel_normalized_paths:
            try: os.remove(path)
            except OSError: pass
        if temp_normalized_path:
            try:
                os.remove(temp_normalized_path)
            except OSError:
                pass
        if temp_download_path:
            try:
                os.remove(temp_download_path)
            except OSError:
                pass
