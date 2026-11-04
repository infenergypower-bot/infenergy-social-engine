"""One final editorial boundary shared by every live provider adapter."""
from __future__ import annotations

import functools
import inspect
import os
from pathlib import Path


def assert_not_scrapped(content: dict, data_dir: str | None = None) -> None:
    if content.get('scrapped') or str(content.get('title', '')).upper().startswith('[SCRAPPED]'):
        raise ValueError('scrapped_content_cannot_publish')
    identifier = (content.get('production_approval') or {}).get('package_id') or content.get('post_id')
    if not identifier:
        return
    import sqlite3
    from inventory_db import get_db_path
    root = data_dir or os.environ.get('DATA_DIR', str(Path(__file__).resolve().parents[1] / 'data'))
    path = Path(get_db_path(root))
    if not path.is_file():
        return
    with sqlite3.connect(path, timeout=5) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='production_packages'").fetchone():
            row = connection.execute('SELECT state FROM production_packages WHERE id=?', (identifier,)).fetchone()
            if row and row[0] == 'SCRAPPED':
                raise ValueError('scrapped_content_cannot_publish')


def authorize_automatic(content: dict, platform: str, data_dir: str | None = None) -> None:
    """Opt-in automation accepts only finished, current, quality-reviewed inventory."""
    from datetime import datetime, timezone
    from dispatch_outbox import _gemini_assets_ready, _gemini_copy_ready
    from publication_contract import evaluate_publication_readiness
    from social.production_quality import require
    require(content,data_dir or os.environ.get('DATA_DIR',str(Path(__file__).resolve().parents[1]/'data')))
    generation = content.get('gemini_generation') or {}
    if not (content.get('gemini_copy') or {}).get('strict_provider') or not generation.get('strict_provider'):
        raise ValueError('automatic_publication_requires_strict_quality_pipeline')
    if not _gemini_copy_ready(content) or not _gemini_assets_ready(content, int(generation.get('required_image_count') or 0)):
        raise ValueError('automatic_publication_quality_checks_failed')
    due = (content.get('platform_schedule') or {}).get(platform) or datetime.now(timezone.utc).isoformat()
    result = evaluate_publication_readiness(content, scheduled_at=due, platforms=[platform], dispatch_enabled=True)
    if not result.ready:
        raise ValueError('automatic_publication_not_ready:' + ','.join(result.reason_codes))


def authorize(content: dict, platform: str, *, data_dir: str | None = None) -> None:
    assert_not_scrapped(content, data_dir)
    if os.environ.get('EDITORIAL_APPROVAL_MODE', 'required') == 'automatic' and not content.get('production_approval'):
        authorize_automatic(content, platform, data_dir)
        return
    # Explicit migration switch for an existing autonomous installation. New
    # installations require revision-bound approval by default.
    if os.environ.get("EDITORIAL_APPROVAL_MODE", "required") == "legacy" and not content.get("production_approval"):
        return
    from production_policy import digest
    from production_workflow import Production
    stamp=content.get("production_approval") or {}
    if not all(stamp.get(k) for k in ["package_id","revision","digest"]):
        raise ValueError("editorial_approval_required_import_into_production_desk")
    root=data_dir or os.environ.get("DATA_DIR",str(Path(__file__).resolve().parents[1]/"data"))
    store=Production(root); item=store.get(stamp["package_id"]); p=item["payload"]; approval=item["approval"] or {}
    if item["revision"]!=stamp["revision"] or approval.get("digest")!=stamp["digest"] or digest(p)!=stamp["digest"]:
        raise ValueError("editorial_approval_stale")
    if platform not in p["platforms"]:raise ValueError("platform_not_approved")
    if not store.checks(item["id"])["passed"]:raise ValueError("approved_asset_or_reference_changed")
    expected=p["captions"][platform]
    post=(content.get("platform_posts") or {}).get(platform) or {}
    actual=post.get("final_caption") or content.get({"facebook":"fb_caption","instagram":"ig_caption","linkedin":"li_text"}.get(platform,"")) or content.get("caption")
    if actual!=expected:raise ValueError("approved_caption_changed")
    caption_key={"facebook":"fb_caption","instagram":"ig_caption","linkedin":"li_text"}.get(platform)
    if caption_key in content and content[caption_key]!=expected:raise ValueError("adapter_caption_changed")
    expected_mode="STORY" if platform=="instagram" and p["rules"].get("aspect_ratio")=="9:16" else ""
    if post.get("media_type","")!=expected_mode or content.get("instagram_reel"):raise ValueError("unapproved_media_type")
    images=post.get("image_urls") or content.get("image_urls") or [post.get("image_url") or content.get("image_url")]
    from urllib.parse import urlparse
    public=os.environ.get("PUBLIC_BASE_URL") or ("https://"+os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
    if any(urlparse(str(s)).scheme!="https" or urlparse(str(s)).netloc!=urlparse(public).netloc or not urlparse(str(s)).path.startswith('/media/') for s in images):
        raise ValueError("approved_media_host_changed")
    if [Path(str(s).split('?',1)[0]).name for s in images]!=[Path(a["path"]).name for a in p["assets"]]:
        raise ValueError("approved_media_changed")
    if any(urlparse(str(url)).path!='/media/'+Path(asset["path"]).name for url,asset in zip(images,p["assets"])):raise ValueError("approved_media_path_changed")
    if content.get("primary_publish_image_url") and content["primary_publish_image_url"]!=images[0]:raise ValueError("adapter_image_changed")
    if any(candidate and candidate!=images[0] for candidate in [post.get("image_url"),post.get("primary_image_url"),content.get("image_url")]):raise ValueError("primary_image_changed")
    if content.get("image_urls") and content["image_urls"]!=images:raise ValueError("top_level_images_changed")
    generated=(content.get("generated_visuals") or {}).get(platform)
    if generated and Path(generated).resolve()!=store.local_asset(p["assets"][0]["path"]):raise ValueError("adapter_local_image_changed")
    carousel=content.get("carousel_assets") or []
    if len(images)>1 or carousel:
        if [a.get("public_url") for a in carousel]!=images:raise ValueError("adapter_carousel_changed")
        if any(Path(a.get("local_path","")).resolve()!=store.local_asset(asset["path"]) for a,asset in zip(carousel,p["assets"])):raise ValueError("adapter_carousel_local_image_changed")
    configured=os.environ.get({"facebook":"META_PAGE_ID","instagram":"META_IG_USER_ID","linkedin":"LINKEDIN_AUTHOR_URN"}.get(platform,""),"")
    if platform=="linkedin":configured=os.environ.get("LINKEDIN_ORGANIZATION_URN","") or configured
    if not configured or configured!=p["destination_accounts"].get(platform):raise ValueError("approved_destination_account_mismatch")


def guarded(platform):
    def decorate(fn):
        signature=inspect.signature(fn)
        @functools.wraps(fn)
        def invoke(*args,**kwargs):
            bound=signature.bind(*args,**kwargs);bound.apply_defaults()
            if not bound.arguments.get("dry_run",False):authorize(bound.arguments["content"],platform)
            return fn(*args,**kwargs)
        return invoke
    return decorate
