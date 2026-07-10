"""Local-only storage helpers for reviewed real SL100 log cases.

The files managed here deliberately never persist original ES log messages.
They only keep a lookup reference, a one-way fingerprint, and the reviewer's
label so later evals can re-query the live, read-only log source.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from sl100_es import SHANGHAI_TZ


LOCAL_CASES_PATH = Path("evals/sl100_real_cases.local.jsonl")
LOCAL_CANDIDATES_PATH = Path(".sl100/review_candidates.local.jsonl")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    path.write_text(content, encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def local_time_from_es(timestamp: str) -> str:
    value = timestamp.replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")


def evidence_fingerprint(message: str) -> str:
    """Return a stable fingerprint without retaining the redacted message itself."""
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", "<TIME>", message)
    normalized = re.sub(r"\d+", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def incident_signature(message: str) -> str:
    """Fingerprint an error signature while ignoring request-scoped identifiers."""
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", "<TIME>", message)
    normalized = re.sub(
        r'(?i)(["\']?(?:message[_-]?id|msg[_-]?id|request[_-]?id)["\']?\s*[:=]\s*)["\']?[^,"\'}\s]+["\']?',
        r"\1<IDENTIFIER>",
        normalized,
    )
    normalized = re.sub(r"\d+", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def incident_dedup_key(service: str, signature: str) -> str:
    """Scope an incident signature to its service for reviewed-case de-duplication."""
    return f"{service}:{signature}"
