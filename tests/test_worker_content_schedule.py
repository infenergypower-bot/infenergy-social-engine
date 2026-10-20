from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import subprocess

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import worker  # noqa: E402
from scripts import run_engine  # noqa: E402
from social import living_intelligence  # noqa: E402


def _asset_handler(path):
    handler = object.__new__(worker.HealthHandler)
    handler.path = path
    handler.headers = {}
    handler.wfile = BytesIO()
    handler.statuses = []
    handler.headers_sent = {}
    handler.send_response = handler.statuses.append
    handler.send_header = handler.headers_sent.__setitem__
    handler.end_headers = lambda: None
    handler.send_error = lambda status: handler.statuses.append(status)
    return handler


def test_approved_asset_route_serves_images_for_get_and_head(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "__file__", str(tmp_path / "worker.py"))
    assets = tmp_path / "production_assets"
    assets.mkdir()
    payload = b"approved-image"
    (assets / "creative.png").write_bytes(payload)

    get_handler = _asset_handler("/approved-assets/creative.png")
    get_handler.do_GET()
    head_handler = _asset_handler("/approved-assets/creative.png")
    head_handler.do_HEAD()

    assert get_handler.statuses == [200]
    assert get_handler.headers_sent["Content-Type"] == "image/png"
    assert get_handler.wfile.getvalue() == payload
    assert head_handler.statuses == [200]
    assert head_handler.headers_sent["Content-Length"] == str(len(payload))
    assert head_handler.wfile.getvalue() == b""


def test_media_route_streams_in_bounded_chunks(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    media = tmp_path / "public_media"
    media.mkdir()
    payload = b"x" * (1024 * 1024)
    (media / "creative.png").write_bytes(payload)

    class RecordingWriter(BytesIO):
        def __init__(self):
            super().__init__()
            self.largest_write = 0

        def write(self, chunk):
            self.largest_write = max(self.largest_write, len(chunk))
            return super().write(chunk)

    handler = _asset_handler("/media/creative.png")
    handler.wfile = RecordingWriter()
    handler.headers = {}

    handler.do_GET()

    assert handler.statuses == [200]
    assert handler.wfile.getvalue() == payload
    assert handler.wfile.largest_write <= 256 * 1024
    assert handler.headers_sent["ETag"]


def test_media_route_honors_matching_etag(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    media = tmp_path / "public_media"
    media.mkdir()
    path = media / "creative.png"
    path.write_bytes(b"approved-image")
    etag = worker.HealthHandler._media_etag(path.stat())

    handler = _asset_handler("/media/creative.png")
    handler.headers = {"If-None-Match": etag}

    handler.do_GET()

    assert handler.statuses == [304]
    assert handler.headers_sent["ETag"] == etag
    assert handler.wfile.getvalue() == b""


def test_media_route_serves_requested_byte_range(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    media = tmp_path / "public_media"
    media.mkdir()
    (media / "creative.mp4").write_bytes(b"0123456789")

    handler = _asset_handler("/media/creative.mp4")
    handler.headers = {"Range": "bytes=2-5"}

    handler.do_GET()

    assert handler.statuses == [206]
    assert handler.headers_sent["Content-Range"] == "bytes 2-5/10"
    assert handler.headers_sent["Content-Length"] == "4"
    assert handler.wfile.getvalue() == b"2345"


def test_media_route_rejects_invalid_byte_range(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    media = tmp_path / "public_media"
    media.mkdir()
    (media / "creative.mp4").write_bytes(b"0123456789")

    handler = _asset_handler("/media/creative.mp4")
    handler.headers = {"Range": "bytes=20-30"}

    handler.do_GET()

    assert handler.statuses == [416]
    assert handler.headers_sent["Content-Range"] == "bytes */10"
    assert handler.wfile.getvalue() == b""


def test_approved_asset_route_rejects_non_images(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "__file__", str(tmp_path / "worker.py"))
    assets = tmp_path / "production_assets"
    assets.mkdir()
    (assets / "private.txt").write_text("not public", encoding="utf-8")

    handler = _asset_handler("/approved-assets/private.txt")
    handler.do_GET()

    assert handler.statuses == [404]
    assert handler.wfile.getvalue() == b""


def test_monthly_api_summary_exposes_knowledge_refresh_audit():
    assert "knowledge_refresh" in worker.MONTHLY_SUMMARY_KEYS
    assert len(worker.MONTHLY_SUMMARY_KEYS) == len(set(worker.MONTHLY_SUMMARY_KEYS))


def test_post_requests_reach_the_existing_endpoint_handler():
    handler = object.__new__(worker.HealthHandler)
    calls = []
    handler.do_GET = lambda: calls.append("handled")

    handler.do_POST()

    assert calls == ["handled"]


def test_manual_run_credential_prefers_log_safe_header():
    handler = SimpleNamespace(headers={"X-Manual-Run-Token": "header-token"})

    assert worker._manual_run_credential(handler, {"token": ["query-token"]}) == "header-token"
    assert worker._manual_run_credential(SimpleNamespace(headers={}), {"token": ["query-token"]}) == "query-token"


def test_patch_requests_reach_the_intelligence_os_handler(monkeypatch, tmp_path):
    captured = {}

    def handle(method, path, payload, data_dir):
        captured.update(method=method, path=path, payload=payload, data_dir=data_dir)
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(worker, "_os_authorized", lambda *_: (True, 200, {}))
    monkeypatch.setattr(worker, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("social_engine.intelligence_os.web.handle", handle)
    handler = object.__new__(worker.HealthHandler)
    handler.path = "/api/os/generation-requests/request-1/days/2026-09-02"
    handler.headers = {"Content-Length": "17"}
    handler.rfile = BytesIO(b'{"frequency":{}}')
    handler.wfile = BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None

    handler.do_PATCH()

    assert statuses == [200]
    assert captured == {
        "method": "PATCH",
        "path": "/api/os/generation-requests/request-1/days/2026-09-02",
        "payload": {"frequency": {}},
        "data_dir": str(tmp_path),
    }


def test_publication_clocks_dispatch_and_never_generate():
    os.environ.pop("CONTENT_DISPATCH_ENABLED", None)
    os.environ.pop("CONTENT_PREGENERATION_ENABLED", None)
    os.environ.pop("AUTONOMOUS_AI_ENABLED", None)
    worker.register_scheduled_jobs()
    jobs = worker.schedule.jobs
    dispatch_jobs = [job for job in jobs if job.job_func.func is worker._start_dispatch_thread]
    pregeneration_jobs = [job for job in jobs if job.job_func.func is worker._start_pregeneration_thread]
    factory_jobs = [job for job in jobs if job.job_func.func is worker._start_factory_thread]
    legacy_clock_jobs = [job for job in jobs if job.job_func.func is worker.run_slot]
    watchdog_jobs = [job for job in jobs if job.job_func.func is worker.run_delivery_watchdog]

    assert len(dispatch_jobs) == 4
    assert pregeneration_jobs == []
    assert factory_jobs == []
    assert legacy_clock_jobs == []
    assert len(watchdog_jobs) == 1
    assert {job.job_func.args[0] for job in dispatch_jobs} == {"morning", "midday", "evening", "due_sweep"}


def test_dispatch_schedule_can_be_paused_while_month_is_built(monkeypatch):
    monkeypatch.setenv("CONTENT_DISPATCH_ENABLED", "false")

    worker.register_scheduled_jobs()

    dispatch_jobs = [job for job in worker.schedule.jobs if job.job_func.func is worker._start_dispatch_thread]
    assert dispatch_jobs == []


def test_preparation_has_two_bounded_daily_windows(monkeypatch):
    monkeypatch.setenv("CONTENT_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("CONTENT_PREGENERATION_ENABLED", "true")

    worker.register_scheduled_jobs()

    preparation_jobs = [job for job in worker.schedule.jobs if job.job_func.func is worker._start_pregeneration_thread]
    assert len(preparation_jobs) == 2


def test_budget_blocked_product_run_is_durable_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "budget_snapshot", lambda: {
        "total": {"remaining": 0},
        "image": {"remaining": 0},
        "next_reset_at_utc": "2026-09-10T00:00:00+00:00",
    })

    kwargs = {
        "slot": "midday",
        "force_live": True,
        "force_dry_run": False,
        "shadow_mode": False,
        "platforms_override": "facebook,instagram,linkedin",
        "duplicate_mode": "strict",
        "readiness_block_override": "true",
        "product_id_override": "BW-1500W-60AH",
        "no_product": False,
        "funnel_stage_override": "CONVERSION",
        "pipeline_override": "orchestrator",
    }
    accepted, first = worker._start_or_defer_slot(idempotency_key="black-warrior-20260910", **kwargs)
    accepted_again, second = worker._start_or_defer_slot(idempotency_key="black-warrior-20260910", **kwargs)

    assert accepted is True
    assert accepted_again is True
    assert first["status"] == "WAITING_FOR_BUDGET_RESET"
    assert first["eligible_at_utc"] == "2026-09-10T00:00:00+00:00"
    assert first["request"]["product_id_override"] == "BW-1500W-60AH"
    assert second["job_id"] == first["job_id"]
    assert second["duplicate_request"] is True


def test_deferred_run_resumes_once_when_full_workflow_fits(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    worker._save_deferred_run_status(
        job_id="job-1",
        idempotency_key="black-warrior-20260910",
        status="WAITING_FOR_BUDGET_RESET",
        request={
            "slot": "midday",
            "force_live": True,
            "force_dry_run": False,
            "shadow_mode": False,
            "platforms_override": "facebook,instagram,linkedin",
            "duplicate_mode": "strict",
            "readiness_block_override": "true",
            "product_id_override": "BW-1500W-60AH",
            "no_product": False,
            "funnel_stage_override": "CONVERSION",
            "pipeline_override": "orchestrator",
        },
    )
    monkeypatch.setattr(worker, "budget_snapshot", lambda: {
        "total": {"remaining": 8},
        "image": {"remaining": 2},
        "next_reset_at_utc": "2026-09-11T00:00:00+00:00",
    })
    starts = []
    monkeypatch.setattr(worker, "_start_slot_thread", lambda **kwargs: starts.append(kwargs) or True)

    result = worker.resume_deferred_run()
    second = worker.resume_deferred_run()

    assert result["status"] == "RUNNING"
    assert second["status"] == "RUNNING"
    assert len(starts) == 1
    assert starts[0]["deferred_job_id"] == "job-1"
    assert starts[0]["product_id_override"] == "BW-1500W-60AH"


def test_active_deferred_run_cannot_be_overwritten(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    worker._save_deferred_run_status(job_id="first", idempotency_key="first-key", status="RUNNING", request={})

    accepted, result = worker._start_or_defer_slot(
        idempotency_key="second-key", slot="morning", force_live=True, force_dry_run=False,
        shadow_mode=False, platforms_override="facebook", duplicate_mode="strict",
        readiness_block_override="true", product_id_override="BW-1500W-60AH", no_product=False,
        funnel_stage_override="CONVERSION", pipeline_override="orchestrator",
    )

    assert accepted is False
    assert result["job_id"] == "first"
    assert result["conflict"] == "another_durable_run_is_active"


def test_stale_running_deferred_job_recovers_after_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "RUN_START_LOCK", worker.threading.Lock())
    worker._save_deferred_run_status(job_id="stale", status="RUNNING", request={})
    monkeypatch.setattr(worker, "budget_snapshot", lambda: {
        "total": {"remaining": 0}, "image": {"remaining": 0},
        "next_reset_at_utc": "2026-09-10T00:00:00+00:00",
    })

    result = worker.resume_deferred_run(recover_stale=True)

    assert result["status"] == "WAITING_FOR_BUDGET_RESET"
    assert result["error"] == "recovered_after_process_restart"


def test_factory_requires_explicit_autonomous_ai_opt_in(monkeypatch):
    monkeypatch.delenv("RUN_FACTORY_ON_STARTUP", raising=False)
    monkeypatch.setenv("AUTONOMOUS_AI_ENABLED", "true")
    monkeypatch.setenv("CONTENT_FACTORY_ENABLED", "true")
    worker.register_scheduled_jobs()

    factory_jobs = [job for job in worker.schedule.jobs if job.job_func.func is worker._start_factory_thread]

    assert len(factory_jobs) == 1
    assert os.environ.get("RUN_FACTORY_ON_STARTUP", "false") == "false"


def test_factory_schedule_can_be_disabled_when_month_is_prebuilt(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_AI_ENABLED", "true")
    monkeypatch.setenv("CONTENT_FACTORY_ENABLED", "false")

    worker.register_scheduled_jobs()

    factory_jobs = [job for job in worker.schedule.jobs if job.job_func.func is worker._start_factory_thread]
    watchdog_jobs = [job for job in worker.schedule.jobs if job.job_func.func is worker.run_delivery_watchdog]
    assert factory_jobs == []
    assert len(watchdog_jobs) == 1


def test_delivery_watchdog_requests_factory_when_today_has_no_publishable_inventory(monkeypatch):
    recoveries = []
    monkeypatch.setattr(worker, "_data_dir", lambda: "unused")
    monkeypatch.setattr(worker, "daily_status", lambda *_: {
        "published": 0,
        "ready": 0,
        "missing": 3,
        "slots": [],
    })
    monkeypatch.setattr(worker, "_start_factory_thread", lambda: recoveries.append("requested"))
    monkeypatch.setenv("AUTONOMOUS_AI_ENABLED", "true")
    monkeypatch.setenv("CONTENT_FACTORY_ENABLED", "true")

    result = worker.run_delivery_watchdog()

    assert result["recovery_requested"] is True
    assert recoveries == ["requested"]


def test_delivery_watchdog_reports_gap_without_autonomous_generation(monkeypatch):
    recoveries = []
    monkeypatch.setattr(worker, "_data_dir", lambda: "unused")
    monkeypatch.setattr(worker, "daily_status", lambda *_: {
        "published": 0, "ready": 0, "missing": 3, "slots": [],
    })
    monkeypatch.setattr(worker, "_start_factory_thread", lambda: recoveries.append("requested"))
    monkeypatch.setenv("AUTONOMOUS_AI_ENABLED", "false")
    monkeypatch.setenv("CONTENT_FACTORY_ENABLED", "false")

    result = worker.run_delivery_watchdog()

    assert result["recovery_requested"] is True
    assert result["autonomous_recovery_enabled"] is False
    assert recoveries == []


def test_delivery_watchdog_does_not_replace_covered_inventory(monkeypatch):
    recoveries = []
    monkeypatch.setattr(worker, "_data_dir", lambda: "unused")
    monkeypatch.setattr(worker, "daily_status", lambda *_: {
        "published": 1,
        "ready": 2,
        "missing": 0,
        "slots": [{"status": "PUBLISHED"}, {"status": "READY"}, {"status": "READY"}],
    })
    monkeypatch.setattr(worker, "_start_factory_thread", lambda: recoveries.append("requested"))

    result = worker.run_delivery_watchdog()

    assert result["recovery_requested"] is False
    assert recoveries == []


def test_factory_prioritizes_today_before_prefilling_tomorrow(monkeypatch, tmp_path):
    today = worker.datetime.now(worker.timezone.utc).date().isoformat()
    generated_dates = []
    monkeypatch.setattr(worker, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(worker, "init_content_operations", lambda *_: None)
    monkeypatch.setattr(worker, "reconcile_ready_inventory", lambda *_: [])
    monkeypatch.setattr(worker, "reconcile_stale_claims", lambda *_: [])
    monkeypatch.setattr(worker, "reconcile_confirmed_transactions", lambda *_: [])
    monkeypatch.setattr(worker, "daily_status", lambda *_: {"slots": [], "missing": 3})
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *args, **kwargs: generated_dates.append(kwargs["env"]["POST_CONTENT_DATE"])
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    worker.run_content_factory()

    assert generated_dates == [today, today, today]


def test_main_runs_due_sweep_immediately_on_startup(monkeypatch):
    startup_order = []
    dispatches = []
    pregenerations = []
    monkeypatch.setenv("CONTENT_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("CONTENT_PREGENERATION_ENABLED", "true")
    monkeypatch.setenv("CONTENT_PREGENERATION_ON_STARTUP", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(worker, "start_health_server", lambda: None)
    monkeypatch.setattr(worker, "_load_meta_runtime_from_state", lambda: (False, "not_configured"))
    monkeypatch.setattr(worker, "_auto_bootstrap_visual_repo", lambda: {"status": "ok", "summary": {}})
    monkeypatch.setattr(worker, "run_intelligence_enrichment", lambda: None)
    monkeypatch.setattr(worker, "init_content_operations", lambda *_: None)
    monkeypatch.setattr(worker, "apply_growth_schedule_to_ready_inventory", lambda *_: {"updated": 0})
    monkeypatch.setattr(worker, "resume_deferred_run", lambda **kwargs: None)
    monkeypatch.setattr(worker, "run_delivery_watchdog", lambda: startup_order.append("watchdog"))
    monkeypatch.setattr(worker, "_start_dispatch_thread", lambda slot: (startup_order.append("dispatch"), dispatches.append(slot)))
    monkeypatch.setattr(worker, "pregenerate_scheduled_content", lambda: (startup_order.append("pregeneration"), pregenerations.append("started")))
    monkeypatch.setattr(worker.schedule, "run_pending", lambda: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        worker.main()
    except KeyboardInterrupt:
        pass

    assert dispatches == ["startup_sweep"]
    assert pregenerations == ["started"]
    assert startup_order == ["pregeneration", "watchdog", "dispatch"]


def test_dispatch_sweep_records_publication_result(monkeypatch):
    result = {
        "status": "COMPLETE",
        "processed": 1,
        "published": 1,
        "failed": 0,
        "results": [{
            "status": "PUBLISHED",
            "outbox_id": "outbox-1",
            "platforms": {"facebook": {"state": "CONFIRMED_SUCCESS", "external_id": "fb-1"}},
        }],
    }
    monkeypatch.setattr(worker, "RUN_LOCK", worker.threading.Lock())
    monkeypatch.setattr(worker, "_data_dir", lambda: "unused")
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=worker.json.dumps(result),
            stderr="provider deprecation warning",
        ),
    )

    worker.dispatch_scheduled_slot("startup_sweep")

    assert worker.LAST_DISPATCH["status"] == "complete"
    assert worker.LAST_DISPATCH["trigger"] == "startup_sweep"
    assert worker.LAST_DISPATCH["exit_code"] == 0
    assert worker.LAST_DISPATCH["result"] == result
    assert worker.LAST_DISPATCH["started_at_utc"]
    assert worker.LAST_DISPATCH["finished_at_utc"]


def test_manual_monthly_generation_builds_prepares_and_pregenerates_to_idle(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "build_monthly_calendar", lambda **kwargs: {
        "queued": 2, "calendar_path": "calendar.json", "single_image_posts": 1, "carousel_posts": 1,
    })
    monkeypatch.setattr(worker, "prepare_monthly_gemini_prompts", lambda data_dir: {
        "prepared_entries": 2, "prepared_prompts": 5,
    })
    results = iter([
        {"status": "PREGENERATED", "outbox_id": "one"},
        {"status": "PREGENERATED", "outbox_id": "two"},
        {"status": "IDLE"},
    ])
    monkeypatch.setattr(worker, "_pregenerate_one_package", lambda: next(results))

    worker.run_manual_monthly_generation("job-1", days=2, start_date=None, replace_unpublished=True)

    status = worker._monthly_generation_status()
    assert status["status"] == "COMPLETE"
    assert status["phase"] == "COMPLETE"
    assert status["queued"] == 2
    assert status["prepared_prompts"] == 5
    assert status["pregenerated_packages"] == 2


def test_manual_monthly_generation_propagates_weekly_brand_mix(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    captured = {}
    monkeypatch.setattr(worker, "build_monthly_calendar", lambda **kwargs: captured.update(kwargs) or {
        "queued": 0, "calendar_path": "calendar.json", "single_image_posts": 103, "carousel_posts": 17,
    })
    monkeypatch.setattr(worker, "prepare_monthly_gemini_prompts", lambda data_dir: {
        "prepared_entries": 0, "prepared_prompts": 0,
    })

    worker.run_manual_monthly_generation(
        "job-120", days=120, start_date="2026-09-01", replace_unpublished=True, content_plan="weekly_brand_mix",
    )

    assert captured["days"] == 120
    assert captured["replace_unpublished"] is True
    assert captured["content_plan"] == "weekly_brand_mix"
    assert worker._monthly_generation_status()["status"] == "COMPLETE"


def test_manual_monthly_generation_rejects_second_request_while_first_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    worker._save_monthly_generation_status(job_id="job-1", status="ACCEPTED", phase="QUEUED")

    accepted, status = worker._start_manual_monthly_generation(
        days=30, start_date=None, replace_unpublished=True,
    )

    assert accepted is False
    assert status["job_id"] == "job-1"
    assert status["status"] == "ACCEPTED"


def test_manual_monthly_generation_completes_without_dispatch_when_nothing_new_was_queued(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "build_monthly_calendar", lambda **kwargs: {
        "queued": 0, "calendar_path": "calendar.json", "single_image_posts": 1, "carousel_posts": 0,
    })
    monkeypatch.setattr(worker, "prepare_monthly_gemini_prompts", lambda data_dir: {
        "prepared_entries": 0, "prepared_prompts": 0,
    })
    monkeypatch.setattr(worker, "_pregenerate_one_package", lambda: pytest.fail("unrelated outbox work was dispatched"))

    worker.run_manual_monthly_generation("job-1", days=1, start_date=None, replace_unpublished=False)

    status = worker._monthly_generation_status()
    assert status["status"] == "COMPLETE"
    assert status["queued"] == 0
    assert status["pregenerated_packages"] == 0


def test_manual_no_product_override_is_scoped_to_one_run(monkeypatch):
    captured_env = {}

    def fake_run(*args, **kwargs):
        captured_env.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.delenv("CONTENT_BUCKET_OVERRIDE", raising=False)
    monkeypatch.setenv("CANDIDATE_POOL_TARGET_DEPTH", "0")
    monkeypatch.setattr(living_intelligence, "heartbeat", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "_auto_bootstrap_visual_repo", lambda: {})
    monkeypatch.setattr(worker, "_auto_refresh_meta_if_due", lambda: (False, "not_due"))
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(worker, "_last_run_outcome", lambda: {"slot": "morning", "status": "skipped_no_eligible_platforms"})

    worker.run_slot("morning", force_live=True, no_product=True, funnel_stage_override="ATTENTION")

    assert captured_env["CONTENT_BUCKET_OVERRIDE"] == "no_product"
    assert captured_env["POST_FUNNEL_STAGE_OVERRIDE"] == "ATTENTION"
    assert captured_env["CANDIDATE_POOL_RUNTIME_ENABLED"] == "false"
    assert captured_env["POST_TEXT_ONLY"] == "true"
    assert "CONTENT_BUCKET_OVERRIDE" not in os.environ


def test_manual_run_never_refills_candidate_pool_before_seven_candidate_council(monkeypatch):
    captured_env = {}

    def fake_run(*args, **kwargs):
        captured_env.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(living_intelligence, "heartbeat", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "_auto_bootstrap_visual_repo", lambda: {})
    monkeypatch.setattr(worker, "_auto_refresh_meta_if_due", lambda: (False, "not_due"))
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(worker, "_last_run_outcome", lambda: {"slot": "midday", "status": "skipped_no_eligible_platforms"})

    worker.run_slot("midday", force_live=True, product_id_override="CAMP-FAN-12K")

    assert captured_env["CANDIDATE_POOL_RUNTIME_ENABLED"] == "false"
    assert captured_env.get("POST_CANDIDATE_COUNT", "7") == "7"
    assert captured_env["POST_TEXT_ONLY"] == "true"


def test_run_slot_decodes_timeout_output_and_records_failure(monkeypatch):
    monkeypatch.delenv("RUN_SLOT_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(living_intelligence, "heartbeat", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "_auto_bootstrap_visual_repo", lambda: {})
    monkeypatch.setattr(worker, "_auto_refresh_meta_if_due", lambda: (False, "not_due"))
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"partial stdout", stderr=b"partial stderr")
        ),
    )

    worker.run_slot("midday", force_live=True)

    assert worker.LAST_RUN["status"] == "generation_failed"
    assert worker.LAST_RUN["error"] == "run_timeout_after_900s"
    assert worker.LAST_RUN["finished_at_utc"]


def test_run_slot_recognizes_durable_dispatch_handoff(monkeypatch):
    monkeypatch.setattr(living_intelligence, "heartbeat", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "_auto_bootstrap_visual_repo", lambda: {})
    monkeypatch.setattr(worker, "_auto_refresh_meta_if_due", lambda: (False, "not_due"))
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='{"status":"QUEUED"}', stderr=""),
    )
    monkeypatch.setattr(worker, "_last_run_outcome", lambda: {
        "slot": "midday", "status": "queued_for_durable_dispatch", "detail": "outbox-1",
    })

    worker.run_slot("midday", force_live=True, product_id_override="BW-1500W-60AH")

    assert worker.LAST_RUN["status"] == "queued_for_durable_dispatch"
    assert worker.LAST_RUN["error"] == "outbox-1"

def test_followup_council_candidates_skip_repeated_phase2_enrichment(monkeypatch):
    observed = []

    def fake_generate(slot, **kwargs):
        observed.append(os.environ.get("ENABLE_PHASE2_CREATIVE_STACK"))
        return {"slot": slot, **kwargs}

    monkeypatch.setenv("ENABLE_PHASE2_CREATIVE_STACK", "true")
    monkeypatch.setenv("COUNCIL_ENRICH_FIRST_CANDIDATE_ONLY", "true")
    monkeypatch.setattr(run_engine.generate_posts, "generate", fake_generate)

    result = run_engine._generate_followup_candidate("midday", product_id_override="CAMP-FAN-12K")

    assert observed == ["false"]
    assert os.environ["ENABLE_PHASE2_CREATIVE_STACK"] == "true"
    assert result["product_id_override"] == "CAMP-FAN-12K"


def test_final_evidence_ignores_out_of_scope_wordpress_copy():
    content = {
        "product_metrics": ["12000mAh"],
        "wp_content": "This unverified safety claim prevents heat distress.",
        "platform_posts": {
            "facebook": {"final_caption": "Published 12000mAh capacity. Compare it with your actual needs."},
            "instagram": {"final_caption": "Published 12000mAh capacity. Compare it with your actual needs."},
            "linkedin": {"final_caption": "Published 12000mAh capacity. Compare it with your actual needs."},
        },
    }

    readiness = run_engine._final_channel_evidence_readiness(
        content,
        {"wordpress": False, "facebook": True, "instagram": True, "linkedin": True},
    )

    claims = " ".join(item["claim"] for item in readiness["claims"])
    assert "heat distress" not in claims
    assert readiness["status"] != "HIGH_RISK_UNVERIFIED"
