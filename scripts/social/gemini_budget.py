"""Persistent fail-closed Gemini request budget shared by workers and subprocesses."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any


class GeminiBudgetExceeded(RuntimeError):
    pass


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data"))


def ledger_path() -> str:
    return os.environ.get("GEMINI_USAGE_PATH", os.path.join(_data_dir(), "social", "gemini_usage.json"))


def _limits() -> tuple[int, int]:
    return (
        max(1, int(os.environ.get("GEMINI_DAILY_CALL_LIMIT", "12"))),
        max(1, int(os.environ.get("GEMINI_DAILY_IMAGE_LIMIT", "3"))),
    )


@contextmanager
def _ledger_lock(path: str):
    lock_path = f"{path}.mutex.sqlite"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # A database lock is released by the OS even after SIGKILL. Old sentinel
    # files are ignored; there is no unsafe stale-owner deletion race.
    connection = sqlite3.connect(lock_path, timeout=5)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise GeminiBudgetExceeded("Gemini budget ledger is busy; generation paused to prevent unmetered calls") from exc
        yield
    finally:
        connection.rollback()
        connection.close()


def _load(path: str, day: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
        if not isinstance(value,dict) or not isinstance(value.get("calls"),list) or not isinstance(value.get("day"),str):
            raise ValueError("invalid_usage_ledger")
        datetime.fromisoformat(value["day"])
        if value["day"] == day:
            return value
    except FileNotFoundError:
        return {"day": day, "calls": []}
    except (OSError, ValueError, TypeError) as exc:
        raise GeminiBudgetExceeded("Gemini usage ledger cannot be read; generation paused until the ledger is repaired") from exc
    return {"day": day, "calls": []}


def _snapshot(ledger: dict[str, Any], path: str) -> dict[str, Any]:
    total_limit, image_limit = _limits()
    calls = ledger["calls"]
    image_used = sum(1 for call in calls if call.get("kind") == "image")
    day = datetime.fromisoformat(str(ledger["day"])).date()
    next_reset = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=timezone.utc)
    return {
        "day": ledger["day"],
        "total": {"used": len(calls), "limit": total_limit, "remaining": max(0, total_limit - len(calls))},
        "image": {"used": image_used, "limit": image_limit, "remaining": max(0, image_limit - image_used)},
        "next_reset_at_utc": next_reset.isoformat(),
        "ledger_path": os.path.abspath(path),
        "recent_calls": list(reversed(calls[-10:])),
    }


def budget_snapshot() -> dict[str, Any]:
    path = ledger_path()
    day = datetime.now(timezone.utc).date().isoformat()
    with _ledger_lock(path):
        return _snapshot(_load(path, day), path)


def preflight_gemini_workflow(*, image_calls: int, reasoning_calls: int = 0) -> dict[str, Any]:
    """Fail before spending when the complete permitted workflow cannot fit."""
    if image_calls < 0 or reasoning_calls < 0:
        raise ValueError("Gemini workflow call counts cannot be negative")
    snapshot = budget_snapshot()
    total_required = image_calls + reasoning_calls
    if snapshot["image"]["remaining"] < image_calls:
        raise GeminiBudgetExceeded(
            f"GEMINI_IMAGE_BUDGET_EXHAUSTED: workflow requires {image_calls} image calls but only "
            f"{snapshot['image']['remaining']} remain; retry_at={snapshot['next_reset_at_utc']}"
        )
    if snapshot["total"]["remaining"] < total_required:
        raise GeminiBudgetExceeded(
            f"GEMINI_TOTAL_BUDGET_EXHAUSTED: workflow requires {total_required} calls but only "
            f"{snapshot['total']['remaining']} remain; retry_at={snapshot['next_reset_at_utc']}"
        )
    return {
        "allowed": True,
        "image_calls": image_calls,
        "reasoning_calls": reasoning_calls,
        "remaining": snapshot,
    }


def reserve_gemini_call(
    kind: str,
    model: str,
    purpose: str,
    *,
    package_id: str = "",
    campaign_id: str = "",
    asset_id: str = "",
    attempt_number: int = 1,
    initiating_subsystem: str = "unknown",
) -> dict[str, Any]:
    if kind not in {"image", "reasoning"}:
        raise ValueError(f"unsupported Gemini call kind: {kind}")
    path = ledger_path()
    now = datetime.now(timezone.utc)
    with _ledger_lock(path):
        ledger = _load(path, now.date().isoformat())
        current = _snapshot(ledger, path)
        if current["total"]["remaining"] < 1:
            raise GeminiBudgetExceeded(
                f"Gemini daily call budget exhausted ({current['total']['used']}/{current['total']['limit']}); generation paused until 00:00 UTC"
            )
        if kind == "image" and current["image"]["remaining"] < 1:
            raise GeminiBudgetExceeded(
                f"Gemini daily image budget exhausted ({current['image']['used']}/{current['image']['limit']}); image generation paused until 00:00 UTC"
            )
        call = {
            "call_id": os.urandom(16).hex(),
            "at": now.isoformat(),
            "kind": kind,
            "model": str(model),
            "purpose": str(purpose)[:160],
            "package_id": str(package_id),
            "campaign_id": str(campaign_id),
            "asset_id": str(asset_id),
            "attempt_number": max(1, int(attempt_number)),
            "initiating_subsystem": str(initiating_subsystem),
            "status": "RESERVED",
        }
        ledger["calls"].append(call)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(ledger, file, indent=2)
        os.replace(temporary, path)
        snapshot = _snapshot(ledger, path)
    from content_operations import reserve_ai_attempt
    reserve_ai_attempt(
        _data_dir(), call_id=call["call_id"], provider="gemini", model=str(model),
        operation_type=kind, outbox_id=str(package_id), campaign_id=str(campaign_id),
        asset_id=str(asset_id), attempt_number=max(1, int(attempt_number)), reason=str(purpose)[:160],
        initiating_subsystem=str(initiating_subsystem), estimated_usage={},
    )
    snapshot["call"] = dict(call)
    return snapshot


def complete_gemini_call(call_id: str, *, succeeded: bool, actual_usage: dict[str, Any] | None = None) -> None:
    path = ledger_path()
    day = datetime.now(timezone.utc).date().isoformat()
    with _ledger_lock(path):
        ledger = _load(path, day)
        matched = False
        for call in ledger["calls"]:
            if call.get("call_id") == call_id:
                call["status"] = "SUCCEEDED" if succeeded else "FAILED"
                call["completed_at"] = datetime.now(timezone.utc).isoformat()
                call["actual_usage"] = actual_usage or {}
                matched = True
                break
        if not matched:
            raise ValueError(f"gemini_call_not_reserved:{call_id}")
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(ledger, file, indent=2)
        os.replace(temporary, path)
    from content_operations import complete_ai_attempt
    complete_ai_attempt(
        _data_dir(), call_id=call_id,
        status="SUCCEEDED" if succeeded else "FAILED", actual_usage=actual_usage or {},
    )


@contextmanager
def tracked_gemini_call(kind: str, model: str, purpose: str, **metadata: Any):
    receipt = reserve_gemini_call(kind, model, purpose, **metadata)
    call_id = str(receipt["call"]["call_id"])
    try:
        yield call_id
    except Exception:
        complete_gemini_call(call_id, succeeded=False)
        raise
    else:
        complete_gemini_call(call_id, succeeded=True)
