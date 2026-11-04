"""One final editorial boundary shared by every live provider adapter."""
from __future__ import annotations

import functools
import inspect
import os
from pathlib import Path


def authorize(content: dict, platform: str, *, data_dir: str | None = None) -> None:
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
