import os
import base64
import requests


def _binary_auth_header(mime_type: str, file_name: str) -> dict:
    headers = _auth_header().copy()
    headers["Content-Type"] = mime_type
    headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
    return headers

def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _wp_url() -> str:
    url = _required_env("WP_URL").rstrip("/")
    if url.endswith("/wp-admin"):
        url = url[:-9]
    return url


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


def _auth_header() -> dict:
    username = _required_env("WP_USERNAME")
    app_password = _required_env("WP_APP_PASSWORD")
    token = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


from publication_authorization import guarded as _editorial_guard

@_editorial_guard("wordpress")
def publish(content: dict, dry_run: bool = False) -> dict:
    wp_url = os.environ.get("WP_URL", "https://www.infenergypower.com").rstrip("/")
    if dry_run:
        print(f"[DRY RUN] WordPress: would publish '{content['wp_title']}'")
        return {"id": "dry-run", "link": wp_url}

    payload = {
        "title": content["wp_title"],
        "content": content["wp_content"],
        "excerpt": content["wp_excerpt"],
        "status": "publish",
    }
    live_url = _wp_url()
    resp = requests.post(
        f"{live_url}/wp-json/wp/v2/posts",
        headers=_auth_header(),
        json=payload,
        timeout=30,
    )
    _raise_with_body(resp)
    data = resp.json()
    print(f"[WordPress] Published: {data['link']}")
    return {"id": data["id"], "link": data["link"]}


def upload_media(file_path: str, *, dry_run: bool = False) -> dict:
    if not file_path or not os.path.exists(file_path):
        raise RuntimeError(f"Media file not found: {file_path}")

    wp_url = os.environ.get("WP_URL", "https://www.infenergypower.com").rstrip("/")
    if dry_run:
        print(f"[DRY RUN] WordPress media: would upload '{file_path}'")
        return {"id": "dry-run", "source_url": wp_url}

    file_name = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{_wp_url()}/wp-json/wp/v2/media",
            headers=_binary_auth_header("image/png", file_name),
            data=f.read(),
            timeout=60,
        )
    _raise_with_body(resp)
    data = resp.json()
    source_url = data.get("source_url") or data.get("guid", {}).get("rendered", "")
    print(f"[WordPress] Uploaded media: {source_url}")
    return {"id": data.get("id"), "source_url": source_url}
