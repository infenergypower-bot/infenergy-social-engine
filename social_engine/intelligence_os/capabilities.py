from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .db import connect, encode, utc_now
from .governance import PolicyEngine
from .intelligence import AutomationService, ResearchIntelligence
from .knowledge import ResearchService, StrategyService, WorldModel
from .models import DEFAULT_MASTER_MODEL, CopilotMaster, new_session_id
from .operations import AttentionService, JobService
from .registry import Capability, CapabilityRegistry, ExecutionContext


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
CENTRAL_TIME = ZoneInfo("America/Chicago")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def register_core_capabilities(registry: CapabilityRegistry, policies: PolicyEngine) -> None:
    data_dir = registry.data_dir
    strategy = StrategyService(data_dir)
    research = ResearchService(data_dir)
    world = WorldModel(data_dir)
    jobs = JobService(data_dir)
    attention = AttentionService(data_dir)
    external_research = ResearchIntelligence(data_dir)
    automations = AutomationService(data_dir)

    def health(_: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        import inventory_db
        from content_operations import daily_status

        db_path = inventory_db.get_db_path(context.data_dir)
        provider_config = {
            "copilot": {"sdk_installed": _module_available("copilot"), "master_model": os.environ.get("INFENERGY_MASTER_MODEL", "gpt-5.6-sol")},
            "gemini": {"configured": bool(os.environ.get("GEMINI_API_KEY", "").strip())},
            "facebook": {"configured": bool(os.environ.get("META_PAGE_ID", "").strip() and os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip())},
            "instagram": {"configured": bool((os.environ.get("META_IG_USER_ID", "") or os.environ.get("META_INSTAGRAM_BUSINESS_ID", "")).strip())},
            "linkedin": {"configured": bool(os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip())},
            "wordpress": {"configured": bool(os.environ.get("WP_URL", "").strip())},
        }
        return {
            "status": "DEGRADED" if not provider_config["copilot"]["sdk_installed"] else "OPERATIONAL",
            "database": {"path": db_path, "exists": os.path.exists(db_path)},
            "providers": provider_config,
            "social_today": daily_status(context.data_dir, date.today()),
            "dry_run": os.environ.get("SOCIAL_DRY_RUN", "true").lower() != "false",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def model_status(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        return CopilotMaster(data_dir).status().__dict__

    def products_list(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        import inventory_db
        products = inventory_db.fetch_products(context.data_dir)
        query = str(payload.get("query", "")).lower().strip()
        if query:
            products = [item for item in products if query in str(item.get("name", "")).lower() or query in str(item.get("sku", "")).lower()]
        limit = max(1, min(int(payload.get("limit", 100)), 500))
        return {"count": len(products[:limit]), "products": products[:limit], "source": "inventory_db"}

    def product_get(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        import inventory_db
        product_id = str(payload["product_id"])
        item = next((row for row in inventory_db.fetch_products(context.data_dir) if str(row.get("id") or row.get("product_id")) == product_id), None)
        if not item:
            raise KeyError(f"product_not_found:{product_id}")
        return {"product": item, "source": "inventory_db"}

    def calendar(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import daily_status
        start = date.fromisoformat(str(payload.get("start_date") or date.today().isoformat()))
        days = max(1, min(int(payload.get("days", 7)), 120))
        return {"start_date": start.isoformat(), "days": [daily_status(context.data_dir, start + timedelta(days=index)) for index in range(days)]}

    def schedule_post(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import create_council_session, daily_status, ensure_daily_slots, mark_ready, replace_unpublished_slot

        content_date = str(payload["content_date"])
        slot = str(payload.get("slot", "midday")).strip().lower()
        if slot not in {"morning", "midday", "evening"}:
            raise ValueError("slot_must_be_morning_midday_or_evening")
        default_hours = {"morning": 8, "midday": 12, "evening": 18}
        requested_at = str(payload.get("scheduled_at") or "").strip()
        if requested_at:
            scheduled_datetime = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            if scheduled_datetime.tzinfo is None:
                scheduled_datetime = scheduled_datetime.replace(tzinfo=CENTRAL_TIME)
        else:
            scheduled_datetime = datetime.combine(
                date.fromisoformat(content_date), datetime.min.time(), CENTRAL_TIME,
            ).replace(hour=default_hours[slot])
        scheduled_at = scheduled_datetime.isoformat()
        if scheduled_datetime.date().isoformat() != content_date:
            raise ValueError("scheduled_at_must_match_content_date")
        package = dict(payload["package"])
        platforms = [str(item).lower() for item in payload.get("platforms", []) if str(item).strip()]
        if platforms:
            package["platforms"] = platforms
            package.setdefault("platform_policy", {})["platforms"] = platforms
        existing_day = daily_status(context.data_dir, content_date)
        existing_slot = next((item for item in existing_day.get("slots", []) if str(item.get("slot", "")).lower() == slot), None)
        if existing_slot and (existing_slot.get("content_id") or existing_slot.get("outbox_id")) and not bool(payload.get("replace_existing", False)):
            raise ValueError(f"slot_already_occupied:{content_date}:{slot}:set_replace_existing_true")
        plan = {
            "actions": ["ensure_daily_slots", "create_decision_record", "write_content_outbox"],
            "affected_resources": [{"type": "social_slot", "id": f"{content_date}:{slot}"}],
            "estimated_cost_usd": 0.0,
            "risks": ["External publication occurs later only if dispatch policy permits"],
            "irreversible_steps": [],
        }
        if context.dry_run:
            return {"plan": plan, "would_schedule": {"content_date": content_date, "slot": slot, "scheduled_at": scheduled_at}}
        day_start = datetime.combine(date.fromisoformat(content_date), datetime.min.time(), CENTRAL_TIME)
        schedule = {
            name: day_start.replace(hour=hour).isoformat()
            for name, hour in default_hours.items()
        }
        schedule[slot] = scheduled_at
        ensure_daily_slots(context.data_dir, content_date, schedule, package.get("platform_policy", {}))
        replace_unpublished_slot(context.data_dir, content_date, slot)
        decision_id = create_council_session(
            context.data_dir, content_date=content_date, slot=slot,
            blackboard={"source": "infenergy_intelligence_os", "transaction_id": context.transaction_id},
            rationale=[str(payload.get("rationale", "Owner-directed schedule"))],
        )
        outbox_id = mark_ready(
            context.data_dir, content_date=content_date, slot=slot,
            scheduled_at=scheduled_at, decision_id=decision_id, package=package,
        )
        return {
            "outbox_id": outbox_id, "decision_id": decision_id, "plan": plan,
            "_rollback": {"content_date": content_date, "slot": slot, "outbox_id": outbox_id},
        }

    def rollback_schedule(data: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import replace_unpublished_slot
        cancelled = replace_unpublished_slot(context.data_dir, str(data["content_date"]), str(data["slot"]))
        if not cancelled:
            return {"status": "NOT_REVERSIBLE", "reason": "slot_already_claimed_or_published", "_irreversible": [data]}
        return {"status": "ROLLED_BACK", "content_date": data["content_date"], "slot": data["slot"]}

    def schedule_job_campaign(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        job_id = str(payload["job_id"])
        job = jobs.get(job_id)
        if job["status"] != "COMPLETED":
            raise ValueError(f"job_not_completed:{job_id}")
        start = date.fromisoformat(str(payload["start_date"]))
        end = date.fromisoformat(str(payload["end_date"]))
        if end < start or (end - start).days >= 120:
            raise ValueError("campaign_range_must_be_1_to_120_days")
        result = job.get("result", {})
        deliverables = next(
            (result.get(key) for key in ("posts", "packages", "episodes") if isinstance(result.get(key), list)),
            [],
        )
        dates = [(start + timedelta(days=index)).isoformat() for index in range((end - start).days + 1)]
        requested_slots = payload.get("slots")
        if not isinstance(requested_slots, list) or not requested_slots:
            requested_slots = [str(payload.get("slot", "midday"))]
        slots = [str(item).lower() for item in requested_slots]
        by_date_slot: dict[tuple[str, str], dict[str, Any]] = {}
        for item in deliverables:
            if not isinstance(item, dict):
                continue
            content_date = str(item.get("content_date") or item.get("date") or "")
            item_slot = str(item.get("slot") or (slots[0] if len(slots) == 1 else "")).lower()
            if content_date and item_slot:
                by_date_slot[(content_date, item_slot)] = item
        missing = [f"{content_date}:{slot}" for content_date in dates for slot in slots if (content_date, slot) not in by_date_slot]
        if missing:
            raise ValueError(f"job_deliverables_missing_dates:{','.join(missing)}")
        default_times = {"morning": "13:00:00+00:00", "midday": "17:00:00+00:00", "evening": "23:00:00+00:00"}
        schedule_times = payload.get("schedule_times", {})
        if not isinstance(schedule_times, dict):
            schedule_times = {}
        if len(slots) == 1 and payload.get("scheduled_time"):
            schedule_times[slots[0]] = str(payload["scheduled_time"])
        platforms = list(payload.get("platforms", ["facebook", "instagram", "linkedin"]))
        campaign = job.get("result", {}).get("campaign", {})
        campaign_name = campaign.get("name", "Campaign") if isinstance(campaign, dict) else str(campaign)
        if context.dry_run:
            return {
                "job_id": job_id, "campaign": campaign_name, "would_schedule": len(dates) * len(slots),
                "date_range": {"start": dates[0], "end": dates[-1]}, "slots": slots,
                "platforms": platforms, "publication_enabled": False,
            }
        scheduled: list[dict[str, Any]] = []
        try:
            for content_date in dates:
                for slot in slots:
                    deliverable = by_date_slot[(content_date, slot)]
                    scheduled_time = str(schedule_times.get(slot) or default_times.get(slot) or "17:00:00+00:00")
                    scheduled_result = schedule_post({
                        "content_date": content_date,
                        "slot": slot,
                        "scheduled_at": f"{content_date}T{scheduled_time}",
                        "package": {
                            "source_job_id": job_id,
                            "campaign": campaign_name,
                            "deliverable_date": content_date,
                            "slot": slot,
                            "deliverable": deliverable,
                            "platforms": platforms,
                            "publication_enabled": False,
                        },
                        "rationale": f"Load owner-approved {campaign_name} deliverable from completed job {job_id} without publishing.",
                    }, context)
                    scheduled.append({"content_date": content_date, "slot": slot, "outbox_id": scheduled_result["outbox_id"]})
        except Exception:
            for item in reversed(scheduled):
                rollback_schedule(item, context)
            raise
        resolved_alerts = attention.resolve_matching("job_deliverables_missing_dates:")
        return {
            "job_id": job_id, "campaign": campaign_name, "scheduled_count": len(scheduled),
            "platform_adaptation_count": len(scheduled) * len(platforms), "slots": slots,
            "publication_enabled": False, "scheduled": scheduled,
            "resolved_attention_items": resolved_alerts,
            "_rollback": {"slots": scheduled},
        }

    def rollback_job_campaign(data: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        results = [rollback_schedule(item, context) for item in reversed(data.get("slots", []))]
        irreversible = [item for item in results if item.get("status") != "ROLLED_BACK"]
        return {
            "status": "ROLLED_BACK" if not irreversible else "PARTIAL_ROLLBACK",
            "rolled_back": len(results) - len(irreversible),
            "_irreversible": irreversible,
        }

    def goals_get(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        return {"goals": strategy.list_goals(active_only=False)}

    def goal_create(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        if context.dry_run:
            return {"would_create": payload, "production_mutated": False}
        goal = strategy.create_goal(
            str(payload["name"]), str(payload["description"]),
            priority=int(payload.get("priority", 50)), metrics=payload.get("metrics", []),
            constraints=payload.get("constraints", []), horizon=str(payload.get("horizon", "ongoing")),
        )
        return {"goal": goal, "_rollback": {"table": "os_goals", "id": goal["id"]}}

    def policy_list(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        return {"policies": policies.list_policies(active_only=False)}

    def policy_create(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        if context.dry_run:
            return {"would_create": payload, "production_mutated": False}
        policy = policies.create_policy(
            capability=str(payload["capability"]), rule=str(payload["rule"]),
            approval_level=str(payload["approval_level"]), created_by=context.actor,
            scope=payload.get("scope", {}), limits=payload.get("limits", {}),
            valid_until=payload.get("valid_until"),
        )
        return {"policy": policy}

    def research_mission(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        plan = {
            "question": payload["question"],
            "workstreams": payload.get("workstreams", ["primary_sources", "market", "competitors", "consumer_voice", "verification"]),
            "source_requirements": payload.get("source_requirements", ["authoritative", "current", "corroborated_for_high_impact_claims"]),
        }
        if context.dry_run:
            return {"plan": plan, "production_mutated": False}
        mission = research.create_mission(
            str(payload["question"]), scope=payload.get("scope", {}),
            workstreams=plan["workstreams"], source_requirements=plan["source_requirements"],
            freshness_requirement=str(payload.get("freshness_requirement", "current")),
            depth=str(payload.get("depth", "standard")),
        )
        job = jobs.create(
            job_type="RESEARCH_MISSION", objective=str(payload["question"]),
            plan=["plan_sources", *plan["workstreams"], "corroborate", "synthesize", "rank_implications"],
            operation_id=context.operation_id,
        )
        return {"mission": mission, "job": job}

    def jobs_list(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        return {"jobs": jobs.list()}

    def job_steer(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        if context.dry_run:
            return {"would_steer": payload, "production_mutated": False}
        return {"job": jobs.steer(str(payload["job_id"]), str(payload["instruction"]), context.actor)}

    def job_control(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        action = str(payload["action"]).lower()
        statuses = {"pause": "PAUSED", "continue": "RUNNING", "cancel": "CANCELED"}
        if action not in statuses:
            raise ValueError("action_must_be_pause_continue_or_cancel")
        if context.dry_run:
            return {"would_transition": statuses[action], "job_id": payload["job_id"]}
        return {"job": jobs.transition(str(payload["job_id"]), statuses[action])}

    def job_complete(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        job_id = str(payload["job_id"])
        result = dict(payload["result"])
        job = jobs.get(job_id)
        if job["status"] in {"CANCELED", "COMPLETED"}:
            raise ValueError(f"job_not_completable:{job['status']}")
        if context.dry_run:
            return {"would_complete_job": job_id, "deliverable_keys": sorted(result), "production_mutated": False}
        for step in job["steps"]:
            if step["status"] not in {"COMPLETED", "SKIPPED"}:
                jobs.checkpoint(
                    job_id, int(step["ordinal"]), status="COMPLETED",
                    checkpoint={"completed_by": context.actor, "deliverables_persisted": True},
                    result={"deliverable_keys": sorted(result)},
                )
        return {"job": jobs.transition(job_id, "COMPLETED", progress=1.0, result=result), "deliverables": result}

    def scenario_create(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        if context.dry_run:
            return {"scenario": payload, "production_mutated": False}
        return {"scenario": strategy.create_scenario(
            str(payload["premise"]), assumptions=payload.get("assumptions", []),
            baseline=payload.get("baseline", {}), changed_variables=payload.get("changed_variables", []),
            projected_effects=payload.get("projected_effects", []), confidence=float(payload.get("confidence", 0.3)),
            evidence=payload.get("evidence", []), limitations=payload.get("limitations", ["Projection is not an observed fact"]),
        )}

    def world_search(payload: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        return {"entities": world.search(str(payload["query"]), int(payload.get("limit", 50)))}

    def attention_get(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        return {"attention": attention.list_open()}

    def content_120(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from build_monthly_content import build_monthly_calendar, prepare_monthly_gemini_prompts

        horizons = payload.get("horizons") or [
            {"days": "1-14", "state": "production_ready"},
            {"days": "15-30", "state": "approved_concepts"},
            {"days": "31-60", "state": "adaptive_concepts"},
            {"days": "61-90", "state": "themes_story_arcs"},
            {"days": "91-120", "state": "direction_opportunity_reserve"},
        ]
        plan = [
            "load_goals_and_strategy", "analyze_recent_content", "analyze_performance",
            "fingerprint_creative_history", "research_market_and_competitors",
            "analyze_products_and_campaigns", "design_story_arcs_and_franchises",
            "build_rolling_horizon", "diversity_and_saturation_review",
            "owner_strategy_review", "produce_locked_horizon", "qa_and_package", "schedule_approved",
        ]
        if context.dry_run:
            return {"objective": payload.get("objective", "Build adaptive 120-day content system"), "plan": plan, "horizons": horizons, "estimated_cost": "requires provider pricing and production scope", "production_mutated": False}
        job = jobs.create(
            job_type="ADAPTIVE_120_DAY_CONTENT",
            objective=str(payload.get("objective", "Build adaptive 120-day content system")),
            plan=plan, operation_id=context.operation_id,
        )
        jobs.transition(job["id"], "RUNNING", progress=0.05)
        jobs.checkpoint(
            job["id"], 0, status="COMPLETED",
            checkpoint={"builder": "build_monthly_calendar", "days": 120},
        )

        def build_in_background() -> None:
            try:
                content_plan = str(payload.get("content_plan") or "weekly_brand_mix")
                calendar = build_monthly_calendar(
                    data_dir=context.data_dir,
                    start_date=payload.get("start_date"),
                    days=120,
                    enqueue=True,
                    replace_unpublished=bool(payload.get("replace_unpublished", True)),
                    content_plan=content_plan,
                )
                prepared = (
                    {"prepared_entries": 0, "prepared_prompts": 0}
                    if content_plan == "content_plan_120"
                    else prepare_monthly_gemini_prompts(context.data_dir)
                )
                result = {
                    "calendar_path": calendar.get("calendar_path"),
                    "queued": int(calendar.get("queued") or 0),
                    "cancelled_outbox": int(calendar.get("cancelled_outbox") or 0),
                    "single_image_posts": int(calendar.get("single_image_posts") or 0),
                    "carousel_posts": int(calendar.get("carousel_posts") or 0),
                    "product_posts": int(calendar.get("product_posts") or 0),
                    "current_event_posts": int(calendar.get("current_event_posts") or 0),
                    "superhero_posts": int(calendar.get("superhero_posts") or 0),
                    "micro_mission_posts": int(calendar.get("micro_mission_posts") or 0),
                    "historical_mission_posts": int(calendar.get("historical_mission_posts") or 0),
                    "prepared_entries": int(prepared.get("prepared_entries") or 0),
                    "prepared_prompts": int(prepared.get("prepared_prompts") or 0),
                    "content_plan": content_plan,
                    "replace_unpublished": bool(payload.get("replace_unpublished", True)),
                }
                for step in jobs.get(job["id"])["steps"]:
                    if step["status"] not in {"COMPLETED", "SKIPPED"}:
                        jobs.checkpoint(
                            job["id"], int(step["ordinal"]), status="COMPLETED",
                            checkpoint={"builder": "build_monthly_calendar", "calendar_path": result["calendar_path"]},
                            result={"queued": result["queued"]},
                        )
                jobs.transition(job["id"], "COMPLETED", progress=1.0, result=result)
            except Exception as exc:
                jobs.transition(
                    job["id"], "FAILED",
                    result={"error": f"{type(exc).__name__}: {exc}", "builder": "build_monthly_calendar"},
                )

        threading.Thread(
            target=build_in_background,
            name=f"content-120-{job['id'][:8]}",
            daemon=True,
        ).start()
        return {
            "job": jobs.get(job["id"]), "horizons": horizons,
            "locked_days": int(payload.get("locked_days", 7)), "dispatched": True,
        }

    def research_news(payload: dict[str, Any], _: ExecutionContext) -> dict[str, Any]:
        return external_research.search_news(
            str(payload.get("query", "energy technology business world news")),
            limit=int(payload.get("limit", 10)), freshness_days=int(payload.get("freshness_days", 2)),
        )

    def findings_get(payload: dict[str, Any], _: ExecutionContext) -> dict[str, Any]:
        return {"findings": external_research.list_findings(int(payload.get("limit", 100)))}

    def automations_get(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        return {"automations": automations.list(), "runs": automations.list_runs(), "watches": automations.list_watches()}

    def automation_create(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        for step in payload.get("steps", []):
            registry.get(str(step.get("capability", "")))
        if context.dry_run:
            return {"would_create": payload, "required_capabilities": [step["capability"] for step in payload.get("steps", [])], "production_mutated": False}
        automation = automations.create(
            name=str(payload["name"]), trigger=payload["trigger"], steps=payload["steps"],
            created_by=context.actor, conditions=payload.get("conditions", []),
            permissions=payload.get("permissions", {}), approval_rules=payload.get("approval_rules", {}),
            schedule=payload.get("schedule", {}), failure_policy=payload.get("failure_policy", {}),
        )
        return {"automation": automation}

    def automation_control(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        status = {"pause": "PAUSED", "resume": "ACTIVE", "disable": "DISABLED"}.get(str(payload["action"]).lower())
        if not status:
            raise ValueError("action_must_be_pause_resume_or_disable")
        if context.dry_run:
            return {"would_set_status": status, "automation_id": payload["automation_id"]}
        return {"automation": automations.set_status(str(payload["automation_id"]), status)}

    def watch_create(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        if context.dry_run:
            return {"would_create": payload, "production_mutated": False}
        return {"watch": automations.create_watch(
            subject=str(payload["subject"]), scope=payload.get("scope", {}),
            frequency=str(payload.get("frequency", "daily")), source_policy=payload.get("source_policy", {}),
            materiality_threshold=float(payload.get("materiality_threshold", 0.7)),
            condition=payload.get("condition", {}), actions=payload.get("actions", []),
            expires_at=payload.get("expires_at"),
        )}

    def decisions_get(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from social_engine.intelligence_os.db import connect, decode
        with connect(context.data_dir) as connection:
            rows = connection.execute("SELECT * FROM os_decisions ORDER BY created_at DESC LIMIT ?", (max(1, min(int(payload.get("limit", 100)), 500)),)).fetchall()
        decisions = []
        for row in rows:
            item = dict(row)
            for key in ("evidence_json", "alternatives_json", "assumptions_json", "goals_affected_json", "policies_applied_json", "actions_json"):
                item[key[:-5]] = decode(item.pop(key), [])
            decisions.append(item)
        return {"decisions": decisions}

    def opportunities_get(_: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from social_engine.intelligence_os.db import connect, decode
        with connect(context.data_dir) as connection:
            rows = connection.execute("SELECT * FROM os_opportunities WHERE status!='DISMISSED' ORDER BY potential_value DESC, confidence DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row); item["evidence"] = decode(item.pop("evidence_json"), []); result.append(item)
        return {"opportunities": result}

    def risks_get(_: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from social_engine.intelligence_os.db import connect, decode
        with connect(context.data_dir) as connection:
            rows = connection.execute("SELECT * FROM os_risks WHERE status!='CLOSED' ORDER BY impact DESC, likelihood DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row); item["evidence"] = decode(item.pop("evidence_json"), []); result.append(item)
        return {"risks": result}

    def creative_score(payload: dict[str, Any], _: ExecutionContext) -> dict[str, Any]:
        from score_content import score_content
        content = payload.get("content")
        if not isinstance(content, dict):
            raise ValueError("content_object_required")
        return {
            "evaluation": score_content(content, payload.get("platforms")),
            "thresholds": {"approve": 82, "regenerate_once": 75, "reject": 0},
            "production_mutated": False,
        }

    def agents_list(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        from agents.dispatcher import agent_contracts, available_agents
        return {"agents": available_agents(), "contracts": agent_contracts()}

    def agent_run(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from agents.dispatcher import run_agent
        name = str(payload["name"])
        params = dict(payload.get("params", {}))
        if context.dry_run:
            return {"would_run": name, "params": params, "production_mutated": False}
        if name == "carousel_slide_writer" and str(params.get("objective", "")).strip():
            result = creative_carousel_generate(params, context)
            return {
                "agent": name,
                "delegated_capability": "creative.carousel.generate",
                "output": result,
                "_rollback": result.pop("_rollback", {}),
            }
        result = run_agent(name, context.data_dir, params)
        if result.get("error"):
            detail = str(result.get("detail") or "").strip()
            raise ValueError(": ".join(part for part in (str(result["error"]), detail) if part))
        return {"agent": name, "output": result}

    def creative_carousel_generate(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from agents.carousel_slide_writer import PLATFORM_LIMITS, run as write_slides
        from build_monthly_content import _render_assets

        platform = str(payload.get("platform", "instagram_feed")).strip().lower()
        slide_count = int(payload.get("slide_count", 6))
        platform_limit = PLATFORM_LIMITS.get(platform, 10)
        objective = str(payload["objective"]).strip()
        title = str(payload.get("title") or objective).strip()
        if context.dry_run:
            return {
                "would_generate": {"objective": objective, "platform": platform, "slide_count": slide_count},
                "platform_limit": platform_limit, "production_mutated": False,
            }
        product = dict(payload.get("product", {}))
        product_id = str(payload.get("product_id", "")).strip()
        if product_id and not product:
            import inventory_db
            product = next(
                (item for item in inventory_db.fetch_products(context.data_dir) if str(item.get("id") or item.get("product_id")) == product_id),
                {},
            )
            if not product:
                raise KeyError(f"product_not_found:{product_id}")
        authored = write_slides(
            context.data_dir,
            principle_key=str(payload.get("principle_key", "contrapositive")),
            archetype_key=str(payload.get("archetype_key", "preparedness_buyer")),
            product=product,
            creative_brief=objective,
            platform=platform,
            slide_count=slide_count,
        )
        thought = {
            "id": f"CREATIVE-{uuid.uuid4().hex[:12]}",
            "format": "carousel",
            "statement": objective,
            "expansion": str(payload.get("supporting_message", objective)),
            "prompt": str(payload.get("cta", "What will you prepare first?")),
            "pillar": str(payload.get("pillar", "preparedness_mindset")),
            "visual_motif": str(payload.get("visual_motif", "premium editorial energy story")),
            "slides": [
                {
                    "role": (
                        "COVER" if index == 0
                        else "FINALE" if index == len(authored["slides"]) - 1
                        else slide["slide_role"]
                    ),
                    "headline": slide["on_image_headline"],
                    "supporting": slide["on_image_subline"],
                }
                for index, slide in enumerate(authored["slides"])
            ],
            "cta": str(payload.get("cta", "What will you prepare first?")),
        }
        rendered = _render_assets(context.data_dir, thought, 0)
        assets = rendered["slides"]
        from PIL import Image
        asset_validation = []
        for index, asset in enumerate(assets, start=1):
            path = Path(str(asset.get("local_path", "")))
            validation = {
                "slide": index,
                "local_path": str(path),
                "exists": path.is_file(),
                "size_bytes": path.stat().st_size if path.is_file() else 0,
                "decodable": False,
                "width": 0,
                "height": 0,
            }
            if validation["exists"] and validation["size_bytes"] > 0:
                try:
                    with Image.open(path) as image:
                        validation["width"], validation["height"] = image.size
                        image.verify()
                    validation["decodable"] = True
                except Exception:
                    pass
            validation["valid"] = bool(
                validation["exists"]
                and validation["size_bytes"] > 0
                and validation["decodable"]
                and validation["width"] > 0
                and validation["height"] > 0
            )
            asset_validation.append(validation)
        invalid_assets = [item["slide"] for item in asset_validation if not item["valid"]]
        if invalid_assets:
            raise RuntimeError(f"carousel_asset_validation_failed:{','.join(str(item) for item in invalid_assets)}")
        supporting = str(payload.get("supporting_message") or "").strip()
        cta = str(payload.get("cta") or "What would you protect first?").strip()
        facebook_caption = str(payload.get("caption") or "\n\n".join(part for part in (title, objective, supporting, cta) if part)).strip()
        instagram_caption = "\n\n".join(part for part in (objective, supporting, cta, "#Infenergy #Preparedness #PracticalPower") if part)
        linkedin_caption = "\n\n".join(part for part in (title, objective, supporting, f"Practical next step: {cta}") if part)
        platforms = [str(item).lower() for item in payload.get("platforms", []) if str(item).strip()]
        if not platforms:
            platforms = ["facebook", "instagram"]
        package = {
            "post_id": thought["id"].lower(),
            "objective": objective,
            "visual_format": "CAROUSEL",
            "carousel_slides": authored["slides"],
            "carousel_assets": assets,
            "generated_visuals": {name: rendered["primary"]["local_path"] for name in platforms},
            "title": title,
            "fb_caption": facebook_caption,
            "ig_caption": instagram_caption,
            "li_text": linkedin_caption,
            "platforms": platforms,
            "platform_policy": {"platforms": platforms},
            "platform_posts": {},
        }
        platform_copies = {
            "facebook": facebook_caption,
            "instagram": instagram_caption,
            "linkedin": linkedin_caption,
        }
        package["platform_posts"] = {
            name: {"platform": name, "final_caption": platform_copies.get(name, facebook_caption), "content_format": "carousel"}
            for name in platforms
        }
        return {
            "status": "GENERATED",
            "slide_count": len(authored["slides"]),
            "platform": platform,
            "platform_limit": platform_limit,
            "package": package,
            "assets": assets,
            "asset_validation": asset_validation,
            "all_assets_valid": True,
            "next_action": "Call social.schedule with this package; one owner approval will schedule it.",
            "_rollback": {"asset_paths": [item["local_path"] for item in assets]},
        }

    def flagship_creative_produce(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from social.command_center import compile_command
        from social.command_context import originality_context, resolve_product_context
        from social.memory_intelligence import append_content_record, append_visual_record
        from social.visual_provider import default_provider

        command = str(payload["command"]).strip()
        contract = compile_command(command)
        product_context = resolve_product_context(command, context.data_dir)
        originality_references = originality_context(command, context.data_dir)
        if context.dry_run:
            return {
                "production_status": "CONTRACT_READY", "creative_contract": contract,
                "product_context": product_context, "originality_references": originality_references,
                "production_mutated": False,
            }

        exact_strings = list(contract["exact_visible_text"])
        headline = exact_strings[0] if exact_strings else command[:300]
        story_beats = list(contract["story_beats"])
        story = {
            "setup": story_beats[0]["prompt"] if story_beats else command,
            "trigger": "A recognizable energy, connection, preparation, or human-continuity problem becomes visible.",
            "action": contract["infenergy_action"],
            "obstacle": story_beats[2]["prompt"] if len(story_beats) > 2 else "The first interpretation is incomplete.",
            "escalation": story_beats[3]["prompt"] if len(story_beats) > 3 else "The action reveals the true system problem.",
            "payoff": story_beats[-1]["prompt"] if story_beats else "The visible action earns the resolution.",
        }
        profile = product_context.get("profile", {})
        persona = product_context.get("persona", {})
        product_evidence = product_context.get("evidence", {})
        creative_request = {
            "requestId": contract["request_id"],
            "contentId": f"command-center-{uuid.uuid4().hex[:12]}",
            "contentFormatIdentifier": contract.get("content_format_identifier") or None,
            "contentFormatContract": contract.get("content_format_contract") or {},
            "objective": command,
            "contentWorlds": ["ENTERTAINMENT"] if contract["characters"] else ["HUMAN_LIFE"],
            "platform": contract["platform"],
            "format": contract["format"],
            "requestedRoute": contract["creative_mode"],
            "humanTruth": persona.get("why_it_matters") or profile.get("core_customer_truth") or "Energy matters because it keeps people connected to the life, work, family, and experiences that matter.",
            "humanTension": persona.get("problem") or "A relatable interruption, uncertainty, or misconception needs a meaningful response.",
            "dominantIdea": contract.get("topic") or (exact_strings[0] if exact_strings else persona.get("desired_outcome") or profile.get("primary_promise") or command),
            "audienceReaction": persona.get("emotional_driver") or "I recognize that moment, and I want to see what Infenergy does next.",
            "emotionalMode": contract["emotional_mode"],
            "characters": contract["characters"],
            "canonRequired": contract["canon_required"],
            "story": story,
            "product": profile.get("product_name") or "",
            "productRole": persona.get("product_role") or profile.get("market_role") or "",
            "verifiedProof": [str(item) for item in product_evidence.get("verified_facts", [])],
            "productTruthVersion": product_evidence.get("updated_at_utc") or profile.get("schema_version") or "",
            "visualStandard": (
                "Cinematic 3D photorealism; premium superhero-film key art; believable anatomy, skin, armor, lighting, "
                "depth, body mechanics, cape physics, material response, and environmental energy illumination."
            ),
            "visualHero": "CHARACTER" if contract["characters"] else "TRANSFORMATION",
            "whatHappens": contract["infenergy_action"],
            "mustInclude": [
                "Infenergy causes the visual outcome through a physically legible action",
                "implied before, present action, and after",
                *([f"Connect {profile.get('product_name')} to {persona.get('use_case') or persona.get('life_context') or profile.get('market_role')}"] if profile else []),
                *([f'Exact visible text: {value}' for value in exact_strings]),
            ],
            "mustAvoid": [
                "generic superhero redesign", "static mannequin pose", "cheap plastic CGI", "random neon",
                "hallucinated words, logos, signs, or numbering", "unrelated additional wording",
                *[str(item) for item in profile.get("content_boundaries", [])],
                *[f"Do not repeat prior creative execution: {item}" for item in originality_references],
            ],
            "referenceAssetIds": [],
            "continuityRequirements": contract["continuity_requirements"],
            "oneSecondMessage": headline,
            "imageJob": contract["infenergy_action"],
            "headlineJob": (
                f"Render exactly {headline} as physical scene geometry with correct spelling, perspective, material, light, shadow, and interaction."
                if contract["integrated_typography"] and exact_strings else "Add no generated text unless explicitly required."
            ),
            "captionJob": "Explain the human meaning and story without narrating what the image already shows.",
            "ctaJob": "Use a proportionate participation or brand action only after the story resolves.",
            "visualConcept": ["PHYSICAL_TYPOGRAPHY", "CHARACTER_CAUSES_OUTCOME"] if contract["integrated_typography"] else ["CINEMATIC_STORY_MOMENT"],
            "visualGrammar": ["SINGLE_FOCAL_HIERARCHY", "ACTION_WITH_CONSEQUENCE", "IMPLIED_BEFORE_AFTER"],
            "environment": "A scene-specific, physically believable environment chosen to make the requested metaphor immediately legible.",
            "humanBehavior": "The human consequence is visible through behavior, not exposition.",
            "beforeFrame": "The problem exists and has a recognizable consequence.",
            "afterFrame": "Infenergy's earned action changes the situation.",
            "composition": {"aspectRatio": contract["aspect_ratio"], "cardCount": contract["card_count"], "panelCount": contract.get("panel_count", 0), "exactStrings": exact_strings, "negative_space": "platform-safe headline region integrated with the focal action"},
            "camera": {"language": "cinematic shot variety serving sequential story continuity"},
            "lighting": "Physically believable cinematic light with motivated green energy spill and controlled violet only at high output.",
            "colorCharacter": "realistic environmental color; controlled Infenergy green energy; no indiscriminate neon coating",
            "productVisibility": 2 if profile else 0,
            "productInteraction": persona.get("product_role") or profile.get("market_role") or "none",
            "productVisualLock": {"required": bool(profile), **({"product": profile.get("product_name")} if profile else {})},
            "textMode": "HEADLINE" if exact_strings else "NONE",
            "layoutArchetype": "INTEGRATED_PHYSICAL_TYPOGRAPHY" if contract["integrated_typography"] else "CINEMATIC_STORY",
            "referencePackage": {
                "platform_targets": [contract["platform"]],
                "product_asset_urls": [product_evidence["source_image_url"]] if product_evidence.get("source_image_url") else [],
                "consumer_profile_id": profile.get("product_id"),
                "persona_id": persona.get("persona_id"),
                "persona_call_to_action": persona.get("call_to_action") or profile.get("primary_call_to_action"),
                "infenergy_reason_why": profile.get("infenergy_reason_why"),
            },
            "productionStrategy": {
                "strategy": "REFERENCE_GUIDED" if contract["canon_required"] else "CONCEPT_GUIDED",
                "candidate_count": 4 if contract["canon_required"] else 2,
                "max_major_revisions": contract["repair_policy"]["max_major_revisions"],
                "fallback_ladder": ["repair_selected_candidate", "regenerate_failed_frame", "block_delivery"],
            },
            "qualityGovernance": {
                "risk": "HIGH" if contract["canon_required"] else "MEDIUM",
                "blocking": True,
                "decision": "DO_NOT_DELIVER_UNLESS_CANON_TEXT_STORY_VISUAL_AND_CONTINUITY_GATES_PASS",
                "requiredGates": contract["quality_gates"],
                "exactStrings": exact_strings,
                "dialogueContract": contract.get("dialogue_quality_contract") or {},
            },
        }
        art_direction = {
            "creative_request": creative_request,
            "sequence_briefs": story_beats,
            "visual_message": headline,
            "visual_format": contract["format"],
            "primary_subject": "Infenergy" if contract["characters"] else command,
            "action": contract["infenergy_action"],
            "must_include": creative_request["mustInclude"],
            "must_avoid": creative_request["mustAvoid"],
        }
        provider = default_provider()
        visual = provider.generate(
            art_direction=art_direction,
            positive_prompt=(
                f"{contract['visual_style']}. {contract['infenergy_action']} "
                f"Locked exact strings: {exact_strings or 'none'}. Obey the complete CreativeRequest and sequence briefs."
            ),
            negative_prompt="generic superhero, static pose, character drift, malformed anatomy, misspelled text, random text, weak continuity",
            platform=contract["platform"],
        )
        studio_result = visual.provider_meta.get("creative_result", {}) if isinstance(visual.provider_meta, dict) else {}
        asset_ids = [str(item) for item in studio_result.get("assets", []) if str(item).strip()]
        assets = [f"{provider.base_url}/api/assets/{asset_id}" for asset_id in asset_ids] if hasattr(provider, "base_url") else ([visual.asset_path] if visual.asset_path else [])
        expected_assets = contract["card_count"] if contract["deliverable"] == "carousel" else 1
        studio_status = str(studio_result.get("status") or "")
        if visual.kind != "generated_image":
            return {
                "production_status": "GENERATION_FAILED",
                "creative_contract": contract,
                "provider": visual.as_dict(),
                "failure": "finished_asset_provider_unavailable",
                "next_action": "Restore Entertainment Studio credentials/provider capacity, then retry this same command.",
            }
        if studio_status != "APPROVED":
            return {
                "production_status": "REPAIR_REQUIRED" if studio_status != "REJECTED" else "VALIDATION_FAILED",
                "creative_contract": contract,
                "assets": assets,
                "studio_result": studio_result,
                "failure": f"studio_status:{studio_status}",
            }
        if len(assets) != expected_assets:
            return {
                "production_status": "ASSEMBLY_FAILED",
                "creative_contract": contract,
                "assets": assets,
                "studio_result": studio_result,
                "failure": f"expected_{expected_assets}_assets_received_{len(assets)}",
            }
        generation_metadata = studio_result.get("generationMetadata") or {}
        canon_reference_ids = generation_metadata.get("canonReferenceAssetIds") or []
        candidate_evaluations = generation_metadata.get("candidateEvaluations") or []
        creative_qa = generation_metadata.get("creativeQA") or {}
        selected_candidates = [
            item for item in candidate_evaluations
            if isinstance(item, dict) and item.get("selected") and item.get("qaStatus") in {"PASS", "TECHNICAL_PASS"}
        ]
        required_candidate_count = int(creative_request["productionStrategy"]["candidate_count"])
        if contract["canon_required"] and not canon_reference_ids:
            return {
                "production_status": "VALIDATION_FAILED", "creative_contract": contract,
                "assets": assets, "studio_result": studio_result,
                "failure": "canon_reference_provenance_missing",
            }
        if len(candidate_evaluations) < required_candidate_count or not selected_candidates:
            return {
                "production_status": "VALIDATION_FAILED", "creative_contract": contract,
                "assets": assets, "studio_result": studio_result,
                "failure": "candidate_evaluation_provenance_incomplete",
            }
        performed_gates = {str(item).upper() for item in creative_qa.get("testsPerformed", [])}
        required_gates = {str(item).upper() for item in contract["quality_gates"]}
        if creative_qa.get("status") != "PASS" or not required_gates.issubset(performed_gates):
            return {
                "production_status": "VALIDATION_FAILED", "creative_contract": contract,
                "assets": assets, "studio_result": studio_result,
                "failure": "final_pixel_qa_provenance_incomplete",
            }
        platform_variants = generation_metadata.get("platformVariants") or []
        platform_variant_assets = [
            {
                **item,
                "public_url": f"{provider.base_url}/api/assets/{item['assetId']}",
            }
            for item in platform_variants
            if isinstance(item, dict) and item.get("assetId") and hasattr(provider, "base_url")
        ]
        package = {
            "post_id": creative_request["contentId"],
            "objective": command,
            "creative_contract": contract,
            "creative_request": creative_request,
            "visual_format": contract["format"].upper(),
            "carousel_assets": [{"public_url": url, "local_path": ""} for url in assets] if expected_assets > 1 else [],
            "generated_visuals": {contract["platform"]: assets[0]},
            "platforms": [contract["platform"]],
            "platform_policy": {"platforms": [contract["platform"]]},
            "quality_decision": studio_result.get("qualityDecision", {}),
            "generation_metadata": generation_metadata,
            "product_context": product_context,
            "platform_variant_assets": platform_variant_assets,
            "studio_ancestry": {
                "creative_id": studio_result.get("creativeId"),
                "version_id": studio_result.get("versionId"),
                "sequence_nodes": studio_result.get("sequenceNodes") or [],
            },
        }
        creative_id = creative_request["contentId"]
        now = utc_now()
        preflight = {
            "passed": True,
            "studio_status": studio_status,
            "quality_decision": studio_result.get("qualityDecision", {}),
            "canon_reference_asset_ids": canon_reference_ids,
            "candidate_evaluations": candidate_evaluations,
            "platform_variants": platform_variants,
        }
        with connect(context.data_dir) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO os_creatives VALUES (?, ?, ?, ?, ?, ?, ?, 'DELIVERED', ?, ?, '{}', ?, ?)",
                (
                    creative_id, context.actor, headline, command, contract["platform"],
                    encode([contract["platform"]]), expected_assets, encode(package), encode(preflight), now, now,
                ),
            )
            connection.commit()
        memory_record = {
            "source": "creative.command.produce", "creative_id": creative_id, "objective": command,
            "product_id": profile.get("product_id"), "persona_id": persona.get("persona_id"),
            "visual_signature": f"{contract['creative_mode']}|{contract['emotional_mode']}|{contract['infenergy_action']}",
            "created_at": now,
        }
        append_content_record(memory_record, data_dir=context.data_dir)
        append_visual_record({**memory_record, "assets": assets, "platform": contract["platform"]}, data_dir=context.data_dir)
        return {
            "production_status": "DELIVERED",
            "creative_contract": contract,
            "package": package,
            "assets": assets,
            "asset_count": len(assets),
            "studio_status": studio_status or "APPROVED",
            "creative_id": creative_id,
            "studio_creative_id": studio_result.get("creativeId"),
            "studio_version_id": studio_result.get("versionId"),
            "studio_sequence_nodes": studio_result.get("sequenceNodes") or [],
            "product_context": product_context,
            "canon_reference_asset_ids": canon_reference_ids,
            "candidate_evaluations": candidate_evaluations,
            "platform_variants": platform_variants,
            "quality_decision": studio_result.get("qualityDecision", {}),
            "next_action": "Review the delivered assets; scheduling remains a separate owner-approved action.",
        }

    def creative_post_compose(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_plan_120 import POST_TYPE_LABELS
        from social import model_router

        post_type = str(payload["post_type"]).strip()
        if post_type not in POST_TYPE_LABELS:
            raise ValueError(f"unknown_post_type:{post_type}")
        copy_provider = str(payload.get("copy_provider") or "gemini").strip().lower()
        if copy_provider not in {"gemini", DEFAULT_MASTER_MODEL}:
            raise ValueError("copy_provider_must_be_gemini_or_gpt-5.6-sol")
        visual_format = str(payload.get("visual_format") or "single_image").strip().lower()
        if visual_format not in {"single_image", "carousel"}:
            raise ValueError("visual_format_must_be_single_image_or_carousel")
        branded_formats = {
            "infenergy_micro_mission": {
                "visual_format": "carousel", "slide_count": 8,
                "copy_contract": (
                    "Also return a visible_text object with cover, six story_beats, and finale. "
                    "Each value must be concise text intended to appear on the image cards."
                ),
                "visual_contract": (
                    "Create an Infenergy Micro Mission as exactly 8 still-image 4:5 carousel cards. "
                    "Use a cover, six sequential story beats, and a branded finale. Integrate the supplied "
                    "visible text legibly into each card with top-to-bottom story continuity"
                ),
            },
            "infenergy_storypage": {
                "visual_format": "single_image", "slide_count": 1,
                "copy_contract": (
                    "Also return a visible_text object with headline, panel_lines, and brand_ending. "
                    "panel_lines must contain 3 to 6 concise strings intended to appear inside the image."
                ),
                "visual_contract": (
                    "Create an Infenergy StoryPage as exactly one 9:16 image with 3 to 6 readable comic panels, "
                    "a hero panel, mobile-safe integrated text, and a branded ending"
                ),
            },
            "superhero_text_integration": {
                "visual_format": "single_image", "slide_count": 1,
                "copy_contract": (
                    "Also return visible_text as one short, memorable string intended to appear exactly in the image."
                ),
                "visual_contract": (
                    "Create exactly one 4:5 cinematic Infenergy superhero image. Integrate the supplied exact visible "
                    "text physically and legibly into the scene as intentional typography"
                ),
            },
        }
        branded_contract = branded_formats.get(post_type)
        if branded_contract:
            visual_format = str(branded_contract["visual_format"])
        platforms = [str(item).strip().lower() for item in payload.get("platforms", []) if str(item).strip()]
        if not platforms or any(item not in {"facebook", "instagram", "linkedin"} for item in platforms):
            raise ValueError("platforms_must_include_supported_platform")
        brief = str(payload["brief"]).strip()
        if not brief:
            raise ValueError("creative_brief_required")
        slide_count = max(2, min(int(payload.get("slide_count", 6)), 10))
        if branded_contract:
            slide_count = int(branded_contract["slide_count"])
        copy_prompt = (
            "Return only one JSON object with string keys title and master_copy, plus a platform_posts object. "
            "platform_posts must contain exactly the requested platform keys and each value must contain a final_caption string. "
            "Write finished, original Infenergy social copy grounded only in the supplied brief. Do not invent product facts. "
            f"{str(branded_contract.get('copy_contract') or '') if branded_contract else ''} "
            f"Post type: {POST_TYPE_LABELS[post_type]}. Platforms: {', '.join(platforms)}. Brief: {brief}"
        )
        if context.dry_run:
            return {
                "would_compose": True, "post_type": post_type, "copy_provider": copy_provider,
                "image_provider": "gemini", "visual_format": visual_format, "platforms": platforms,
                "slide_count": slide_count,
                "content_format_identifier": post_type if branded_contract else None,
                "visible_text_required": bool(branded_contract),
                "production_mutated": False,
            }
        if copy_provider == "gemini":
            authored = model_router.generate_json(
                "copy_editing", copy_prompt,
                system_instruction="You are Infenergy's social copywriter. Return strict JSON and no markdown.",
            )
            if authored is None:
                raise RuntimeError(f"gemini_copy_generation_failed:{model_router.last_error() or 'empty_response'}")
            model = model_router.route_for("copy_editing")
            provider = "gemini"
        else:
            master = CopilotMaster(context.data_dir)
            if master.model != DEFAULT_MASTER_MODEL:
                raise ValueError(f"exact_master_model_required:{DEFAULT_MASTER_MODEL}:configured:{master.model}")
            response = asyncio.run(master.converse(
                copy_prompt, session_id=new_session_id(),
                system_message="You are Infenergy's social copywriter. Return strict JSON and no markdown.", tools=[],
            ))
            raw = str(response.get("content") or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
            authored = json.loads(raw)
            model = master.model
            provider = str(response.get("provider") or "github-copilot-sdk")
        platform_posts = authored.get("platform_posts") if isinstance(authored, dict) else None
        if not isinstance(platform_posts, dict) or any(
            not isinstance(platform_posts.get(platform), dict)
            or not str(platform_posts[platform].get("final_caption") or "").strip()
            for platform in platforms
        ):
            raise ValueError("composer_copy_response_missing_platform_captions")
        visible_text = authored.get("visible_text") if isinstance(authored, dict) else None
        visible_text_valid = not branded_contract
        if post_type == "infenergy_micro_mission":
            visible_text_valid = (
                isinstance(visible_text, dict)
                and bool(str(visible_text.get("cover") or "").strip())
                and bool(str(visible_text.get("finale") or "").strip())
                and isinstance(visible_text.get("story_beats"), list)
                and len(visible_text["story_beats"]) == 6
                and all(str(item).strip() for item in visible_text["story_beats"])
            )
        elif post_type == "infenergy_storypage":
            visible_text_valid = (
                isinstance(visible_text, dict)
                and bool(str(visible_text.get("headline") or "").strip())
                and bool(str(visible_text.get("brand_ending") or "").strip())
                and isinstance(visible_text.get("panel_lines"), list)
                and 3 <= len(visible_text["panel_lines"]) <= 6
                and all(str(item).strip() for item in visible_text["panel_lines"])
            )
        elif post_type == "superhero_text_integration":
            visible_text_valid = isinstance(visible_text, str) and bool(visible_text.strip())
        if not visible_text_valid:
            raise ValueError(f"composer_copy_response_invalid_visible_text:{post_type}")
        format_instruction = str(branded_contract["visual_contract"]) if branded_contract else (
            f"Create an explicitly {slide_count}-card carousel"
            if visual_format == "carousel"
            else "Create exactly one single image, not a carousel, not multiple cards"
        )
        text_instruction = (
            f"Exact visible text contract: {json.dumps(visible_text, ensure_ascii=True)}."
            if branded_contract
            else "Do not render captions or extra visible text in the image."
        )
        visual = flagship_creative_produce({
            "command": (
                f"{format_instruction} for {platforms[0]}. Post type: {POST_TYPE_LABELS[post_type]}. "
                f"Creative direction: {brief}. {text_instruction}"
            )
        }, context)
        if visual.get("production_status") != "DELIVERED":
            raise RuntimeError(f"composer_visual_generation_failed:{visual.get('failure') or visual.get('production_status')}")
        creative_id = str(visual["creative_id"])
        package = dict(visual["package"])
        assets = list(visual.get("assets") or [])
        package.update({
            "title": str(authored.get("title") or POST_TYPE_LABELS[post_type]).strip(),
            "master_copy": str(authored.get("master_copy") or "").strip(),
            "visible_text": visible_text,
            "post_type": post_type,
            "post_type_label": POST_TYPE_LABELS[post_type],
            "content_type": post_type,
            "platforms": platforms,
            "platform_policy": {"platforms": platforms},
            "routing": {"platforms": platforms},
            "platform_posts": platform_posts,
            "fb_caption": str((platform_posts.get("facebook") or {}).get("final_caption") or ""),
            "ig_caption": str((platform_posts.get("instagram") or {}).get("final_caption") or ""),
            "li_text": str((platform_posts.get("linkedin") or {}).get("final_caption") or ""),
            "copy_generation_source": copy_provider,
            "copy_generation": {"provider": provider, "model": model, "fallback_allowed": False},
            "image_generation": {"provider": "gemini", "fallback_allowed": False},
            "composer": {
                "brief": brief, "visual_format": visual_format, "slide_count": len(assets),
                "content_format_identifier": post_type if branded_contract else None,
            },
        })
        if visual_format == "single_image" and assets:
            package["primary_publish_image_url"] = assets[0]
            package["carousel_assets"] = []
        with connect(context.data_dir) as connection:
            connection.execute(
                "UPDATE os_creatives SET title=?, idea=?, platforms_json=?, slide_count=?, status='DELIVERED', package_json=?, updated_at=? WHERE id=?",
                (package["title"], brief, encode(platforms), len(assets), encode(package), utc_now(), creative_id),
            )
            connection.commit()
        return {
            "production_status": "DELIVERED", "creative_id": creative_id, "creative": package,
            "assets": assets, "copy_provider": copy_provider, "copy_model": model,
            "image_provider": "gemini", "next_action": "Save edits, schedule, or publish now.",
        }

    def creative_scored_story_reel(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from social.reels import build_scored_story_plan, render_scored_story_reel, technical_qa

        package = dict(payload["package"])
        carousel_assets = package.get("carousel_assets")
        if not isinstance(carousel_assets, list) or len(carousel_assets) < 2:
            raise ValueError("package_requires_at_least_two_carousel_assets")
        slide_texts = payload.get("slide_texts")
        if not isinstance(slide_texts, list):
            slide_texts = [
                " ".join(part for part in (str(slide.get("on_image_headline") or ""), str(slide.get("on_image_subline") or "")) if part).strip()
                for slide in package.get("carousel_slides", [])
                if isinstance(slide, dict)
            ]
        visual_readings = payload.get("visual_readings")
        if not isinstance(visual_readings, list):
            visual_readings = package.get("visual_readings")
        if not isinstance(visual_readings, list) or len(visual_readings) != len(carousel_assets):
            story_roles = ("setup", "pressure", "evidence", "reveal", "adaptation", "response", "resolution", "reflection")
            camera_moves = ("push", "pan_right", "pull", "pan_left", "hold")
            visual_readings = []
            for index, asset in enumerate(carousel_assets):
                text = str(slide_texts[index] if index < len(slide_texts) else "").strip()
                visual_readings.append({
                    "story_role": story_roles[min(index, len(story_roles) - 1)],
                    "camera_move": camera_moves[index % len(camera_moves)],
                    "focal_x": 0.5,
                    "focal_y": 0.5,
                    "visual_summary": text or f"Carousel frame {index + 1}",
                    "narrative_change": f"Advance the ordered story to frame {index + 1} of {len(carousel_assets)}.",
                    "reading_order": [text] if text else [str(asset.get("local_path") or f"frame {index + 1}")],
                })
        plan = build_scored_story_plan(
            post_id=str(package.get("post_id") or uuid.uuid4().hex),
            carousel_assets=carousel_assets,
            slide_texts=[str(item) for item in slide_texts],
            emotions=[str(item) for item in payload.get("emotions", [])],
            narration_path=str(payload.get("narration_path") or "") or None,
            motion_intensity=float(payload.get("motion_intensity", 0.55)),
            visual_readings=visual_readings,
        )
        plan["auto_narration"] = bool(payload.get("auto_narration", True))
        if context.dry_run:
            return {"would_render": plan, "production_mutated": False}
        artifact = render_scored_story_reel(plan, data_dir=context.data_dir)
        qa = technical_qa(artifact, plan)
        if qa["status"] != "PASS":
            raise RuntimeError(f"scored_story_technical_qa_failed:{','.join(qa['reasons'])}")
        package["instagram_reel"] = artifact
        package["visual_format"] = "SCORED_STORY_REEL"
        package.setdefault("platform_posts", {})["instagram"] = {
            **package.get("platform_posts", {}).get("instagram", {}),
            "platform": "instagram", "media_type": "REEL", "content_format": "reel",
            "final_caption": package.get("ig_caption", ""),
        }
        return {
            "status": "RENDERED", "package": package, "instagram_reel": artifact,
            "plan": plan, "technical_qa": qa,
            "platform_support": {
                "instagram": "READY_FOR_REEL_SCHEDULING",
                "facebook": "CAROUSEL_ASSETS_RETAINED_VIDEO_UPLOAD_NOT_ENABLED",
                "linkedin": "CAROUSEL_ASSETS_RETAINED_VIDEO_UPLOAD_NOT_ENABLED",
            },
            "next_action": "Call social.schedule with this package and include instagram to schedule the Reel.",
            "_rollback": {"asset_paths": [artifact[key] for key in ("reel_artifact_path", "cover_path", "final_freeze_frame_path", "static_derivative_path")]},
        }

    def rollback_creative(data: dict[str, Any], _: ExecutionContext) -> dict[str, Any]:
        removed = []
        for path in data.get("asset_paths", []):
            candidate = Path(str(path))
            if candidate.is_file():
                candidate.unlink()
                removed.append(str(candidate))
        return {"status": "ROLLED_BACK", "removed_assets": removed}

    def publication_operations(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import daily_index, operations_readiness
        content_date = payload.get("content_date")
        index = daily_index(context.data_dir, content_date)
        readiness = operations_readiness(context.data_dir, lead_hours=int(payload.get("lead_hours", 2)))
        failures = []
        for detail in index.get("details", []):
            for outbox in detail.get("outbox", []):
                for transaction in outbox.get("transactions", []):
                    if transaction.get("state") in {"FAILED", "AMBIGUOUS"} or transaction.get("last_error"):
                        failures.append(transaction)
        return {"index": index, "readiness": readiness, "publication_failures": failures}

    def publication_detail(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import content_detail
        result = content_detail(context.data_dir, str(payload["decision_id"]))
        if not result:
            raise KeyError(f"content_decision_not_found:{payload['decision_id']}")
        return result

    def publication_dispatch(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from dispatch_outbox import dispatch_due
        outbox_id = str(payload.get("outbox_id") or "").strip() or None
        publish_now = bool(payload.get("publish_now", False))
        if context.dry_run:
            from content_operations import operations_readiness
            return {"would_dispatch_due": True, "outbox_id": outbox_id, "publish_now": publish_now, "readiness": operations_readiness(context.data_dir), "production_mutated": False}
        return dispatch_due(data_dir=context.data_dir, outbox_id=outbox_id, force=publish_now)
    def publication_prepare_next(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from dispatch_outbox import pregenerate_upcoming
        outbox_id = str(payload.get("outbox_id") or "").strip() or None
        if context.dry_run:
            return {
                "would_prepare_next": True,
                "outbox_id": outbox_id,
                "copy_provider": "gemini",
                "visual_provider": "gemini",
                "external_publication": False,
                "production_mutated": False,
            }
        result = pregenerate_upcoming(data_dir=context.data_dir, outbox_id=outbox_id)
        return {**result, "external_publication": False}

    def publication_correct_ready(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from build_monthly_content import _gemini_generation_plan, _load_product_brief
        from content_operations import daily_index, platform_transaction, release_outbox, update_ready_package
        from social_visuals import approve_product_reference

        content_date = str(payload["content_date"])
        outbox_id = str(payload["outbox_id"])
        index = daily_index(context.data_dir, content_date)
        outbox = next(
            (
                item
                for detail in index.get("details", [])
                for item in detail.get("outbox", [])
                if str(item.get("outbox_id")) == outbox_id
            ),
            None,
        )
        if not outbox:
            raise KeyError(f"outbox_not_found_for_date:{content_date}:{outbox_id}")
        if outbox.get("status") not in {"READY", "EXTERNAL_ACTION_REQUIRED"}:
            raise ValueError(f"outbox_not_ready_for_correction:{outbox.get('status')}")
        package = dict(outbox.get("package") or {})
        thought = dict(package.get("generation_thought") or {})
        if not thought:
            raise ValueError("generation_thought_required_for_correction")
        transactions = {
            platform: platform_transaction(context.data_dir, outbox_id, platform)
            for platform in ("facebook", "instagram", "linkedin")
        }
        unsafe = [
            platform
            for platform, transaction in transactions.items()
            if transaction and transaction.get("state") not in {"CONFIRMED_SUCCESS", "CONFIRMED_FAILURE", "AUTH_ACTION_REQUIRED"}
        ]
        if unsafe:
            raise ValueError(f"platform_transaction_in_progress:{','.join(unsafe)}")
        preserved = {
            platform: transaction.get("external_id")
            for platform, transaction in transactions.items()
            if transaction.get("state") == "CONFIRMED_SUCCESS"
        }
        target_platforms = [platform for platform in ("facebook", "instagram", "linkedin") if platform not in preserved]
        if not target_platforms:
            raise ValueError("all_platforms_already_published")
        plan = {
            "outbox_id": outbox_id,
            "preserved_publications": preserved,
            "corrected_platforms": target_platforms,
            "copy_provider": "gemini",
            "visual_provider": "gemini",
            "external_publication": False,
        }
        if context.dry_run:
            return {"would_correct": plan, "production_mutated": False}

        if outbox.get("status") == "EXTERNAL_ACTION_REQUIRED":
            release_outbox(context.data_dir, outbox_id, "", reset_attempts=True)

        if not str(thought.get("id") or "").endswith("-CORRECTED"):
            thought["id"] = f"{thought.get('id')}-CORRECTED"
        product_id = str(package.get("product_id") or thought.get("product_id") or "")
        product = _load_product_brief(context.data_dir, product_id) if product_id else None
        product_source = str(package.get("product_image_url") or (product or {}).get("source_image_url") or "").strip()
        if product_id and product_source:
            package["product_image_url"] = product_source
            package["product_image_approval"] = approve_product_reference(product_id, product_source)
        package["generation_thought"] = thought
        package["gemini_copy"] = {
            "provider": "gemini",
            "task": "copy_editing",
            "status": "PENDING",
            "strict_provider": True,
            "fallback_allowed": False,
            "generation_timing": "before_image_generation",
            "required_outputs": ["statement", "expansion", "action", "visible_text", "platform_captions"],
            "source_statement": str(thought.get("statement") or ""),
        }
        package["gemini_generation"] = _gemini_generation_plan(thought, product)
        package["copy_generation_source"] = "pending_strict_gemini"
        package["primary_publish_image_url"] = ""
        visuals = dict(package.get("generated_visuals") or {})
        for platform in target_platforms:
            visuals.pop(platform, None)
        visuals["render_engines"] = {platform: "pending_gemini" for platform in target_platforms}
        visuals["artifact_reviews"] = {
            platform: {"verdict": "PENDING_GEMINI", "issues": []}
            for platform in target_platforms
        }
        package["generated_visuals"] = visuals
        for platform in target_platforms:
            post = package.setdefault("platform_posts", {}).setdefault(platform, {"platform": platform})
            post["caption"] = ""
            post["final_caption"] = ""
            post["final_caption_qa"] = {"status": "PENDING_GEMINI", "reasons": ["strict_gemini_copy_required"]}
        package["fb_caption"] = package.get("fb_caption", "") if "facebook" in preserved else ""
        package["ig_caption"] = package.get("ig_caption", "") if "instagram" in preserved else ""
        package["li_text"] = package.get("li_text", "") if "linkedin" in preserved else ""
        package["correction"] = {
            "corrected_at_utc": utc_now(),
            "reason": str(payload.get("reason") or "creative_contract_mismatch"),
            **plan,
        }
        if not update_ready_package(context.data_dir, outbox_id, package):
            raise RuntimeError("outbox_changed_before_correction")
        return {**plan, "status": "PENDING_STRICT_GEMINI"}

    def publication_republish(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from build_monthly_content import _gemini_generation_plan, _load_product_brief
        from content_operations import daily_index, mark_ready
        from posting_schedule import CENTRAL_TIME
        from social_visuals import approve_product_reference

        content_date = str(payload["content_date"])
        source_outbox_id = str(payload["outbox_id"])
        index = daily_index(context.data_dir, content_date)
        source = next(
            (
                item
                for detail in index.get("details", [])
                for item in detail.get("outbox", [])
                if str(item.get("outbox_id")) == source_outbox_id
            ),
            None,
        )
        if not source:
            raise KeyError(f"outbox_not_found_for_date:{content_date}:{source_outbox_id}")
        package = dict(source.get("package") or {})
        thought = dict(package.get("generation_thought") or {})
        if not thought:
            raise ValueError("generation_thought_required_for_republication")
        plan = {
            "source_outbox_id": source_outbox_id,
            "platforms": ["facebook", "instagram", "linkedin"],
            "copy_provider": "gemini",
            "visual_provider": "gemini",
            "new_publication_transaction": True,
        }
        if context.dry_run:
            return {"would_republish": plan, "production_mutated": False}

        replacement_suffix = uuid.uuid4().hex[:10]
        thought = {
            "id": f"GEMINI-TODAY-{replacement_suffix}",
            "kind": "fresh_gemini_product_editorial",
            "format": "single",
            "statement": "Create a completely new Infenergy post for today.",
            "expansion": "Choose one specific, useful human portable-power moment that has not appeared in the supplied package.",
            "action": "Give the audience one practical next step.",
            "overlay_text": "Pending Gemini authorship",
            "pillar": "Freedom, Designed",
            "visual_motif": "A fresh, specific, photorealistic portable-power moment authored by Gemini.",
            "visual_execution": "premium single-image editorial scene",
            "image_scene": "Gemini must author a new scene from the factual product brief.",
            "product_id": str(package.get("product_id") or ""),
            "content_type": "product",
            "generation_contract": {
                "format": "single_image_product",
                "audience_name": "people who depend on portable devices away from outlets",
                "content_job": "fresh practical product education",
                "product_role": str((package.get("generation_contract") or {}).get("product_role") or "portable charging continuity"),
                "visible_text": {},
            },
        }
        package["post_id"] = f"{str(package.get('post_id') or package.get('content_id') or 'replacement')}-republish-{replacement_suffix}"
        package["content_id"] = package["post_id"]
        package["generation_thought"] = thought
        package["generation_contract"] = dict(thought["generation_contract"])
        package["consumer_root"] = {}
        package["consumer_root_id"] = ""
        package["consumer_world_id"] = ""
        package["consumer_moment_id"] = ""
        package["consumer_receipt"] = {}
        package["consumer_story_contract"] = {}
        package["consumer_selection_receipt"] = {}
        package["routing"] = {"platforms": plan["platforms"]}
        package["platforms"] = plan["platforms"]
        package["platform_policy"] = {"platforms": plan["platforms"]}
        package["gemini_copy"] = {
            "provider": "gemini",
            "task": "copy_editing",
            "status": "PENDING",
            "strict_provider": True,
            "fallback_allowed": False,
            "generation_timing": "before_image_generation",
            "required_outputs": ["statement", "expansion", "action", "image_scene", "visible_text", "platform_captions"],
            "source_statement": str(thought.get("statement") or ""),
        }
        product_id = str(package.get("product_id") or thought.get("product_id") or "")
        product = _load_product_brief(context.data_dir, product_id) if product_id else None
        product_source = str(package.get("product_image_url") or (product or {}).get("source_image_url") or "").strip()
        if product_id and product_source:
            package["product_image_url"] = product_source
            package["product_image_approval"] = approve_product_reference(product_id, product_source)
        package["gemini_generation"] = _gemini_generation_plan(thought, product)
        package["copy_generation_source"] = "pending_strict_gemini"
        package["primary_publish_image_url"] = ""
        package["carousel_assets"] = []
        package["generated_visuals"] = {
            "render_engines": {platform: "pending_gemini" for platform in plan["platforms"]},
            "artifact_reviews": {
                platform: {"verdict": "PENDING_GEMINI", "issues": []}
                for platform in plan["platforms"]
            },
        }
        package["platform_posts"] = {
            platform: {
                "platform": platform,
                "caption": "",
                "final_caption": "",
                "destination_url": str(package.get("destination_url") or ""),
                "content_format": "single_image_product",
                "final_caption_qa": {"status": "PENDING_GEMINI", "reasons": ["strict_gemini_copy_required"]},
            }
            for platform in plan["platforms"]
        }
        package["fb_caption"] = ""
        package["ig_caption"] = ""
        package["li_text"] = ""
        scheduled_at = datetime.now(CENTRAL_TIME).isoformat()
        package["platform_schedule"] = {platform: scheduled_at for platform in plan["platforms"]}
        package["replacement"] = {
            "created_at_utc": utc_now(),
            "reason": str(payload.get("reason") or "owner_requested_corrected_republication"),
            **plan,
        }
        replacement_outbox_id = mark_ready(
            context.data_dir,
            content_date=content_date,
            slot=str(source.get("slot") or "morning"),
            scheduled_at=scheduled_at,
            decision_id=str(source["decision_id"]),
            package=package,
        )
        return {**plan, "status": "PENDING_STRICT_GEMINI", "replacement_outbox_id": replacement_outbox_id}

    def publication_hold(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import hold_outbox

        outbox_id = str(payload["outbox_id"])
        reason = str(payload.get("reason") or "owner_rejected_content")
        if context.dry_run:
            return {"would_hold": outbox_id, "reason": reason, "production_mutated": False}
        hold_outbox(context.data_dir, outbox_id, reason)
        return {"status": "HELD", "outbox_id": outbox_id, "reason": reason}

    def publication_reschedule(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import reschedule_outbox

        outbox_id = str(payload["outbox_id"])
        scheduled_at = str(payload["scheduled_at"])
        slot = str(payload.get("slot") or "").strip() or None
        if context.dry_run:
            return {"would_reschedule": outbox_id, "scheduled_at": scheduled_at, "slot": slot, "production_mutated": False}
        result = reschedule_outbox(context.data_dir, outbox_id, scheduled_at=scheduled_at, slot=slot)
        return {**result, "status": "RESCHEDULED", "_rollback": {"outbox_id": outbox_id, **result["previous"]}}

    def publication_master_remake(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import daily_index, update_ready_package

        outbox_id = str(payload["outbox_id"])
        content_date = str(payload["content_date"])
        instruction = str(payload["instruction"]).strip()
        index = daily_index(context.data_dir, content_date)
        outbox = next((item for detail in index.get("details", []) for item in detail.get("outbox", []) if str(item.get("outbox_id")) == outbox_id), None)
        if not outbox:
            raise KeyError(f"outbox_not_found_for_date:{content_date}:{outbox_id}")
        if outbox.get("status") != "READY":
            raise ValueError(f"outbox_not_ready_for_remake:{outbox.get('status')}")
        master = CopilotMaster(context.data_dir)
        if master.model != DEFAULT_MASTER_MODEL:
            raise ValueError(f"exact_master_model_required:{DEFAULT_MASTER_MODEL}:configured:{master.model}")
        package = dict(outbox.get("package") or {})
        if context.dry_run:
            return {"would_remake": outbox_id, "instruction": instruction, "model": master.model, "production_mutated": False}
        response = asyncio.run(master.converse(
            "Return only a JSON object with keys title, master_copy, platform_posts, and creative_direction. "
            "Preserve factual claims and platform keys. Each platform_posts value must contain final_caption. "
            f"Owner instruction: {instruction}\nCurrent package: {json.dumps(package, ensure_ascii=True)}",
            session_id=new_session_id(),
            system_message="You are the Infenergy post editor. Edit only the requested content. Return strict JSON with no markdown.",
            tools=[],
        ))
        raw = str(response.get("content") or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        remake = json.loads(raw)
        if not isinstance(remake, dict) or not isinstance(remake.get("platform_posts"), dict):
            raise ValueError("master_remake_invalid_response")
        prior = {key: value for key, value in package.items() if key != "version_history"}
        history = list(package.get("version_history") or [])
        history.append({"version": len(history) + 1, "replaced_at": utc_now(), "model": master.model, "instruction": instruction, "package": prior})
        package.update({key: remake[key] for key in ("title", "master_copy", "platform_posts", "creative_direction") if key in remake})
        package["version_history"] = history
        package["last_remake"] = {"model": master.model, "provider": response.get("provider"), "instruction": instruction, "created_at": utc_now()}
        if not update_ready_package(context.data_dir, outbox_id, package):
            raise RuntimeError("outbox_changed_before_master_remake")
        return {"status": "REMADE", "outbox_id": outbox_id, "model": master.model, "version": len(history)}

    def publication_edit_ready(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import daily_index, update_ready_package
        from content_plan_120 import POST_TYPE_LABELS

        outbox_id = str(payload["outbox_id"])
        content_date = str(payload["content_date"])
        changes = dict(payload["changes"])
        allowed = {"title", "master_copy", "post_type", "platform_posts"}
        unexpected = sorted(set(changes) - allowed)
        if unexpected:
            raise ValueError(f"unsupported_edit_fields:{','.join(unexpected)}")
        post_type = str(changes.get("post_type") or "").strip()
        if post_type and post_type not in POST_TYPE_LABELS:
            raise ValueError(f"unknown_post_type:{post_type}")
        platform_posts = changes.get("platform_posts")
        if platform_posts is not None:
            if not isinstance(platform_posts, dict) or any(platform not in {"facebook", "instagram", "linkedin"} for platform in platform_posts):
                raise ValueError("platform_posts_must_use_supported_platforms")
            for value in platform_posts.values():
                if not isinstance(value, dict) or not isinstance(value.get("final_caption"), str):
                    raise ValueError("platform_post_final_caption_required")
        index = daily_index(context.data_dir, content_date)
        outbox = next((item for detail in index.get("details", []) for item in detail.get("outbox", []) if str(item.get("outbox_id")) == outbox_id), None)
        if not outbox:
            raise KeyError(f"outbox_not_found_for_date:{content_date}:{outbox_id}")
        if outbox.get("status") != "READY":
            raise ValueError(f"outbox_not_ready_for_edit:{outbox.get('status')}")
        if context.dry_run:
            return {"would_edit": outbox_id, "fields": sorted(changes), "production_mutated": False}
        package = dict(outbox.get("package") or {})
        prior = {key: value for key, value in package.items() if key != "version_history"}
        history = list(package.get("version_history") or [])
        history.append({"version": len(history) + 1, "replaced_at": utc_now(), "actor": context.actor, "reason": "owner_direct_edit", "package": prior})
        for field in ("title", "master_copy"):
            if field in changes:
                package[field] = str(changes[field])
        if post_type:
            package["post_type"] = post_type
            package["post_type_label"] = POST_TYPE_LABELS[post_type]
            package["content_type"] = post_type
        if platform_posts is not None:
            existing_posts = dict(package.get("platform_posts") or {})
            for platform, value in platform_posts.items():
                existing = dict(existing_posts.get(platform) or {})
                existing.update(value)
                existing_posts[platform] = existing
            package["platform_posts"] = existing_posts
        package["version_history"] = history
        if not update_ready_package(context.data_dir, outbox_id, package):
            raise RuntimeError("outbox_changed_before_owner_edit")
        return {"status": "EDITED", "outbox_id": outbox_id, "version": len(history), "fields": sorted(changes)}

    def rollback_publication_reschedule(data: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        from content_operations import reschedule_outbox

        result = reschedule_outbox(
            context.data_dir, str(data["outbox_id"]), scheduled_at=str(data["scheduled_at"]), slot=str(data["slot"]),
        )
        return {"status": "ROLLED_BACK", **result}

    def publication_delete(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        import importlib

        platform = str(payload["platform"]).strip().lower()
        post_id = str(payload["post_id"]).strip()
        if platform not in {"facebook", "instagram", "linkedin"}:
            raise ValueError("platform_must_be_facebook_instagram_or_linkedin")
        if not post_id:
            raise ValueError("post_id_required")
        if context.dry_run:
            return {"would_delete": {"platform": platform, "post_id": post_id}, "production_mutated": False}
        result = importlib.import_module(f"publish_{platform}").delete(post_id)
        return {"platform": platform, "post_id": post_id, "provider_result": result}

    def brand_positioning(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        from business_intelligence.brand import build_identity, build_positioning, build_voice, build_why, build_worldview
        values = {
            "identity": build_identity(), "why": build_why(), "worldview": build_worldview(),
            "positioning": build_positioning(), "voice": build_voice(),
        }
        return {key: asdict(value) if is_dataclass(value) else value for key, value in values.items()}

    def product_match(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        import inventory_db
        topic = str(payload.get("topic", ""))
        archetype = str(payload.get("archetype", "preparedness_buyer"))
        archetype_words = {
            "preparedness_buyer": "backup outage emergency battery generator home",
            "mobile_professional": "portable charger usb commuter travel work",
            "outdoor_adventurer": "solar camping outdoors off-grid travel",
        }.get(archetype, archetype.replace("_", " "))
        terms = {term for term in re.findall(r"[a-z0-9]+", f"{topic} {archetype_words}".lower()) if len(term) > 2}
        candidates = []
        for product in inventory_db.fetch_products(context.data_dir):
            text = " ".join((str(product.get("name", "")), str(product.get("categories", "")), str(product.get("description", "")), str(product.get("verified_facts", "")))).lower()
            matched = sorted(term for term in terms if term in text)
            candidates.append({
                "product_id": product.get("id") or product.get("product_id"),
                "name": product.get("name"), "score": len(matched), "matched_terms": matched,
                "evidence_eligible": bool(product.get("verified_facts")),
            })
        candidates.sort(key=lambda item: (item["evidence_eligible"], item["score"]), reverse=True)
        limit = max(1, min(int(payload.get("limit", 5)), 25))
        return {"topic": topic, "archetype": archetype, "candidates": candidates[:limit], "method": "catalog_evidence_keyword_fit"}

    def platforms_status(_: dict[str, Any], __: ExecutionContext) -> dict[str, Any]:
        from platform_publishing import list_platforms
        platforms = list_platforms()
        return {
            "platforms": platforms,
            "connected": [item["platform"] for item in platforms if item["status"] == "CONNECTED"],
            "action_required": [item["platform"] for item in platforms if item["status"] in {"REAUTH_REQUIRED", "ERROR"}],
        }

    definitions = [
        Capability("system.health", "System health", "Inspect actual Infenergy provider, database, and social status.", "SYSTEM_HEALTH", health),
        Capability("models.status", "Master model status", "Enumerate authenticated Copilot models and verify the configured master model.", "SYSTEM_HEALTH", model_status),
        Capability("products.list", "List products", "Retrieve canonical Infenergy product records.", "PRODUCTS", products_list, object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}})),
        Capability("products.get", "Get product", "Retrieve one canonical product by identifier.", "PRODUCTS", product_get, object_schema({"product_id": {"type": "string"}}, ["product_id"])),
        Capability("social.calendar.get", "Get social calendar", "Retrieve durable Social Engine slots over a date range.", "SOCIAL", calendar, object_schema({"start_date": {"type": "string"}, "days": {"type": "integer"}})),
        Capability("social.schedule", "Approve and schedule post", "Schedule one finished creative package with one owner approval. A time is selected automatically from the slot when scheduled_at is omitted.", "SOCIAL", schedule_post, object_schema({"content_date": {"type": "string"}, "slot": {"type": "string"}, "scheduled_at": {"type": "string"}, "package": {"type": "object"}, "platforms": {"type": "array"}, "replace_existing": {"type": "boolean"}, "rationale": {"type": "string"}}, ["content_date", "package"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=True, rollback_handler=rollback_schedule),
        Capability("social.schedule_job_campaign", "Load completed campaign into calendar", "Load a completed job's dated campaign deliverables from episodes, packages, or posts into one or more durable Social Engine slots with one owner approval and no publication.", "SOCIAL", schedule_job_campaign, object_schema({"job_id": {"type": "string"}, "start_date": {"type": "string"}, "end_date": {"type": "string"}, "slot": {"type": "string"}, "slots": {"type": "array"}, "scheduled_time": {"type": "string"}, "schedule_times": {"type": "object"}, "platforms": {"type": "array"}}, ["job_id", "start_date", "end_date"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=True, rollback_handler=rollback_job_campaign),
        Capability("goals.get", "Get goals", "Return active and historical Infenergy goals.", "GOALS", goals_get),
        Capability("goals.create", "Create goal", "Create a versioned persistent Infenergy goal.", "GOALS", goal_create, object_schema({"name": {"type": "string"}, "description": {"type": "string"}, "priority": {"type": "integer"}, "metrics": {"type": "array"}, "constraints": {"type": "array"}, "horizon": {"type": "string"}}, ["name", "description"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("policies.get", "Get operating policies", "Return exact current autonomy and approval policies.", "OPERATIONS", policy_list),
        Capability("policies.create", "Create operating policy", "Create a scoped, time-aware operating policy for an Infenergy capability.", "OPERATIONS", policy_create, object_schema({"capability": {"type": "string"}, "rule": {"type": "string"}, "approval_level": {"type": "string"}, "scope": {"type": "object"}, "limits": {"type": "object"}, "valid_until": {"type": "string"}}, ["capability", "rule", "approval_level"]), risk_level="GOVERNANCE", permission_requirement="AUTONOMOUS"),
        Capability("research.mission.create", "Create research mission", "Create a durable multi-workstream research mission with source requirements.", "RESEARCH", research_mission, object_schema({"question": {"type": "string"}, "scope": {"type": "object"}, "workstreams": {"type": "array"}, "source_requirements": {"type": "array"}, "freshness_requirement": {"type": "string"}, "depth": {"type": "string"}}, ["question"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", synchronous=False),
        Capability("jobs.get", "Get jobs", "Inspect durable jobs, steps, progress, checkpoints, errors, and steering.", "OPERATIONS", jobs_list),
        Capability("jobs.steer", "Steer job", "Add an owner instruction to a running durable job.", "OPERATIONS", job_steer, object_schema({"job_id": {"type": "string"}, "instruction": {"type": "string"}}, ["job_id", "instruction"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL"),
        Capability("jobs.control", "Control job", "Pause, continue, or cancel a durable job.", "OPERATIONS", job_control, object_schema({"job_id": {"type": "string"}, "action": {"type": "string"}}, ["job_id", "action"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL"),
        Capability("jobs.complete", "Complete job with deliverables", "Persist the actual completed deliverables for an approved durable job and close every tracked step. Never call with a promise or outline; result must contain the work product.", "OPERATIONS", job_complete, object_schema({"job_id": {"type": "string"}, "result": {"type": "object"}}, ["job_id", "result"]), risk_level="INTERNAL_MUTATION", permission_requirement="AUTONOMOUS"),
        Capability("scenario.create", "Create scenario", "Create an immutable non-production business scenario with explicit uncertainty.", "STRATEGY", scenario_create, object_schema({"premise": {"type": "string"}, "assumptions": {"type": "array"}, "baseline": {"type": "object"}, "changed_variables": {"type": "array"}, "projected_effects": {"type": "array"}, "confidence": {"type": "number"}, "evidence": {"type": "array"}, "limitations": {"type": "array"}}, ["premise"]), risk_level="INTERNAL_MUTATION", permission_requirement="AUTONOMOUS"),
        Capability("world.search", "Search world model", "Search temporal Infenergy entities and current assertions.", "KNOWLEDGE", world_search, object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])),
        Capability("attention.get", "Get attention", "Return the highest-materiality unresolved executive attention items.", "OPERATIONS", attention_get),
        Capability("content.plan_120_days", "Build and schedule 120 days", "Build and queue a complete research-informed 120-day weekly brand content calendar with one owner approval.", "SOCIAL", content_120, object_schema({"objective": {"type": "string"}, "start_date": {"type": "string"}, "replace_unpublished": {"type": "boolean"}, "content_plan": {"type": "string"}, "locked_days": {"type": "integer"}, "horizons": {"type": "array"}}), risk_level="INTERNAL_MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", synchronous=False),
        Capability("research.news", "Research current news", "Retrieve current news through an external provider and store provenance-bearing time-limited findings.", "NEWS", research_news, object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}, "freshness_days": {"type": "integer"}}), cost_class="LOW"),
        Capability("research.findings.get", "Get research findings", "Retrieve the durable Intelligence Library with provenance, credibility, and freshness.", "RESEARCH", findings_get, object_schema({"limit": {"type": "integer"}})),
        Capability("automations.get", "Get automations", "Inspect exact durable automations, schedules, watches, permissions, and status.", "AUTOMATIONS", automations_get),
        Capability("automations.create", "Create automation", "Create a durable capability-based automation with explicit trigger, conditions, approval rules, and failure policy.", "AUTOMATIONS", automation_create, object_schema({"name": {"type": "string"}, "trigger": {"type": "object"}, "conditions": {"type": "array"}, "steps": {"type": "array"}, "permissions": {"type": "object"}, "approval_rules": {"type": "object"}, "schedule": {"type": "object"}, "failure_policy": {"type": "object"}}, ["name", "trigger", "steps"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("automations.control", "Control automation", "Pause, resume, or disable a durable automation.", "AUTOMATIONS", automation_control, object_schema({"automation_id": {"type": "string"}, "action": {"type": "string"}}, ["automation_id", "action"]), risk_level="MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL"),
        Capability("watches.create", "Create intelligence watch", "Create a condition-based monitor with source and materiality policies.", "AUTOMATIONS", watch_create, object_schema({"subject": {"type": "string"}, "scope": {"type": "object"}, "frequency": {"type": "string"}, "source_policy": {"type": "object"}, "materiality_threshold": {"type": "number"}, "condition": {"type": "object"}, "actions": {"type": "array"}, "expires_at": {"type": "string"}}, ["subject"]), risk_level="INTERNAL_MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL"),
        Capability("decisions.get", "Get decisions", "Retrieve actual durable decision rationale, evidence, assumptions, goals, policies, and actions.", "DECISIONS", decisions_get, object_schema({"limit": {"type": "integer"}})),
        Capability("opportunities.get", "Get opportunities", "Return ranked, evidence-bearing Infenergy opportunities.", "BUSINESS_INTELLIGENCE", opportunities_get),
        Capability("risks.get", "Get risks", "Return ranked, evidence-bearing Infenergy risks and mitigations.", "BUSINESS_INTELLIGENCE", risks_get),
        Capability("creative.score", "Score creative", "Evaluate supplied content with the preserved platform-native quality rubric without generating or publishing anything.", "CREATIVE_STUDIO", creative_score, object_schema({"content": {"type": "object"}, "platforms": {"type": "array"}}, ["content"])),
        Capability("creative.command.produce", "Produce flagship creative from a command", "Own a natural-language Infenergy creative request from intent through canon-aware Entertainment Studio generation, sequence assembly, blocking QA, automatic repair, and finished asset delivery. Never returns a plan as a deliverable.", "CREATIVE_STUDIO", flagship_creative_produce, object_schema({"command": {"type": "string"}}, ["command"]), risk_level="INTERNAL_MUTATION", cost_class="MEDIUM", permission_requirement="AUTONOMOUS"),
        Capability("creative.post.compose", "Compose image and copy post", "Generate one saved post with owner-selected post type and Gemini or exact gpt-5.6-sol copy, plus a Gemini-backed image. Scheduling and publication remain separate governed actions.", "CREATIVE_STUDIO", creative_post_compose, object_schema({"post_type": {"type": "string"}, "copy_provider": {"type": "string"}, "visual_format": {"type": "string"}, "slide_count": {"type": "integer"}, "brief": {"type": "string"}, "platforms": {"type": "array"}}, ["post_type", "copy_provider", "brief", "platforms"]), risk_level="INTERNAL_MUTATION", cost_class="MEDIUM", permission_requirement="AUTONOMOUS"),
        Capability("creative.carousel.generate", "Generate carousel package", "Author and render a complete platform-safe carousel package with a caller-selected 2-to-10 slide count. This creates draft assets but does not schedule or publish them.", "CREATIVE_STUDIO", creative_carousel_generate, object_schema({"objective": {"type": "string"}, "title": {"type": "string"}, "platform": {"type": "string"}, "platforms": {"type": "array"}, "slide_count": {"type": "integer"}, "product_id": {"type": "string"}, "product": {"type": "object"}, "principle_key": {"type": "string"}, "archetype_key": {"type": "string"}, "supporting_message": {"type": "string"}, "caption": {"type": "string"}, "cta": {"type": "string"}, "pillar": {"type": "string"}, "visual_motif": {"type": "string"}}, ["objective"]), risk_level="INTERNAL_MUTATION", cost_class="MEDIUM", permission_requirement="AUTONOMOUS", supports_rollback=True, rollback_handler=rollback_creative),
        Capability("creative.scored_story_reel.generate", "Render scored story Reel", "Animate an existing ordered carousel into a vertical H.264 story Reel with readable timing, emotional scoring, free local narration, and Instagram-ready media. Facebook and LinkedIn retain carousel assets until their video upload paths are enabled.", "CREATIVE_STUDIO", creative_scored_story_reel, object_schema({"package": {"type": "object"}, "slide_texts": {"type": "array"}, "emotions": {"type": "array"}, "visual_readings": {"type": "array"}, "narration_path": {"type": "string"}, "auto_narration": {"type": "boolean"}, "motion_intensity": {"type": "number"}}, ["package"]), risk_level="INTERNAL_MUTATION", cost_class="LOW", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=True, rollback_handler=rollback_creative),
        Capability("agents.list", "List operational agents", "List every preserved specialist agent and its accepted parameters and aliases.", "OPERATIONS", agents_list),
        Capability("agents.run", "Run operational agent", "Run a registered specialist with parameters from agents.list. For a complete rendered carousel package, carousel_slide_writer with objective delegates to creative.carousel.generate and validates every asset without scheduling or publishing. Mutation-capable agent execution remains owner-approved.", "OPERATIONS", agent_run, object_schema({"name": {"type": "string"}, "params": {"type": "object"}}, ["name"]), risk_level="INTERNAL_MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=True, rollback_handler=rollback_creative),
        Capability("publication.operations.get", "Get publication operations", "Inspect exact daily slots, candidate decisions, outbox state, platform transactions, failures, and readiness actions.", "SOCIAL", publication_operations, object_schema({"content_date": {"type": "string"}, "lead_hours": {"type": "integer"}})),
        Capability("publication.detail.get", "Get publication decision detail", "Retrieve one content council decision with candidates, rationale, outbox packages, and provider transaction evidence.", "SOCIAL", publication_detail, object_schema({"decision_id": {"type": "string"}}, ["decision_id"])),
        Capability("publication.correct_ready", "Correct ready publication", "Re-render and update one READY or externally blocked outbox package while preserving immutable confirmed platform publications. This does not publish or delete anything.", "SOCIAL", publication_correct_ready, object_schema({"content_date": {"type": "string"}, "outbox_id": {"type": "string"}, "reason": {"type": "string"}}, ["content_date", "outbox_id"]), risk_level="INTERNAL_MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("publication.republish", "Republish corrected publication", "Create a new all-platform publication transaction from an existing package, regenerate strict Gemini copy and visuals, and preserve all previous provider records.", "SOCIAL", publication_republish, object_schema({"content_date": {"type": "string"}, "outbox_id": {"type": "string"}, "reason": {"type": "string"}}, ["content_date", "outbox_id"]), risk_level="INTERNAL_MUTATION", cost_class="MEDIUM", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("publication.hold", "Hold publication", "Prevent one ready publication package from being claimed or dispatched without deleting its audit history.", "SOCIAL", publication_hold, object_schema({"outbox_id": {"type": "string"}, "reason": {"type": "string"}}, ["outbox_id"]), risk_level="INTERNAL_MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("publication.reschedule", "Reschedule publication", "Atomically move one unclaimed publication package to a new date, time, and optional slot without changing its creative or history.", "SOCIAL", publication_reschedule, object_schema({"outbox_id": {"type": "string"}, "scheduled_at": {"type": "string"}, "slot": {"type": "string"}}, ["outbox_id", "scheduled_at"]), risk_level="INTERNAL_MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=True, rollback_handler=rollback_publication_reschedule),
        Capability("publication.master_remake", "Conversational post remake", "Edit one READY post from an owner instruction with exactly gpt-5.6-sol, preserving the prior package as immutable version history and never silently substituting a model.", "SOCIAL", publication_master_remake, object_schema({"content_date": {"type": "string"}, "outbox_id": {"type": "string"}, "instruction": {"type": "string"}}, ["content_date", "outbox_id", "instruction"]), risk_level="INTERNAL_MUTATION", cost_class="MEDIUM", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("publication.edit_ready", "Edit ready publication", "Version and update only owner-editable copy or a registry-backed post type on one READY package; preserve media, routing, canon, and transaction evidence.", "SOCIAL", publication_edit_ready, object_schema({"content_date": {"type": "string"}, "outbox_id": {"type": "string"}, "changes": {"type": "object"}}, ["content_date", "outbox_id", "changes"]), risk_level="INTERNAL_MUTATION", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("publication.prepare_next", "Prepare next publication", "Generate and validate strict Gemini copy and visuals for the next eligible READY package without publishing it; optionally target one exact owner-approved outbox package.", "SOCIAL", publication_prepare_next, object_schema({"outbox_id": {"type": "string"}}), risk_level="INTERNAL_MUTATION", cost_class="MEDIUM", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("publication.dispatch", "Dispatch due publications", "Dispatch due approved outbox packages through preserved idempotent platform publishers; optionally target one exact outbox for approved publish-now; preview safely with dry run.", "SOCIAL", publication_dispatch, object_schema({"outbox_id": {"type": "string"}, "publish_now": {"type": "boolean"}}), risk_level="EXTERNAL_IRREVERSIBLE", cost_class="MEDIUM", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("publication.delete", "Delete exact publication", "Delete one exact platform publication by immutable provider ID after owner approval.", "SOCIAL", publication_delete, object_schema({"platform": {"type": "string"}, "post_id": {"type": "string"}}, ["platform", "post_id"]), risk_level="EXTERNAL_IRREVERSIBLE", permission_requirement="EXECUTE_WITH_APPROVAL", supports_rollback=False),
        Capability("brand.positioning.get", "Get brand positioning", "Return owner-first identity, purpose, worldview, competitive position, and voice constraints with preserved source hierarchy.", "BRAND", brand_positioning),
        Capability("products.match", "Match products to intent", "Rank evidence-eligible catalog products against an audience archetype and topic without changing inventory.", "PRODUCTS", product_match, object_schema({"topic": {"type": "string"}, "archetype": {"type": "string"}, "limit": {"type": "integer"}})),
        Capability("platforms.status", "Get platform connections", "Return machine-readable publishing capabilities, feature flags, and credential health for Facebook, Instagram, LinkedIn, TikTok, and YouTube without exposing secrets.", "SYSTEM_HEALTH", platforms_status),
    ]
    for capability in definitions:
        registry.register(capability)


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False