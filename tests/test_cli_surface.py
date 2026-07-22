from __future__ import annotations

import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "iot_ops_agent", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_unified_cli_help_and_dry_run() -> None:
    help_result = run_cli("--help")
    assert help_result.returncode == 0
    assert "iot-ops diagnose" in help_result.stdout
    assert "iot-ops mcp serve" in help_result.stdout

    diagnose = run_cli("diagnose", "deviceShadow websocket 异常", "--dry-run")
    assert diagnose.returncode == 0
    assert '"primary_service": "deviceShadow"' in diagnose.stdout


def test_legacy_console_aliases_are_installed() -> None:
    scripts = {entry.name for entry in entry_points(group="console_scripts")}
    expected = {
        "iot-ops",
        "sl100-agent",
        "sl100-diagnose",
        "sl100-es-logs",
        "sl100-mcp-server",
        "sl100-review-cases",
    }
    assert expected <= scripts


def test_lifecycle_help_and_clean_dry_run_do_not_require_docker() -> None:
    help_result = subprocess.run(
        [str(ROOT / "bin" / "iotops"), "help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert "./bin/iotops up" not in help_result.stdout
    assert "Build and start" in help_result.stdout

    clean_result = subprocess.run(
        [str(ROOT / "bin" / "iotops"), "clean", "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean_result.returncode == 0
    assert "nothing was removed" in clean_result.stdout
