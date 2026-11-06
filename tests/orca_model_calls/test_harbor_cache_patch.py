"""The optional Harbor cache patch changes location, not registry behavior."""

from pathlib import Path
import runpy

import pytest


PATCH = Path(__file__).resolve().parents[2] / "patches" / "harbor_constants.py"


@pytest.mark.parametrize("cache_dir", [None, "~/orca-cache", "/tmp/orca-cache"])
def test_cache_location_override(monkeypatch, cache_dir):
    monkeypatch.delenv("HARBOR_CACHE_DIR", raising=False)
    if cache_dir is not None:
        monkeypatch.setenv("HARBOR_CACHE_DIR", cache_dir)
    constants = runpy.run_path(str(PATCH))
    root = Path(cache_dir or "~/.cache/harbor").expanduser()
    assert constants["CACHE_DIR"] == root
    assert constants["TASK_CACHE_DIR"] == root / "tasks"
    assert constants["PACKAGE_CACHE_DIR"] == root / "tasks" / "packages"
    assert constants["DATASET_CACHE_DIR"] == root / "datasets"
    assert constants["NOTIFICATIONS_PATH"] == root / "notifications.json"


def test_registry_override_is_preserved(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY_WEBSITE_URL", "https://harbor.example.test")
    constants = runpy.run_path(str(PATCH))
    assert constants["HARBOR_REGISTRY_TASKS_URL"] == "https://harbor.example.test/tasks"
    assert constants["HARBOR_REGISTRY_DATASETS_URL"] == "https://harbor.example.test/datasets"
    assert constants["HARBOR_VIEWER_JOBS_URL"] == "https://harbor.example.test/jobs"
    assert constants["MAIN_SERVICE_NAME"] == "main"
