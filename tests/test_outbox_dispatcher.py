from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

import dispatch_outbox  # noqa: E402
from posting_schedule import growth_schedule  # noqa: E402
from content_operations import (  # noqa: E402
    apply_growth_schedule_to_ready_inventory,
    content_detail,
    create_council_session,
    ensure_daily_slots,
    get_db_path,
    mark_ready,
    platform_transaction,
    reconcile_confirmed_transactions,
    reconcile_ready_inventory,
    recent_outbox_activity,
    transition_package,
)
from publication_contract import PackageState  # noqa: E402


def _ready_package(data_dir: str, platforms: list[str], slot: str = "morning") -> str:
    day = date(2026, 8, 19).isoformat()
    schedule = {
        "morning": f"{day}T13:00:00+00:00",
        "midday": f"{day}T17:00:00+00:00",
        "evening": f"{day}T23:00:00+00:00",
    }
    ensure_daily_slots(data_dir, day, schedule, {"platforms": platforms})
    decision_id = create_council_session(
        data_dir,
        content_date=day,
        slot=slot,
        blackboard={"human_reality": "real", "brain": {}, "heart": {}, "content_job": "TEACH"},
    )
    package = {
        "post_id": "post-1",
        "fb_caption": "Facebook final\n\nUseful depth",
        "ig_caption": "Instagram final\n\nUseful depth",
        "li_text": "LinkedIn final\n\nUseful depth",
        "destination_url": "https://www.infenergypower.com/product/powerpulse-pro-200/",
        "platform_posts": {
            platform: {
                "final_caption": f"{platform.title()} final\n\nUseful depth",
                "destination_url": "https://www.infenergypower.com/product/powerpulse-pro-200/",
                "utm_url": f"https://www.infenergypower.com/product/powerpulse-pro-200/?utm_source={platform}",
            }
            for platform in platforms
        },
        "routing": {"platforms": platforms},
        "generated_visuals": {platform: f"/data/{platform}.png" for platform in platforms},
    }
    return mark_ready(
        data_dir,
        content_date=day,
        slot=slot,
        scheduled_at=schedule[slot],
        decision_id=decision_id,
        package=package,
    )


def test_dispatcher_publishes_archived_payload_without_generation(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook", "instagram", "linkedin"])
    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-1"}) as facebook, \
        patch.object(dispatch_outbox.publish_instagram, "publish", return_value={"id": "ig-1"}) as instagram, \
        patch.object(dispatch_outbox.publish_linkedin, "publish", return_value={"id": "li-1"}) as linkedin:
        result = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")

    assert result["status"] == "PUBLISHED"
    assert facebook.call_args.args[0]["platform_posts"]["facebook"]["final_caption"].count("\n\n") == 1
    assert instagram.call_args.args[0]["platform_posts"]["instagram"]["final_caption"].count("\n\n") == 1
    assert linkedin.call_args.args[0]["platform_posts"]["linkedin"]["final_caption"].count("\n\n") == 1
    assert platform_transaction(data_dir, outbox_id, "facebook")["external_id"] == "fb-1"


def test_dispatcher_routes_intelligence_os_package_without_legacy_routing(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook", "instagram"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = {
        "post_id": "intelligence-os-carousel",
        "platforms": ["facebook", "instagram"],
        "platform_policy": {"platforms": ["facebook", "instagram"]},
        "platform_posts": {
            "facebook": {"final_caption": "Facebook final"},
            "instagram": {"final_caption": "Instagram final"},
        },
        "primary_publish_image_url": "https://example.test/intelligence-os.png",
        "carousel_assets": [],
    }
    connection.execute(
        "UPDATE content_outbox SET package_json=?, scheduled_at=? WHERE outbox_id=?",
        (json.dumps(package), "2026-08-19T13:00:00-05:00", outbox_id),
    )
    connection.commit()
    connection.close()

    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-os"}) as facebook, \
        patch.object(dispatch_outbox.publish_instagram, "publish", return_value={"id": "ig-os"}) as instagram:
        before_due = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T17:59:59+00:00")
        result = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T18:00:01+00:00")

    assert before_due["status"] == "IDLE"
    assert result["status"] == "PUBLISHED"
    assert result["outbox_id"] == outbox_id
    assert facebook.call_count == 1
    assert instagram.call_count == 1


def test_dispatcher_maps_approved_remote_image_for_facebook():
    package = {
        "image_url": "https://example.com/approved.png",
        "platform_posts": {"facebook": {"final_caption": "Approved post"}},
    }

    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-approved"}) as facebook:
        result = dispatch_outbox._publish(package, "facebook")

    assert result == {"id": "fb-approved"}
    assert facebook.call_args.args[0]["primary_publish_image_url"] == package["image_url"]
    assert facebook.call_args.args[0]["owner_supplied_visual"] is True


def test_delivery_enforcement_never_adds_unrequested_platforms(monkeypatch):
    monkeypatch.setattr(dispatch_outbox, "_delivery_enforced", lambda: True)

    platforms = dispatch_outbox._enabled_platforms({
        "platforms": ["instagram", "linkedin"],
        "platform_policy": {"platforms": ["instagram", "linkedin"]},
    })

    assert platforms == ["instagram", "linkedin"]


def test_dispatcher_recovers_existing_no_routed_platforms_failure(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = {
        "post_id": "recover-intelligence-os-post",
        "platforms": ["facebook"],
        "platform_posts": {"facebook": {"final_caption": "Recovered post"}},
        "primary_publish_image_url": "https://example.test/recovered.png",
    }
    connection.execute(
        "UPDATE content_outbox SET package_json=?, status='EXTERNAL_ACTION_REQUIRED', last_error='no_routed_platforms' WHERE outbox_id=?",
        (json.dumps(package), outbox_id),
    )
    connection.execute(
        "UPDATE daily_slots SET status='EXTERNAL_ACTION_REQUIRED', last_error='no_routed_platforms' WHERE outbox_id=?",
        (outbox_id,),
    )
    connection.commit()
    connection.close()

    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-recovered"}) as facebook:
        result = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")

    assert result["status"] == "PUBLISHED"
    assert result["outbox_id"] == outbox_id
    assert facebook.call_count == 1
    assert platform_transaction(data_dir, outbox_id, "facebook")["external_id"] == "fb-recovered"


def test_dispatcher_recovers_intelligence_os_package_stranded_by_inventory_reconciliation(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = {
        "post_id": "recover-reconciled-intelligence-os-post",
        "platform_policy": {"platforms": ["facebook"]},
        "platform_posts": {"facebook": {"final_caption": "Recovered reconciled post"}},
        "primary_publish_image_url": "https://example.test/reconciled.png",
    }
    connection.execute(
        "UPDATE content_outbox SET package_json=?, status='RECOVERING', last_error='ready_package_has_no_routed_platforms' WHERE outbox_id=?",
        (json.dumps(package), outbox_id),
    )
    connection.execute(
        "UPDATE daily_slots SET status='RECOVERING', last_error='ready_package_has_no_routed_platforms' WHERE outbox_id=?",
        (outbox_id,),
    )
    connection.commit()
    connection.close()

    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-reconciled"}) as facebook:
        result = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")

    assert result["status"] == "PUBLISHED"
    assert result["outbox_id"] == outbox_id
    assert facebook.call_count == 1
    assert platform_transaction(data_dir, outbox_id, "facebook")["external_id"] == "fb-reconciled"


def test_inventory_reconciliation_accepts_intelligence_os_platform_policy(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = {
        "post_id": "valid-intelligence-os-post",
        "platform_policy": {"platforms": ["facebook"]},
        "platform_posts": {"facebook": {"final_caption": "Scheduled post"}},
    }
    connection.execute(
        "UPDATE content_outbox SET package_json=? WHERE outbox_id=?",
        (json.dumps(package), outbox_id),
    )
    connection.commit()
    connection.close()

    assert reconcile_ready_inventory(data_dir) == []
    connection = sqlite3.connect(get_db_path(data_dir))
    status = connection.execute(
        "SELECT status FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0]
    connection.close()
    assert status == "READY"


def test_recent_outbox_activity_exposes_persisted_publication_receipts(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-receipt"}):
        dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")

    activity = recent_outbox_activity(data_dir)

    assert activity[0]["outbox_id"] == outbox_id
    assert activity[0]["status"] == "PUBLISHED"
    assert activity[0]["published_at"]
    assert activity[0]["platforms"] == [{
        "platform": "facebook",
        "state": "CONFIRMED_SUCCESS",
        "external_id": "fb-receipt",
        "attempt_count": 1,
        "last_error": None,
        "updated_at": activity[0]["platforms"][0]["updated_at"],
    }]


def test_strict_generation_missing_assets_never_runs_generation_during_dispatch(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    package["gemini_generation"] = {"strict_provider": True}
    connection.execute(
        "UPDATE content_outbox SET package_json=? WHERE outbox_id=?", (json.dumps(package), outbox_id)
    )
    connection.commit()
    connection.close()
    prepare = Mock()
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", prepare)

    result = dispatch_outbox.dispatch_due_batch(
        data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00", limit=25,
    )

    assert result["processed"] == 1
    assert result["published"] == 0
    assert result["results"][0]["status"] == "AWAITING_PREPARATION"
    prepare.assert_not_called()

    second = dispatch_outbox.dispatch_due_batch(
        data_dir=data_dir, now_utc="2026-08-19T13:00:02+00:00", limit=25,
    )
    assert second["processed"] == 0


def test_pregeneration_retries_transient_failed_preparation(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    package["gemini_generation"] = {
        "provider": "gemini",
        "strict_provider": True,
        "fallback_allowed": False,
        "required_image_count": 1,
        "prompts": [{}],
    }
    connection.execute(
        "UPDATE content_outbox SET package_json=?, scheduled_at=?, lifecycle_state='PREPARATION_REQUIRED' WHERE outbox_id=?",
        (json.dumps(package), datetime.now(timezone.utc).isoformat(), outbox_id),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", Mock(side_effect=RuntimeError("temporary provider error")))

    first = dispatch_outbox.pregenerate_upcoming(data_dir=data_dir)

    assert first["status"] == PackageState.FAILED_PREPARATION.value
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", lambda package, _: package)
    monkeypatch.setattr(dispatch_outbox, "evaluate_outbox_readiness", lambda *_, **__: {
        "ready": True,
        "state": PackageState.READY_TO_DISPATCH.value,
        "reason_codes": [],
    })

    second = dispatch_outbox.pregenerate_upcoming(data_dir=data_dir)

    assert second == {"status": "PREGENERATED", "outbox_id": outbox_id}
    connection = sqlite3.connect(get_db_path(data_dir))
    transitions = connection.execute(
        "SELECT destination_state, reason_code FROM package_transitions WHERE outbox_id=? ORDER BY transition_id",
        (outbox_id,),
    ).fetchall()
    connection.close()
    assert (PackageState.PREPARATION_REQUIRED.value, "PREPARATION_RETRY_SCHEDULED") in transitions


def test_pregeneration_hydrates_and_persists_existing_product_approval(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    package.update({
        "product_id": "PRODUCT-01",
        "product_image_url": "https://example.test/product.png",
        "gemini_generation": {
            "provider": "gemini",
            "strict_provider": True,
            "fallback_allowed": False,
            "required_image_count": 1,
            "prompts": [{}],
        },
    })
    connection.execute(
        "UPDATE content_outbox SET package_json=?, scheduled_at=?, lifecycle_state='PREPARATION_REQUIRED' WHERE outbox_id=?",
        (json.dumps(package), datetime.now(timezone.utc).isoformat(), outbox_id),
    )
    connection.commit()
    connection.close()
    approval = {
        "product_id": "PRODUCT-01",
        "source_url": "https://example.test/product.png",
        "sha256": "approved-digest",
    }
    monkeypatch.setattr(dispatch_outbox, "build_product_image_approval", Mock(return_value=approval))
    prepare_assets = Mock(side_effect=lambda prepared, _: prepared)
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", prepare_assets)
    monkeypatch.setattr(dispatch_outbox, "evaluate_outbox_readiness", lambda *_, **__: {
        "ready": True,
        "state": PackageState.READY_TO_DISPATCH.value,
        "reason_codes": [],
    })

    result = dispatch_outbox.pregenerate_upcoming(data_dir=data_dir)

    assert result == {"status": "PREGENERATED", "outbox_id": outbox_id}
    assert prepare_assets.call_args.args[0]["product_image_approval"] == approval
    connection = sqlite3.connect(get_db_path(data_dir))
    persisted = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    connection.close()
    assert persisted["product_image_approval"] == approval


def test_pregeneration_blocks_product_when_approval_cannot_be_hydrated(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    package.update({
        "product_id": "PRODUCT-01",
        "product_image_url": "https://example.test/unavailable.png",
        "gemini_generation": {
            "provider": "gemini",
            "strict_provider": True,
            "fallback_allowed": False,
            "required_image_count": 1,
            "prompts": [{}],
        },
    })
    connection.execute(
        "UPDATE content_outbox SET package_json=?, scheduled_at=?, lifecycle_state='PREPARATION_REQUIRED' WHERE outbox_id=?",
        (json.dumps(package), datetime.now(timezone.utc).isoformat(), outbox_id),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(dispatch_outbox, "build_product_image_approval", Mock(return_value={}))
    prepare_assets = Mock()
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", prepare_assets)

    result = dispatch_outbox.pregenerate_upcoming(data_dir=data_dir)

    assert result["status"] == PackageState.FAILED_PREPARATION.value
    assert result["error"] == "RuntimeError:product_reference_identity_not_approved"
    prepare_assets.assert_not_called()


def test_pregeneration_persists_completed_copy_before_image_failure(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    package.update({
        "gemini_copy": {"provider": "gemini", "strict_provider": True, "fallback_allowed": False, "status": "PENDING"},
        "gemini_generation": {
            "provider": "gemini",
            "strict_provider": True,
            "fallback_allowed": False,
            "required_image_count": 1,
            "prompts": [{}],
        },
    })
    connection.execute(
        "UPDATE content_outbox SET package_json=?, scheduled_at=?, lifecycle_state='PREPARATION_REQUIRED' WHERE outbox_id=?",
        (json.dumps(package), datetime.now(timezone.utc).isoformat(), outbox_id),
    )
    connection.commit()
    connection.close()

    def complete_copy(prepared, _):
        prepared["gemini_copy"].update({
            "status": "COMPLETE",
            "model_output_sha256": "copy-digest",
            "qa": {"clarity": "PASS"},
        })
        return prepared

    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_copy", complete_copy)
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", Mock(side_effect=RuntimeError("image provider error")))

    result = dispatch_outbox.pregenerate_upcoming(data_dir=data_dir)

    assert result["status"] == PackageState.FAILED_PREPARATION.value
    connection = sqlite3.connect(get_db_path(data_dir))
    persisted = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    connection.close()
    assert persisted["gemini_copy"]["status"] == "COMPLETE"
    assert persisted["gemini_copy"]["model_output_sha256"] == "copy-digest"


def test_budget_blocked_pregeneration_retries_when_base_workflow_fits(monkeypatch):
    package = {
        "gemini_copy": {"provider": "gemini", "strict_provider": True, "fallback_allowed": False, "status": "PENDING"},
        "gemini_generation": {
            "provider": "gemini", "strict_provider": True, "fallback_allowed": False,
            "required_image_count": 1, "prompts": [{}],
        },
    }
    transitions = []
    preflights = []
    monkeypatch.setattr(dispatch_outbox, "upcoming_ready_packages", lambda *_, **__: [{
        "outbox_id": "budget-1", "scheduled_at": "2026-09-15T12:00:00+00:00",
        "lifecycle_state": PackageState.BLOCKED_BUDGET.value, "package": package,
    }])
    monkeypatch.setattr(dispatch_outbox, "transition_package", lambda *args, **kwargs: transitions.append((args, kwargs)))
    monkeypatch.setattr(dispatch_outbox, "preflight_gemini_workflow", lambda **kwargs: preflights.append(kwargs))
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_copy", lambda prepared, _: prepared)
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", lambda prepared, _: prepared)
    monkeypatch.setattr(dispatch_outbox, "update_ready_package", lambda *args: True)
    monkeypatch.setattr(dispatch_outbox, "evaluate_outbox_readiness", lambda *_, **__: {
        "ready": True, "state": PackageState.READY_TO_DISPATCH.value, "reason_codes": [],
    })

    result = dispatch_outbox.pregenerate_upcoming(data_dir="unused")

    assert result == {"status": "PREGENERATED", "outbox_id": "budget-1"}
    assert preflights == [{"image_calls": 1, "reasoning_calls": 2}]
    assert transitions[0][0][2:4] == (PackageState.PREPARATION_REQUIRED.value, "BUDGET_CAPACITY_RESTORED")


def test_budget_blocked_pregeneration_does_not_retry_before_capacity_returns(monkeypatch):
    package = {
        "gemini_generation": {
            "provider": "gemini", "strict_provider": True, "fallback_allowed": False,
            "required_image_count": 1, "prompts": [{}],
        },
    }
    transitions = []
    prepare_assets = Mock()
    monkeypatch.setattr(dispatch_outbox, "upcoming_ready_packages", lambda *_, **__: [{
        "outbox_id": "budget-1", "scheduled_at": "2026-09-15T12:00:00+00:00",
        "lifecycle_state": PackageState.BLOCKED_BUDGET.value, "package": package,
    }])
    monkeypatch.setattr(
        dispatch_outbox,
        "preflight_gemini_workflow",
        Mock(side_effect=dispatch_outbox.GeminiBudgetExceeded("daily budget exhausted")),
    )
    monkeypatch.setattr(dispatch_outbox, "transition_package", lambda *args, **kwargs: transitions.append((args, kwargs)))
    monkeypatch.setattr(dispatch_outbox, "_prepare_gemini_assets", prepare_assets)

    result = dispatch_outbox.pregenerate_upcoming(data_dir="unused")

    assert result == {
        "status": "BLOCKED_BUDGET",
        "outbox_id": "budget-1",
        "reason_code": "AI_BUDGET_EXHAUSTED",
        "error": "daily budget exhausted",
    }
    assert transitions == []
    prepare_assets.assert_not_called()


def test_daily_package_publishes_each_platform_at_its_own_growth_window(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook", "instagram", "linkedin"])
    connection = sqlite3.connect(get_db_path(data_dir))
    package = json.loads(connection.execute(
        "SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0])
    package["platform_schedule"] = {
        "facebook": "2026-08-19T09:05:00-05:00",
        "linkedin": "2026-08-19T16:05:00-05:00",
        "instagram": "2026-08-19T17:05:00-05:00",
    }
    connection.execute(
        "UPDATE content_outbox SET package_json=?, scheduled_at=? WHERE outbox_id=?",
        (json.dumps(package), package["platform_schedule"]["facebook"], outbox_id),
    )
    connection.commit()
    connection.close()

    with (
        patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-window"}) as facebook,
        patch.object(dispatch_outbox.publish_linkedin, "publish", return_value={"id": "li-window"}) as linkedin,
        patch.object(dispatch_outbox.publish_instagram, "publish", return_value={"id": "ig-window"}) as instagram,
    ):
        morning = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T14:05:01+00:00")
        afternoon = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T21:05:01+00:00")
        evening = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T22:05:01+00:00")

    assert morning["status"] == "PARTIAL_SCHEDULED"
    assert afternoon["status"] == "PARTIAL_SCHEDULED"
    assert evening["status"] == "PUBLISHED"
    assert facebook.call_count == linkedin.call_count == instagram.call_count == 1
    assert platform_transaction(data_dir, outbox_id, "facebook")["external_id"] == "fb-window"
    assert platform_transaction(data_dir, outbox_id, "linkedin")["external_id"] == "li-window"
    assert platform_transaction(data_dir, outbox_id, "instagram")["external_id"] == "ig-window"


def test_growth_schedule_migrates_ready_inventory_with_central_dst(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook", "instagram", "linkedin"])

    first = apply_growth_schedule_to_ready_inventory(data_dir, "2026-08-19")
    second = apply_growth_schedule_to_ready_inventory(data_dir, "2026-08-19")
    connection = sqlite3.connect(get_db_path(data_dir))
    row = connection.execute(
        "SELECT scheduled_at, package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    connection.close()
    package = json.loads(row[1])

    assert first == {"updated": 1}
    assert second == {"updated": 1}
    assert row[0] == "2026-08-19T08:05:00-05:00"
    assert package["platform_schedule"] == {
        "facebook": "2026-08-19T08:05:00-05:00",
        "instagram": "2026-08-19T12:05:00-05:00",
        "linkedin": "2026-08-19T16:05:00-05:00",
    }


def test_growth_schedule_covers_every_platform_every_day():
    expected_platforms = {"facebook", "instagram", "linkedin"}

    for day in range(19, 26):
        schedule = growth_schedule(f"2026-08-{day}")
        assert set(schedule) == expected_platforms
        assert all(value.startswith(f"2026-08-{day:02d}T") for value in schedule.values())
        assert all(value.endswith("-05:00") for value in schedule.values())


def test_publish_adapts_platform_posts_to_legacy_caption_keys():
    package = {
        "platform_posts": {
            "facebook": {"final_caption": "Facebook exact"},
            "instagram": {"final_caption": "Instagram exact"},
            "linkedin": {"final_caption": "LinkedIn exact"},
        },
    }

    with (
        patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb"}) as facebook,
        patch.object(dispatch_outbox.publish_instagram, "publish", return_value={"id": "ig"}) as instagram,
        patch.object(dispatch_outbox.publish_linkedin, "publish", return_value={"id": "li"}) as linkedin,
    ):
        dispatch_outbox._publish(package, "facebook")
        dispatch_outbox._publish(package, "instagram")
        dispatch_outbox._publish(package, "linkedin")

        empty_package = {"platform_posts": {"facebook": {"final_caption": ""}}}
        dispatch_outbox._publish(empty_package, "facebook")
        assert facebook.call_args.args[0]["fb_caption"] == ""

    assert facebook.call_args_list[0].args[0]["fb_caption"] == "Facebook exact"
    assert instagram.call_args.args[0]["ig_caption"] == "Instagram exact"
    assert linkedin.call_args.args[0]["li_text"] == "LinkedIn exact"


def test_growth_schedule_requeues_only_fixed_caption_adapter_failures(tmp_path):
    data_dir = str(tmp_path)
    recoverable_id = _ready_package(data_dir, ["facebook"], slot="morning")
    unrelated_id = _ready_package(data_dir, ["facebook"], slot="evening")
    connection = sqlite3.connect(get_db_path(data_dir))
    connection.execute(
        "UPDATE content_outbox SET status='FAILED', attempt_count=4, last_error=? WHERE outbox_id=?",
        ("facebook:KeyError:'fb_caption'", recoverable_id),
    )
    connection.execute(
        "UPDATE content_outbox SET status='FAILED', attempt_count=4, last_error=? WHERE outbox_id=?",
        ("facebook:provider_rejected_post", unrelated_id),
    )
    connection.commit()
    connection.close()

    result = apply_growth_schedule_to_ready_inventory(data_dir, "2026-08-19")
    connection = sqlite3.connect(get_db_path(data_dir))
    rows = dict(connection.execute(
        "SELECT outbox_id, status || ':' || attempt_count FROM content_outbox WHERE outbox_id IN (?, ?)",
        (recoverable_id, unrelated_id),
    ).fetchall())
    connection.close()

    assert result == {"updated": 1}
    assert rows[recoverable_id] == "READY:0"
    assert rows[unrelated_id] == "FAILED:4"


def test_partial_retry_never_resends_confirmed_platform(tmp_path):
    data_dir = str(tmp_path)
    _ready_package(data_dir, ["facebook", "instagram"])
    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-1"}) as facebook, \
        patch.object(dispatch_outbox.publish_instagram, "publish", side_effect=RuntimeError("temporary")) as instagram:
        first = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")
        assert first["status"] == "PARTIAL_RETRY"
        instagram.side_effect = None
        instagram.return_value = {"id": "ig-1"}
        second = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:32+00:00")

    assert second["status"] == "PUBLISHED"
    assert facebook.call_count == 1
    assert instagram.call_count == 2


def test_due_batch_does_not_let_failed_oldest_post_starve_later_post(tmp_path):
    data_dir = str(tmp_path)
    first_outbox = _ready_package(data_dir, ["facebook"])
    second_outbox = _ready_package(data_dir, ["facebook"], "midday")

    responses = iter([{"id": "skipped", "reason": "temporary"}, {"id": "fb-later"}])
    with patch.object(dispatch_outbox.publish_facebook, "publish", side_effect=lambda *_, **__: next(responses)):
        result = dispatch_outbox.dispatch_due_batch(
            data_dir=data_dir,
            now_utc="2026-08-19T17:00:01+00:00",
        )

    assert result["processed"] == 2
    assert [item["status"] for item in result["results"]] == ["PARTIAL_RETRY", "PUBLISHED"], result["results"]
    assert result["published"] == 1, result
    assert result["results"][0]["status"] == "PARTIAL_RETRY"
    assert result["results"][1]["status"] == "PUBLISHED"
    assert platform_transaction(data_dir, second_outbox, "facebook")["external_id"] == "fb-later"


def test_outbox_stops_retrying_after_configured_attempt_limit(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    _ready_package(data_dir, ["facebook"])
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "1")

    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "skipped", "reason": "invalid payload"}):
        result = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")

    assert result["status"] == "FAILED"
    assert dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:30:01+00:00")["status"] == "IDLE"


def test_external_success_remains_after_aggregate_persistence_error(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    with patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-1"}), \
        patch.object(dispatch_outbox, "finalize_outbox", side_effect=sqlite3.OperationalError("disk busy")):
        try:
            dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")
        except sqlite3.OperationalError:
            pass
    assert platform_transaction(data_dir, outbox_id, "facebook")["state"] == "CONFIRMED_SUCCESS"
    assert reconcile_confirmed_transactions(data_dir) == [
        {"outbox_id": outbox_id, "reason": "confirmed_transactions_reconciled"}
    ]


def test_dispatcher_recovers_packshot_only_package_before_publisher_call(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    database = sqlite3.connect(get_db_path(data_dir))
    row = database.execute("SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    import json
    package = json.loads(row[0])
    package["generated_visuals"]["render_engines"] = {"facebook": "approved_product_photo"}
    package["generated_visuals"]["artifact_reviews"] = {"facebook": {"verdict": "PASS"}}
    package["visual_plan"] = {"creative_route": "PRODUCT_IN_CONTEXT"}
    database.execute("UPDATE content_outbox SET package_json=? WHERE outbox_id=?", (json.dumps(package), outbox_id))
    database.commit()
    database.close()

    with patch.object(dispatch_outbox.publish_facebook, "publish") as facebook:
        result = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")

    assert result["status"] == "CONTENT_RECOVERING"
    assert "packshot_only" in result["error"]
    facebook.assert_not_called()


def test_enforced_delivery_dispatches_rejected_creative_only_to_requested_platforms(tmp_path):
    data_dir = str(tmp_path)
    outbox_id = _ready_package(data_dir, ["facebook"])
    database = sqlite3.connect(get_db_path(data_dir))
    row = database.execute("SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    import json
    package = json.loads(row[0])
    package["generated_visuals"]["render_engines"] = {"facebook": "approved_product_photo"}
    package["generated_visuals"]["artifact_reviews"] = {"facebook": {"verdict": "REGENERATE_VISUAL"}}
    package["visual_plan"] = {"creative_route": "PRODUCT_IN_CONTEXT"}
    database.execute("UPDATE content_outbox SET package_json=? WHERE outbox_id=?", (json.dumps(package), outbox_id))
    database.commit()
    database.close()

    with patch.dict(os.environ, {"ENFORCE_SOCIAL_DELIVERY": "true"}, clear=False), \
        patch.object(dispatch_outbox.publish_facebook, "publish", return_value={"id": "fb-enforced"}) as facebook, \
        patch.object(dispatch_outbox.publish_instagram, "publish", return_value={"id": "ig-enforced"}) as instagram, \
        patch.object(dispatch_outbox.publish_linkedin, "publish", return_value={"id": "li-enforced"}) as linkedin:
        result = dispatch_outbox.dispatch_due(data_dir=data_dir, now_utc="2026-08-19T13:00:01+00:00")

    assert result["status"] == "PUBLISHED"
    facebook.assert_called_once()
    instagram.assert_not_called()
    linkedin.assert_not_called()
    assert platform_transaction(data_dir, outbox_id, "facebook")["external_id"] == "fb-enforced"
