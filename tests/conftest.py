from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_gemini_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_USAGE_PATH", str(tmp_path / "gemini-usage.json"))


@pytest.fixture(autouse=True)
def legacy_adapter_fixtures(monkeypatch, request):
    # Historical publisher tests intentionally use packages from before the
    # Production Desk. Its own suite tests the required default boundary.
    if request.module.__name__ != "test_production_workflow":
        monkeypatch.setenv("EDITORIAL_APPROVAL_MODE", "legacy")
