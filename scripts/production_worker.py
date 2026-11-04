"""Durable creative worker. Never publishes; scheduling feeds the existing outbox."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from production_policy import context, digest
from production_workflow import Production, now


def text_stage(store, job, payload):
    from social import model_router
    rules=payload["rules"]
    if job["stage"]=="caption" and not payload["concept"]:
        raise ValueError("develop_concept_before_caption")
    source=context()
    # The brand/Constitution are stable references; no compiled checkout-first defaults.
    owner={x["path"]:x["data"] for x in source["sources"]}
    company=owner.pop("data/marketing/infenergy_company_knowledge.json")
    owner["company"]={k:company[k] for k in ["brand","voice","product_truth_policy","values","consumer_benefits","content_pillars","editorial_playbook","visual_language"]}
    product_sources={}
    if payload["product_id"]:
        for root in [Path(store.data_dir),Path(__file__).resolve().parents[1]/"data"]:
            candidate=(root/"product_briefs"/(payload["product_id"]+".json")).resolve()
            if candidate.parent==root.resolve()/"product_briefs" and candidate.is_file():
                product_sources["product_brief"]=json.loads(candidate.read_text());break
        profiles=Path(__file__).resolve().parents[1]/"data/marketing/product_consumer_profiles.json"
        product_sources["consumer_profile"]=json.loads(profiles.read_text()).get("profiles",{}).get(payload["product_id"],{})
    references=store.references(payload["references"])
    if rules["character"] and not any(r["kind"]=="character" for r in references):
        raise ValueError("register_and_select_character_canon_before_generation")
    with store.db() as c:
        examples=[dict(r) for r in c.execute("SELECT verdict,explanation,content FROM production_examples WHERE format=? ORDER BY created_at DESC LIMIT 6",(rules["format_id"],))]
        recent=[json.loads(r[0]).get("concept",{}) for r in c.execute("SELECT payload FROM production_packages WHERE id<>? ORDER BY updated_at DESC LIMIT 12",(job["package_id"],))]
    if job["stage"]=="concept":
        schema="Return JSON {human_moment,hook,payoff,visual_direction,brain_movement,heart_response,storyboard:[{action,dialogue,shot}],image_text:[string],novelty_reason}. Develop the premise before copy. Supply exactly one storyboard and one concise image_text string per format card; use an empty string when a frame needs no words. For controlled text, reserve the bottom 23% as negative space; keep faces, hands and the product clear of that region. For integrated text, words must physically interact with the scene. No permanent canon invention."
        if rules.get("panels"):
            schema+=" StoryPage exception: storyboard contains ONE page with panels:[{action,dialogue,speaker}] of exactly four panels. The first panel is the hero panel; subsequent panels progress top-to-bottom to an earned brand ending. Dialogue is at most 10 words per panel. image_text contains ONE string joining the exact panel dialogue in reading order. Leave text-free space at the bottom of each panel, not one page-wide headline region."
    else:
        schema="Return JSON {captions: {"+",".join(p+': string' for p in payload["platforms"])+"}}. Captions add meaning beyond the declared image text. Respect the format, no forced sale or question, no universal paragraph/hashtag minimum."
    prompt=json.dumps({"task":schema,"owner_sources":owner,"rules":rules,"brief":payload["brief"],"locked_concept":payload["concept"],"image_text":payload["image_text"],"product_evidence":payload["evidence"],"product_sources":product_sources,"references":references,"examples":examples,"recent_concepts":recent},ensure_ascii=False)
    result=model_router.generate_json("topic_generation" if job["stage"]=="concept" else "copy_editing",prompt,system_instruction="You are Infenergy's creative editor. Honor explicit owner direction and canonical source precedence. Draft content only; do not claim approval, publication or viral outcomes.")
    if not isinstance(result,dict): raise ValueError("text_generation_unavailable_check_model_route_or_credentials")
    provenance=list(payload.get("provenance",[]))+[{"stage":job["stage"],"provider":"gemini","model_route":model_router.route_for("topic_generation" if job["stage"]=="concept" else "copy_editing"),"prompt_sha256":digest(prompt),"rules_digest":rules["digest"],"sources":[{"path":s["path"],"sha256":s["sha256"]} for s in source["sources"]],"reference_ids":payload["references"],"created_at":now()}]
    if job["stage"]=="concept":
        if not all(isinstance(result.get(k),str) and result[k].strip() for k in ["human_moment","hook","payoff","visual_direction","brain_movement","heart_response"]): raise ValueError("concept_response_incomplete")
        text=result.pop("image_text",[])
        if not isinstance(text,list) or len(text)!=rules["cards"] or any(not isinstance(s,str) for s in text):raise ValueError("concept_requires_exact_text_for_each_frame")
        if not isinstance(result.get("storyboard"),list) or len(result["storyboard"])!=rules["cards"]:raise ValueError("concept_storyboard_frame_count_mismatch")
        if rules["typography"]=="controlled":
            if rules.get("panels"):
                panels=result["storyboard"][0].get("panels") or []
                if len(panels)!=rules["panels"] or any(not isinstance(p,dict) or not p.get("action") or len(str(p.get("dialogue","")).split())>10 for p in panels):raise ValueError("storypage_requires_four_readable_panels")
                positions=[(.295,.10),(.495,.075),(.675,.075),(.855,.09)]
                result["text_layers"]=[[{"text":str(p["dialogue"]),"style":"dialogue","panel_index":i,"rect":[.06,positions[i][0],.88,positions[i][1]]} for i,p in enumerate(panels) if p.get("dialogue")]]
                text=[" ".join(str(p.get("dialogue","")) for p in panels).strip()]
            else:result["text_layers"]=[[{"text":s,"style":"headline","rect":[.06,.70,.88,.23]}] if s else [] for s in text]
        for key in ["current_source","source_published_at","relevance_expires_at"]:
            if key in payload["concept"]:result[key]=payload["concept"][key]
        return {"concept":result,"image_text":text,"provenance":provenance}
    captions=result.get("captions")
    if not isinstance(captions,dict) or any(not isinstance(captions.get(p),str) or not captions[p].strip() for p in payload["platforms"]):raise ValueError("caption_response_incomplete")
    return {"captions":{p:captions[p] for p in payload["platforms"]},"provenance":provenance}


def visual_stage(store, job, payload):
    """One paid image attempt, using the actual owner-approved image bytes.

    Studio remains available in the advanced workspace. This path avoids its
    multi-candidate fanout and hidden repairs; every repair is an explicit job.
    """
    if not payload["concept"]: raise ValueError("concept_required_before_visual_production")
    refs=store.references(payload["references"])
    if payload["rules"]["character"] and not any(r["kind"]=="character" for r in refs): raise ValueError("character_canon_required")
    if payload["rules"]["product"]=="required" and (not payload["evidence"] or not any(r["kind"]=="product" for r in refs)): raise ValueError("verified_product_evidence_and_reference_required")
    index=int(job["stage"].split(':')[1]) if ':' in job["stage"] else len(payload["assets"])
    if index>len(payload["assets"]) or index>=payload["rules"]["cards"]: raise ValueError("invalid_frame_index")
    if not os.environ.get("GEMINI_API_KEY"): raise ValueError("gemini_connection_required")
    from google import genai
    from google.genai import types
    from social.gemini_budget import reserve_gemini_call, complete_gemini_call
    from production_policy import digest
    import mimetypes
    instructions={"rules":payload["rules"],"locked_concept":payload["concept"],"frame_index":index,
        "frame":(payload["concept"].get("storyboard") or [])[index] if index<len(payload["concept"].get("storyboard") or []) else payload["concept"]["visual_direction"],
        "exact_image_text":payload["image_text"][index:index+1],"product_id":payload["product_id"],"product_role":payload["product_role"],"verified_evidence":payload["evidence"],
        "canon":[{"kind":r["kind"],"name":r["name"],"version":r["version"],"canon":r["canon"]} for r in refs]}
    aspect=payload["rules"].get("aspect_ratio","4:5")
    prompt=f"Create ONE finished {aspect} image for this locked frame. Preserve approved identity, product geometry and story continuity from the attached references. Do not introduce new permanent canon or unsupported capabilities. "
    if payload["rules"].get("panels"):prompt+="This is one 9:16 StoryPage with four distinct panels in top-to-bottom reading order: a large hero panel in the first 40%, then three smaller panels in the remaining space, ending in the branded story payoff. Match the exact panel actions and leave the declared dialogue rectangles unlettered. "
    if payload["rules"]["typography"]=="controlled": prompt+="Create unlettered artwork with clear negative space for separately typeset text. No words, watermarks, invented labels or speech balloons. "
    elif payload["rules"]["typography"]=="integrated": prompt+="Render ONLY the exact declared text, physically integrated with the scene, with correct spelling and credible perspective, shadow and interaction. "
    else: prompt+="No visible lettering. "
    prompt+=json.dumps(instructions,ensure_ascii=False)
    contents=[prompt]
    for ref in refs:
        for asset in ref["files"]:
            actual=store.asset_record(asset["path"])
            if actual["sha256"]!=asset["sha256"]: raise ValueError("approved_reference_file_changed")
            contents.append(types.Part.from_bytes(data=store.local_asset(asset["path"]).read_bytes(),mime_type=mimetypes.guess_type(asset["path"])[0] or "image/png"))
    if payload["assets"]:
        contents.append("Earlier draft frame for visual continuity; do not copy its words or composition:")
        previous=payload["assets"][max(0,index-1)]["path"]
        contents.append(types.Part.from_bytes(data=store.local_asset(previous).read_bytes(),mime_type=mimetypes.guess_type(previous)[0] or "image/png"))
    model=os.environ.get("GEMINI_IMAGE_MODEL","gemini-2.5-flash-image")
    client=genai.Client(api_key=os.environ["GEMINI_API_KEY"],http_options=types.HttpOptions(timeout=180_000,retry_options=types.HttpRetryOptions(attempts=1)))
    reserved=reserve_gemini_call("image",model,"production_frame",package_id=job["package_id"],asset_id=str(index),initiating_subsystem="production_worker")
    try:
        response=client.models.generate_content(model=model,contents=contents,config=types.GenerateContentConfig(response_modalities=["TEXT","IMAGE"],image_config=types.ImageConfig(aspect_ratio=aspect)))
    except Exception:
        complete_gemini_call(reserved["call"]["call_id"],succeeded=False)
        raise
    usage=getattr(response,"usage_metadata",None)
    complete_gemini_call(reserved["call"]["call_id"],succeeded=True,actual_usage=usage.model_dump(mode="json") if usage else {})
    images=[part.inline_data.data for candidate in response.candidates or [] for part in candidate.content.parts or [] if getattr(part,"inline_data",None) and getattr(part.inline_data,"mime_type","").startswith("image/")]
    if len(images)!=1: raise ValueError("provider_must_return_exactly_one_image")
    from io import BytesIO
    from PIL import Image
    target=Path(store.data_dir)/"public_media"/f"production-{job['id']}-{index}.png"
    target.parent.mkdir(parents=True,exist_ok=True)
    from production_images import normalize
    with Image.open(BytesIO(images[0])) as image:
        if min(image.size)<720:raise ValueError("provider_artwork_resolution_too_low")
        normalize(image,aspect).save(target,"PNG")
    asset={**store.asset_record(str(target.relative_to(store.data_dir))),"provider":"gemini","model":model,"prompt_digest":digest(instructions),"frame_index":index}
    existing=list(payload["assets"])
    if index<len(existing):existing[index]=asset
    else:existing.append(asset)
    return {"assets":existing,"provenance":list(payload.get("provenance",[]))+[{"stage":"visual","frame_index":index,"provider":"gemini","model":model,"call_id":reserved["call"]["call_id"],"reference_ids":payload["references"],"actual_usage":usage.model_dump(mode="json") if usage else {},"created_at":now()}]}


def schedule_stage(store, job, payload):
    from content_operations import mark_ready
    from production_policy import digest
    item=store.get(job["package_id"])
    if not item["approval"] or item["approval"]["digest"]!=digest(payload):raise ValueError("approval_changed")
    check=store.checks(item["id"])
    if not check["passed"]:raise ValueError("current_assets_or_review_invalid")
    base=os.environ.get("PUBLIC_BASE_URL") or ("https://"+os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
    if not base.startswith("https://"):raise ValueError("public_https_media_base_required_for_scheduling")
    outbox_id=hashlib.sha256((item["id"]+":"+str(item["revision"])).encode()).hexdigest()[:32]
    images=[base.rstrip('/')+"/media/"+Path(a["path"]).name for a in payload["assets"]]
    package={"post_id":item["id"],"content_id":item["id"],"title":payload["title"],"product_id":payload["product_id"],"image_url":images[0],"image_urls":images,
             "generated_visuals":{p:str(store.local_asset(payload["assets"][0]["path"])) for p in payload["platforms"]},
             "routing":{"platforms":payload["platforms"]},"platform_posts":{p:{"platform":p,"final_caption":payload["captions"][p],"image_url":images[0],"image_urls":images,**({"media_type":"STORY"} if p=="instagram" and payload["rules"].get("aspect_ratio")=="9:16" else {})} for p in payload["platforms"]},
             "carousel_assets":[{"public_url":url,"local_path":str(store.local_asset(asset["path"]))} for url,asset in zip(images,payload["assets"])],
             "production_approval":{"package_id":item["id"],"revision":item["revision"],"digest":item["approval"]["digest"]},"destination_accounts":payload["destination_accounts"]}
    mark_ready(store.data_dir,content_date=payload["scheduled_at"][:10],slot=payload["slot"],scheduled_at=payload["scheduled_at"],decision_id="production:"+item["id"],package=package,outbox_id=outbox_id)
    return {"outbox_id":outbox_id}


def run_one(store, owner="worker"):
    job=store.claim(owner)
    if not job:return False
    import threading
    stop=threading.Event()
    def renew():
        while not stop.wait(30):
            if not store.heartbeat(job): break
    heartbeat=threading.Thread(target=renew,daemon=True);heartbeat.start()
    # Receipts survive a crash between provider completion and the SQL commit.
    # A reconciled retry reuses the receipt instead of spending again.
    receipt=Path(store.data_dir)/"production_receipts"/(job["id"]+".json")
    try:
        payload=store.get(job["package_id"])["payload"]
        if receipt.is_file():
            outcome=json.loads(receipt.read_text())
            if outcome["revision"]!=job["revision"]:raise ValueError("receipt_revision_mismatch")
            store.finish(job,updates=outcome.get("updates"),result=outcome.get("result"))
            return True
        if job["stage"] in {"concept","caption"} or job["stage"].startswith("visual"):
            from social.gemini_budget import preflight_gemini_workflow, GeminiBudgetExceeded
            try:
                preflight_gemini_workflow(image_calls=int(job["stage"].startswith("visual")),reasoning_calls=int(job["stage"] in {"concept","caption"}))
            except GeminiBudgetExceeded:
                store.wait_for_budget(job)
                return True
        updates=None;result=None
        if job["stage"] in {"concept","caption"}: updates=text_stage(store,job,payload)
        elif job["stage"].split(':')[0]=="visual": updates=visual_stage(store,job,payload)
        elif job["stage"]=="schedule": result=schedule_stage(store,job,payload)
        elif job["stage"]=="inspect":
            from production_typography import inspect_text
            from PIL import Image,ImageOps
            assets=[]
            for index,asset in enumerate(payload["assets"]):
                if "instagram" in payload["platforms"]:
                    path=store.local_asset(asset["path"])
                    with Image.open(path) as source:
                        if source.format!="JPEG" or source.mode!="RGB" or path.suffix.lower() not in {".jpg",".jpeg"} or source.getexif().get(274,1)!=1:
                            target=Path(store.data_dir)/"public_media"/f"delivery-{job['id']}-{index}.jpg"
                            ImageOps.exif_transpose(source).convert("RGB").save(target,"JPEG",quality=95,subsampling=0,optimize=True)
                            asset={**asset,**store.asset_record(str(target.relative_to(store.data_dir))),"source_path":asset.get("source_path") or asset["path"]}
                expected=payload["image_text"][index:index+1]
                assets.append({**asset,"inspection":{"sha256":store.asset_record(asset["path"])["sha256"],"expected":expected,"ocr":inspect_text(store.local_asset(asset["path"]),expected,regions=(asset.get("typography") or {}).get("layers"))}})
            updates={"assets":assets}
        elif job["stage"]=="typography":
            if payload["rules"]["typography"]!="controlled":raise ValueError("this_format_requires_integrated_scene_text")
            layers=payload["concept"].get("text_layers") or []
            if len(layers)!=len(payload["assets"]):raise ValueError("text_layers_required_for_each_asset")
            from production_typography import render
            assets=[]
            for index,asset in enumerate(payload["assets"]):
                target=Path(store.data_dir)/"public_media"/f"type-{job['id']}-{index}.png"
                source=asset.get("source_path") or asset["path"]
                if not layers[index]:
                    from PIL import Image
                    from production_images import normalize
                    with Image.open(store.local_asset(source)) as image:
                        if min(image.size)<720:raise ValueError("source_artwork_resolution_too_low")
                        normalize(image,payload["rules"].get("aspect_ratio","4:5")).save(target,"PNG")
                    assets.append({**store.asset_record(str(target.relative_to(store.data_dir))),"source_path":source})
                    continue
                per_frame=payload["concept"].get("protected_regions_by_frame") or []
                protected=per_frame[index] if index<len(per_frame) else payload["concept"].get("protected_regions",[])
                receipt_type=render(store.local_asset(source),target,layers[index],protected=protected,aspect_ratio=payload["rules"].get("aspect_ratio","4:5"))
                assets.append({**store.asset_record(str(target.relative_to(store.data_dir))),"source_path":source,"typography":receipt_type})
            updates={"assets":assets}
        receipt.parent.mkdir(parents=True,exist_ok=True)
        temporary=receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps({"revision":job["revision"],"updates":updates,"result":result}))
        os.replace(temporary,receipt)
        store.finish(job,updates=updates,result=result)
    except Exception as exc:
        from production_workflow import Conflict
        if isinstance(exc,Conflict):return True
        # Keep provider URLs, credentials and exception bodies out of the audit log.
        code=str(exc) if type(exc) is ValueError else type(exc).__name__
        try:store.finish(job,error=code[:300],retryable=False)
        except Conflict:pass
    finally:
        stop.set();heartbeat.join(timeout=1)
    return True


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--once",action="store_true");args=parser.parse_args()
    store=Production(os.environ.get("DATA_DIR",str(Path(__file__).resolve().parents[1]/"data")))
    while True:
        worked=run_one(store,"pid:"+str(os.getpid()))
        if args.once:break
        if not worked:time.sleep(2)


if __name__=="__main__":main()
