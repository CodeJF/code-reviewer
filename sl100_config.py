"""
Configuration helpers for the SL100 diagnosis product surface.

Defaults are intentionally safe and read-only. A local override can be supplied
through SL100_CONFIG or configs/sl100.local.json; that file is ignored by git.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "elasticsearch": {
        "ssh_host": "sl100-93",
        "base_url": "http://127.0.0.1:9200",
        "max_hits": 200,
        "services": {
            "gateway": "api-gateway",
            "deviceShadow": "api-device-shadow",
            "pushService": "api-push-service",
            "access": "api-access",
        },
    },
    "remote_logs": {
        "gateway": {
            "host": "sl100-115",
            "logs": {
                "error": "/home/work/service/gateway/log/error.log",
                "debug": "/home/work/service/gateway/log/debug.log",
                "access": "/home/work/service/gateway/log/access.log",
                "sql": "/home/work/service/gateway/log/sql.log",
                "stderr": "/home/work/service/gateway/log/std_err.log",
                "stdout": "/home/work/service/gateway/log/srd_out.log",
            },
        },
        "deviceShadow": {
            "host": "sl100-115",
            "logs": {
                "error": "/home/work/service/deviceShadow/log/error.log",
                "debug": "/home/work/service/deviceShadow/log/debug.log",
                "stderr": "/home/work/service/deviceShadow/log/std_err.log",
                "stdout": "/home/work/service/deviceShadow/log/srd_out.log",
            },
        },
        "scheduledTask": {
            "host": "sl100-115",
            "logs": {
                "debug": "/home/work/service/scheduledTask/log/debug.log",
                "stderr": "/home/work/service/scheduledTask/log/std_err.log",
                "stdout": "/home/work/service/scheduledTask/log/srd_out.log",
            },
        },
        "pushService": {
            "host": "sl100-15",
            "logs": {
                "error": "/home/work/service/pushService/log/error.log",
                "debug": "/home/work/service/pushService/log/debug.log",
                "stderr": "/home/work/service/pushService/log/std_err.log",
                "stdout": "/home/work/service/pushService/log/srd_out.log",
            },
        },
        "cloudStorage": {
            "host": "sl100-15",
            "logs": {
                "error": "/home/work/service/cloudStorage/log/error.log",
                "debug": "/home/work/service/cloudStorage/log/debug.log",
                "stderr": "/home/work/service/cloudStorage/log/std_err.log",
                "stdout": "/home/work/service/cloudStorage/log/srd_out.log",
            },
        },
        "AdminService": {
            "host": "sl100-15",
            "remote_name": "adminService",
            "logs": {
                "error": "/home/work/service/adminService/log/error.log",
                "debug": "/home/work/service/adminService/log/debug.log",
                "sql": "/home/work/service/adminService/log/sql.log",
                "stderr": "/home/work/service/adminService/log/std_err.log",
                "stdout": "/home/work/service/adminService/log/srd_out.log",
            },
        },
    },
}


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = path or os.environ.get("SL100_CONFIG") or "configs/sl100.local.json"
    candidate = Path(config_path).expanduser()
    if not candidate.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    override = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(override, dict):
        raise ValueError(f"SL100 config must be a JSON object: {candidate}")
    return _merge_dict(DEFAULT_CONFIG, override)


def get_es_config() -> dict[str, Any]:
    return load_config()["elasticsearch"]


def get_remote_logs_config() -> dict[str, Any]:
    return load_config()["remote_logs"]
