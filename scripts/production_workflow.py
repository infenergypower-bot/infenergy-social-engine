"""Versioned creative production, leased jobs and editorial approvals.

Uses the existing inventory database and delivery outbox. No publisher is called
by this module. Durable jobs are run by production_worker.py, never an HTTP thread.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from production_policy import FAMILIES, caption_review, digest, resolve, visual_message_digest


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("nonnegative_finite_number_required")
    return result


class Conflict(ValueError):
    pass


class Production:
    def __init__(self, data_dir: str):
        from content_operations import init_content_operations
        self.data_dir = str(Path(data_dir).resolve())
        self.db_path = init_content_operations(self.data_dir)
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS production_packages (
              id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL,
              payload TEXT NOT NULL, approval TEXT, outbox_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS production_revisions (
              package_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
              actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(package_id,revision));
            CREATE TABLE IF NOT EXISTS production_jobs (
              id TEXT PRIMARY KEY, package_id TEXT NOT NULL, revision INTEGER NOT NULL,
              stage TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL DEFAULT 2, owner TEXT, lease_until TEXT,
              available_at TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL,
              UNIQUE(package_id,revision,stage));
            CREATE INDEX IF NOT EXISTS production_jobs_due ON production_jobs(state,available_at);
            CREATE TABLE IF NOT EXISTS production_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, package_id TEXT, actor TEXT NOT NULL,
              event TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS production_references (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
              files TEXT NOT NULL, canon TEXT NOT NULL, approved_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS production_observations (
              id TEXT PRIMARY KEY, package_id TEXT NOT NULL, platform TEXT NOT NULL,
              window_hours INTEGER NOT NULL, metrics TEXT NOT NULL, source TEXT NOT NULL,
              collected_at TEXT NOT NULL, UNIQUE(package_id,platform,window_hours,collected_at));
            CREATE TABLE IF NOT EXISTS production_examples (
              id TEXT PRIMARY KEY, format TEXT NOT NULL, verdict TEXT NOT NULL, explanation TEXT NOT NULL,
              content TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            c.execute("BEGIN IMMEDIATE")
            if "next_stages" not in {r[1] for r in c.execute("PRAGMA table_info(production_jobs)")}:
                c.execute("ALTER TABLE production_jobs ADD COLUMN next_stages TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def event(self, c, package_id, event, detail=None, actor="owner"):
        c.execute("INSERT INTO production_events(package_id,actor,event,detail,created_at) VALUES(?,?,?,?,?)",
                  (package_id, actor, event, json.dumps(detail or {}), now()))

    def get(self, identifier: str) -> dict:
        with self.db() as c:
            row = c.execute("SELECT * FROM production_packages WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise KeyError("production_package_not_found")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["approval"] = json.loads(result["approval"]) if result["approval"] else None
        return result

    def create(self, body: dict) -> dict:
        format_id = str(body.get("format", "educational_story_carousel"))
        rules = resolve(format_id, objective=body.get("objective", "audience_growth"), overrides=body.get("overrides"))
        brief = str(body.get("brief", "")).strip()
        if not brief or len(brief) > 12000:
            raise ValueError("brief_required_max_12000_characters")
        payload = {"title": str(body.get("title") or brief[:80]), "brief": brief, "rules": rules,
                   "platforms": body.get("platforms", ["instagram"]), "concept": {}, "captions": {},
                   "image_text": [], "assets": [], "references": [], "scheduled_at": "", "slot": "midday",
                   "product_id": "", "product_role": "", "evidence": [], "review": {}, "destination_accounts": {}, "provenance":[]}
        self.validate_payload(payload)
        configured={"facebook":os.environ.get("META_PAGE_ID",""),"instagram":os.environ.get("META_IG_USER_ID",""),"linkedin":os.environ.get("LINKEDIN_ORGANIZATION_URN") or os.environ.get("LINKEDIN_AUTHOR_URN","")}
        payload["destination_accounts"]={p:configured[p] for p in payload["platforms"] if p in configured and configured[p]}
        identifier = uuid.uuid4().hex
        with self.db() as c:
            c.execute("INSERT INTO production_packages VALUES(?,1,'CONCEPT',?,NULL,NULL,?,?)", (identifier, json.dumps(payload), now(), now()))
            c.execute("INSERT INTO production_revisions VALUES(?,1,?,'owner',?)", (identifier, json.dumps(payload), now()))
            self.event(c, identifier, "CREATED")
        return self.get(identifier)

    @staticmethod
    def validate_payload(payload):
        for key in ["title", "brief", "product_id", "product_role"]:
            if not isinstance(payload.get(key), str) or len(payload[key]) > 12000:
                raise ValueError("invalid_"+key)
        if not isinstance(payload.get("platforms"), list) or not payload["platforms"] or any(not isinstance(p,str) for p in payload["platforms"]) or set(payload["platforms"]) - {"facebook", "instagram", "linkedin"}:
            raise ValueError("supported_platforms_required")
        if len(payload["platforms"]) != len(set(payload["platforms"])):
            raise ValueError("duplicate_platform")
        if payload.get("scheduled_at"):
            dt = datetime.fromisoformat(payload["scheduled_at"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("schedule_requires_timezone_offset")
        if payload.get("slot") not in {"morning", "midday", "evening"}:
            raise ValueError("invalid_slot")
        for field, cls in [("concept", dict), ("captions", dict), ("assets", list), ("references", list), ("review", dict), ("image_text", list), ("evidence", list), ("destination_accounts", dict)]:
            if not isinstance(payload.get(field), cls):
                raise ValueError(f"invalid_{field}")
        if any(not isinstance(s, str) or len(s) > 500 for s in payload["image_text"]):
            raise ValueError("invalid_image_text")
        if any(not isinstance(s, str) or len(s) > 5000 for s in payload["captions"].values()):
            raise ValueError("invalid_caption")
        payload["captions"]={k:v.strip() for k,v in payload["captions"].items()}
        if any(not isinstance(a, dict) or not isinstance(a.get("path"), str) or not a.get("sha256") for a in payload["assets"]):
            raise ValueError("invalid_asset_record")
        if any(not isinstance(v, str) for v in payload["references"] + payload["evidence"]):
            raise ValueError("invalid_references_or_evidence")
        if any(not isinstance(v, str) for v in payload["destination_accounts"].values()):
            raise ValueError("invalid_destination_account")
        if len(payload["assets"]) > payload["rules"]["cards"]:
            raise ValueError("too_many_assets_for_format")
        for key in ["human_moment","hook","payoff","visual_direction","brain_movement","heart_response"]:
            if key in payload["concept"] and not isinstance(payload["concept"][key],str):raise ValueError("invalid_concept_"+key)
        if "storyboard" in payload["concept"] and (not isinstance(payload["concept"]["storyboard"],list) or any(not isinstance(f,dict) for f in payload["concept"]["storyboard"])):raise ValueError("invalid_storyboard")

    def revise(self, identifier, updates, revision, *, actor="owner"):
        allowed = {"title", "brief", "concept", "captions", "image_text", "references", "scheduled_at", "slot", "product_id", "product_role", "evidence", "review", "destination_accounts", "platforms", "assets"}
        if set(updates) - allowed:
            raise ValueError("unsupported_revision_field")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM production_packages WHERE id=?", (identifier,)).fetchone()
            if not row or row["revision"] != int(revision):
                raise Conflict("revision_conflict_reload_package")
            if row['state'] == 'SCRAPPED':
                raise Conflict('scrapped_package_cannot_be_revised')
            if row["outbox_id"] or row["state"] == "SCHEDULING":
                raise Conflict("scheduled_package_requires_new_draft")
            if c.execute("SELECT 1 FROM production_jobs WHERE package_id=? AND state='RUNNING'", (identifier,)).fetchone():
                raise Conflict("production_in_progress")
            payload = json.loads(row["payload"])
            payload.update(updates)
            # Refresh a draft's rules through a revision, preserving the old revision and approval history.
            prior_rules = payload["rules"]
            if prior_rules["typography"] == "controlled":
                payload["concept"] = {key: value for key, value in payload["concept"].items() if key not in {
                    "text_layers", "protected_regions", "protected_regions_by_frame",
                }}
            payload["rules"] = resolve(prior_rules["format_id"], objective=prior_rules["objective"], overrides={
                "tone": prior_rules["tone"], "natural_response": prior_rules["natural_response"],
                "typography": "integrated" if prior_rules["typography"] == "controlled" else prior_rules["typography"],
            })
            if set(updates) - {"review"}:
                payload["review"] = {}
                payload.pop('production_quality',None)
            self.validate_payload(payload)
            c.execute("UPDATE production_packages SET revision=revision+1,payload=?,approval=NULL,state='DRAFT',updated_at=? WHERE id=?", (json.dumps(payload), now(), identifier))
            c.execute("INSERT INTO production_revisions VALUES(?,?,?,?,?)", (identifier, int(revision)+1, json.dumps(payload), actor, now()))
            c.execute("UPDATE production_jobs SET state='SUPERSEDED' WHERE package_id=? AND state IN ('QUEUED','RETRY','WAITING_BUDGET')", (identifier,))
            self.event(c, identifier, "REVISED", {"fields": sorted(updates)}, actor)
        return self.get(identifier)

    def scrap(self, identifier, revision):
        """Retain the record and receipts while preventing any further production."""
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM production_packages WHERE id=?', (identifier,)).fetchone()
            if not row or row['revision'] != int(revision):
                raise Conflict('revision_conflict_reload_package')
            if row['outbox_id'] or row['state'] == 'SCHEDULING':
                raise Conflict('scheduled_package_requires_delivery_hold_first')
            if c.execute("SELECT 1 FROM production_jobs WHERE package_id=? AND state='RUNNING'", (identifier,)).fetchone():
                raise Conflict('production_in_progress')
            c.execute("UPDATE production_packages SET state='SCRAPPED',revision=revision+1,approval=NULL,updated_at=? WHERE id=?", (now(),identifier))
            c.execute('INSERT INTO production_revisions VALUES(?,?,?,?,?)', (identifier,int(revision)+1,row['payload'],'owner',now()))
            c.execute("UPDATE production_jobs SET state='SUPERSEDED',next_stages='[]' WHERE package_id=? AND state IN ('QUEUED','RETRY','WAITING_BUDGET')", (identifier,))
            self.event(c, identifier, 'SCRAPPED', {'reason':'owner_rejected_content'})
        return self.get(identifier)

    def local_asset(self, relative_path: str) -> Path:
        root = Path(self.data_dir).resolve()
        p = (root / relative_path).resolve()
        if root not in p.parents or not p.is_file() or p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("asset_must_be_image_inside_data_directory")
        return p

    def asset_record(self, relative_path):
        from PIL import Image
        p = self.local_asset(relative_path)
        with Image.open(p) as im:
            im.verify()
        with Image.open(p) as im:
            width, height = im.size
            image_format=im.format
        import hashlib
        return {"path": p.relative_to(self.data_dir).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "width": width, "height": height,"format":image_format}

    def delivery_asset(self, relative_path):
        import shutil
        record=self.asset_record(relative_path)
        source=self.local_asset(record["path"])
        if source.parent==Path(self.data_dir)/"public_media":return record
        target=Path(self.data_dir)/"public_media"/("import-"+record["sha256"]+source.suffix.lower())
        target.parent.mkdir(exist_ok=True)
        if not target.exists():shutil.copyfile(source,target)
        result=self.asset_record(str(target.relative_to(self.data_dir)))
        if result["sha256"]!=record["sha256"]:raise ValueError("public_media_copy_changed")
        return result

    def register_reference(self, body):
        if body.get("kind") not in {"character", "product", "brand"} or not str(body.get("name", "")).strip() or not str(body.get("canon", "")).strip():
            raise ValueError("reference_kind_name_and_canon_required")
        files = [self.asset_record(s) for s in body.get("files", [])]
        if not files:
            raise ValueError("approved_reference_images_required")
        identifier = uuid.uuid4().hex
        with self.db() as c:
            c.execute("INSERT INTO production_references VALUES(?,?,?,?,?,?,?,?)", (identifier, body["kind"], body["name"], str(body.get("version", "1")), json.dumps(files), str(body["canon"]), "owner", now()))
            self.event(c, None, "REFERENCE_APPROVED", {"reference_id": identifier})
        return identifier

    def references(self, identifiers=None):
        with self.db() as c:
            rows = c.execute("SELECT * FROM production_references ORDER BY created_at DESC").fetchall()
        return [{**dict(r), "files": json.loads(r["files"])} for r in rows if identifiers is None or r["id"] in identifiers]

    def checks(self, identifier):
        item = self.get(identifier); p = item["payload"]; errors = []; warnings = []
        if item['state'] == 'SCRAPPED': errors.append('scrapped_package_cannot_publish')
        from social.production_quality import enabled as quality_enabled,ready as quality_ready
        if quality_enabled() and not quality_ready(p,self.data_dir):errors.append('production_quality_review_required_or_stale')
        concept = p["concept"]
        for key in ["human_moment", "hook", "payoff", "visual_direction", "brain_movement", "heart_response"]:
            if not str(concept.get(key, "")).strip(): errors.append("concept_"+key+"_missing")
        refs = self.references(p["references"])
        if len(refs) != len(set(p["references"])): errors.append("reference_not_found")
        if p["rules"]["character"] and not any(r["kind"] == "character" for r in refs): errors.append("character_canon_required")
        if (p["rules"]["product"] == "required" or p["product_id"] or any(r["kind"]=="product" for r in refs)) and (not p["product_id"] or not p["product_role"] or not p["evidence"] or not any(r["kind"] == "product" for r in refs)):
            errors.append("product_identity_role_evidence_and_reference_required")
        for ref in refs:
            for asset in ref["files"]:
                try:
                    if self.asset_record(asset["path"])["sha256"] != asset["sha256"]: errors.append("reference_file_changed")
                except (ValueError, OSError): errors.append("reference_file_missing")
        with self.db() as c:
            recent = [json.loads(r[0]) for r in c.execute("SELECT payload FROM production_packages WHERE id<>? ORDER BY updated_at DESC LIMIT 30", (identifier,))]
        for platform in p["platforms"]:
            review = caption_review(p["captions"].get(platform, ""), image_text=p["image_text"], recent=[r.get("captions", {}).get(platform, "") for r in recent])
            errors.extend(platform+":"+x for x in review["findings"])
            if not p["destination_accounts"].get(platform): errors.append(platform+":destination_account_required")
            limit = {"instagram":2200,"linkedin":3000,"facebook":5000}[platform]
            if len(p["captions"].get(platform,"")) > limit: errors.append(platform+":caption_exceeds_platform_limit")
        if len(p["assets"]) != p["rules"]["cards"]: errors.append("finished_assets_required_for_each_frame")
        for index, asset in enumerate(p["assets"]):
            try:
                actual = self.asset_record(asset["path"])
                if self.local_asset(asset["path"]).parent!=Path(self.data_dir)/"public_media":errors.append("finished_asset_must_be_in_public_media")
                if actual["sha256"] != asset["sha256"]: errors.append("asset_file_changed")
                if p["rules"]["typography"] != "controlled":
                    if (asset.get("typography") or {}).get("mode") == "controlled":
                        errors.append("regenerate_frame_with_text_inside_the_image")
                    if asset.get("provider") == "gemini" and (
                        asset.get("image_message_version") != 1
                        or asset.get("message_sha256") != visual_message_digest(p, index)
                    ):
                        errors.append("image_message_changed_regenerate_the_frame")
                if min(actual["width"], actual["height"]) < 720: errors.append("asset_resolution_too_low")
                if "instagram" in p["platforms"] and (actual["format"]!="JPEG" or Path(actual["path"]).suffix.lower() not in {".jpg",".jpeg"}):errors.append("instagram_delivery_requires_jpeg_inspect_final_assets")
                ratio=actual["width"]/actual["height"]
                if not ((.55<=ratio<=.58) if p["rules"].get("aspect_ratio")=="9:16" else (.8<=ratio<=1.91)): errors.append("asset_aspect_ratio_unsupported")
                inspection=asset.get("inspection") or {}
                if inspection.get("sha256") != actual["sha256"]: errors.append("final_pixel_inspection_required")
                elif inspection.get("ocr",{}).get("status") != "PASS":
                    warnings.append("ocr_requires_manual_text_review")
                    if len(str(p["review"].get("text_exception","")).strip()) < 12: errors.append("ocr_exception_explanation_required")
                if inspection.get("ocr",{}).get("low_confidence_candidates") or inspection.get("ocr",{}).get("background_review_candidates"):warnings.append("background_shapes_require_visual_review")
            except (KeyError, ValueError, OSError): errors.append("asset_missing_or_invalid")
        if p["rules"]["typography"] != "none" and not p["image_text"]: warnings.append("no_visible_text_declared")
        if not p["scheduled_at"]: errors.append("schedule_required")
        if p["rules"]["format_id"]=="today_news":
            from urllib.parse import urlparse
            try:
                published=datetime.fromisoformat(str(concept["source_published_at"]).replace("Z","+00:00"))
                expiry=datetime.fromisoformat(str(concept["relevance_expires_at"]).replace("Z","+00:00"))
                due=datetime.fromisoformat(p["scheduled_at"].replace("Z","+00:00"))
                if not published.tzinfo or not expiry.tzinfo or not published<=due<=expiry or expiry<datetime.now(timezone.utc) or expiry>published+timedelta(days=7):raise ValueError("stale_news")
                if urlparse(str(concept["current_source"])).scheme!="https":raise ValueError("source_url_required")
            except (KeyError,ValueError,TypeError):errors.append("current_news_requires_source_and_valid_relevance_window_max_7_days")
        if p["rules"]["cards"]>1 and len(concept.get("storyboard",[]))!=p["rules"]["cards"]: errors.append("storyboard_frame_count_mismatch")
        if p["rules"]["cards"]>1 and any(not str(f.get("action","")).strip() for f in concept.get("storyboard",[])):errors.append("storyboard_action_missing")
        if p["rules"].get("panels") and (not concept.get("storyboard") or len(concept["storyboard"][0].get("panels",[]))!=p["rules"]["panels"]):errors.append("storypage_panel_count_mismatch")
        if p["image_text"] and len(p["image_text"])!=p["rules"]["cards"]: errors.append("declared_text_required_for_each_frame")
        for index,asset in enumerate(p["assets"]):
            expected=p["image_text"][index:index+1]
            if asset.get("inspection",{}).get("expected")!=expected: errors.append("image_text_changed_reinspect_pixels")
        if not all(p["review"].get(k) is True for k in ["brand", "caption", "visual", "mobile", "canon", "truth"]): errors.append("owner_editorial_review_required")
        return {"passed": not errors, "errors": sorted(set(errors)), "warnings": warnings, "rubric": p["rules"]["rubric"], "viral_prediction": None}

    def approve(self, identifier, revision):
        checks = self.checks(identifier)
        if not checks["passed"]: raise ValueError("review_blocked:"+",".join(checks["errors"]))
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM production_packages WHERE id=?", (identifier,)).fetchone()
            if row["revision"] != int(revision): raise Conflict("revision_conflict_reload_package")
            approval = {"revision": row["revision"], "digest": digest(json.loads(row["payload"])), "actor": "owner", "at": now()}
            c.execute("UPDATE production_packages SET approval=?,state='APPROVED',updated_at=? WHERE id=?", (json.dumps(approval), now(), identifier))
            self.event(c, identifier, "APPROVED", approval)
        return self.get(identifier)

    def queue(self, identifier, stage, revision):
        import re
        if stage == "typography":
            raise ValueError("text_must_be_generated_inside_the_image_regenerate_the_frame")
        if stage not in {"draft", "concept", "caption", "visual", "typography", "inspect", "quality_plan", "quality", "schedule"} and not re.fullmatch(r"visual:[0-9]",stage): raise ValueError("unknown_production_stage")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM production_packages WHERE id=?", (identifier,)).fetchone()
            if not row or row["revision"] != int(revision): raise Conflict("revision_conflict_reload_package")
            if row['state'] == 'SCRAPPED': raise Conflict('scrapped_package_cannot_be_queued')
            if row["outbox_id"]: raise Conflict("already_scheduled")
            p = json.loads(row["payload"])
            if p["rules"]["typography"] == "controlled" and stage not in {"inspect", "schedule"}:
                raise ValueError("save_creative_edits_then_regenerate_text_inside_the_image")
            remaining=[]
            if stage=="draft":
                if p["assets"]:raise ValueError("full_draft_requires_empty_assets_use_frame_repairs_to_preserve_existing_work")
                remaining=["caption"]+["visual"]*p["rules"]["cards"]+["inspect"]
                from social.production_quality import enabled as quality_enabled
                if quality_enabled():remaining=['caption','quality_plan']+['visual']*p['rules']['cards']+['inspect','quality']
                stage="concept"
            if c.execute("SELECT 1 FROM production_jobs WHERE package_id=? AND state IN ('QUEUED','RUNNING','RETRY','WAITING_BUDGET')", (identifier,)).fetchone(): raise Conflict("job_already_pending")
            if stage == "schedule":
                a = json.loads(row["approval"] or "{}")
                if a.get("digest") != digest(p) or a.get("revision") != int(revision): raise ValueError("current_revision_approval_required")
            identifier_job = uuid.uuid4().hex
            try:
                c.execute("INSERT INTO production_jobs(id,package_id,revision,stage,state,available_at,created_at,next_stages) VALUES(?,?,?,?,'QUEUED',?,?,?)", (identifier_job, identifier, revision, stage, now(), now(),json.dumps(remaining)))
            except sqlite3.IntegrityError:
                raise Conflict("stage_already_attempted_create_revision_to_retry")
            if stage == "schedule": c.execute("UPDATE production_packages SET state='SCHEDULING' WHERE id=?", (identifier,))
            self.event(c, identifier, "JOB_QUEUED", {"stage": stage, "job_id": identifier_job})
        return {"job_id": identifier_job, "state": "QUEUED"}

    def heartbeat(self, job):
        with self.db() as c:
            changed=c.execute("UPDATE production_jobs SET lease_until=? WHERE id=? AND owner=? AND state='RUNNING' AND lease_until>?",((datetime.now(timezone.utc)+timedelta(seconds=600)).isoformat(),job["id"],job["owner"],now())).rowcount
        return bool(changed)

    def wait_for_budget(self, job):
        reset=datetime.combine(datetime.now(timezone.utc).date()+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc).isoformat()
        with self.db() as c:
            changed=c.execute("UPDATE production_jobs SET state='WAITING_BUDGET',available_at=?,error='daily_budget_exhausted',attempts=attempts-1,owner=NULL,lease_until=NULL WHERE id=? AND state='RUNNING' AND owner=?",(reset,job["id"],job["owner"])).rowcount
            if not changed:raise Conflict("worker_lease_lost")
            self.event(c,job["package_id"],"WAITING_BUDGET",{"job_id":job["id"],"retry_at":reset},"worker")

    def retry_job(self, identifier, *, acknowledge_possible_spend=False):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            job=c.execute("SELECT * FROM production_jobs WHERE id=?",(identifier,)).fetchone()
            if not job:raise KeyError("job_not_found")
            package=c.execute("SELECT * FROM production_packages WHERE id=?",(job["package_id"],)).fetchone()
            if job["state"] not in {"FAILED","RECONCILIATION_REQUIRED"} or package["revision"]!=job["revision"] or package["outbox_id"]:raise Conflict("job_cannot_be_retried")
            if not acknowledge_possible_spend:raise ValueError("explicit_retry_acknowledgement_required")
            if c.execute("SELECT 1 FROM production_jobs WHERE package_id=? AND state IN ('RUNNING','QUEUED','RETRY','WAITING_BUDGET')",(job["package_id"],)).fetchone():raise Conflict("job_already_pending")
            if job["attempts"]>=job["max_attempts"]:raise Conflict("attempt_limit_reached_clone_draft_to_continue")
            c.execute("UPDATE production_jobs SET state='QUEUED',available_at=?,owner=NULL,lease_until=NULL WHERE id=?",(now(),identifier))
            self.event(c,job["package_id"],"RETRY_ACKNOWLEDGED",{"job_id":identifier,"possible_additional_spend":True})
        return {"job_id":identifier,"state":"QUEUED"}

    def claim(self, owner, *, lease_seconds=600):
        stamp = now()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            # External generation may have completed before a crash. Never spend again automatically.
            c.execute("UPDATE production_jobs SET state=CASE WHEN stage LIKE 'visual%' OR stage IN ('concept','caption') THEN 'RECONCILIATION_REQUIRED' ELSE 'RETRY' END,owner=NULL,error='worker_lease_expired' WHERE state='RUNNING' AND lease_until<?", (stamp,))
            c.execute("UPDATE production_jobs SET state='FAILED',error='attempt_limit_reached' WHERE state IN ('QUEUED','RETRY') AND attempts>=max_attempts")
            row = c.execute("SELECT j.* FROM production_jobs j JOIN production_packages p ON p.id=j.package_id AND p.revision=j.revision WHERE j.state IN ('QUEUED','RETRY','WAITING_BUDGET') AND j.available_at<=? ORDER BY j.created_at LIMIT 1", (stamp,)).fetchone()
            if not row: return None
            token = owner+":"+uuid.uuid4().hex
            c.execute("UPDATE production_jobs SET state='RUNNING',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?", (token, (datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat(), row["id"]))
            self.event(c, row["package_id"], "JOB_STARTED", {"stage": row["stage"], "job_id": row["id"]}, "worker")
        return {**dict(row), "owner": token, "attempts": row["attempts"]+1}

    def finish(self, job, *, updates=None, result=None, error=None, retryable=False):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            owned = c.execute("SELECT * FROM production_jobs WHERE id=? AND state='RUNNING' AND owner=? AND lease_until>?", (job["id"], job["owner"], now())).fetchone()
            if not owned: raise Conflict("worker_lease_lost")
            row = c.execute("SELECT * FROM production_packages WHERE id=?", (job["package_id"],)).fetchone()
            if row["revision"] != job["revision"]: raise Conflict("job_revision_superseded")
            state = "RETRY" if error and retryable and owned["attempts"] < owned["max_attempts"] else "FAILED" if error else "SUCCEEDED"
            if updates and not error:
                p = json.loads(row["payload"]); p.update(updates); p["review"] = {}
                self.validate_payload(p)
                c.execute("UPDATE production_packages SET revision=revision+1,payload=?,approval=NULL,state='REVIEW',updated_at=? WHERE id=?", (json.dumps(p), now(), row["id"]))
                c.execute("INSERT INTO production_revisions VALUES(?,?,?,?,?)", (row["id"], row["revision"]+1, json.dumps(p), "worker:"+job["stage"], now()))
            if result and result.get("outbox_id") and not error:
                c.execute("UPDATE production_packages SET state='SCHEDULED',outbox_id=?,updated_at=? WHERE id=?", (result["outbox_id"], now(), row["id"]))
            c.execute("UPDATE production_jobs SET state=?,result=?,error=?,owner=NULL,lease_until=NULL,available_at=? WHERE id=?", (state, json.dumps(result or {}), error, (datetime.now(timezone.utc)+timedelta(seconds=60*owned["attempts"])).isoformat(), job["id"]))
            remaining=json.loads(owned["next_stages"])
            if (updates or {}).get('production_quality',{}).get('plan',{}).get('status')=='BLOCKED':remaining=[]
            if state=="SUCCEEDED" and remaining:
                next_revision=row["revision"]+int(bool(updates))
                next_id=uuid.uuid4().hex
                c.execute("INSERT INTO production_jobs(id,package_id,revision,stage,state,available_at,created_at,next_stages) VALUES(?,?,?,?,'QUEUED',?,?,?)",(next_id,row["id"],next_revision,remaining[0],now(),now(),json.dumps(remaining[1:])))
                self.event(c,row["id"],"JOB_QUEUED",{"stage":remaining[0],"job_id":next_id,"source":"draft_sequence"},"worker")
            self.event(c, row["id"], "JOB_"+state, {"stage": job["stage"], "error": error}, "worker")

    def observe(self, identifier, body):
        self.get(identifier)
        if body.get("platform") not in {"facebook", "instagram", "linkedin"}: raise ValueError("invalid_platform")
        metrics = body.get("metrics", {})
        if not isinstance(metrics, dict) or set(metrics)-{"reach", "shares", "saves", "comments", "profile_visits", "follows", "clicks", "orders", "completed", "started"}: raise ValueError("invalid_metrics")
        metrics = {k: finite(v) for k,v in metrics.items() if v is not None}
        collected = datetime.fromisoformat(str(body["collected_at"]).replace("Z", "+00:00"))
        if collected.tzinfo is None or not body.get("source"): raise ValueError("observation_source_and_timestamp_required")
        window = int(body["window_hours"])
        if window not in {24,168,720}: raise ValueError("observation_window_must_be_24_168_720")
        with self.db() as c:
            c.execute("INSERT INTO production_observations VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, identifier, body["platform"], window, json.dumps(metrics), str(body["source"]), collected.isoformat()))

    def snapshot(self):
        with self.db() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM production_packages ORDER BY updated_at DESC LIMIT 100")]
            jobs = [dict(r) for r in c.execute("SELECT * FROM production_jobs ORDER BY created_at DESC LIMIT 100")]
            events = [dict(r) for r in c.execute("SELECT * FROM production_events ORDER BY id DESC LIMIT 100")]
            observations = [dict(r) for r in c.execute("SELECT * FROM production_observations ORDER BY collected_at DESC LIMIT 300")]
            examples = [dict(r) for r in c.execute("SELECT * FROM production_examples ORDER BY created_at DESC LIMIT 50")]
            deliveries={r["outbox_id"]:{"state":r["lifecycle_state"],"status":r["status"],"readiness":json.loads(r["readiness_json"] or "{}")} for r in c.execute("SELECT outbox_id,lifecycle_state,status,readiness_json FROM content_outbox WHERE outbox_id IN (SELECT outbox_id FROM production_packages WHERE outbox_id IS NOT NULL)")}
        packages=[self.get(i) for i in ids]
        for item in packages:
            item["delivery"]=deliveries.get(item["outbox_id"])
            from social.production_quality import matches as quality_matches
            item['quality_fresh']={s:quality_matches(item['payload'],self.data_dir,s) for s in ('plan','release')} if item['payload'].get('production_quality') else {}
        for r in observations:
            m = json.loads(r.pop("metrics")); reach = m.get("reach")
            r["metrics"] = m; r["rates"] = {key+"_per_reach": m[key]/reach if reach and key in m else None for key in ["shares","saves","follows","comments"]}
        from content_operations import classify_backlog, publishing_metrics
        from posting_schedule import inventory_snapshot
        from social.production_quality import dashboard as quality_dashboard
        from production_health import snapshot as health_snapshot
        connection_checks={}
        for event in events:
            if event["event"]=="CONNECTION_CHECK":
                detail=json.loads(event["detail"])
                connection_checks.setdefault(detail["provider"],{**detail,"checked_at":event["created_at"],"verified":detail["status"]=="PASS"})
        backup_path=next((Path(self.data_dir)/'backups'/name for name in ['before-production-quality-v1.json','before-posting-schedule-v1.json','before-production-desk-v1.json'] if (Path(self.data_dir)/'backups'/name).is_file()),Path(self.data_dir)/'backups/before-production-desk-v1.json')
        runtime={"approval_mode":os.environ.get("EDITORIAL_APPROVAL_MODE","required"),"deployment_id":os.environ.get("RAILWAY_DEPLOYMENT_ID"),"commit":os.environ.get("BUILD_GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA"),"backup":json.loads(backup_path.read_text()) if backup_path.is_file() else None}

        return {"packages": packages, "jobs": jobs, "events": events,
                "observations": observations, "examples": examples, "references": self.references(),
                "formats": FAMILIES, "publishing": publishing_metrics(self.data_dir),
                "backlog": {k:len(v) for k,v in classify_backlog(self.data_dir).items()},
                "runtime":runtime,"connection_checks":connection_checks,"operations_health":health_snapshot(self.data_dir),
                "scheduled_inventory":inventory_snapshot(self.data_dir),
                "quality":quality_dashboard(self.data_dir),
                "dispatch_enabled":os.environ.get("CONTENT_DISPATCH_ENABLED","false").lower() in {"1","true","yes","on"},
                "connections": {p: {"configured": bool(os.environ.get(k)), "verified": False} for p,k in [("gemini","GEMINI_API_KEY"),("meta","META_PAGE_ACCESS_TOKEN"),("studio","ENTERTAINMENT_STUDIO_TOKEN")]}}

    def add_example(self, body):
        if body.get("format") not in FAMILIES or body.get("verdict") not in {"excellent", "reject"} or not body.get("explanation") or not body.get("content"):
            raise ValueError("format_verdict_explanation_content_required")
        with self.db() as c:
            c.execute("INSERT INTO production_examples VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, body["format"], body["verdict"], str(body["explanation"]), str(body["content"]), now()))

    def clone(self, identifier):
        if self.get(identifier)['state'] == 'SCRAPPED': raise Conflict('scrapped_package_cannot_be_cloned')
        original=self.get(identifier)["payload"]
        overrides={k:original["rules"][k] for k in ["tone","typography","natural_response"]}
        if overrides["typography"] == "controlled": overrides["typography"] = "integrated"
        draft=self.create({"brief":original["brief"],"title":original["title"]+" (new draft)","format":original["rules"]["format_id"],"objective":original["rules"]["objective"],"platforms":original["platforms"],"overrides":overrides})
        updates={k:v for k,v in original.items() if k not in {"rules","scheduled_at","review","provenance","title"}}
        return self.revise(draft["id"],updates,draft["revision"])

    def import_outbox(self, identifier, format_id):
        with self.db() as c:
            row=c.execute("SELECT package_json FROM content_outbox WHERE outbox_id=?",(identifier,)).fetchone()
        if not row:raise KeyError("outbox_not_found")
        old=json.loads(row[0]); platforms=list((old.get("routing") or {}).get("platforms") or ["instagram"])
        draft=self.create({"brief":old.get("topic") or old.get("title") or "Imported post: review the original before production", "format":format_id,"platforms":platforms})
        captions={p:(old.get("platform_posts",{}).get(p) or {}).get("final_caption") or old.get({"facebook":"fb_caption","instagram":"ig_caption","linkedin":"li_text"}[p],"") for p in platforms}
        paths=[a.get("local_path","") for a in old.get("carousel_assets",[]) if isinstance(a,dict)]
        if not paths:paths=[(old.get("generated_visuals") or {}).get(platforms[0],"")]
        assets=[]
        for path in paths:
            if not isinstance(path,str) or not path:continue
            try:record=self.delivery_asset(path)
            except (OSError,ValueError):continue
            if record["sha256"] not in {a["sha256"] for a in assets}:assets.append(record)
        assets=assets[:draft["payload"]["rules"]["cards"]]
        return self.revise(draft["id"],{"captions":captions,"assets":assets,"product_id":old.get("product_id","")},draft["revision"])

    def upload(self, body):
        import base64
        from PIL import Image,ImageOps
        from io import BytesIO
        try:raw=base64.b64decode(body["data"],validate=True)
        except Exception:raise ValueError("invalid_base64_image")
        if len(raw)>8*1024*1024:raise ValueError("image_exceeds_8_mb")
        with Image.open(BytesIO(raw)) as image:
            if image.width*image.height>25_000_000:raise ValueError("image_dimensions_too_large")
            image=ImageOps.exif_transpose(image)
            relative="public_media/upload-"+uuid.uuid4().hex+".png"
            target=Path(self.data_dir)/relative;target.parent.mkdir(parents=True,exist_ok=True)
            image.convert("RGB").save(target,"PNG")
        return self.asset_record(relative)

    def estimate(self, identifier, rates=None):
        p=self.get(identifier)["payload"]; rates=rates or {}
        from social.production_quality import enabled as quality_enabled
        repairs=int(rates.get("repair_images",1)); text_calls=int(rates.get("text_calls",4 if quality_enabled() else 2))
        if not 0<=repairs<=10 or not 0<=text_calls<=20:raise ValueError("estimate_count_out_of_range")
        images=max(0,p["rules"]["cards"]-len(p["assets"]))+repairs
        known=all(rates.get(k) is not None for k in ["image_usd","text_call_usd"])
        amount=round(images*finite(rates["image_usd"])+text_calls*finite(rates["text_call_usd"]),4) if known else None
        from social.gemini_budget import budget_snapshot
        budget=budget_snapshot(); daily=budget["image"]["limit"]
        return {"image_attempts_planned":images,"text_calls_planned":text_calls,"estimated_usd":amount,
                "price_source":str(rates.get("source") or "unverified_operator_estimate"),"actual_provider_bill":None,
                "fits_current_image_window":images<=budget["image"]["remaining"],"minimum_image_budget_days":math.ceil(images/daily) if daily else None,
                "budget":budget,"note":"Estimate only. QA, token usage and provider repairs can change actual spend; one image is produced per durable visual job."}
