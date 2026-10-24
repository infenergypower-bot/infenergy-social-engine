from __future__ import annotations

import os
import json
import sqlite3
import sys
from datetime import date, datetime, timezone

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from content_operations import (  # noqa: E402
    archive_candidate,
    begin_platform_transaction,
    claim_due,
    complete_platform_transaction,
    complete_ai_attempt,
    classify_backlog,
    content_detail,
    content_operations_workspace,
    create_council_session,
    daily_status,
    daily_markdown,
    ensure_daily_slots,
    find_eligible_creative,
    init_content_operations,
    mark_ready,
    mark_slot_external_action,
    operations_readiness,
    publishing_metrics,
    reconcile_ready_inventory,
    reconcile_stale_claims,
    reschedule_outbox,
    reserve_ai_attempt,
    today_schedule,
    upcoming_ready_packages,
    update_ready_package,
)
from publication_contract import PackageState  # noqa: E402


def _schedule(day: str) -> dict[str, str]:
    return {
        "morning": f"{day}T13:00:00+00:00",
        "midday": f"{day}T17:00:00+00:00",
        "evening": f"{day}T23:00:00+00:00",
    }


def test_daily_slots_outbox_and_archive_survive_restart(tmp_path):
    day = "2026-08-19"
    data_dir = str(tmp_path)
    init_content_operations(data_dir)
    slots = ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    assert [slot["slot"] for slot in slots] == ["morning", "midday", "evening"]

    decision_id = create_council_session(
        data_dir,
        content_date=day,
        slot="morning",
        blackboard={
            "human_reality": "A household decides what must keep working first.",
            "brain": {"before": "buy more", "movement": "prioritize", "after": "match needs"},
            "heart": {"response": "clarity", "after": "calm capability"},
            "content_job": "HELP_PLAN",
        },
        rationale=["Preparedness starts with priorities, not purchases."],
    )
    for ordinal in range(1, 8):
        archive_candidate(
            data_dir,
            decision_id=decision_id,
            ordinal=ordinal,
            content={"post_id": f"candidate-{ordinal}", "master_copy": f"Draft {ordinal}"},
            status="SELECTED" if ordinal == 4 else "NOT_SELECTED",
            score=90 + ordinal,
            loss_reasons=[] if ordinal == 4 else ["lower_ranked_compliant_candidate"],
        )

    package = {
        "content_id": "candidate-4",
        "master_copy": "Final copy",
        "platform_presentations": {
            "facebook": {"final_caption": "Facebook final\n\nSecond paragraph"},
            "instagram": {"final_caption": "Instagram final\n\nSecond paragraph"},
            "linkedin": {"final_caption": "LinkedIn final\n\nSecond paragraph"},
        },
        "routing": {"platforms": ["facebook", "instagram", "linkedin"]},
        "platform_posts": {
            "facebook": {"final_caption": "Facebook final"},
            "instagram": {"final_caption": "Instagram final"},
            "linkedin": {"final_caption": "LinkedIn final"},
        },
        "primary_publish_image_url": "https://example.test/final.png",
        "media_asset": {"status": "READY", "role": "FINAL_SOCIAL_CREATIVE"},
    }
    outbox_id = mark_ready(
        data_dir,
        content_date=day,
        slot="morning",
        scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package=package,
    )

    # Reinitialize against the same SQLite file to simulate a process restart.
    init_content_operations(data_dir)
    status = daily_status(data_dir, day)
    assert status["required"] == 3
    assert status["ready"] == 1
    assert status["missing"] == 2
    detail = content_detail(data_dir, decision_id)
    assert len(detail["candidates"]) == 7
    assert detail["candidates"][3]["status"] == "SELECTED"

    claimed = claim_due(data_dir, "2026-08-19T13:00:01+00:00")
    assert claimed and claimed["outbox_id"] == outbox_id
    assert claimed["package"]["platform_presentations"]["instagram"]["final_caption"].count("\n\n") == 1
    assert claim_due(data_dir, "2026-08-19T13:00:02+00:00") is None


def test_pregeneration_updates_only_unclaimed_ready_package(tmp_path):
    day = "2026-08-19"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(
        data_dir,
        content_date=day,
        slot="morning",
        blackboard={"content_job": "TEACH"},
    )
    outbox_id = mark_ready(
        data_dir,
        content_date=day,
        slot="morning",
        scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package={"content_id": "content-1", "generation": "pending", "routing": {"platforms": ["facebook"]}, "platform_posts": {"facebook": {"final_caption": "Ready copy"}}, "primary_publish_image_url": "https://example.test/ready.png"},
    )

    rows = upcoming_ready_packages(data_dir, before_utc="2026-08-20T00:00:00+00:00")
    assert [row["outbox_id"] for row in rows] == [outbox_id]
    assert update_ready_package(data_dir, outbox_id, {"content_id": "content-1", "generation": "complete", "routing": {"platforms": ["facebook"]}, "platform_posts": {"facebook": {"final_caption": "Ready copy"}}, "primary_publish_image_url": "https://example.test/ready.png"}) is True

    claimed = claim_due(data_dir, "2026-08-19T13:00:01+00:00")
    assert claimed["package"]["generation"] == "complete"
    assert update_ready_package(data_dir, outbox_id, {"content_id": "overwritten"}) is False


def test_budget_blocked_package_respects_stale_preparation_cutoff(tmp_path):
    day = "2026-08-19"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(
        data_dir, content_date=day, slot="morning", blackboard={"content_job": "TEACH"},
    )
    outbox_id = mark_ready(
        data_dir, content_date=day, slot="morning", scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package={"content_id": "budget-retry", "routing": {"platforms": ["facebook"]}},
    )
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    connection.execute(
        "UPDATE content_outbox SET lifecycle_state='BLOCKED_BUDGET' WHERE outbox_id=?", (outbox_id,),
    )
    connection.commit()
    connection.close()

    rows = upcoming_ready_packages(
        data_dir,
        before_utc="2026-08-20T00:00:00+00:00",
        after_utc="2026-08-19T20:00:00+00:00",
    )

    assert rows == []


def test_initialization_migrates_legacy_ready_draft_to_preparation_required(tmp_path):
    day = "2026-08-19"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(
        data_dir, content_date=day, slot="morning", blackboard={"content_job": "TEACH"},
    )
    outbox_id = mark_ready(
        data_dir, content_date=day, slot="morning", scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package={"content_id": "legacy", "routing": {"platforms": ["facebook"]}},
    )
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    connection.execute(
        "UPDATE content_outbox SET lifecycle_state='DRAFT', reason_code='LEGACY_STATE_IMPORTED' WHERE outbox_id=?",
        (outbox_id,),
    )
    connection.commit()
    connection.close()

    init_content_operations(data_dir)

    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    row = connection.execute(
        "SELECT lifecycle_state, reason_code FROM content_outbox WHERE outbox_id=?", (outbox_id,),
    ).fetchone()
    transition = connection.execute(
        "SELECT source_state, destination_state, reason_code FROM package_transitions WHERE outbox_id=? ORDER BY created_at DESC LIMIT 1",
        (outbox_id,),
    ).fetchone()
    connection.close()
    assert row == ("PREPARATION_REQUIRED", "LEGACY_READY_REQUIRES_PREPARATION")
    assert transition == ("DRAFT", "PREPARATION_REQUIRED", "LEGACY_READY_REQUIRES_PREPARATION")


def test_publishing_metrics_report_stalled_automation(tmp_path):
    day = "2026-08-19"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(
        data_dir, content_date=day, slot="morning", blackboard={"content_job": "TEACH"},
    )
    outbox_id = mark_ready(
        data_dir, content_date=day, slot="morning", scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package={"content_id": "stalled", "routing": {"platforms": ["facebook"]}},
    )
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    connection.execute(
        "UPDATE content_outbox SET lifecycle_state='DRAFT', reason_code='LEGACY_STATE_IMPORTED' WHERE outbox_id=?",
        (outbox_id,),
    )
    connection.commit()
    connection.close()

    metrics = publishing_metrics(data_dir, now_utc="2026-08-21T13:00:00+00:00")

    assert metrics["automation_status"] == "DEGRADED"
    assert metrics["legacy_ready_drafts"] == 1
    assert metrics["automation_issues"] == [
        "LEGACY_READY_STUCK_IN_DRAFT", "NO_CONFIRMED_PUBLICATION_36H",
    ]


def test_unprepared_older_row_does_not_block_dispatchable_claim(tmp_path):
    day = "2026-08-19"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    older_decision = create_council_session(
        data_dir, content_date=day, slot="morning", blackboard={"content_job": "TEACH"},
    )
    older_id = mark_ready(
        data_dir, content_date=day, slot="morning", scheduled_at=_schedule(day)["morning"],
        decision_id=older_decision,
        package={"content_id": "older", "routing": {"platforms": ["facebook"]}},
    )
    ready_decision = create_council_session(
        data_dir, content_date=day, slot="midday", blackboard={"content_job": "TEACH"},
    )
    ready_id = mark_ready(
        data_dir, content_date=day, slot="midday", scheduled_at=_schedule(day)["midday"],
        decision_id=ready_decision,
        package={
            "content_id": "ready", "routing": {"platforms": ["facebook"]},
            "platform_posts": {"facebook": {"final_caption": "Ready copy"}},
            "primary_publish_image_url": "https://example.test/ready.png",
        },
    )
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    connection.execute(
        "UPDATE content_outbox SET lifecycle_state='PREPARATION_REQUIRED' WHERE outbox_id=?", (older_id,),
    )
    connection.commit()
    connection.close()

    claimed = claim_due(data_dir, "2026-08-19T17:00:01+00:00")

    assert claimed and claimed["outbox_id"] == ready_id


def test_claim_due_can_target_one_future_outbox_for_approved_publish_now(tmp_path):
    data_dir = str(tmp_path)
    day = "2026-08-20"
    ensure_daily_slots(data_dir, day, _schedule(day), {"platforms": ["facebook"]})
    decision_id = create_council_session(data_dir, content_date=day, slot="morning", blackboard={"content_job": "TEACH"})
    target = mark_ready(
        data_dir, content_date=day, slot="morning", scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id, package={
            "content_id": "target",
            "routing": {"platforms": ["facebook"]},
            "platform_posts": {"facebook": {"final_caption": "Ready copy"}},
            "primary_publish_image_url": "https://example.test/ready.png",
        },
    )

    assert claim_due(data_dir, "2026-08-19T00:00:00+00:00") is None
    claimed = claim_due(data_dir, "2026-08-19T00:00:00+00:00", outbox_id=target, force=True)

    assert claimed["outbox_id"] == target
    assert claimed["status"] == "CLAIMED"


def test_approved_publish_now_can_retry_external_action_package(tmp_path):
    data_dir = str(tmp_path)
    day = "2026-08-20"
    ensure_daily_slots(data_dir, day, _schedule(day), {"platforms": ["facebook", "linkedin"]})
    decision_id = create_council_session(data_dir, content_date=day, slot="morning", blackboard={"content_job": "TEACH"})
    target = mark_ready(
        data_dir, content_date=day, slot="morning", scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id, package={
            "content_id": "target",
            "routing": {"platforms": ["facebook", "linkedin"]},
            "platform_posts": {
                "facebook": {"final_caption": "Ready copy"},
                "linkedin": {"final_caption": "Ready copy"},
            },
            "primary_publish_image_url": "https://example.test/ready.png",
        },
    )
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    connection.execute(
        """UPDATE content_outbox SET status='EXTERNAL_ACTION_REQUIRED',
           lifecycle_state='RECONCILIATION_REQUIRED', last_error='linkedin:expired token'
           WHERE outbox_id=?""",
        (target,),
    )
    connection.commit()
    connection.close()

    assert claim_due(data_dir, "2026-08-20T13:00:01+00:00") is None
    claimed = claim_due(
        data_dir,
        "2026-08-20T13:00:01+00:00",
        outbox_id=target,
        force=True,
    )

    assert claimed["outbox_id"] == target
    assert claimed["status"] == "CLAIMED"


def test_reschedule_outbox_moves_ready_package_and_daily_slot_atomically(tmp_path):
    data_dir = str(tmp_path)
    first_day = "2026-08-20"
    second_day = "2026-08-21"
    ensure_daily_slots(data_dir, first_day, _schedule(first_day), {"platforms": ["facebook"]})
    decision_id = create_council_session(data_dir, content_date=first_day, slot="morning", blackboard={"content_job": "TEACH"})
    outbox_id = mark_ready(
        data_dir, content_date=first_day, slot="morning", scheduled_at=_schedule(first_day)["morning"],
        decision_id=decision_id, package={"content_id": "move-me", "routing": {"platforms": ["facebook"]}, "platform_posts": {"facebook": {"final_caption": "Ready copy"}}, "primary_publish_image_url": "https://example.test/ready.png"},
    )

    result = reschedule_outbox(data_dir, outbox_id, scheduled_at=f"{second_day}T17:30:00+00:00", slot="midday")

    assert result["previous"] == {"content_date": first_day, "slot": "morning", "scheduled_at": _schedule(first_day)["morning"]}
    moved = content_operations_workspace(data_dir, now_utc="2026-08-20T00:00:00+00:00")["upcoming"][0]
    assert moved["content_date"] == second_day
    assert moved["slot"] == "midday"
    assert moved["scheduled_at"] == f"{second_day}T17:30:00+00:00"
    assert daily_status(data_dir, first_day)["slots"][0]["status"] == "UNPLANNED"


def test_platform_transaction_states_are_idempotent_and_persistent(tmp_path):
    day = date(2026, 8, 19)
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day.isoformat()), {"platforms": ["facebook"]})
    decision_id = create_council_session(
        data_dir,
        content_date=day.isoformat(),
        slot="morning",
        blackboard={"content_job": "TEACH"},
    )
    outbox_id = mark_ready(
        data_dir,
        content_date=day.isoformat(),
        slot="morning",
        scheduled_at=_schedule(day.isoformat())["morning"],
        decision_id=decision_id,
        package={"content_id": "content-1"},
    )
    request_key = begin_platform_transaction(
        data_dir,
        outbox_id=outbox_id,
        platform="facebook",
        payload={"message": "Line one\n\nLine two"},
    )
    assert request_key == f"{outbox_id}:facebook"
    complete_platform_transaction(
        data_dir,
        outbox_id=outbox_id,
        platform="facebook",
        state="CONFIRMED_SUCCESS",
        external_id="fb-123",
        provider_response={"id": "fb-123"},
    )
    init_content_operations(data_dir)
    # A second begin reuses the same request key instead of creating another transaction.
    assert begin_platform_transaction(
        data_dir,
        outbox_id=outbox_id,
        platform="facebook",
        payload={"message": "Line one\n\nLine two"},
    ) == request_key


def test_restart_reopens_ready_package_without_routed_platform(tmp_path):
    day = "2026-08-20"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(
        data_dir,
        content_date=day,
        slot="morning",
        blackboard={"content_job": "TEACH"},
    )
    outbox_id = mark_ready(
        data_dir,
        content_date=day,
        slot="morning",
        scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package={"content_id": "content-1", "routing": {"platforms": []}},
    )

    recovered = reconcile_ready_inventory(data_dir)
    status = daily_status(data_dir, day)

    assert recovered == [{"outbox_id": outbox_id, "reason": "ready_package_has_no_routed_platforms"}]
    assert status["slots"][0]["status"] == "RECOVERING"


def test_provider_outage_is_external_action_not_content_failure(tmp_path):
    day = "2026-08-20"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(
        data_dir,
        content_date=day,
        slot="evening",
        blackboard={"content_job": "TEACH", "final_copy": {"instagram": "Archived copy"}},
    )

    mark_slot_external_action(
        data_dir,
        content_date=day,
        slot="evening",
        decision_id=decision_id,
        error="gemini_monthly_spend_cap",
    )
    status = daily_status(data_dir, day)
    detail = content_detail(data_dir, decision_id)

    evening = next(slot for slot in status["slots"] if slot["slot"] == "evening")
    assert evening["status"] == "EXTERNAL_ACTION_REQUIRED"
    assert evening["last_error"] == "gemini_monthly_spend_cap"
    assert detail["status"] == "EXTERNAL_ACTION_REQUIRED"


def test_operations_readiness_detects_missing_package_before_clock(tmp_path):
    day = "2026-08-20"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})

    readiness = operations_readiness(
        data_dir,
        now_utc=datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc),
        lead_hours=2,
        publisher_ready={"facebook": True, "instagram": True, "linkedin": True},
        dispatcher_active=True,
    )

    morning = next(slot for slot in readiness["slots"] if slot["slot"] == "morning")
    assert morning["late_for_readiness"] is True
    assert readiness["service_health"] == "HEALTHY"
    assert readiness["content_supply_health"] == "ACTION_REQUIRED"
    assert any(action["action"] == "RECOVER_OR_PULL_READY_RESERVE" for action in readiness["actions"])


def test_operations_readiness_honors_explicit_single_required_slot(tmp_path):
    today = "2026-08-20"
    tomorrow = "2026-08-21"
    data_dir = str(tmp_path)
    for day in (today, tomorrow):
        ensure_daily_slots(
            data_dir,
            day,
            _schedule(day),
            {"platforms": ["facebook", "instagram", "linkedin"], "required_slots": ["midday"]},
        )
        decision_id = create_council_session(
            data_dir,
            content_date=day,
            slot="midday",
            blackboard={"content_job": "TEACH"},
        )
        mark_ready(
            data_dir,
            content_date=day,
            slot="midday",
            scheduled_at=_schedule(day)["midday"],
            decision_id=decision_id,
            package={"content_id": f"content-{day}", "routing": {"platforms": ["facebook"]}},
        )

    readiness = operations_readiness(
        data_dir,
        now_utc=datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc),
        publisher_ready={"facebook": True},
    )

    assert readiness["today"]["required"] == 1
    assert readiness["today"]["missing"] == 0
    assert readiness["tomorrow"]["required"] == 1
    assert readiness["tomorrow"]["ready"] == 1
    assert readiness["content_supply_health"] == "READY"
    assert all(action["action"] != "REPLENISH" for action in readiness["actions"])
    assert all(action["action"] != "RECOVER_OR_PULL_READY_RESERVE" for action in readiness["actions"])
    assert readiness["next_slot"]["slot"] == "midday"


def test_operations_readiness_derives_production_publisher_health(tmp_path, monkeypatch):
    for day in ("2026-08-20", "2026-08-21"):
        ensure_daily_slots(
            str(tmp_path),
            day,
            _schedule(day),
            {"platforms": ["facebook", "instagram", "linkedin"], "required_slots": ["midday"]},
        )
    monkeypatch.setattr(
        "platform_publishing.list_platforms",
        lambda: [
            {"platform": "facebook", "publishing_enabled": True},
            {"platform": "instagram", "publishing_enabled": True},
            {"platform": "linkedin", "publishing_enabled": True},
            {"platform": "youtube", "publishing_enabled": False},
        ],
    )

    readiness = operations_readiness(
        str(tmp_path),
        now_utc=datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc),
    )

    assert readiness["publisher_health"] == "READY"
    assert all(action["action"] != "RESTORE_PUBLISHER" for action in readiness["actions"])


def test_restart_recovers_stale_claim_without_external_transaction(tmp_path):
    day = "2026-08-20"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(data_dir, content_date=day, slot="morning", blackboard={"content_job": "TEACH"})
    outbox_id = mark_ready(
        data_dir,
        content_date=day,
        slot="morning",
        scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package={"content_id": "content-1", "routing": {"platforms": ["facebook"]}, "platform_posts": {"facebook": {"final_caption": "Ready copy"}}, "primary_publish_image_url": "https://example.test/ready.png"},
    )
    claim_due(data_dir, "2026-08-20T13:00:01+00:00")

    recovered = reconcile_stale_claims(
        data_dir,
        now_utc=datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc),
        stale_minutes=15,
    )
    status = daily_status(data_dir, day)

    assert recovered == [{"outbox_id": outbox_id, "reason": "stale_claim_recovered_after_restart"}]
    assert status["slots"][0]["status"] == "READY"


def test_ai_attempts_are_durable_and_complete_once(tmp_path):
    data_dir = str(tmp_path)
    reserve_ai_attempt(
        data_dir, call_id="call-1", provider="gemini", model="test-model",
        operation_type="image", attempt_number=1, reason="test", initiating_subsystem="pytest",
    )
    complete_ai_attempt(data_dir, call_id="call-1", status="SUCCEEDED", actual_usage={"images": 1})
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    row = connection.execute("SELECT status, actual_usage_json, completed_at FROM ai_generation_attempts WHERE call_id='call-1'").fetchone()
    connection.close()
    assert row[0] == "SUCCEEDED"
    assert json.loads(row[1]) == {"images": 1}
    assert row[2]


def test_inventory_backlog_and_today_schedule_are_read_only(tmp_path):
    data_dir = str(tmp_path)
    day = "2026-08-19"
    ensure_daily_slots(data_dir, day, _schedule(day), {"platforms": ["facebook"]})
    decision = create_council_session(data_dir, content_date=day, slot="morning", blackboard={})
    outbox_id = mark_ready(
        data_dir, content_date=day, slot="morning", scheduled_at=_schedule(day)["morning"], decision_id=decision,
        package={"content_id": "eligible", "routing": {"platforms": ["facebook"]}, "platform_posts": {"facebook": {"final_caption": "Ready copy"}}, "primary_publish_image_url": "https://example.test/eligible.png"},
    )
    assert [item["outbox_id"] for item in find_eligible_creative(data_dir, {"platform": "facebook"})] == [outbox_id]
    assert today_schedule(data_dir, day)["packages"][0]["outbox_id"] == outbox_id
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    connection.execute("UPDATE content_outbox SET scheduled_at=? WHERE outbox_id=?", (f"{day}T13:00:00", outbox_id))
    connection.commit()
    connection.close()
    assert classify_backlog(data_dir, now_utc="2026-08-19T12:00:00+00:00")["ready_relevant"][0]["outbox_id"] == outbox_id


def test_database_rejects_invalid_lifecycle_state(tmp_path):
    data_dir = str(tmp_path)
    init_content_operations(data_dir)
    connection = sqlite3.connect(os.path.join(data_dir, "inventory.db"))
    with pytest.raises(sqlite3.IntegrityError, match="invalid_lifecycle_state"):
        connection.execute("INSERT INTO content_outbox (outbox_id,content_id,decision_id,content_date,slot,scheduled_at,package_json,status,created_at,ready_at,lifecycle_state) VALUES ('o','c','d','2026-01-01','s','2026-01-01T00:00:00+00:00','{}','READY','n','n','MADE_UP')")
    connection.close()


def test_human_readable_daily_ledger_is_derived_from_canonical_records(tmp_path):
    day = "2026-08-20"
    data_dir = str(tmp_path)
    ensure_daily_slots(data_dir, day, _schedule(day), {"mode": "owner_schedule"})
    decision_id = create_council_session(
        data_dir,
        content_date=day,
        slot="morning",
        blackboard={"human_reality": "A household plans before pressure.", "brain": {"movement": "PRIORITIZE"}, "heart": {"after": "CLARITY"}, "content_job": "HELP_PLAN"},
    )
    archive_candidate(
        data_dir,
        decision_id=decision_id,
        ordinal=1,
        content={"post_id": "draft-1"},
        status="NOT_SELECTED",
        score=70,
        loss_reasons=["weaker premise"],
    )
    mark_ready(
        data_dir,
        content_date=day,
        slot="morning",
        scheduled_at=_schedule(day)["morning"],
        decision_id=decision_id,
        package={"content_id": "final-1", "topic": "Outage priorities", "routing": {"platforms": ["facebook"]}},
    )

    ledger = daily_markdown(data_dir, day)

    assert "INFENERGY CONTENT - 2026-08-20" in ledger
    assert "SLOT 1 - MORNING" in ledger
    assert "Human Reality: A household plans before pressure." in ledger
    assert "Candidate 1: NOT_SELECTED; why not selected: weaker premise" in ledger
