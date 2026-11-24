#!/usr/bin/env python3
"""Read-only public Infenergy smoke checks. No credentials or customer data."""
import json
from urllib.request import Request, urlopen
from urllib.parse import urljoin
from html.parser import HTMLParser
from datetime import datetime, timezone
import sys

BASE = "https://infenergypower.com/"
CHECKS = {
    "homepage": "",
    "travel": "powercharge-pro-travel/",
    "work": "powercharge-pro-work/",
    "family": "powercharge-pro-family/",
    "health": "wp-json/infenergy-growth/v1/health",
    "operations": "wp-json/infenergy-growth/v1/operations-health",
}

def fetch(path):
    req = Request(urljoin(BASE, path), headers={"User-Agent": "InfenergyPublicHealthCheck/1.0", "Accept": "*/*"})
    with urlopen(req, timeout=20) as resp:
        return resp.status, resp.read(1_500_000).decode("utf-8", errors="replace")

def evaluate(key, status, body):
    if status != 200:
        return ["HTTP " + str(status)]
    if key in {"travel", "work", "family"}:
        return [] if "See product details" in body else ["Product CTA not found"]
    if key == "health":
        data = json.loads(body)
        return [name + " not ready" for name in ("loaded", "lab_ready", "scheduled", "audit_completed") if data.get(name) is not True]
    if key == "operations":
        data = json.loads(body)
        return [name + " not ready" for name in ("loaded", "local_commerce", "catalog", "campaign_pages_provisioned", "seo_drafts_created", "scheduler_registered", "full_audit_completed") if data.get(name) is not True]
    return [] if "Infenergy" in body else ["Brand text missing"]

def run():
    result = {"utc": datetime.now(timezone.utc).isoformat(), "checks": {}}
    for key, path in CHECKS.items():
        try:
            status, body = fetch(path)
            errors = evaluate(key, status, body)
        except Exception as exc:
            errors = [type(exc).__name__ + ": " + str(exc)]
        result["checks"][key] = {"ok": not errors, "errors": errors}
    print(json.dumps(result, indent=2))
    return 0 if all(item["ok"] for item in result["checks"].values()) else 1

if __name__ == "__main__":
    sys.exit(run())
