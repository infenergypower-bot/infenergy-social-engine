"""Durable content council, daily-slot, archive, outbox, and transaction state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from inventory_db import get_db_path, init_inventory_db
from canon_registry import attach_product_canon
from publication_contract import (
    PackageState,
    asset_identity,
    assert_transition_allowed,
    creative_fingerprints,
    evaluate_publication_readiness,
)

SLOTS = ("morning", "midday", "evening")
PLATFORMS = ("facebook", "instagram", "linkedin")


def configured_platforms(package: dict[str, Any]) -> list[str]:
    routing = package.get("routing") if isinstance(package.get("routing"), dict) else {}
    configured = routing.get("platforms") if isinstance(routing.get("platforms"), list) else []
    if not configured and isinstance(package.get("platforms"), list):
        configured = package["platforms"]
    platform_policy = package.get("platform_policy") if isinstance(package.get("platform_policy"), dict) else {}
    if not configured and isinstance(platform_policy.get("platforms"), list):
        configured = platform_policy["platforms"]
    return [platform for platform in PLATFORMS if platform in configured]


def apply_growth_schedule_to_ready_inventory(data_dir: str, start_date: str | date | None = None) -> dict[str, int]:
    from posting_schedule import CENTRAL_TIME, growth_schedule

    first_date = start_date.isoformat() if isinstance(start_date, date) else str(
        start_date or datetime.now(CENTRAL_TIME).date().isoformat()
    )
    connection = _connect(data_dir)
    updated = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT outbox_id, content_date, package_json, status, last_error FROM content_outbox
            WHERE content_date >= ? AND (
                status IN ('READY', 'DUE')
                OR (
                    status='FAILED'
                    AND (last_error LIKE '%fb_caption%' OR last_error LIKE '%ig_caption%' OR last_error LIKE '%li_text%')
                )
            )
            """,
            (first_date,),
        ).fetchall()
        for row in rows:
            package = _decode(row["package_json"], {})
            platforms = configured_platforms(package)
            if not platforms:
                continue
            platform_schedule = growth_schedule(str(row["content_date"]))
            package["platform_schedule"] = {
                platform: platform_schedule[platform] for platform in platforms
            }
            first_due = min(package["platform_schedule"].values(), key=datetime.fromisoformat)
            connection.execute(
                """
                UPDATE content_outbox SET package_json=?, scheduled_at=?, status='READY',
                    attempt_count=CASE WHEN status='FAILED' THEN 0 ELSE attempt_count END,
                    next_attempt_at=NULL, last_error=NULL
                WHERE outbox_id=?
                """,
                (_json(package), first_due, row["outbox_id"]),
            )
            connection.execute(
                "UPDATE daily_slots SET scheduled_at=?, status='READY', last_error=NULL, updated_at=? WHERE outbox_id=?",
                (first_due, _now(), row["outbox_id"]),
            )
            updated += 1
        connection.commit()
        return {"updated": updated}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _connect(data_dir: str) -> sqlite3.Connection:
    init_inventory_db(data_dir)
    connection = sqlite3.connect(get_db_path(data_dir), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_content_operations(data_dir: str) -> str:
    connection = _connect(data_dir)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS council_sessions (
                decision_id TEXT PRIMARY KEY,
                content_date TEXT NOT NULL,
                slot TEXT NOT NULL,
                status TEXT NOT NULL,
                blackboard_json TEXT NOT NULL,
                rationale_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_council_date_slot
                ON council_sessions(content_date, slot);

            CREATE TABLE IF NOT EXISTS content_candidates (
                candidate_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                score REAL,
                loss_reasons_json TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES council_sessions(decision_id)
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_decision
                ON content_candidates(decision_id, ordinal);

            CREATE TABLE IF NOT EXISTS daily_slots (
                content_date TEXT NOT NULL,
                slot TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                platform_policy_json TEXT NOT NULL,
                content_id TEXT,
                decision_id TEXT,
                outbox_id TEXT,
                status TEXT NOT NULL,
                ready_at TEXT,
                claimed_at TEXT,
                published_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(content_date, slot)
            );

            CREATE TABLE IF NOT EXISTS content_outbox (
                outbox_id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                content_date TEXT NOT NULL,
                slot TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                package_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                ready_at TEXT NOT NULL,
                claimed_at TEXT,
                next_attempt_at TEXT,
                published_at TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_due
                ON content_outbox(status, scheduled_at);

            CREATE TABLE IF NOT EXISTS platform_transactions (
                outbox_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                state TEXT NOT NULL,
                request_key TEXT NOT NULL,
                request_payload_json TEXT NOT NULL,
                external_id TEXT,
                provider_response_json TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(outbox_id, platform),
                UNIQUE(request_key),
                FOREIGN KEY(outbox_id) REFERENCES content_outbox(outbox_id)
            );

            CREATE TABLE IF NOT EXISTS package_transitions (
                transition_id TEXT PRIMARY KEY,
                outbox_id TEXT NOT NULL,
                source_state TEXT NOT NULL,
                destination_state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                actor TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                attempt_id TEXT,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(outbox_id) REFERENCES content_outbox(outbox_id)
            );
            CREATE INDEX IF NOT EXISTS idx_package_transitions_outbox
                ON package_transitions(outbox_id, created_at);

            CREATE TABLE IF NOT EXISTS asset_reservations (
                asset_identity TEXT PRIMARY KEY,
                outbox_id TEXT NOT NULL,
                reservation_state TEXT NOT NULL CHECK(reservation_state IN ('RESERVED', 'CONSUMED', 'RELEASED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(outbox_id) REFERENCES content_outbox(outbox_id)
            );
            CREATE INDEX IF NOT EXISTS idx_asset_reservations_outbox
                ON asset_reservations(outbox_id, reservation_state);

            CREATE TABLE IF NOT EXISTS publication_history (
                publication_id TEXT PRIMARY KEY,
                outbox_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                asset_identity TEXT NOT NULL,
                asset_sha256 TEXT,
                perceptual_fingerprint TEXT,
                copy_fingerprint TEXT NOT NULL,
                external_id TEXT NOT NULL,
                published_at TEXT NOT NULL,
                UNIQUE(outbox_id, platform),
                UNIQUE(platform, external_id),
                FOREIGN KEY(outbox_id) REFERENCES content_outbox(outbox_id)
            );
            CREATE INDEX IF NOT EXISTS idx_publication_history_asset
                ON publication_history(asset_identity, platform);

            CREATE TABLE IF NOT EXISTS ai_generation_attempts (
                attempt_id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                outbox_id TEXT,
                campaign_id TEXT,
                asset_id TEXT,
                attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
                reason TEXT NOT NULL,
                initiating_subsystem TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('RESERVED', 'SUCCEEDED', 'FAILED')),
                estimated_usage_json TEXT NOT NULL,
                actual_usage_json TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(outbox_id) REFERENCES content_outbox(outbox_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ai_attempts_outbox
                ON ai_generation_attempts(outbox_id, created_at);
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(content_outbox)").fetchall()}
        if "next_attempt_at" not in columns:
            connection.execute("ALTER TABLE content_outbox ADD COLUMN next_attempt_at TEXT")
        additive_columns = {
            "lifecycle_state": "TEXT NOT NULL DEFAULT 'DRAFT'",
            "reason_code": "TEXT NOT NULL DEFAULT 'LEGACY_STATE_IMPORTED'",
            "readiness_json": "TEXT NOT NULL DEFAULT '{}'",
            "correlation_id": "TEXT",
            "active_attempt_id": "TEXT",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "package_version": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in additive_columns.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE content_outbox ADD COLUMN {name} {definition}")
        valid_lifecycle_states = ",".join(f"'{state.value}'" for state in PackageState)
        connection.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS validate_outbox_lifecycle_insert
            BEFORE INSERT ON content_outbox
            WHEN NEW.lifecycle_state NOT IN ({valid_lifecycle_states})
            BEGIN SELECT RAISE(ABORT, 'invalid_lifecycle_state'); END;
            CREATE TRIGGER IF NOT EXISTS validate_outbox_lifecycle_update
            BEFORE UPDATE OF lifecycle_state ON content_outbox
            WHEN NEW.lifecycle_state NOT IN ({valid_lifecycle_states})
            BEGIN SELECT RAISE(ABORT, 'invalid_lifecycle_state'); END;
            CREATE TRIGGER IF NOT EXISTS validate_platform_transaction_insert
            BEFORE INSERT ON platform_transactions
            WHEN NEW.state NOT IN ('REQUEST_SENT','CONFIRMED_SUCCESS','CONFIRMED_FAILURE','AMBIGUOUS','AUTH_ACTION_REQUIRED')
            BEGIN SELECT RAISE(ABORT, 'invalid_platform_transaction_state'); END;
            CREATE TRIGGER IF NOT EXISTS validate_platform_transaction_update
            BEFORE UPDATE OF state ON platform_transactions
            WHEN NEW.state NOT IN ('REQUEST_SENT','CONFIRMED_SUCCESS','CONFIRMED_FAILURE','AMBIGUOUS','AUTH_ACTION_REQUIRED')
            BEGIN SELECT RAISE(ABORT, 'invalid_platform_transaction_state'); END;
            """
        )
        connection.execute(
            "UPDATE content_outbox SET correlation_id=COALESCE(correlation_id, outbox_id) WHERE correlation_id IS NULL"
        )
        legacy_rows = connection.execute(
            """
            SELECT outbox_id FROM content_outbox
            WHERE status='READY' AND lifecycle_state='DRAFT'
                AND reason_code='LEGACY_STATE_IMPORTED'
            """
        ).fetchall()
        for row in legacy_rows:
            _record_transition(
                connection,
                outbox_id=str(row["outbox_id"]),
                destination=PackageState.PREPARATION_REQUIRED.value,
                reason_code="LEGACY_READY_REQUIRES_PREPARATION",
                actor="content_operations.init_content_operations",
            )
        connection.commit()
        return get_db_path(data_dir)
    finally:
        connection.close()


def _record_transition(
    connection: sqlite3.Connection,
    *,
    outbox_id: str,
    destination: str,
    reason_code: str,
    actor: str,
    attempt_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    row = connection.execute(
        "SELECT lifecycle_state, correlation_id FROM content_outbox WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"outbox_not_found:{outbox_id}")
    source = str(row["lifecycle_state"] or PackageState.DRAFT.value)
    assert_transition_allowed(source, destination)
    now = _now()
    connection.execute(
        """
        UPDATE content_outbox SET lifecycle_state=?, reason_code=?, active_attempt_id=?,
            package_version=package_version+1 WHERE outbox_id=?
        """,
        (destination, reason_code, attempt_id, outbox_id),
    )
    connection.execute(
        """
        INSERT INTO package_transitions
        (transition_id, outbox_id, source_state, destination_state, reason_code,
         actor, correlation_id, attempt_id, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            outbox_id,
            source,
            destination,
            reason_code,
            actor,
            str(row["correlation_id"] or outbox_id),
            attempt_id,
            _json(detail or {}),
            now,
        ),
    )


def transition_package(
    data_dir: str,
    outbox_id: str,
    destination: str,
    reason_code: str,
    *,
    actor: str,
    attempt_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _record_transition(
            connection,
            outbox_id=outbox_id,
            destination=destination,
            reason_code=reason_code,
            actor=actor,
            attempt_id=attempt_id,
            detail=detail,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _publication_identities(connection: sqlite3.Connection, exclude_outbox_id: str = "") -> set[str]:
    identities: set[str] = set()
    for row in connection.execute(
        "SELECT asset_identity, asset_sha256, perceptual_fingerprint, copy_fingerprint FROM publication_history WHERE outbox_id<>?",
        (exclude_outbox_id,),
    ):
        for kind, value in zip(("asset", "sha256", "perceptual", "copy"), row):
            if value:
                identities.add(f"{kind}:{value}")
    return identities


def _active_reservations(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT asset_identity, outbox_id FROM asset_reservations WHERE reservation_state IN ('RESERVED', 'CONSUMED')"
        )
    }


def _reserve_asset(connection: sqlite3.Connection, outbox_id: str, package: dict[str, Any]) -> None:
    if package.get("unique_creative_required") is not True:
        return
    identities = creative_fingerprints(package, configured_platforms(package))
    if not identities:
        raise ValueError("unique_asset_identity_missing")
    now = _now()
    for identity in identities:
        existing = connection.execute(
            "SELECT outbox_id, reservation_state FROM asset_reservations WHERE asset_identity=?",
            (identity,),
        ).fetchone()
        if existing and str(existing["outbox_id"]) != outbox_id and str(existing["reservation_state"]) != "RELEASED":
            raise ValueError("asset_reserved_by_other_package")
        connection.execute(
            """
            INSERT INTO asset_reservations(asset_identity, outbox_id, reservation_state, created_at, updated_at)
            VALUES (?, ?, 'RESERVED', ?, ?)
            ON CONFLICT(asset_identity) DO UPDATE SET
                outbox_id=excluded.outbox_id, reservation_state='RESERVED', updated_at=excluded.updated_at
            """,
            (identity, outbox_id, now, now),
        )


def evaluate_outbox_readiness(
    data_dir: str,
    outbox_id: str,
    *,
    dispatch_enabled: bool | None = None,
    actor: str = "readiness_evaluator",
    persist: bool = True,
) -> dict[str, Any]:
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT package_json, scheduled_at, lifecycle_state FROM content_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"outbox_not_found:{outbox_id}")
        package = _decode(row["package_json"], {})
        evaluation = evaluate_publication_readiness(
            package,
            scheduled_at=str(row["scheduled_at"]),
            platforms=configured_platforms(package),
            dispatch_enabled=(str(os.environ.get("CONTENT_DISPATCH_ENABLED", "false")).lower() in {"1", "true", "yes", "on"}) if dispatch_enabled is None else dispatch_enabled,
            publication_history=_publication_identities(connection, outbox_id),
            reserved_assets=_active_reservations(connection),
            package_id=outbox_id,
        )
        result = evaluation.to_dict()
        if persist:
            connection.execute(
                "UPDATE content_outbox SET readiness_json=?, reason_code=? WHERE outbox_id=?",
                (_json(result), result["reason_codes"][0], outbox_id),
            )
            if evaluation.ready:
                _reserve_asset(connection, outbox_id, package)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_publication_history(
    data_dir: str,
    *,
    outbox_id: str,
    platform: str,
    external_id: str,
) -> None:
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT package_json FROM content_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        if not row:
            raise ValueError(f"outbox_not_found:{outbox_id}")
        package = _decode(row["package_json"], {})
        identity = asset_identity(package)
        caption = str(((package.get("platform_posts") or {}).get(platform) or {}).get("final_caption") or "")
        generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
        assets = generation.get("assets") if isinstance(generation.get("assets"), list) else []
        first = assets[0] if assets and isinstance(assets[0], dict) else {}
        asset_sha256 = str(first.get("sha256") or package.get("asset_sha256") or "").strip().lower()
        perceptual = str(first.get("perceptual_hash") or package.get("perceptual_fingerprint") or "").strip().lower()
        copy_fingerprint = hashlib.sha256(" ".join(caption.lower().split()).encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT OR IGNORE INTO publication_history
            (publication_id, outbox_id, platform, asset_identity, asset_sha256, perceptual_fingerprint, copy_fingerprint, external_id, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, outbox_id, platform, identity, asset_sha256 or None, perceptual or None, copy_fingerprint, external_id, _now()),
        )
        for fingerprint in creative_fingerprints(package, configured_platforms(package)):
            connection.execute(
                "UPDATE asset_reservations SET reservation_state='CONSUMED', updated_at=? WHERE asset_identity=? AND outbox_id=?",
                (_now(), fingerprint, outbox_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def why_not_published(data_dir: str, outbox_id: str) -> dict[str, Any]:
    connection = _connect(data_dir)
    try:
        package = connection.execute("SELECT * FROM content_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        if not package:
            return {"outbox_id": outbox_id, "found": False, "reason_codes": ["PACKAGE_NOT_FOUND"]}
        transitions = [dict(row) for row in connection.execute(
            "SELECT source_state, destination_state, reason_code, actor, correlation_id, attempt_id, detail_json, created_at FROM package_transitions WHERE outbox_id=? ORDER BY created_at",
            (outbox_id,),
        )]
        for transition in transitions:
            transition["detail"] = _decode(transition.pop("detail_json"), {})
        deliveries = [dict(row) for row in connection.execute(
            "SELECT platform, state, external_id, attempt_count, last_error, updated_at FROM platform_transactions WHERE outbox_id=? ORDER BY platform",
            (outbox_id,),
        )]
        row = dict(package)
        readiness = _decode(row.pop("readiness_json"), {})
        row.pop("package_json", None)
        return {
            "outbox_id": outbox_id,
            "found": True,
            "current_state": row.get("lifecycle_state"),
            "legacy_status": row.get("status"),
            "scheduled_at": row.get("scheduled_at"),
            "reason_codes": readiness.get("reason_codes") or [row.get("reason_code")],
            "readiness": readiness,
            "transitions": transitions,
            "platform_deliveries": deliveries,
            "platform_api_calls": sum(int(item.get("attempt_count") or 0) for item in deliveries),
        }
    finally:
        connection.close()


def publishing_metrics(data_dir: str, *, now_utc: str | None = None) -> dict[str, Any]:
    now = now_utc or _now()
    connection = _connect(data_dir)
    try:
        states = {str(row[0]): int(row[1]) for row in connection.execute(
            "SELECT lifecycle_state, COUNT(*) FROM content_outbox GROUP BY lifecycle_state"
        )}
        overdue = int(connection.execute(
            "SELECT COUNT(*) FROM content_outbox WHERE datetime(scheduled_at)<datetime(?) AND lifecycle_state NOT IN ('PUBLISHED','CANCELLED')",
            (now,),
        ).fetchone()[0])
        deliveries = {str(row[0]): int(row[1]) for row in connection.execute(
            "SELECT state, COUNT(*) FROM platform_transactions GROUP BY state"
        )}
        stuck = int(connection.execute(
            """SELECT COUNT(*) FROM content_outbox
               WHERE lifecycle_state IN ('PREPARING','AWAITING_QA','DISPATCHING')
                 AND datetime(COALESCE(claimed_at, ready_at, created_at)) < datetime(?, '-30 minutes')""",
            (now,),
        ).fetchone()[0])
        legacy_drafts = int(connection.execute(
            "SELECT COUNT(*) FROM content_outbox WHERE status='READY' AND lifecycle_state='DRAFT'"
        ).fetchone()[0])
        last_success = connection.execute(
            "SELECT MAX(updated_at) FROM platform_transactions WHERE state='CONFIRMED_SUCCESS'"
        ).fetchone()[0]
        hours_since_success = None
        if last_success:
            try:
                current = datetime.fromisoformat(now.replace("Z", "+00:00"))
                confirmed = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                hours_since_success = max(0.0, (current - confirmed).total_seconds() / 3600)
            except ValueError:
                hours_since_success = None
        issues = []
        if legacy_drafts:
            issues.append("LEGACY_READY_STUCK_IN_DRAFT")
        if stuck:
            issues.append("STUCK_TRANSIENT_PACKAGE")
        if overdue and (hours_since_success is None or hours_since_success >= 36):
            issues.append("NO_CONFIRMED_PUBLICATION_36H")
        return {
            "automation_status": "DEGRADED" if issues else "HEALTHY",
            "automation_issues": issues,
            "package_states": states,
            "past_scheduled_time": overdue,
            "stuck_transient_states": stuck,
            "legacy_ready_drafts": legacy_drafts,
            "last_confirmed_success_at": last_success,
            "hours_since_confirmed_success": round(hours_since_success, 1) if hours_since_success is not None else None,
            "platform_transactions": deliveries,
        }
    finally:
        connection.close()


def reserve_ai_attempt(
    data_dir: str,
    *,
    call_id: str,
    provider: str,
    model: str,
    operation_type: str,
    outbox_id: str = "",
    campaign_id: str = "",
    asset_id: str = "",
    attempt_number: int = 1,
    reason: str = "",
    initiating_subsystem: str = "unknown",
    estimated_usage: dict[str, Any] | None = None,
) -> str:
    init_content_operations(data_dir)
    attempt_id = uuid.uuid4().hex
    connection = _connect(data_dir)
    try:
        valid_outbox = None
        if outbox_id:
            valid_outbox = connection.execute(
                "SELECT outbox_id FROM content_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
        connection.execute(
            """
            INSERT INTO ai_generation_attempts
            (attempt_id, call_id, provider, model, operation_type, outbox_id, campaign_id,
             asset_id, attempt_number, reason, initiating_subsystem, status,
             estimated_usage_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)
            """,
            (
                attempt_id, call_id, provider, model, operation_type,
                outbox_id if valid_outbox else None, campaign_id or None, asset_id or None,
                max(1, int(attempt_number)), reason, initiating_subsystem,
                _json(estimated_usage or {}), _now(),
            ),
        )
        connection.commit()
        return attempt_id
    finally:
        connection.close()


def complete_ai_attempt(
    data_dir: str,
    *,
    call_id: str,
    status: str,
    actual_usage: dict[str, Any] | None = None,
) -> None:
    if status not in {"SUCCEEDED", "FAILED"}:
        raise ValueError(f"invalid_ai_attempt_status:{status}")
    connection = _connect(data_dir)
    try:
        changed = connection.execute(
            """UPDATE ai_generation_attempts SET status=?, actual_usage_json=?, completed_at=?
               WHERE call_id=? AND status='RESERVED'""",
            (status, _json(actual_usage or {}), _now(), call_id),
        ).rowcount
        if changed != 1:
            raise ValueError(f"ai_attempt_not_reserved:{call_id}")
        connection.commit()
    finally:
        connection.close()


def find_eligible_creative(data_dir: str, requirements: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    """Return existing ready packages that satisfy requirements; never generate content."""
    platform = str(requirements.get("platform") or "").strip().lower()
    provider = str(requirements.get("provider") or "").strip().lower()
    product_id = str(requirements.get("product_id") or "").strip()
    campaign_id = str(requirements.get("campaign_id") or "").strip()
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            """SELECT outbox_id, package_json, scheduled_at, lifecycle_state, created_at
               FROM content_outbox
               WHERE lifecycle_state IN ('READY_TO_DISPATCH','PREPARATION_REQUIRED','AWAITING_QA')
               ORDER BY datetime(scheduled_at), created_at LIMIT 250"""
        ).fetchall()
    finally:
        connection.close()
    eligible: list[dict[str, Any]] = []
    for row in rows:
        package = _decode(row["package_json"], {})
        platforms = configured_platforms(package)
        generation = package.get("gemini_generation") if isinstance(package.get("gemini_generation"), dict) else {}
        if platform and platform not in platforms:
            continue
        if provider and str(generation.get("provider") or "").lower() != provider:
            continue
        if product_id and str(package.get("product_id") or "") != product_id:
            continue
        if campaign_id and str(package.get("campaign_id") or "") != campaign_id:
            continue
        readiness = evaluate_outbox_readiness(data_dir, str(row["outbox_id"]), dispatch_enabled=True, persist=False)
        if readiness["ready"]:
            eligible.append({
                "outbox_id": str(row["outbox_id"]),
                "scheduled_at": str(row["scheduled_at"]),
                "asset_identity": asset_identity(package),
                "platforms": platforms,
                "package": package,
            })
        if len(eligible) >= max(1, min(100, int(limit))):
            break
    return eligible


def classify_backlog(data_dir: str, *, now_utc: str | None = None) -> dict[str, list[dict[str, Any]]]:
    now = datetime.fromisoformat((now_utc or _now()).replace("Z", "+00:00"))
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    buckets = {
        "ready_relevant": [], "preparation_required": [], "blocked_budget": [],
        "blocked_canon": [], "blocked_inventory": [], "expired": [],
        "published": [], "operator_review": [],
    }
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            "SELECT outbox_id, content_date, scheduled_at, lifecycle_state, reason_code FROM content_outbox ORDER BY datetime(scheduled_at)"
        ).fetchall()
    finally:
        connection.close()
    for raw in rows:
        row = dict(raw)
        state = str(row.get("lifecycle_state") or "")
        scheduled = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
        scheduled = scheduled.replace(tzinfo=timezone.utc) if scheduled.tzinfo is None else scheduled.astimezone(timezone.utc)
        if state == PackageState.PUBLISHED.value:
            bucket = "published"
        elif state == PackageState.BLOCKED_BUDGET.value:
            bucket = "blocked_budget"
        elif state == PackageState.BLOCKED_CANON.value:
            bucket = "blocked_canon"
        elif state in {PackageState.BLOCKED_NO_ELIGIBLE_ASSET.value, PackageState.BLOCKED_DUPLICATE.value}:
            bucket = "blocked_inventory"
        elif state in {PackageState.PREPARATION_REQUIRED.value, PackageState.PREPARING.value, PackageState.AWAITING_QA.value, PackageState.QA_REJECTED.value}:
            bucket = "preparation_required"
        elif state == PackageState.READY_TO_DISPATCH.value and scheduled >= now - timedelta(days=2):
            bucket = "ready_relevant"
        elif state not in {PackageState.CANCELLED.value} and scheduled < now - timedelta(days=2):
            bucket = "expired"
        else:
            bucket = "operator_review"
        buckets[bucket].append(row)
    return buckets


def today_schedule(data_dir: str, content_date: str | None = None) -> dict[str, Any]:
    day = str(content_date or datetime.now(timezone.utc).date().isoformat())
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            """SELECT o.outbox_id, o.content_id, o.slot, o.scheduled_at, o.status,
                      o.lifecycle_state, o.reason_code, o.readiness_json, o.package_json
               FROM content_outbox o WHERE o.content_date=? ORDER BY datetime(o.scheduled_at)""",
            (day,),
        ).fetchall()
        items = []
        for raw in rows:
            row = dict(raw)
            package = _decode(row.pop("package_json"), {})
            row["readiness"] = _decode(row.pop("readiness_json"), {})
            row["platforms"] = configured_platforms(package)
            row["asset_identity"] = asset_identity(package)
            items.append(row)
        return {"date": day, "count": len(items), "packages": items}
    finally:
        connection.close()


def ensure_daily_slots(
    data_dir: str,
    content_date: str | date,
    schedule: dict[str, str],
    platform_policy: dict[str, Any],
) -> list[dict[str, Any]]:
    init_content_operations(data_dir)
    day = content_date.isoformat() if isinstance(content_date, date) else str(content_date)
    now = _now()
    connection = _connect(data_dir)
    try:
        for slot in SLOTS:
            scheduled_at = str(schedule[slot])
            connection.execute(
                """
                INSERT INTO daily_slots (
                    content_date, slot, scheduled_at, platform_policy_json,
                    status, updated_at
                ) VALUES (?, ?, ?, ?, 'UNPLANNED', ?)
                ON CONFLICT(content_date, slot) DO UPDATE SET
                    scheduled_at=excluded.scheduled_at,
                    platform_policy_json=excluded.platform_policy_json,
                    updated_at=excluded.updated_at
                """,
                (day, slot, scheduled_at, _json(platform_policy), now),
            )
        connection.commit()
        return daily_status(data_dir, day)["slots"]
    finally:
        connection.close()


def replace_unpublished_slot(data_dir: str, content_date: str, slot: str) -> bool:
    """Cancel an unpublished package so a planned replacement is the only due item."""
    if slot not in SLOTS:
        raise ValueError(f"unsupported slot: {slot}")
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT outbox_id, status FROM daily_slots WHERE content_date=? AND slot=?",
            (content_date, slot),
        ).fetchone()
        if not row or not row["outbox_id"]:
            connection.commit()
            return True
        if str(row["status"] or "") in {"CLAIMED", "PUBLISHING", "PUBLISHED"}:
            connection.rollback()
            return False
        now = _now()
        connection.execute(
            "UPDATE content_outbox SET status='CANCELLED', last_error='replaced_by_monthly_company_truth_calendar' WHERE outbox_id=? AND status NOT IN ('CLAIMED', 'PUBLISHING', 'PUBLISHED')",
            (row["outbox_id"],),
        )
        connection.execute(
            """
            UPDATE daily_slots SET content_id=NULL, decision_id=NULL, outbox_id=NULL,
                status='UNPLANNED', ready_at=NULL, claimed_at=NULL, published_at=NULL,
                last_error=NULL, updated_at=? WHERE content_date=? AND slot=?
            """,
            (now, content_date, slot),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cancel_unpublished_inventory(data_dir: str, reason: str = "replaced_by_monthly_content_plan") -> dict[str, int]:
    """Cancel queued work that has not reached a publisher transaction."""
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT o.outbox_id FROM content_outbox o
            WHERE o.status IN ('READY', 'DUE', 'RECOVERING', 'EXTERNAL_ACTION_REQUIRED')
              AND NOT EXISTS (
                  SELECT 1 FROM platform_transactions t
                  WHERE t.outbox_id=o.outbox_id AND t.state IN ('REQUEST_SENT', 'CONFIRMED_SUCCESS', 'AMBIGUOUS')
              )
            """
        ).fetchall()
        outbox_ids = [str(row["outbox_id"]) for row in rows]
        now = _now()
        for outbox_id in outbox_ids:
            connection.execute(
                "UPDATE content_outbox SET status='CANCELLED', last_error=? WHERE outbox_id=?",
                (reason, outbox_id),
            )
            connection.execute(
                """
                UPDATE daily_slots SET content_id=NULL, decision_id=NULL, outbox_id=NULL,
                    status='UNPLANNED', ready_at=NULL, claimed_at=NULL, published_at=NULL,
                    last_error=NULL, updated_at=? WHERE outbox_id=?
                """,
                (now, outbox_id),
            )
        connection.commit()
        return {"cancelled_outbox": len(outbox_ids)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_council_session(
    data_dir: str,
    *,
    content_date: str,
    slot: str,
    blackboard: dict[str, Any],
    rationale: list[str] | None = None,
    decision_id: str | None = None,
) -> str:
    identifier = decision_id or uuid.uuid4().hex
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute(
            """
            INSERT INTO council_sessions VALUES (?, ?, ?, 'COUNCIL_ACTIVE', ?, ?, ?, ?)
            """,
            (identifier, content_date, slot, _json(blackboard), _json(rationale or []), now, now),
        )
        connection.execute(
            """
            UPDATE daily_slots SET decision_id=?, status='COUNCIL_ACTIVE', updated_at=?
            WHERE content_date=? AND slot=?
            """,
            (identifier, now, content_date, slot),
        )
        connection.commit()
        return identifier
    finally:
        connection.close()


def archive_candidate(
    data_dir: str,
    *,
    decision_id: str,
    ordinal: int,
    content: dict[str, Any],
    status: str,
    score: float | None = None,
    loss_reasons: list[str] | None = None,
) -> str:
    candidate_id = str(content.get("candidate_id") or content.get("post_id") or uuid.uuid4().hex)
    connection = _connect(data_dir)
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO content_candidates
            (candidate_id, decision_id, ordinal, status, score, loss_reasons_json, content_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (candidate_id, decision_id, ordinal, status, score, _json(loss_reasons or []), _json(content), _now()),
        )
        connection.commit()
        return candidate_id
    finally:
        connection.close()


def mark_ready(
    data_dir: str,
    *,
    content_date: str,
    slot: str,
    scheduled_at: str,
    decision_id: str,
    package: dict[str, Any],
) -> str:
    package = attach_product_canon(data_dir, package)
    outbox_id = uuid.uuid4().hex
    content_id = str(package.get("content_id") or package.get("post_id") or uuid.uuid4().hex)
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        session = connection.execute(
            "SELECT blackboard_json FROM council_sessions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        blackboard = _decode(session["blackboard_json"], {}) if session else {}
        platform_posts = package.get("platform_posts") if isinstance(package.get("platform_posts"), dict) else {}
        blackboard.update({
            "research": package.get("research") or package.get("research_summary") or {},
            "content_readiness": package.get("evidence_readiness") or package.get("claim_ledger") or {},
            "master_copy": package.get("master_copy") or package.get("copy") or package.get("wp_content") or "",
            "final_copy": {
                "facebook": ((platform_posts.get("facebook") or {}).get("final_caption") or package.get("fb_caption") or ""),
                "instagram": ((platform_posts.get("instagram") or {}).get("final_caption") or package.get("ig_caption") or ""),
                "linkedin": ((platform_posts.get("linkedin") or {}).get("final_caption") or package.get("li_text") or ""),
            },
            "engagement": package.get("engagement") or {"cta": package.get("selected_cta") or ""},
            "creative_routes": package.get("creative_routes") or package.get("visual_plan", {}).get("creative_routes") or [],
            "art_direction": package.get("visual_plan") or {},
            "visual_assets": package.get("generated_visuals") or {},
            "selected_visual": package.get("primary_publish_image_url") or package.get("generated_visuals") or {},
            "platform_presentations": platform_posts,
            "destination": package.get("destination_url") or "",
            "schedule": {"date": content_date, "slot": slot, "scheduled_at": scheduled_at},
            "archive_state": "ARCHIVED",
            "publication_state": "READY",
        })
        connection.execute(
            """
            INSERT INTO content_outbox
            (outbox_id, content_id, decision_id, content_date, slot, scheduled_at,
             package_json, status, created_at, ready_at, lifecycle_state, reason_code,
             readiness_json, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?, 'DRAFT', 'PACKAGE_CREATED', '{}', ?)
            """,
            (outbox_id, content_id, decision_id, content_date, slot, scheduled_at, _json(package), now, now, outbox_id),
        )
        _record_transition(
            connection,
            outbox_id=outbox_id,
            destination=PackageState.PREPARATION_REQUIRED.value,
            reason_code="PACKAGE_ENQUEUED",
            actor="content_operations.mark_ready",
        )
        connection.execute(
            """
            UPDATE daily_slots SET content_id=?, decision_id=?, outbox_id=?, status='READY',
                ready_at=?, updated_at=?, last_error=NULL
            WHERE content_date=? AND slot=?
            """,
            (content_id, decision_id, outbox_id, now, now, content_date, slot),
        )
        connection.execute(
            "UPDATE council_sessions SET status='READY', blackboard_json=?, updated_at=? WHERE decision_id=?",
            (_json(blackboard), now, decision_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    readiness = evaluate_outbox_readiness(data_dir, outbox_id, dispatch_enabled=True)
    if readiness["ready"]:
        transition_package(
            data_dir,
            outbox_id,
            PackageState.READY_TO_DISPATCH.value,
            "READINESS_PASSED",
            actor="content_operations.mark_ready",
            detail=readiness,
        )
    return outbox_id


def enqueue_durable_package(
    data_dir: str,
    *,
    source_id: str,
    package: dict[str, Any],
    scheduled_at: str | None = None,
    actor: str = "content_operations.enqueue_durable_package",
) -> str:
    """Atomically enqueue an idempotent package that is not owned by a daily slot."""
    init_content_operations(data_dir)
    package = attach_product_canon(data_dir, package)
    normalized_source_id = str(source_id).strip()
    if not normalized_source_id:
        raise ValueError("source_id_required")
    content_id = f"external:{normalized_source_id}"
    due_at = scheduled_at or _now()
    content_date = due_at[:10]
    slot = f"external-{hashlib.sha256(normalized_source_id.encode('utf-8')).hexdigest()[:16]}"
    outbox_id = uuid.uuid4().hex
    decision_id = uuid.uuid4().hex
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT outbox_id FROM content_outbox WHERE content_id=? ORDER BY created_at DESC LIMIT 1",
            (content_id,),
        ).fetchone()
        if existing:
            connection.commit()
            return str(existing["outbox_id"])
        connection.execute(
            "INSERT INTO council_sessions VALUES (?, ?, ?, 'READY', ?, '[]', ?, ?)",
            (decision_id, content_date, slot, _json({"source_id": normalized_source_id, "actor": actor}), now, now),
        )
        connection.execute(
            """
            INSERT INTO content_outbox
            (outbox_id, content_id, decision_id, content_date, slot, scheduled_at,
             package_json, status, created_at, ready_at, lifecycle_state, reason_code,
             readiness_json, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?, 'DRAFT', 'PACKAGE_CREATED', '{}', ?)
            """,
            (outbox_id, content_id, decision_id, content_date, slot, due_at, _json(package), now, now, outbox_id),
        )
        _record_transition(
            connection,
            outbox_id=outbox_id,
            destination=PackageState.PREPARATION_REQUIRED.value,
            reason_code="PACKAGE_ENQUEUED",
            actor=actor,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    readiness = evaluate_outbox_readiness(data_dir, outbox_id, dispatch_enabled=True, actor=f"{actor}.readiness")
    if readiness["ready"]:
        transition_package(
            data_dir,
            outbox_id,
            PackageState.READY_TO_DISPATCH.value,
            "READINESS_PASSED",
            actor=actor,
            detail=readiness,
        )
    return outbox_id


def claim_due(
    data_dir: str,
    now_utc: str | None = None,
    *,
    outbox_id: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    now = now_utc or _now()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        target_clause = "AND outbox_id=?" if outbox_id else ""
        due_clause = "" if force else "AND datetime(scheduled_at) <= datetime(?)"
        owner_retry = bool(force and outbox_id)
        status_clause = (
            "status IN ('READY', 'DUE', 'EXTERNAL_ACTION_REQUIRED')"
            if owner_retry
            else """(
                status IN ('READY', 'DUE')
                OR (status='EXTERNAL_ACTION_REQUIRED' AND last_error='no_routed_platforms')
                OR (status='RECOVERING' AND last_error='ready_package_has_no_routed_platforms')
            )"""
        )
        lifecycle_clause = (
            "lifecycle_state IN ('READY_TO_DISPATCH', 'PARTIALLY_PUBLISHED', 'FAILED_DELIVERY', 'RECONCILIATION_REQUIRED')"
            if owner_retry
            else "lifecycle_state IN ('READY_TO_DISPATCH', 'PARTIALLY_PUBLISHED', 'FAILED_DELIVERY')"
        )
        parameters: list[Any] = []
        if not force:
            parameters.append(now)
        parameters.append(now)
        if outbox_id:
            parameters.append(outbox_id)
        row = connection.execute(
            f"""
            SELECT * FROM content_outbox
                        WHERE ({status_clause}) {due_clause}
                            AND {lifecycle_clause}
                            AND (next_attempt_at IS NULL OR datetime(next_attempt_at) <= datetime(?))
                            {target_clause}
            ORDER BY datetime(scheduled_at), created_at LIMIT 1
            """,
            parameters,
        ).fetchone()
        if not row:
            connection.commit()
            return None
        claimed_at = now
        attempt_id = uuid.uuid4().hex
        changed = connection.execute(
            f"""
            UPDATE content_outbox SET status='CLAIMED', claimed_at=?, attempt_count=attempt_count+1,
                lease_owner=?, lease_expires_at=?
            WHERE outbox_id=? AND ({status_clause})
            """,
            (
                claimed_at,
                f"pid:{os.getpid()}",
                (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(minutes=15)).isoformat(),
                row["outbox_id"],
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return None
        _record_transition(
            connection,
            outbox_id=str(row["outbox_id"]),
            destination=PackageState.DISPATCHING.value,
            reason_code="DISPATCH_LEASE_ACQUIRED",
            actor="content_operations.claim_due",
            attempt_id=attempt_id,
        )
        connection.execute(
            """
            UPDATE daily_slots SET status='CLAIMED', claimed_at=?, updated_at=?
            WHERE outbox_id=?
            """,
            (claimed_at, claimed_at, row["outbox_id"]),
        )
        connection.commit()
        result = dict(row)
        result["status"] = "CLAIMED"
        result["claimed_at"] = claimed_at
        result["attempt_count"] = int(result.get("attempt_count") or 0) + 1
        result["active_attempt_id"] = attempt_id
        result["package"] = _decode(result.pop("package_json"), {})
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_claimed_package(data_dir: str, outbox_id: str, package: dict[str, Any]) -> None:
    connection = _connect(data_dir)
    try:
        changed = connection.execute(
            "UPDATE content_outbox SET package_json=? WHERE outbox_id=? AND status='CLAIMED'",
            (_json(package), outbox_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("claimed_outbox_package_update_failed")
        connection.commit()
    finally:
        connection.close()


def upcoming_ready_packages(
    data_dir: str,
    *,
    before_utc: str,
    after_utc: str | None = None,
    limit: int = 1,
) -> list[dict[str, Any]]:
    connection = _connect(data_dir)
    try:
        after_clause = "AND datetime(scheduled_at) >= datetime(?)" if after_utc else ""
        parameters: list[Any] = [before_utc]
        if after_utc:
            parameters.append(after_utc)
        parameters.append(max(1, limit))
        rows = connection.execute(
            f"""
            SELECT outbox_id, scheduled_at, lifecycle_state, package_json FROM content_outbox
            WHERE status='READY' AND datetime(scheduled_at) <= datetime(?)
                {after_clause}
                AND lifecycle_state IN ('PREPARATION_REQUIRED', 'QA_REJECTED', 'READY_TO_DISPATCH', 'FAILED_PREPARATION', 'BLOCKED_BUDGET')
            ORDER BY datetime(scheduled_at), created_at LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [
            {
                "outbox_id": str(row["outbox_id"]),
                "scheduled_at": str(row["scheduled_at"]),
                "lifecycle_state": str(row["lifecycle_state"]),
                "package": _decode(row["package_json"], {}),
            }
            for row in rows
        ]
    finally:
        connection.close()


def update_ready_package(data_dir: str, outbox_id: str, package: dict[str, Any]) -> bool:
    connection = _connect(data_dir)
    try:
        changed = connection.execute(
            """UPDATE content_outbox SET package_json=?, next_attempt_at=NULL, last_error=NULL, attempt_count=0
                WHERE outbox_id=? AND status='READY'""",
            (_json(package), outbox_id),
        ).rowcount
        connection.commit()
        return changed == 1
    finally:
        connection.close()


def begin_platform_transaction(
    data_dir: str,
    *,
    outbox_id: str,
    platform: str,
    payload: dict[str, Any],
) -> str:
    if platform not in PLATFORMS:
        raise ValueError(f"unsupported platform: {platform}")
    request_key = f"{outbox_id}:{platform}"
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute(
            """
            INSERT INTO platform_transactions
            (outbox_id, platform, state, request_key, request_payload_json,
             attempt_count, created_at, updated_at)
            VALUES (?, ?, 'REQUEST_SENT', ?, ?, 1, ?, ?)
            ON CONFLICT(outbox_id, platform) DO UPDATE SET
                state=CASE
                    WHEN platform_transactions.state='CONFIRMED_SUCCESS' THEN platform_transactions.state
                    WHEN platform_transactions.state='AMBIGUOUS' THEN platform_transactions.state
                    ELSE 'REQUEST_SENT'
                END,
                request_payload_json=excluded.request_payload_json,
                attempt_count=platform_transactions.attempt_count+1,
                updated_at=excluded.updated_at
            """,
            (outbox_id, platform, request_key, _json(payload), now, now),
        )
        connection.commit()
        return request_key
    finally:
        connection.close()


def platform_transaction(data_dir: str, outbox_id: str, platform: str) -> dict[str, Any]:
    connection = _connect(data_dir)
    try:
        row = connection.execute(
            "SELECT * FROM platform_transactions WHERE outbox_id=? AND platform=?",
            (outbox_id, platform),
        ).fetchone()
        if not row:
            return {}
        result = dict(row)
        result["request_payload"] = _decode(result.pop("request_payload_json"), {})
        result["provider_response"] = _decode(result.pop("provider_response_json"), {})
        return result
    finally:
        connection.close()


def recent_outbox_activity(data_dir: str, limit: int = 10) -> list[dict[str, Any]]:
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            """
            SELECT outbox_id, content_id, content_date, slot, scheduled_at, status,
                   attempt_count, published_at, last_error
            FROM content_outbox
            ORDER BY created_at DESC LIMIT ?
            """,
            (max(1, min(50, limit)),),
        ).fetchall()
        activity = []
        for row in rows:
            item = dict(row)
            item["platforms"] = [
                dict(transaction)
                for transaction in connection.execute(
                    """
                    SELECT platform, state, external_id, attempt_count, last_error, updated_at
                    FROM platform_transactions WHERE outbox_id=? ORDER BY platform
                    """,
                    (row["outbox_id"],),
                ).fetchall()
            ]
            activity.append(item)
        return activity
    finally:
        connection.close()


def complete_platform_transaction(
    data_dir: str,
    *,
    outbox_id: str,
    platform: str,
    state: str,
    external_id: str = "",
    provider_response: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    allowed = {"CONFIRMED_SUCCESS", "CONFIRMED_FAILURE", "AMBIGUOUS", "AUTH_ACTION_REQUIRED"}
    if state not in allowed:
        raise ValueError(f"invalid transaction state: {state}")
    connection = _connect(data_dir)
    try:
        connection.execute(
            """
            UPDATE platform_transactions
            SET state=?, external_id=?, provider_response_json=?, last_error=?, updated_at=?
            WHERE outbox_id=? AND platform=?
            """,
            (state, external_id or None, _json(provider_response or {}), error or None, _now(), outbox_id, platform),
        )
        connection.commit()
    finally:
        connection.close()


def release_outbox(
    data_dir: str,
    outbox_id: str,
    error: str,
    *,
    next_attempt_at: str | None = None,
    reset_attempts: bool = False,
) -> None:
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        lifecycle = connection.execute(
            "SELECT lifecycle_state, active_attempt_id FROM content_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        if lifecycle and lifecycle["lifecycle_state"] == PackageState.DISPATCHING.value:
            _record_transition(
                connection,
                outbox_id=outbox_id,
                destination=PackageState.READY_TO_DISPATCH.value,
                reason_code="DELIVERY_RELEASED_FOR_RETRY",
                actor="content_operations.release_outbox",
                attempt_id=lifecycle["active_attempt_id"],
                detail={"error": error, "next_attempt_at": next_attempt_at},
            )
        current_state = str(lifecycle["lifecycle_state"] or "") if lifecycle else ""
        retryable_states = {
            PackageState.DISPATCHING.value,
            PackageState.READY_TO_DISPATCH.value,
            PackageState.PARTIALLY_PUBLISHED.value,
            PackageState.FAILED_DELIVERY.value,
        }
        legacy_status = "READY" if current_state in retryable_states else "RECOVERING"
        effective_next_attempt = next_attempt_at if legacy_status == "READY" else None
        connection.execute(
            """UPDATE content_outbox SET status=?, claimed_at=NULL, next_attempt_at=?, last_error=?,
                lease_owner=NULL, lease_expires_at=NULL, active_attempt_id=NULL,
                attempt_count=CASE WHEN ? THEN 0 ELSE attempt_count END WHERE outbox_id=?""",
            (legacy_status, effective_next_attempt, error, int(reset_attempts), outbox_id),
        )
        connection.execute(
            "UPDATE daily_slots SET status=?, claimed_at=NULL, last_error=?, updated_at=? WHERE outbox_id=?",
            (legacy_status, error, now, outbox_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def hold_outbox(data_dir: str, outbox_id: str, reason: str) -> None:
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """UPDATE content_outbox SET status='HELD', claimed_at=NULL, next_attempt_at=NULL, last_error=?
                WHERE outbox_id=? AND status IN ('READY', 'DUE', 'EXTERNAL_ACTION_REQUIRED')""",
            (reason, outbox_id),
        ).rowcount
        if changed != 1:
            raise ValueError(f"outbox_not_holdable:{outbox_id}")
        connection.execute(
            "UPDATE daily_slots SET status='HELD', claimed_at=NULL, last_error=?, updated_at=? WHERE outbox_id=?",
            (reason, now, outbox_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def recover_outbox(data_dir: str, outbox_id: str, error: str) -> None:
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        lifecycle = connection.execute(
            "SELECT lifecycle_state, active_attempt_id FROM content_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        if lifecycle and lifecycle["lifecycle_state"] in {PackageState.DISPATCHING.value, PackageState.READY_TO_DISPATCH.value}:
            _record_transition(
                connection,
                outbox_id=outbox_id,
                destination=PackageState.PREPARATION_REQUIRED.value,
                reason_code="PACKAGE_RECOVERY_REQUIRED",
                actor="content_operations.recover_outbox",
                attempt_id=lifecycle["active_attempt_id"],
                detail={"error": error},
            )
        connection.execute(
            "UPDATE content_outbox SET status='RECOVERING', claimed_at=NULL, lease_owner=NULL, lease_expires_at=NULL, active_attempt_id=NULL, last_error=? WHERE outbox_id=?",
            (error, outbox_id),
        )
        connection.execute(
            "UPDATE daily_slots SET status='RECOVERING', claimed_at=NULL, last_error=?, updated_at=? WHERE outbox_id=?",
            (error, now, outbox_id),
        )
        connection.execute(
            """
            UPDATE council_sessions SET status='RECOVERING', updated_at=?
            WHERE decision_id=(SELECT decision_id FROM content_outbox WHERE outbox_id=?)
            """,
            (now, outbox_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reconcile_ready_inventory(data_dir: str) -> list[dict[str, str]]:
    connection = _connect(data_dir)
    recovered: list[dict[str, str]] = []
    try:
        rows = connection.execute(
            "SELECT outbox_id, package_json FROM content_outbox WHERE status='READY'"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        package = _decode(row["package_json"], {})
        platforms = configured_platforms(package)
        issue = ""
        if not platforms:
            issue = "ready_package_has_no_routed_platforms"
        visuals = package.get("generated_visuals") if isinstance(package.get("generated_visuals"), dict) else {}
        engines = visuals.get("render_engines") if isinstance(visuals.get("render_engines"), dict) else {}
        reviews = visuals.get("artifact_reviews") if isinstance(visuals.get("artifact_reviews"), dict) else {}
        visual_plan = package.get("visual_plan") if isinstance(package.get("visual_plan"), dict) else {}
        route = str(visual_plan.get("creative_route") or visual_plan.get("visual_format") or "").strip().upper()
        explicit_packshot = route in {"PACKSHOT", "PACKSHOT_ONLY", "PREMIUM_PRODUCT_HERO"}
        for platform in platforms:
            review = reviews.get(platform) if isinstance(reviews.get(platform), dict) else {}
            if review.get("verdict") == "REGENERATE_VISUAL":
                issue = f"{platform}_visual_requires_recovery"
                break
            if str(engines.get(platform) or "") == "approved_product_photo" and not explicit_packshot:
                issue = f"{platform}_packshot_only_without_explicit_route"
                break
        if issue:
            recover_outbox(data_dir, str(row["outbox_id"]), issue)
            recovered.append({"outbox_id": str(row["outbox_id"]), "reason": issue})
    return recovered


def reconcile_stale_claims(
    data_dir: str,
    *,
    now_utc: datetime | None = None,
    stale_minutes: int = 15,
) -> list[dict[str, str]]:
    now = now_utc or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(1, stale_minutes))
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            "SELECT outbox_id, claimed_at FROM content_outbox WHERE status='CLAIMED'"
        ).fetchall()
    finally:
        connection.close()
    recovered: list[dict[str, str]] = []
    for row in rows:
        try:
            claimed_at = datetime.fromisoformat(str(row["claimed_at"] or "").replace("Z", "+00:00"))
        except ValueError:
            claimed_at = datetime.min.replace(tzinfo=timezone.utc)
        if claimed_at > cutoff:
            continue
        connection = _connect(data_dir)
        try:
            protected = connection.execute(
                """
                SELECT 1 FROM platform_transactions
                WHERE outbox_id=? AND state IN ('CONFIRMED_SUCCESS', 'AMBIGUOUS', 'AUTH_ACTION_REQUIRED') LIMIT 1
                """,
                (row["outbox_id"],),
            ).fetchone()
        finally:
            connection.close()
        if protected:
            continue
        release_outbox(data_dir, str(row["outbox_id"]), "stale_claim_recovered_after_restart")
        recovered.append({"outbox_id": str(row["outbox_id"]), "reason": "stale_claim_recovered_after_restart"})
    return recovered


def reconcile_confirmed_transactions(data_dir: str) -> list[dict[str, str]]:
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            "SELECT outbox_id, package_json, status FROM content_outbox WHERE status IN ('CLAIMED', 'PUBLISHING', 'READY')"
        ).fetchall()
    finally:
        connection.close()
    reconciled: list[dict[str, str]] = []
    for row in rows:
        package = _decode(row["package_json"], {})
        platforms = configured_platforms(package)
        if not platforms:
            continue
        connection = _connect(data_dir)
        try:
            states = {
                transaction["platform"]: transaction["state"]
                for transaction in connection.execute(
                    "SELECT platform, state FROM platform_transactions WHERE outbox_id=?",
                    (row["outbox_id"],),
                ).fetchall()
            }
        finally:
            connection.close()
        if all(states.get(platform) == "CONFIRMED_SUCCESS" for platform in platforms):
            finalize_outbox(data_dir, str(row["outbox_id"]), status="PUBLISHED")
            reconciled.append({"outbox_id": str(row["outbox_id"]), "reason": "confirmed_transactions_reconciled"})
    return reconciled


def finalize_outbox(
    data_dir: str,
    outbox_id: str,
    *,
    status: str,
    error: str = "",
) -> None:
    allowed = {"PUBLISHED", "FAILED", "EXTERNAL_ACTION_REQUIRED"}
    if status not in allowed:
        raise ValueError(f"invalid outbox final status: {status}")
    now = _now()
    published_at = now if status == "PUBLISHED" else None
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        lifecycle = connection.execute(
            "SELECT lifecycle_state, active_attempt_id FROM content_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        lifecycle_target = {
            "PUBLISHED": PackageState.PUBLISHED.value,
            "FAILED": PackageState.FAILED_DELIVERY.value,
            "EXTERNAL_ACTION_REQUIRED": PackageState.RECONCILIATION_REQUIRED.value,
        }[status]
        if lifecycle and lifecycle["lifecycle_state"] != lifecycle_target:
            _record_transition(
                connection,
                outbox_id=outbox_id,
                destination=lifecycle_target,
                reason_code={
                    "PUBLISHED": "ALL_PLATFORM_RECEIPTS_CONFIRMED",
                    "FAILED": "DELIVERY_TERMINAL_FAILURE",
                    "EXTERNAL_ACTION_REQUIRED": "DELIVERY_RECONCILIATION_REQUIRED",
                }[status],
                actor="content_operations.finalize_outbox",
                attempt_id=lifecycle["active_attempt_id"],
                detail={"error": error},
            )
        connection.execute(
            """
            UPDATE content_outbox SET status=?, published_at=?, last_error=?, claimed_at=NULL,
                lease_owner=NULL, lease_expires_at=NULL, active_attempt_id=NULL WHERE outbox_id=?
            """,
            (status, published_at, error or None, outbox_id),
        )
        connection.execute(
            """
            UPDATE daily_slots SET status=?, published_at=?, last_error=?, updated_at=? WHERE outbox_id=?
            """,
            (status, published_at, error or None, now, outbox_id),
        )
        connection.execute(
            """
            UPDATE council_sessions SET status=?, updated_at=?
            WHERE decision_id=(SELECT decision_id FROM content_outbox WHERE outbox_id=?)
            """,
            (status, now, outbox_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_slot_external_action(
    data_dir: str,
    *,
    content_date: str,
    slot: str,
    decision_id: str,
    error: str,
) -> None:
    now = _now()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE daily_slots SET status='EXTERNAL_ACTION_REQUIRED', decision_id=?,
                last_error=?, updated_at=? WHERE content_date=? AND slot=?
            """,
            (decision_id, error, now, content_date, slot),
        )
        connection.execute(
            "UPDATE council_sessions SET status='EXTERNAL_ACTION_REQUIRED', updated_at=? WHERE decision_id=?",
            (now, decision_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def daily_status(data_dir: str, content_date: str | date | None = None) -> dict[str, Any]:
    day = content_date.isoformat() if isinstance(content_date, date) else str(content_date or date.today().isoformat())
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            "SELECT * FROM daily_slots WHERE content_date=? ORDER BY scheduled_at",
            (day,),
        ).fetchall()
        slots = []
        for row in rows:
            item = dict(row)
            item["platform_policy"] = _decode(item.pop("platform_policy_json"), {})
            slots.append(item)
        declared_required = next((
            row["platform_policy"].get("required_slots")
            for row in slots
            if isinstance(row.get("platform_policy"), dict)
            and isinstance(row["platform_policy"].get("required_slots"), list)
        ), None)
        required_slots = {
            str(slot) for slot in (declared_required or SLOTS) if str(slot) in SLOTS
        } or set(SLOTS)
        required_rows = [row for row in slots if row["slot"] in required_slots]
        counts = {status: sum(1 for row in required_rows if row["status"] == status) for status in {
            "UNPLANNED", "COUNCIL_ACTIVE", "READY", "DUE", "CLAIMED", "PUBLISHING", "PUBLISHED", "EXTERNAL_ACTION_REQUIRED"
        }}
        ready = counts.get("READY", 0) + counts.get("DUE", 0)
        published = counts.get("PUBLISHED", 0)
        required = len(required_slots)
        return {
            "date": day,
            "required": required,
            "published": published,
            "ready": ready,
            "in_production": counts.get("CLAIMED", 0) + counts.get("PUBLISHING", 0),
            "missing": max(0, required - published - ready - counts.get("CLAIMED", 0) - counts.get("PUBLISHING", 0)),
            "slots": slots,
        }
    finally:
        connection.close()


def scheduled_calendar(
    data_dir: str,
    start_date: str | date | None = None,
    days: int = 42,
) -> dict[str, Any]:
    start = start_date if isinstance(start_date, date) else date.fromisoformat(str(start_date or date.today().isoformat()))
    day_count = max(1, min(int(days), 120))
    end = start + timedelta(days=day_count - 1)
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            """
            SELECT slots.*, outbox.package_json, outbox.status AS outbox_status
            FROM daily_slots AS slots
            LEFT JOIN content_outbox AS outbox ON outbox.outbox_id = slots.outbox_id
            WHERE slots.content_date BETWEEN ? AND ? AND slots.outbox_id IS NOT NULL
            ORDER BY slots.scheduled_at, slots.slot
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        posts_by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            package = _decode(item.pop("package_json"), {})
            item["platform_policy"] = _decode(item.pop("platform_policy_json"), {})
            item["package"] = package
            item["platforms"] = (
                package.get("platforms")
                or package.get("platform_policy", {}).get("platforms")
                or item["platform_policy"].get("platforms")
                or []
            )
            posts_by_date.setdefault(item["content_date"], []).append(item)
        calendar_days = []
        for offset in range(day_count):
            current = (start + timedelta(days=offset)).isoformat()
            calendar_days.append({"date": current, "posts": posts_by_date.get(current, [])})
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "days": calendar_days,
            "scheduled_count": sum(len(item["posts"]) for item in calendar_days),
        }
    finally:
        connection.close()


def content_operations_workspace(
    data_dir: str,
    *,
    now_utc: str | None = None,
    upcoming_days: int = 30,
    history_days: int = 30,
) -> dict[str, Any]:
    now = datetime.fromisoformat(now_utc or _now())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.date()
    upcoming_end = today + timedelta(days=max(1, min(int(upcoming_days), 120)))
    history_start = today - timedelta(days=max(1, min(int(history_days), 365)))
    connection = _connect(data_dir)
    try:
        rows = connection.execute(
            """
            SELECT outbox_id, content_id, decision_id, content_date, slot, scheduled_at,
                   package_json, status, attempt_count, created_at, ready_at, claimed_at,
                   next_attempt_at, published_at, last_error
            FROM content_outbox
            WHERE content_date BETWEEN ? AND ?
            ORDER BY datetime(scheduled_at), created_at
            """,
            (history_start.isoformat(), upcoming_end.isoformat()),
        ).fetchall()
        upcoming: list[dict[str, Any]] = []
        published: list[dict[str, Any]] = []
        media_by_identity: dict[str, dict[str, Any]] = {}
        statuses: set[str] = set()
        platforms: set[str] = set()
        content_types: set[str] = set()
        for row in rows:
            item = dict(row)
            package = _decode(item.pop("package_json"), {})
            transactions = []
            for transaction in connection.execute(
                """
                SELECT platform, state, request_key, request_payload_json, external_id,
                       provider_response_json, attempt_count, last_error, created_at, updated_at
                FROM platform_transactions WHERE outbox_id=? ORDER BY platform
                """,
                (item["outbox_id"],),
            ).fetchall():
                record = dict(transaction)
                record["request_payload"] = _decode(record.pop("request_payload_json"), {})
                record["provider_response"] = _decode(record.pop("provider_response_json"), {})
                transactions.append(record)
                platforms.add(str(record["platform"]))
            scheduled = datetime.fromisoformat(str(item["scheduled_at"]))
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
            routed_platforms = configured_platforms(package)
            platforms.update(routed_platforms)
            content_type = str(package.get("content_type") or package.get("format") or "unspecified")
            content_types.add(content_type)
            statuses.add(str(item["status"]))
            item.update({
                "package": package,
                "platforms": routed_platforms,
                "transactions": transactions,
                "content_type": content_type,
                "prepare_by": (scheduled - timedelta(hours=4)).isoformat(),
                "operationally_visible": now >= scheduled - timedelta(hours=4),
                "readiness": package.get("publication_readiness") or package.get("readiness") or {},
                "rejection_reasons": package.get("rejection_reasons") or package.get("qa_failures") or [],
            })
            media_candidates = []
            if package.get("primary_publish_image_url"):
                media_candidates.append({"url": package["primary_publish_image_url"], "role": "PRIMARY_PUBLISH"})
            for asset in package.get("carousel_assets") or package.get("assets") or []:
                media_candidates.append(asset if isinstance(asset, dict) else {"url": asset})
            if isinstance(package.get("media_asset"), dict):
                media_candidates.append(package["media_asset"])
            item["media"] = []
            for asset in media_candidates:
                source = str(asset.get("public_url") or asset.get("local_path") or asset.get("path") or asset.get("url") or "")
                if not source:
                    continue
                identity = str(asset.get("sha256") or asset.get("asset_sha256") or source)
                media = media_by_identity.setdefault(identity, {
                    "identity": identity,
                    "source": source,
                    "sha256": asset.get("sha256") or asset.get("asset_sha256") or "",
                    "role": asset.get("role") or "CONTENT_ASSET",
                    "status": asset.get("status") or "STORED",
                    "outbox_ids": [],
                })
                if item["outbox_id"] not in media["outbox_ids"]:
                    media["outbox_ids"].append(item["outbox_id"])
                item["media"].append(identity)
            confirmed = [transaction for transaction in transactions if transaction["state"] == "CONFIRMED_SUCCESS"]
            if history_start <= date.fromisoformat(str(item["content_date"])) <= today and confirmed:
                item["confirmed_publications"] = confirmed
                published.append(item)
            elif today <= date.fromisoformat(str(item["content_date"])) <= upcoming_end:
                upcoming.append(item)
        return {
            "generated_at": now.isoformat(),
            "windows": {
                "upcoming_start": today.isoformat(),
                "upcoming_end": upcoming_end.isoformat(),
                "history_start": history_start.isoformat(),
                "history_end": today.isoformat(),
                "preparation_hours": 4,
            },
            "summary": {
                "upcoming": len(upcoming),
                "published": len(published),
                "media": len(media_by_identity),
                "needs_attention": sum(1 for item in upcoming if item["last_error"] or item["rejection_reasons"]),
            },
            "facets": {
                "statuses": sorted(statuses),
                "platforms": sorted(platforms),
                "content_types": sorted(content_types),
            },
            "upcoming": upcoming,
            "published": sorted(published, key=lambda item: item.get("published_at") or item["scheduled_at"], reverse=True),
            "media": list(media_by_identity.values()),
        }
    finally:
        connection.close()


def reschedule_outbox(
    data_dir: str,
    outbox_id: str,
    *,
    scheduled_at: str,
    slot: str | None = None,
) -> dict[str, Any]:
    scheduled = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    if scheduled.tzinfo is None:
        raise ValueError("scheduled_at_timezone_required")
    destination_date = scheduled.date().isoformat()
    connection = _connect(data_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT outbox_id, content_date, slot, scheduled_at, status FROM content_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"outbox_not_found:{outbox_id}")
        if row["status"] not in {"READY", "HELD"}:
            raise ValueError(f"outbox_not_reschedulable:{row['status']}")
        unsafe_transaction = connection.execute(
            """SELECT 1 FROM platform_transactions WHERE outbox_id=?
               AND state IN ('REQUEST_SENT', 'CONFIRMED_SUCCESS', 'AMBIGUOUS') LIMIT 1""",
            (outbox_id,),
        ).fetchone()
        if unsafe_transaction:
            raise ValueError("outbox_has_nonreplaceable_platform_transaction")
        destination_slot = str(slot or row["slot"]).strip().lower()
        if destination_slot not in SLOTS:
            raise ValueError("slot_must_be_morning_midday_or_evening")
        occupied = connection.execute(
            "SELECT outbox_id FROM daily_slots WHERE content_date=? AND slot=? AND outbox_id IS NOT NULL AND outbox_id<>?",
            (destination_date, destination_slot, outbox_id),
        ).fetchone()
        if occupied:
            raise ValueError(f"slot_already_occupied:{destination_date}:{destination_slot}")
        previous = {
            "content_date": str(row["content_date"]),
            "slot": str(row["slot"]),
            "scheduled_at": str(row["scheduled_at"]),
        }
        if previous["content_date"] != destination_date or previous["slot"] != destination_slot:
            connection.execute(
                """UPDATE daily_slots SET content_id=NULL, decision_id=NULL, outbox_id=NULL,
                   status='UNPLANNED', ready_at=NULL, claimed_at=NULL, published_at=NULL,
                   last_error=NULL, updated_at=? WHERE outbox_id=?""",
                (_now(), outbox_id),
            )
            destination = connection.execute(
                "SELECT 1 FROM daily_slots WHERE content_date=? AND slot=?",
                (destination_date, destination_slot),
            ).fetchone()
            if not destination:
                connection.execute(
                    """INSERT INTO daily_slots
                       (content_date, slot, scheduled_at, platform_policy_json, status, updated_at)
                       VALUES (?, ?, ?, '{}', 'UNPLANNED', ?)""",
                    (destination_date, destination_slot, scheduled.isoformat(), _now()),
                )
        connection.execute(
            "UPDATE content_outbox SET content_date=?, slot=?, scheduled_at=?, next_attempt_at=NULL WHERE outbox_id=?",
            (destination_date, destination_slot, scheduled.isoformat(), outbox_id),
        )
        connection.execute(
            """UPDATE daily_slots SET scheduled_at=?, content_id=(SELECT content_id FROM content_outbox WHERE outbox_id=?),
               decision_id=(SELECT decision_id FROM content_outbox WHERE outbox_id=?), outbox_id=?, status=?,
               ready_at=(SELECT ready_at FROM content_outbox WHERE outbox_id=?), last_error=(SELECT last_error FROM content_outbox WHERE outbox_id=?),
               updated_at=? WHERE content_date=? AND slot=?""",
            (scheduled.isoformat(), outbox_id, outbox_id, outbox_id, row["status"], outbox_id, outbox_id, _now(), destination_date, destination_slot),
        )
        connection.commit()
        return {"outbox_id": outbox_id, "content_date": destination_date, "slot": destination_slot, "scheduled_at": scheduled.isoformat(), "previous": previous}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def content_detail(data_dir: str, decision_id: str) -> dict[str, Any]:
    connection = _connect(data_dir)
    try:
        session = connection.execute(
            "SELECT * FROM council_sessions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if not session:
            return {}
        candidates = connection.execute(
            "SELECT * FROM content_candidates WHERE decision_id=? ORDER BY ordinal", (decision_id,)
        ).fetchall()
        result = dict(session)
        result["blackboard"] = _decode(result.pop("blackboard_json"), {})
        result["rationale"] = _decode(result.pop("rationale_json"), [])
        result["candidates"] = []
        for candidate in candidates:
            item = dict(candidate)
            item["loss_reasons"] = _decode(item.pop("loss_reasons_json"), [])
            item["content"] = _decode(item.pop("content_json"), {})
            result["candidates"].append(item)
        outbox = connection.execute(
            "SELECT * FROM content_outbox WHERE decision_id=? ORDER BY created_at", (decision_id,)
        ).fetchall()
        result["outbox"] = []
        for row in outbox:
            item = dict(row)
            item["package"] = _decode(item.pop("package_json"), {})
            transactions = connection.execute(
                "SELECT * FROM platform_transactions WHERE outbox_id=? ORDER BY platform",
                (item["outbox_id"],),
            ).fetchall()
            item["transactions"] = []
            for transaction in transactions:
                record = dict(transaction)
                record["request_payload"] = _decode(record.pop("request_payload_json"), {})
                record["provider_response"] = _decode(record.pop("provider_response_json"), {})
                item["transactions"].append(record)
            result["outbox"].append(item)
        return result
    finally:
        connection.close()


def daily_index(data_dir: str, content_date: str | date | None = None) -> dict[str, Any]:
    summary = daily_status(data_dir, content_date)
    details: list[dict[str, Any]] = []
    for slot in summary["slots"]:
        decision_id = str(slot.get("decision_id") or "")
        details.append(content_detail(data_dir, decision_id) if decision_id else {"slot": slot["slot"]})
    return {**summary, "details": details}


def daily_markdown(data_dir: str, content_date: str | date | None = None) -> str:
    index = daily_index(data_dir, content_date)
    lines = [
        f"INFENERGY CONTENT - {index['date']}",
        "=" * 64,
        f"REQUIRED: {index['required']}",
        f"PUBLISHED: {index['published']}",
        f"READY: {index['ready']}",
        f"MISSING: {index['missing']}",
        "",
    ]
    details_by_id = {detail.get("decision_id"): detail for detail in index["details"] if detail.get("decision_id")}
    for number, slot in enumerate(index["slots"], start=1):
        detail = details_by_id.get(slot.get("decision_id"), {})
        blackboard = detail.get("blackboard") if isinstance(detail.get("blackboard"), dict) else {}
        outbox = (detail.get("outbox") or [{}])[0]
        package = outbox.get("package") if isinstance(outbox.get("package"), dict) else {}
        routing = package.get("routing") if isinstance(package.get("routing"), dict) else {}
        transactions = outbox.get("transactions") or []
        lines.extend([
            f"SLOT {number} - {slot['slot'].upper()}",
            f"Time: {slot['scheduled_at']}",
            f"Platform: {', '.join(routing.get('platforms') or []) or 'not assigned'}",
            f"Product/Topic: {package.get('product_name') or package.get('topic') or blackboard.get('selected_opportunity') or 'not selected'}",
            f"Human Reality: {blackboard.get('human_reality') or 'not recorded'}",
            f"Brain: {json.dumps(blackboard.get('brain') or {}, ensure_ascii=True)}",
            f"Heart: {json.dumps(blackboard.get('heart') or {}, ensure_ascii=True)}",
            f"Content Job: {blackboard.get('content_job') or 'not recorded'}",
            f"Status: {slot['status']}",
            f"External ID: {', '.join(str(item.get('external_id')) for item in transactions if item.get('external_id')) or 'none'}",
            "",
        ])
        candidates = detail.get("candidates") or []
        if candidates:
            lines.append("OTHER CONTENT GENERATED")
            for candidate in candidates:
                lines.append(
                    f"- Candidate {candidate.get('ordinal')}: {candidate.get('status')}"
                    f"; why not selected: {', '.join(candidate.get('loss_reasons') or []) or 'selected/retained'}"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def operations_readiness(
    data_dir: str,
    *,
    now_utc: datetime | None = None,
    lead_hours: int = 2,
    publisher_ready: dict[str, bool] | None = None,
    dispatcher_active: bool = True,
) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    today = daily_status(data_dir, now.date())
    tomorrow = daily_status(data_dir, now.date() + timedelta(days=1))
    if publisher_ready is None:
        from platform_publishing import list_platforms

        publishers = {
            str(platform["platform"]): bool(platform.get("publishing_enabled"))
            for platform in list_platforms()
            if str(platform.get("platform")) in PLATFORMS
        }
    else:
        publishers = publisher_ready
    required_slots = {
        str(slot)
        for row in today["slots"]
        for slot in (row.get("platform_policy", {}).get("required_slots") or [])
        if str(slot) in SLOTS
    } or set(SLOTS)
    actions: list[dict[str, str]] = []
    slots: list[dict[str, Any]] = []
    next_slot: dict[str, Any] | None = None
    for row in today["slots"]:
        try:
            scheduled = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
        except ValueError:
            scheduled = now
        deadline = scheduled - timedelta(hours=max(0, lead_hours))
        accounted = row["status"] in {"READY", "DUE", "CLAIMED", "PUBLISHING", "PUBLISHED"}
        required = row["slot"] in required_slots
        late = required and now >= deadline and not accounted
        slot_state = {
            "slot": row["slot"],
            "scheduled_at": row["scheduled_at"],
            "readiness_deadline": deadline.isoformat(),
            "status": row["status"],
            "accounted_for": accounted,
            "late_for_readiness": late,
            "content_id": row.get("content_id"),
            "outbox_id": row.get("outbox_id"),
            "last_error": row.get("last_error"),
        }
        slots.append(slot_state)
        if required and scheduled >= now and next_slot is None:
            next_slot = slot_state
        if late:
            actions.append({"owner": "OPERATIONS_READINESS_SPECIALIST", "slot": row["slot"], "action": "RECOVER_OR_PULL_READY_RESERVE", "reason": row.get("last_error") or "readiness_deadline_missed"})
    if tomorrow["ready"] < tomorrow["required"]:
        actions.append({"owner": "CONTENT_FACTORY", "slot": "tomorrow", "action": "REPLENISH", "reason": f"tomorrow_ready={tomorrow['ready']}"})
    if not dispatcher_active:
        actions.append({"owner": "OPERATIONS_READINESS_SPECIALIST", "slot": "all", "action": "RESTORE_DISPATCHER", "reason": "dispatcher_inactive"})
    for platform, ready in publishers.items():
        if not ready:
            actions.append({"owner": "OPERATIONS_READINESS_SPECIALIST", "slot": "all", "action": "RESTORE_PUBLISHER", "reason": f"{platform}_not_ready"})
    creative_blocked = any(row["status"] == "EXTERNAL_ACTION_REQUIRED" for row in today["slots"] + tomorrow["slots"])
    content_supply_ready = today["missing"] == 0 and tomorrow["ready"] == tomorrow["required"]
    return {
        "service_health": "HEALTHY",
        "content_supply_health": "READY" if content_supply_ready else "ACTION_REQUIRED",
        "creative_health": "EXTERNAL_ACTION_REQUIRED" if creative_blocked else "READY",
        "publisher_health": "READY" if publishers and all(publishers.values()) else "ACTION_REQUIRED",
        "dispatcher_health": "ACTIVE" if dispatcher_active else "INACTIVE",
        "today": today,
        "tomorrow": tomorrow,
        "slots": slots,
        "next_slot": next_slot,
        "actions": actions,
        "lead_hours": lead_hours,
    }
