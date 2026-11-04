"""Gemini model routing + prompt versioning + API cost tracking.

Master Build §93-§96. Environment-configurable so we do not scatter
model names throughout the codebase.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from social.gemini_budget import GeminiBudgetExceeded, tracked_gemini_call


DEFAULT_MODEL_ROUTES: dict[str, str] = {
    "classification": "gemini-3.6-flash",
    "topic_generation": "gemini-3.6-flash",
    "strategy": "gemini-3.6-flash",
    "visual_direction": "gemini-3.6-flash",
    "copy_editing": "gemini-3.6-flash",
    "image_analysis": "gemini-3.6-flash",
    "fact_reasoning": "gemini-3.6-flash",
    "final_review": "gemini-3.6-flash",
}


def route_for(task: str) -> str:
    """Return the model name for a task; env var GEMINI_ROUTE_<TASK> overrides."""
    env = os.environ.get(f"GEMINI_ROUTE_{task.upper()}")
    if env:
        return env.strip()
    return DEFAULT_MODEL_ROUTES.get(task, "gemini-3.6-flash")


def route_candidates(task: str) -> list[str]:
    """Return ordered, de-duplicated models for one task.

    ``GEMINI_ROUTE_<TASK>`` is authoritative. Optional
    ``GEMINI_ROUTE_<TASK>_FALLBACKS`` or ``GEMINI_MODEL_FALLBACKS`` may list
    comma-separated alternatives so a retired provider model degrades to a
    configured substitute before template fallback.
    """
    task_key = task.upper()
    configured = os.environ.get(f"GEMINI_ROUTE_{task_key}_FALLBACKS", "")
    global_fallbacks = os.environ.get("GEMINI_MODEL_FALLBACKS", "")
    candidates = [route_for(task)]
    for raw in (configured, global_fallbacks):
        candidates.extend(part.strip() for part in raw.split(",") if part.strip())
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in out:
            out.append(candidate)
    return out


_LAST_ERROR: str | None = None
_PROVIDER_UNAVAILABLE_REASON: str | None = None
_PROVIDER_RETRY_AT = 0.0


def last_error() -> str | None:
    """Reason the most recent generate_json() call fell back to None, if any.

    Lets callers surface *why* a post used deterministic template copy
    instead of real Gemini copy without needing separate server log access.
    """
    return _LAST_ERROR


# --- Real Gemini text calls --------------------------------------------------
#
# Mirrors the exact client/config pattern already proven in generate_posts.py,
# social_visuals.py and the agents/ modules (genai.Client + GenerateContentConfig).
# Every caller must tolerate a ``None`` return (no API key / call failure) and
# fall back to its deterministic behavior — this keeps the whole ``social``
# package safe to run without network access, per its existing design.


def generate_json(task: str, prompt: str, *, system_instruction: str = "") -> dict[str, Any] | None:
    """Call Gemini for the given task and parse a JSON object response.

    Returns ``None`` when GEMINI_API_KEY is unset, the SDK is unavailable,
    or the call/parse fails for any reason.
    """
    global _LAST_ERROR, _PROVIDER_UNAVAILABLE_REASON, _PROVIDER_RETRY_AT
    if _PROVIDER_UNAVAILABLE_REASON and time.monotonic() < _PROVIDER_RETRY_AT:
        _LAST_ERROR = _PROVIDER_UNAVAILABLE_REASON
        return None
    _PROVIDER_UNAVAILABLE_REASON = None

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        _LAST_ERROR = "GEMINI_API_KEY not set"
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        _LAST_ERROR = "google-genai SDK not importable"
        return None

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=60_000, retry_options=types.HttpRetryOptions(attempts=1)))
    errors: list[str] = []
    for model in route_candidates(task):
        try:
            config_kwargs: dict[str, Any] = {"response_mime_type": "application/json"}
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction
            with tracked_gemini_call("reasoning", model, task, initiating_subsystem="model_router"):
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
            text = str(getattr(response, "text", "") or "").strip()
            if not text:
                errors.append(f"model={model}: empty response text")
                continue
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                errors.append(f"model={model}: response was not a JSON object")
                continue
            _LAST_ERROR = None
            return parsed
        except GeminiBudgetExceeded:
            raise
        except Exception as exc:
            error = f"model={model}: {type(exc).__name__}: {exc}"
            errors.append(error)
            if "RESOURCE_EXHAUSTED" in str(exc).upper() or "429" in str(exc):
                _PROVIDER_UNAVAILABLE_REASON = f"Gemini provider unavailable: {error}"
                _PROVIDER_RETRY_AT = time.monotonic() + 60
                break
    _LAST_ERROR = f"task={task}: " + " | ".join(errors)
    print(f"[model_router] {_LAST_ERROR}")
    return None


# --- Prompt versioning (§96) -----------------------------------------------


PROMPT_REGISTRY: dict[str, dict[str, Any]] = {}


def register_prompt(
    *,
    prompt_id: str,
    version: str,
    purpose: str,
    template: str,
    notes: str = "",
) -> None:
    PROMPT_REGISTRY[prompt_id] = {
        "prompt_id": prompt_id,
        "version": version,
        "purpose": purpose,
        "template": template,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": notes,
    }


def get_prompt(prompt_id: str) -> dict[str, Any] | None:
    return PROMPT_REGISTRY.get(prompt_id)


# --- API cost intelligence (§94) -------------------------------------------


@dataclass
class ApiCallRecord:
    model: str
    task: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    failed: bool = False
    retry_of: str | None = None
    estimated_cost_usd: float = 0.0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "task": self.task,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "failed": self.failed,
            "retry_of": self.retry_of,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "started_at": self.started_at,
        }


class ApiCostTracker:
    def __init__(self) -> None:
        self.records: list[ApiCallRecord] = []

    def record(self, r: ApiCallRecord) -> None:
        self.records.append(r)

    def totals(self) -> dict[str, Any]:
        return {
            "calls": len(self.records),
            "failed": sum(1 for r in self.records if r.failed),
            "input_tokens": sum(r.input_tokens for r in self.records),
            "output_tokens": sum(r.output_tokens for r in self.records),
            "estimated_cost_usd": round(sum(r.estimated_cost_usd for r in self.records), 6),
        }

    def dump(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"records": [r.as_dict() for r in self.records], "totals": self.totals()}, fh, indent=2)
