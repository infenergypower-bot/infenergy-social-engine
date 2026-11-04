"""Authoritative publication lifecycle and readiness contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from typing import Any
from social.scene_message import SCENE_MESSAGE_VERSION, SEMANTIC_QUALITY_VERSION


class PackageState(str, Enum):
    DRAFT = "DRAFT"
    PREPARATION_REQUIRED = "PREPARATION_REQUIRED"
    PREPARING = "PREPARING"
    AWAITING_QA = "AWAITING_QA"
    QA_REJECTED = "QA_REJECTED"
    READY_TO_DISPATCH = "READY_TO_DISPATCH"
    DISPATCHING = "DISPATCHING"
    PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
    PUBLISHED = "PUBLISHED"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"
    BLOCKED_CANON = "BLOCKED_CANON"
    BLOCKED_PROVIDER_POLICY = "BLOCKED_PROVIDER_POLICY"
    BLOCKED_NO_ELIGIBLE_ASSET = "BLOCKED_NO_ELIGIBLE_ASSET"
    BLOCKED_DUPLICATE = "BLOCKED_DUPLICATE"
    BLOCKED_CONFIGURATION = "BLOCKED_CONFIGURATION"
    FAILED_PREPARATION = "FAILED_PREPARATION"
    FAILED_DELIVERY = "FAILED_DELIVERY"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CANCELLED = "CANCELLED"


class ReasonCode(str, Enum):
    READY = "READY"
    PREPARATION_COPY_MISSING = "PREPARATION_COPY_MISSING"
    PREPARATION_ASSET_MISSING = "PREPARATION_ASSET_MISSING"
    QA_COPY_FAILED = "QA_COPY_FAILED"
    QA_VISUAL_FAILED = "QA_VISUAL_FAILED"
    QA_INCOMPLETE = "QA_INCOMPLETE"
    CANON_NOT_FOUND = "CANON_NOT_FOUND"
    PROVIDER_POLICY_MISMATCH = "PROVIDER_POLICY_MISMATCH"
    ASSET_ALREADY_PUBLISHED = "ASSET_ALREADY_PUBLISHED"
    ASSET_RESERVED_BY_OTHER_PACKAGE = "ASSET_RESERVED_BY_OTHER_PACKAGE"
    PLATFORM_PAYLOAD_INVALID = "PLATFORM_PAYLOAD_INVALID"
    PLATFORM_MAPPING_MISSING = "PLATFORM_MAPPING_MISSING"
    SCHEDULE_INVALID = "SCHEDULE_INVALID"
    DISPATCH_DISABLED = "DISPATCH_DISABLED"


MANDATORY_COPY_GATES = ("schema", "clarity", "product_claims")
MANDATORY_VISUAL_GATES = (
    "TEXT_QA",
    "STORY_QA",
    "VISUAL_QA",
    "CONTINUITY_QA",
    "ORIGINALITY_QA",
    "EMOTIONAL_QA",
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PackageState.DRAFT.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.PREPARATION_REQUIRED.value: {
        PackageState.PREPARING.value,
        PackageState.READY_TO_DISPATCH.value,
        PackageState.BLOCKED_BUDGET.value,
        PackageState.BLOCKED_CANON.value,
        PackageState.BLOCKED_PROVIDER_POLICY.value,
        PackageState.BLOCKED_NO_ELIGIBLE_ASSET.value,
        PackageState.BLOCKED_DUPLICATE.value,
        PackageState.BLOCKED_CONFIGURATION.value,
        PackageState.CANCELLED.value,
    },
    PackageState.PREPARING.value: {
        PackageState.AWAITING_QA.value,
        PackageState.FAILED_PREPARATION.value,
        PackageState.BLOCKED_BUDGET.value,
        PackageState.BLOCKED_CANON.value,
    },
    PackageState.AWAITING_QA.value: {PackageState.QA_REJECTED.value, PackageState.READY_TO_DISPATCH.value},
    PackageState.QA_REJECTED.value: {PackageState.PREPARING.value, PackageState.FAILED_PREPARATION.value},
    PackageState.READY_TO_DISPATCH.value: {
        PackageState.DISPATCHING.value,
        PackageState.PREPARATION_REQUIRED.value,
        PackageState.BLOCKED_DUPLICATE.value,
        PackageState.BLOCKED_CONFIGURATION.value,
        PackageState.CANCELLED.value,
    },
    PackageState.DISPATCHING.value: {
        PackageState.READY_TO_DISPATCH.value,
        PackageState.PREPARATION_REQUIRED.value,
        PackageState.BLOCKED_CANON.value,
        PackageState.BLOCKED_PROVIDER_POLICY.value,
        PackageState.BLOCKED_DUPLICATE.value,
        PackageState.BLOCKED_CONFIGURATION.value,
        PackageState.PARTIALLY_PUBLISHED.value,
        PackageState.PUBLISHED.value,
        PackageState.FAILED_DELIVERY.value,
        PackageState.RECONCILIATION_REQUIRED.value,
    },
    PackageState.PARTIALLY_PUBLISHED.value: {
        PackageState.DISPATCHING.value,
        PackageState.PUBLISHED.value,
        PackageState.FAILED_DELIVERY.value,
        PackageState.RECONCILIATION_REQUIRED.value,
    },
    PackageState.RECONCILIATION_REQUIRED.value: {
        PackageState.DISPATCHING.value,
        PackageState.PARTIALLY_PUBLISHED.value,
        PackageState.PUBLISHED.value,
        PackageState.FAILED_DELIVERY.value,
    },
    PackageState.BLOCKED_BUDGET.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.BLOCKED_CANON.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.BLOCKED_PROVIDER_POLICY.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.BLOCKED_NO_ELIGIBLE_ASSET.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.BLOCKED_DUPLICATE.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.BLOCKED_CONFIGURATION.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.FAILED_PREPARATION.value: {PackageState.PREPARATION_REQUIRED.value, PackageState.CANCELLED.value},
    PackageState.FAILED_DELIVERY.value: {PackageState.DISPATCHING.value, PackageState.CANCELLED.value},
}


def assert_transition_allowed(source: str, destination: str) -> None:
    if source == destination:
        return
    if destination not in ALLOWED_TRANSITIONS.get(source, set()):
        raise ValueError(f"illegal_package_transition:{source}->{destination}")


@dataclass(frozen=True)
class ReadinessEvaluation:
    ready: bool
    state: str
    reason_codes: tuple[str, ...]
    evaluated_at: str
    evaluator: str = "publication_contract"
    evaluator_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        return value


def _passed_gates(value: Any, mandatory: tuple[str, ...]) -> bool:
    if not isinstance(value, dict):
        return False
    for gate in mandatory:
        result = value.get(gate)
        if isinstance(result, dict):
            result = result.get("status")
        if str(result or "").upper() != "PASS":
            return False
    return True


def asset_identity(package: dict[str, Any]) -> str:
    generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
    assets = generation.get("assets") if isinstance(generation.get("assets"), list) else []
    first = assets[0] if assets and isinstance(assets[0], dict) else {}
    return str(
        first.get("asset_id")
        or first.get("sha256")
        or first.get("local_path")
        or package.get("asset_id")
        or package.get("image_url")
        or package.get("primary_publish_image_url")
        or ""
    ).strip()


def creative_fingerprints(package: dict[str, Any], platforms: list[str] | None = None) -> set[str]:
    generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
    assets = generation.get("assets") if isinstance(generation.get("assets"), list) else []
    first = assets[0] if assets and isinstance(assets[0], dict) else {}
    values = {
        "asset": asset_identity(package),
        "source": str(first.get("source_asset_id") or package.get("source_creative_id") or "").strip(),
        "sha256": str(first.get("sha256") or package.get("asset_sha256") or "").strip().lower(),
        "perceptual": str(first.get("perceptual_hash") or package.get("perceptual_fingerprint") or "").strip().lower(),
    }
    fingerprints = {f"{kind}:{value}" for kind, value in values.items() if value}
    posts = package.get("platform_posts") if isinstance(package.get("platform_posts"), dict) else {}
    for platform in platforms or list(posts):
        post = posts.get(platform) if isinstance(posts.get(platform), dict) else {}
        copy = " ".join(str(post.get("final_caption") or post.get("final_text") or "").lower().split())
        if copy:
            fingerprints.add(f"copy:{hashlib.sha256(copy.encode('utf-8')).hexdigest()}")
    return fingerprints


def evaluate_publication_readiness(
    package: dict[str, Any],
    *,
    scheduled_at: str,
    platforms: list[str],
    dispatch_enabled: bool,
    publication_history: set[str] | None = None,
    reserved_assets: dict[str, str] | None = None,
    package_id: str = "",
) -> ReadinessEvaluation:
    """Evaluate every hard publication prerequisite without external calls."""
    reasons: list[str] = []
    posts = package.get("platform_posts") if isinstance(package.get("platform_posts"), dict) else {}
    copy_plan = package.get("gemini_copy") if isinstance(package.get("gemini_copy"), dict) else {}
    generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
    provider_policy = package.get("provider_policy") if isinstance(package.get("provider_policy"), dict) else {}

    if not platforms:
        reasons.append(ReasonCode.PLATFORM_MAPPING_MISSING.value)
    for platform in platforms:
        post = posts.get(platform) if isinstance(posts.get(platform), dict) else {}
        caption = str(post.get("final_caption") or post.get("caption") or package.get({"facebook": "fb_caption", "instagram": "ig_caption", "linkedin": "li_text"}.get(platform, "")) or "").strip()
        if not caption:
            reasons.append(ReasonCode.PREPARATION_COPY_MISSING.value)
        if not str(post.get("image_url") or package.get("image_url") or package.get("primary_publish_image_url") or (package.get("generated_visuals") or {}).get(platform) or "").strip():
            reasons.append(ReasonCode.PREPARATION_ASSET_MISSING.value)

    strict_copy = copy_plan.get("strict_provider") is True
    if strict_copy:
        if copy_plan.get("status") != "COMPLETE" or copy_plan.get("scene_message_version") != SCENE_MESSAGE_VERSION:
            reasons.append(ReasonCode.PREPARATION_COPY_MISSING.value)
        elif not _passed_gates(copy_plan.get("qa"), MANDATORY_COPY_GATES):
            reasons.append(ReasonCode.QA_COPY_FAILED.value if isinstance(copy_plan.get("qa"), dict) else ReasonCode.QA_INCOMPLETE.value)

    strict_generation = generation.get("strict_provider") is True
    if strict_generation:
        required = int(generation.get("required_image_count") or 0)
        assets = generation.get("assets") if isinstance(generation.get("assets"), list) else []
        if generation.get("status") != "COMPLETE" or required < 1 or len(assets) != required:
            reasons.append(ReasonCode.PREPARATION_ASSET_MISSING.value)
        elif strict_copy and any(
            not isinstance(asset, dict)
            or not isinstance(asset.get("generation"), dict)
            or asset["generation"].get("semantic_quality_gate_version") != SEMANTIC_QUALITY_VERSION
            for asset in assets
        ):
            reasons.append(ReasonCode.PREPARATION_ASSET_MISSING.value)
        visual_qa = generation.get("qa") or package.get("creative_qa")
        if not _passed_gates(visual_qa, MANDATORY_VISUAL_GATES):
            reasons.append(ReasonCode.QA_INCOMPLETE.value if not isinstance(visual_qa, dict) else ReasonCode.QA_VISUAL_FAILED.value)

    required_provider = str(provider_policy.get("creative_provider") or ("gemini" if strict_generation else "")).lower()
    actual_provider = str(generation.get("provider") or (package.get("generated_visuals") or {}).get("render_engine") or "").lower()
    if required_provider and actual_provider != required_provider:
        reasons.append(ReasonCode.PROVIDER_POLICY_MISMATCH.value)

    canon = package.get("canon") if isinstance(package.get("canon"), dict) else {}
    if canon.get("required") is True and not all(canon.get(key) for key in ("canon_id", "canon_version", "reference_asset_ids")):
        reasons.append(ReasonCode.CANON_NOT_FOUND.value)

    fingerprints = creative_fingerprints(package, platforms)
    if package.get("unique_creative_required") is True and fingerprints:
        if fingerprints.intersection(publication_history or set()):
            reasons.append(ReasonCode.ASSET_ALREADY_PUBLISHED.value)
        if any((reserved_assets or {}).get(identity) not in {None, package_id} for identity in fingerprints):
            reasons.append(ReasonCode.ASSET_RESERVED_BY_OTHER_PACKAGE.value)

    try:
        parsed = datetime.fromisoformat(str(scheduled_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError):
        reasons.append(ReasonCode.SCHEDULE_INVALID.value)
    if not dispatch_enabled:
        reasons.append(ReasonCode.DISPATCH_DISABLED.value)

    unique_reasons = tuple(dict.fromkeys(reasons))
    blocked_state = PackageState.PREPARATION_REQUIRED.value
    if ReasonCode.CANON_NOT_FOUND.value in unique_reasons:
        blocked_state = PackageState.BLOCKED_CANON.value
    elif ReasonCode.PROVIDER_POLICY_MISMATCH.value in unique_reasons:
        blocked_state = PackageState.BLOCKED_PROVIDER_POLICY.value
    elif ReasonCode.ASSET_ALREADY_PUBLISHED.value in unique_reasons or ReasonCode.ASSET_RESERVED_BY_OTHER_PACKAGE.value in unique_reasons:
        blocked_state = PackageState.BLOCKED_DUPLICATE.value
    elif ReasonCode.DISPATCH_DISABLED.value in unique_reasons or ReasonCode.PLATFORM_MAPPING_MISSING.value in unique_reasons:
        blocked_state = PackageState.BLOCKED_CONFIGURATION.value
    return ReadinessEvaluation(
        ready=not unique_reasons,
        state=PackageState.READY_TO_DISPATCH.value if not unique_reasons else blocked_state,
        reason_codes=unique_reasons or (ReasonCode.READY.value,),
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )
