from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import database as db


ACTIVE_JOB_STATES = ("queued", "processing", "running")


def move_to_trash(connection: Any, project_id: str, timestamp: str) -> None:
    project = _project(connection, project_id)
    if project["deleted_at"]:
        raise HTTPException(409, "Project is already in Trash")
    _reject_active_jobs(connection, project_id)
    connection.execute("UPDATE projects SET deleted_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, project_id))


def restore_from_trash(connection: Any, project_id: str, timestamp: str) -> None:
    project = _project(connection, project_id)
    if not project["deleted_at"]:
        raise HTTPException(409, "Only a trashed project can be restored")
    _reject_active_jobs(connection, project_id)
    connection.execute("UPDATE projects SET deleted_at = NULL, updated_at = ? WHERE id = ?", (timestamp, project_id))


def permanently_delete(project_id: str, confirmed_name: str) -> dict[str, str]:
    with db.connect() as connection:
        project = _project(connection, project_id)
        if not project["deleted_at"]:
            raise HTTPException(409, "Move the project to Trash before permanently deleting it")
        if confirmed_name != project["name"]:
            raise HTTPException(422, "Project name confirmation does not match exactly")
        _reject_active_jobs(connection, project_id)
        project_name = project["name"]

    project_directory = _safe_project_directory(project_id)
    quarantine_root = (db.DATA_ROOT / ".quarantine").resolve()
    quarantine_root.mkdir(parents=True, exist_ok=True)
    quarantine = quarantine_root / f"{project_id}.{uuid.uuid4().hex}"
    moved = False
    if project_directory.exists():
        project_directory.replace(quarantine)
        moved = True

    timestamp = datetime.now(UTC).isoformat()
    try:
        with db.connect() as connection:
            project = _project(connection, project_id)
            if not project["deleted_at"] or project["name"] != confirmed_name:
                raise HTTPException(409, "Project lifecycle changed while deletion was being prepared")
            _reject_active_jobs(connection, project_id)
            _delete_dependencies(connection, project_id)
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            connection.execute(
                """INSERT INTO deletion_tombstones(id, project_id, project_name, trashed_at,
                   permanently_deleted_at, cleanup_status, quarantine_path)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (f"del_{uuid.uuid4().hex}", project_id, project_name, project["deleted_at"], timestamp, str(quarantine) if moved else None),
            )
    except Exception:
        if moved and quarantine.exists() and not project_directory.exists():
            quarantine.replace(project_directory)
        raise

    cleanup_status = "complete"
    if moved:
        try:
            shutil.rmtree(quarantine)
        except OSError:
            cleanup_status = "quarantined_cleanup_required"
    with db.connect() as connection:
        connection.execute(
            "UPDATE deletion_tombstones SET cleanup_status = ?, quarantine_path = CASE WHEN ? = 'complete' THEN NULL ELSE quarantine_path END WHERE project_id = ?",
            (cleanup_status, cleanup_status, project_id),
        )
    return {"project_id": project_id, "status": "permanently_deleted", "cleanup_status": cleanup_status}


def _project(connection: Any, project_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return dict(row)


def _reject_active_jobs(connection: Any, project_id: str) -> None:
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
    active = connection.execute(
        f"SELECT 1 FROM processing_jobs WHERE project_id = ? AND status IN ({placeholders}) LIMIT 1",
        (project_id, *ACTIVE_JOB_STATES),
    ).fetchone()
    if active:
        raise HTTPException(409, "Wait for active project processing to finish before changing its lifecycle")


def _safe_project_directory(project_id: str) -> Path:
    expected_parent = (db.DATA_ROOT / "projects").resolve()
    path = (expected_parent / project_id).resolve()
    if path.parent != expected_parent or path.name != project_id:
        raise HTTPException(400, "Unsafe project storage path")
    return path


def _delete_dependencies(connection: Any, project_id: str) -> None:
    connection.execute("DELETE FROM export_artifacts WHERE export_id IN (SELECT id FROM exports WHERE project_id = ?)", (project_id,))
    connection.execute("DELETE FROM check_results WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM transformations WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM decisions WHERE finding_id IN (SELECT id FROM findings WHERE project_id = ?)", (project_id,))
    connection.execute("DELETE FROM findings WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM relationships WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM validation_rules WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM reviewed_versions WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM processing_jobs WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM exports WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM audit_events WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM scan_runs WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM parsing_configs WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM study_configs WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM files WHERE project_id = ?", (project_id,))
