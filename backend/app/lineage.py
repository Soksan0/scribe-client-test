from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def original_row_ids(file_id: str, row_count: int) -> list[str]:
    """Return stable internal identities for rows in an immutable upload."""
    return [f"{file_id}:row:{ordinal}" for ordinal in range(1, row_count + 1)]


def row_ordinal(row_id: str) -> int:
    try:
        ordinal = int(row_id.rsplit(":row:", 1)[1])
    except (IndexError, ValueError) as error:
        raise ValueError(f"Invalid Scribe row identity: {row_id!r}") from error
    if ordinal < 1:
        raise ValueError(f"Invalid Scribe row identity: {row_id!r}")
    return ordinal


def attach_operation_row_ids(operation: dict[str, Any] | None, row_ids: list[str]) -> dict[str, Any] | None:
    """Attach source row identities while retaining legacy row positions for display."""
    if operation is None:
        return None
    result = deepcopy(operation)
    rows = [int(value) for value in result.get("rows", [])]
    if rows:
        result["row_ids"] = [_row_id_at(row_ids, row) for row in rows]
    for change in result.get("changes", []):
        if "row" in change:
            change["row_id"] = _row_id_at(row_ids, int(change["row"]))
    return result


def attach_profile_row_ids(profile: dict[str, Any], row_ids: list[str]) -> dict[str, Any]:
    if len(row_ids) != int(profile.get("row_count", 0)):
        raise ValueError("The scan row map does not match the profiled dataset")
    profile["row_ids"] = list(row_ids)
    for finding in profile.get("findings", []):
        row_number = finding.get("row_number")
        finding["row_id"] = _row_id_at(row_ids, int(row_number)) if row_number else None
        finding["operation"] = attach_operation_row_ids(finding.get("operation"), row_ids)
    return profile


def operation_row_ordinals(operation: dict[str, Any]) -> list[int]:
    ids = operation.get("row_ids") or []
    return [row_ordinal(value) for value in ids] if ids else [int(value) for value in operation.get("rows", [])]


def change_row_ordinal(change: dict[str, Any]) -> int:
    return row_ordinal(change["row_id"]) if change.get("row_id") else int(change["row"])


def reviewed_row_ids(file_id: str, row_count: int, operations: list[dict[str, Any]]) -> list[str]:
    rows = original_row_ids(file_id, row_count)
    removed = {
        row_ordinal_value
        for operation in operations
        if operation.get("type") == "delete_rows"
        for row_ordinal_value in operation_row_ordinals(operation)
    }
    return [row_id for row_id in rows if row_ordinal(row_id) not in removed]


def write_row_map(path: Path, row_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps({"version": 1, "row_ids": row_ids}), encoding="utf-8")
    temporary.replace(path)


def read_row_map(path: Path, expected_count: int | None = None) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    row_ids = [str(value) for value in payload.get("row_ids", [])]
    if expected_count is not None and len(row_ids) != expected_count:
        raise ValueError("The reviewed row map does not match its dataset")
    return row_ids


def _row_id_at(row_ids: list[str], row_number: int) -> str:
    if row_number < 1 or row_number > len(row_ids):
        raise ValueError(f"Row {row_number} is outside the scan source")
    return row_ids[row_number - 1]
