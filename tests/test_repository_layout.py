from __future__ import annotations

import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_root_contains_no_python_entrypoint_files() -> None:
    assert list(ROOT.glob("*.py")) == []


def test_product_package_is_preserved() -> None:
    assert (ROOT / "src" / "iot_ops_agent" / "web" / "api.py").is_file()
    assert (ROOT / "src" / "iot_ops_agent" / "agent" / "mcp_server.py").is_file()


def test_tutorial_is_archived_outside_the_runtime_image() -> None:
    tutorial = ROOT / "examples" / "code-reviewer-tutorial"
    if not tutorial.exists():
        pytest.skip("examples are intentionally excluded from the runtime image")
    assert (tutorial / "main.py").is_file()
    assert (tutorial / "go-tools" / "main.go").is_file()


def test_lifecycle_script_is_executable_and_outputs_are_not_in_root() -> None:
    lifecycle = ROOT / "bin" / "iotops"
    assert lifecycle.is_file()
    assert os.access(lifecycle, os.X_OK)
    assert list(ROOT.glob("sl100_*.json")) == []
