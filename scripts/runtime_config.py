"""Typed, fail-closed runtime configuration and deployment provenance."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def _bool(name: str, default: bool) -> bool:
    value = str(os.environ.get(name, str(default))).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"CONFIG_INVALID_BOOLEAN:{name}")


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"CONFIG_INVALID_INTEGER:{name}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"CONFIG_OUT_OF_RANGE:{name}:{minimum}-{maximum}")
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    dispatch_enabled: bool
    preparation_enabled: bool
    preparation_lead_time_minutes: int
    delivery_max_attempts: int
    ai_automatic_repairs: int
    gemini_daily_call_limit: int
    gemini_daily_image_limit: int
    build_sha: str
    expected_build_sha: str
    build_timestamp: str
    deployment_id: str

    @property
    def revision_matches(self) -> bool:
        return not self.expected_build_sha or self.expected_build_sha == self.build_sha

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["revision_matches"] = self.revision_matches
        return value


def load_runtime_config() -> RuntimeConfig:
    environment = str(os.environ.get("APP_ENV") or os.environ.get("RAILWAY_ENVIRONMENT_NAME") or "development").strip()
    return RuntimeConfig(
        environment=environment,
        dispatch_enabled=_bool("CONTENT_DISPATCH_ENABLED", False),
        preparation_enabled=_bool("CONTENT_PREGENERATION_ENABLED", False),
        preparation_lead_time_minutes=_int("CONTENT_PREPARATION_LEAD_TIME_MINUTES", 1080, 15, 10080),
        delivery_max_attempts=_int("OUTBOX_MAX_ATTEMPTS", 4, 1, 10),
        ai_automatic_repairs=_int("GEMINI_IMAGE_REPAIR_ATTEMPTS", 0, 0, 5),
        gemini_daily_call_limit=_int("GEMINI_DAILY_CALL_LIMIT", 12, 1, 1000),
        gemini_daily_image_limit=_int("GEMINI_DAILY_IMAGE_LIMIT", 3, 1, 100),
        build_sha=str(os.environ.get("BUILD_GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "unknown").strip(),
        expected_build_sha=str(os.environ.get("EXPECTED_GIT_SHA") or "").strip(),
        build_timestamp=str(os.environ.get("BUILD_TIMESTAMP") or "unknown").strip(),
        deployment_id=str(os.environ.get("RAILWAY_DEPLOYMENT_ID") or "unknown").strip(),
    )


def validate_startup_config(config: RuntimeConfig | None = None) -> dict[str, Any]:
    current = config or load_runtime_config()
    blockers: list[str] = []
    warnings: list[str] = []
    if not current.revision_matches:
        blockers.append("DEPLOYMENT_REVISION_MISMATCH")
    if current.dispatch_enabled:
        platform_groups = {
            "facebook": ("META_PAGE_ID", "META_PAGE_ACCESS_TOKEN"),
            "instagram": ("META_IG_USER_ID", "META_PAGE_ACCESS_TOKEN"),
            "linkedin": ("LINKEDIN_ACCESS_TOKEN",),
        }
        configured = 0
        for variables in platform_groups.values():
            if all(str(os.environ.get(name) or "").strip() for name in variables):
                configured += 1
        if configured == 0:
            blockers.append("PLATFORM_CREDENTIALS_MISSING")
    if current.preparation_enabled and not str(os.environ.get("GEMINI_API_KEY") or "").strip():
        blockers.append("GEMINI_CONFIGURATION_MISSING")
    if current.ai_automatic_repairs + 1 > current.gemini_daily_image_limit:
        blockers.append("AI_RETRY_BUDGET_CONFLICT")
    if current.build_sha == "unknown":
        warnings.append("BUILD_REVISION_UNKNOWN")
    return {
        "status": "BLOCKED" if blockers else "DEGRADED" if warnings else "READY",
        "blockers": blockers,
        "warnings": warnings,
        "config": current.public_dict(),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
