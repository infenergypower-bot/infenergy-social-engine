"""Versioned Product Canon resolution from the repository's approved product briefs."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any


class CanonNotFound(ValueError):
    pass


def _brief_path(data_dir: str, product_id: str) -> str:
    safe_id = os.path.basename(str(product_id).strip())
    return os.path.join(data_dir, "product_briefs", f"{safe_id}.json")


def resolve_product_canon(data_dir: str, product_id: str) -> dict[str, Any]:
    normalized_id = str(product_id or "").strip()
    if not normalized_id:
        return {"required": False, "status": "NOT_APPLICABLE"}
    path = _brief_path(data_dir, normalized_id)
    try:
        with open(path, "rb") as file:
            raw = file.read()
        brief = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CanonNotFound(f"CANON_NOT_FOUND:{normalized_id}") from exc
    if str(brief.get("product_id") or brief.get("sku") or "").strip() != normalized_id:
        raise CanonNotFound(f"CANON_ID_MISMATCH:{normalized_id}")
    digest = hashlib.sha256(raw).hexdigest()
    reference_ids = [f"product-brief:{normalized_id}:{digest}"]
    source_image = str(brief.get("source_image_url") or "").strip()
    if source_image:
        reference_ids.append(f"source-image:{hashlib.sha256(source_image.encode('utf-8')).hexdigest()}")
    return {
        "required": True,
        "status": "RESOLVED",
        "canon_id": normalized_id,
        "canon_version": str(brief.get("updated_at_utc") or digest[:16]),
        "canon_sha256": digest,
        "reference_asset_ids": reference_ids,
        "verified_facts": list(brief.get("verified_facts") or []),
        "forbidden_claims": list(brief.get("forbidden_claims") or []),
        "source_path": os.path.relpath(path, data_dir).replace("\\", "/"),
    }


def attach_product_canon(data_dir: str, package: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(package)
    if enriched.get("production_approval"):
        # The owner-approved registry is authoritative for the new workflow.
        # Verify the approval rather than trusting a caller-supplied canon stamp.
        from publication_authorization import authorize
        from production_workflow import Production
        for platform in (enriched.get("routing") or {}).get("platforms",[]):
            authorize(enriched,platform,data_dir=data_dir)
        item=Production(data_dir).get(enriched["production_approval"]["package_id"])
        payload=item["payload"]
        enriched["canon"]={"required":bool(payload["rules"]["character"] or payload["product_id"]),"status":"RESOLVED", "canon_id":payload["product_id"] or "infenergy-brand", "canon_version":item["approval"]["digest"][:16],"reference_asset_ids":payload["references"],"registry":"production_references"}
        return enriched
    product_id = str(enriched.get("product_id") or "").strip()
    if product_id:
        try:
            enriched["canon"] = resolve_product_canon(data_dir, product_id)
        except CanonNotFound as exc:
            enriched["canon"] = {
                "required": True,
                "status": "NOT_FOUND",
                "canon_id": "",
                "canon_version": "",
                "reference_asset_ids": [],
                "reason": str(exc),
            }
    else:
        enriched.setdefault("canon", {"required": False, "status": "NOT_APPLICABLE"})
    return enriched
