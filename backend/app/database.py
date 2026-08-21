from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DATA_ROOT = Path(os.environ.get("SCRIBE_DATA_DIR", ".scribe_data")).resolve()
DB_PATH = DATA_ROOT / "scribe.sqlite3"


def configure_storage(path: Path) -> None:
    global DATA_ROOT, DB_PATH
    DATA_ROOT = path.resolve()
    DB_PATH = DATA_ROOT / "scribe.sqlite3"


def rule_signature(file_id: str, rule_type: str, parameters: dict[str, Any]) -> str:
    canonical = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{file_id}\0{rule_type}\0{canonical}".encode("utf-8")).hexdigest()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_storage() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deletion_tombstones (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_name TEXT NOT NULL,
                trashed_at TEXT NOT NULL, permanently_deleted_at TEXT NOT NULL,
                cleanup_status TEXT NOT NULL, quarantine_path TEXT
            );
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                filename TEXT NOT NULL, format TEXT NOT NULL, content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, original_path TEXT NOT NULL,
                encoding TEXT, delimiter TEXT, row_count INTEGER, column_count INTEGER,
                status TEXT NOT NULL, profile_json TEXT, warnings_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS validation_rules (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                file_id TEXT NOT NULL REFERENCES files(id), name TEXT NOT NULL,
                rule_type TEXT NOT NULL, parameters_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relationships (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                left_file_id TEXT NOT NULL REFERENCES files(id), left_column TEXT NOT NULL,
                right_file_id TEXT NOT NULL REFERENCES files(id), right_column TEXT NOT NULL,
                cardinality TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS findings (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                file_id TEXT NOT NULL REFERENCES files(id), rule_id TEXT,
                category TEXT NOT NULL, severity TEXT NOT NULL, confidence TEXT NOT NULL,
                title TEXT NOT NULL, explanation TEXT NOT NULL, table_name TEXT NOT NULL,
                column_name TEXT, row_number INTEGER, record_key TEXT, before_json TEXT,
                proposed_json TEXT, operation_json TEXT, affected_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, finding_id TEXT NOT NULL REFERENCES findings(id),
                decision TEXT NOT NULL, edited_value_json TEXT, rationale TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviewed_versions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                file_id TEXT NOT NULL REFERENCES files(id), version_number INTEGER NOT NULL,
                path TEXT NOT NULL, sha256 TEXT NOT NULL, transformation_count INTEGER NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(file_id, version_number)
            );
            CREATE TABLE IF NOT EXISTS processing_jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                file_id TEXT, job_type TEXT NOT NULL, status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS exports (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                status TEXT NOT NULL, bundle_path TEXT, error TEXT,
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS export_artifacts (
                id TEXT PRIMARY KEY, export_id TEXT NOT NULL REFERENCES exports(id),
                kind TEXT NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_runs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                file_id TEXT REFERENCES files(id), source_sha256 TEXT NOT NULL,
                reviewed_plan_hash TEXT NOT NULL DEFAULT '', engine_version TEXT NOT NULL,
                ruleset_hash TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                finding_count INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS check_results (
                id TEXT PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scan_runs(id),
                project_id TEXT NOT NULL REFERENCES projects(id), file_id TEXT REFERENCES files(id),
                section_number INTEGER NOT NULL, check_key TEXT NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL, applicability TEXT NOT NULL DEFAULT 'applicable',
                evidence_json TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(scan_id, check_key)
            );
            CREATE TABLE IF NOT EXISTS transformations (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                file_id TEXT NOT NULL REFERENCES files(id), finding_id TEXT REFERENCES findings(id),
                decision_id TEXT REFERENCES decisions(id), operation_json TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, operation_hash TEXT NOT NULL,
                status TEXT NOT NULL, engine_version TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS parsing_configs (
                file_id TEXT PRIMARY KEY REFERENCES files(id), project_id TEXT NOT NULL REFERENCES projects(id),
                version INTEGER NOT NULL, status TEXT NOT NULL, config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL, canonical_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS study_configs (
                project_id TEXT PRIMARY KEY REFERENCES projects(id), version INTEGER NOT NULL,
                status TEXT NOT NULL, config_json TEXT NOT NULL, config_hash TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS findings_project_status_idx ON findings(project_id, status, severity);
            CREATE INDEX IF NOT EXISTS files_project_idx ON files(project_id);
            CREATE INDEX IF NOT EXISTS rules_project_idx ON validation_rules(project_id, file_id);
            CREATE INDEX IF NOT EXISTS versions_file_idx ON reviewed_versions(file_id, version_number DESC);
            CREATE INDEX IF NOT EXISTS jobs_project_idx ON processing_jobs(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS exports_project_idx ON exports(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS scans_project_idx ON scan_runs(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS checks_scan_idx ON check_results(scan_id, section_number);
            CREATE INDEX IF NOT EXISTS transformations_file_idx ON transformations(file_id, status, created_at);
            CREATE INDEX IF NOT EXISTS parsing_project_idx ON parsing_configs(project_id, status);
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(findings)").fetchall()}
        if "rule_id" not in columns:
            connection.execute("ALTER TABLE findings ADD COLUMN rule_id TEXT")
        rule_columns = {row[1] for row in connection.execute("PRAGMA table_info(validation_rules)").fetchall()}
        if "signature" not in rule_columns:
            connection.execute("ALTER TABLE validation_rules ADD COLUMN signature TEXT")
        project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)").fetchall()}
        if "needs_rescan" not in project_columns:
            connection.execute("ALTER TABLE projects ADD COLUMN needs_rescan INTEGER NOT NULL DEFAULT 0")
        if "deleted_at" not in project_columns:
            connection.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT")
        finding_columns = {row[1] for row in connection.execute("PRAGMA table_info(findings)").fetchall()}
        for name, declaration in (
            ("scan_id", "TEXT"),
            ("fingerprint", "TEXT"),
            ("detector_version", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("disposition", "TEXT NOT NULL DEFAULT 'pending'"),
            ("evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("applicability", "TEXT NOT NULL DEFAULT 'applicable'"),
        ):
            if name not in finding_columns:
                connection.execute(f"ALTER TABLE findings ADD COLUMN {name} {declaration}")
        version_columns = {row[1] for row in connection.execute("PRAGMA table_info(reviewed_versions)").fetchall()}
        if "plan_hash" not in version_columns:
            connection.execute("ALTER TABLE reviewed_versions ADD COLUMN plan_hash TEXT NOT NULL DEFAULT ''")
        if "validation_json" not in version_columns:
            connection.execute("ALTER TABLE reviewed_versions ADD COLUMN validation_json TEXT NOT NULL DEFAULT '{}'")
        if "row_map_path" not in version_columns:
            connection.execute("ALTER TABLE reviewed_versions ADD COLUMN row_map_path TEXT")
        finding_columns = {row[1] for row in connection.execute("PRAGMA table_info(findings)").fetchall()}
        if "row_id" not in finding_columns:
            connection.execute("ALTER TABLE findings ADD COLUMN row_id TEXT")
        if "source_plan_hash" not in finding_columns:
            connection.execute("ALTER TABLE findings ADD COLUMN source_plan_hash TEXT NOT NULL DEFAULT ''")
        scan_columns = {row[1] for row in connection.execute("PRAGMA table_info(scan_runs)").fetchall()}
        if "source_version_id" not in scan_columns:
            connection.execute("ALTER TABLE scan_runs ADD COLUMN source_version_id TEXT")
        if "source_kind" not in scan_columns:
            connection.execute("ALTER TABLE scan_runs ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'original'")
        file_columns = {row[1] for row in connection.execute("PRAGMA table_info(files)").fetchall()}
        if "original_row_count" not in file_columns:
            connection.execute("ALTER TABLE files ADD COLUMN original_row_count INTEGER")
        if "original_column_count" not in file_columns:
            connection.execute("ALTER TABLE files ADD COLUMN original_column_count INTEGER")
        export_columns = {row[1] for row in connection.execute("PRAGMA table_info(exports)").fetchall()}
        if "kind" not in export_columns:
            connection.execute("ALTER TABLE exports ADD COLUMN kind TEXT NOT NULL DEFAULT 'review'")
        if "validation_json" not in export_columns:
            connection.execute("ALTER TABLE exports ADD COLUMN validation_json TEXT NOT NULL DEFAULT '{}'")
        seen_confirmed: set[str] = set()
        for row in connection.execute("SELECT id, file_id, rule_type, parameters_json, status FROM validation_rules ORDER BY created_at, id").fetchall():
            signature = rule_signature(row["file_id"], row["rule_type"], json.loads(row["parameters_json"]))
            if row["status"] == "confirmed" and signature in seen_confirmed:
                connection.execute("UPDATE validation_rules SET status = 'disabled', signature = ? WHERE id = ?", (signature, row["id"]))
                connection.execute("DELETE FROM findings WHERE rule_id = ? AND status = 'pending'", (row["id"],))
            else:
                connection.execute("UPDATE validation_rules SET signature = ? WHERE id = ?", (signature, row["id"]))
                if row["status"] == "confirmed":
                    seen_confirmed.add(signature)
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS rules_confirmed_signature_idx ON validation_rules(signature) WHERE status = 'confirmed'")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, datetime('now'))"
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, datetime('now'))"
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (3, datetime('now'))"
        )
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 4").fetchone() is None:
            seen_findings: set[tuple[object, ...]] = set()
            accepted = connection.execute(
                """SELECT f.id, f.rule_id, f.category, f.column_name, f.row_number, f.before_json,
                          f.proposed_json, f.operation_json, COALESCE(r.signature, f.rule_id, '') AS rule_signature
                   FROM findings f LEFT JOIN validation_rules r ON r.id = f.rule_id
                   WHERE f.status = 'accepted' AND f.rule_id IS NOT NULL
                   ORDER BY f.created_at, f.id"""
            ).fetchall()
            for finding in accepted:
                key = (
                    finding["rule_signature"], finding["category"], finding["column_name"], finding["row_number"],
                    finding["before_json"], finding["proposed_json"], finding["operation_json"],
                )
                if key in seen_findings:
                    connection.execute("UPDATE findings SET status = 'superseded' WHERE id = ?", (finding["id"],))
                else:
                    seen_findings.add(key)
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (4, datetime('now'))")
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 5").fetchone() is None:
            covered_cells: set[tuple[object, ...]] = set()
            for finding in connection.execute(
                "SELECT file_id, operation_json FROM findings WHERE status = 'accepted' AND operation_json IS NOT NULL"
            ).fetchall():
                operation = json.loads(finding["operation_json"])
                if operation.get("type") == "delete_rows":
                    continue
                for row_number in operation.get("rows", []):
                    covered_cells.add((
                        finding["file_id"], operation.get("type"), operation.get("column"), row_number,
                        json.dumps(operation.get("before"), ensure_ascii=False, sort_keys=True, default=str),
                        json.dumps(operation.get("after"), ensure_ascii=False, sort_keys=True, default=str),
                        operation.get("expected"),
                    ))
            pending = connection.execute(
                "SELECT id, file_id, operation_json FROM findings WHERE status = 'pending' AND operation_json IS NOT NULL"
            ).fetchall()
            for finding in pending:
                operation = json.loads(finding["operation_json"])
                if operation.get("type") == "delete_rows":
                    continue
                cells = {
                    (
                        finding["file_id"], operation.get("type"), operation.get("column"), row_number,
                        json.dumps(operation.get("before"), ensure_ascii=False, sort_keys=True, default=str),
                        json.dumps(operation.get("after"), ensure_ascii=False, sort_keys=True, default=str),
                        operation.get("expected"),
                    )
                    for row_number in operation.get("rows", [])
                }
                if cells and cells.issubset(covered_cells):
                    connection.execute("UPDATE findings SET status = 'superseded' WHERE id = ?", (finding["id"],))
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (5, datetime('now'))")
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 6").fetchone() is None:
            timestamp = connection.execute("SELECT datetime('now')").fetchone()[0]
            projects_with_legacy_state = connection.execute(
                """SELECT DISTINCT p.id
                   FROM projects p JOIN files fi ON fi.project_id = p.id
                   WHERE EXISTS (SELECT 1 FROM findings f WHERE f.project_id = p.id)
                      OR EXISTS (SELECT 1 FROM reviewed_versions rv WHERE rv.project_id = p.id)"""
            ).fetchall()
            for project in projects_with_legacy_state:
                project_id = project["id"]
                connection.execute("UPDATE projects SET needs_rescan = 1 WHERE id = ?", (project_id,))
                for file_row in connection.execute(
                    "SELECT id, sha256 FROM files WHERE project_id = ?", (project_id,)
                ).fetchall():
                    scan_id = f"scan_legacy_{uuid.uuid4().hex}"
                    connection.execute(
                        """INSERT INTO scan_runs(id, project_id, file_id, source_sha256, engine_version,
                           status, finding_count, created_at, completed_at)
                           VALUES (?, ?, ?, ?, 'legacy', 'superseded', 0, ?, ?)""",
                        (scan_id, project_id, file_row["id"], file_row["sha256"], timestamp, timestamp),
                    )
                    legacy_findings = connection.execute(
                        "SELECT id, operation_json, status FROM findings WHERE file_id = ? AND status != 'superseded'",
                        (file_row["id"],),
                    ).fetchall()
                    for finding in legacy_findings:
                        connection.execute(
                            """UPDATE findings SET scan_id = ?, fingerprint = ?, detector_version = 'legacy',
                               disposition = ?, status = 'superseded' WHERE id = ?""",
                            (scan_id, f"legacy:{finding['id']}", f"legacy_{finding['status']}", finding["id"]),
                        )
                        if finding["status"] == "accepted" and finding["operation_json"]:
                            operation_hash = hashlib.sha256(finding["operation_json"].encode("utf-8")).hexdigest()
                            connection.execute(
                                """INSERT INTO transformations(id, project_id, file_id, finding_id,
                                   operation_json, source_sha256, operation_hash, status, engine_version,
                                   rationale, created_at, updated_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, 'quarantined', 'legacy',
                                   'Quarantined during research-grade engine migration', ?, ?)""",
                                (f"trn_{uuid.uuid4().hex}", project_id, file_row["id"], finding["id"],
                                 finding["operation_json"], file_row["sha256"], operation_hash, timestamp, timestamp),
                            )
                connection.execute(
                    "UPDATE reviewed_versions SET status = 'legacy', validation_json = ? WHERE project_id = ? AND status = 'ready'",
                    (json.dumps({"status": "legacy", "reason": "Generated by an earlier unversioned engine"}), project_id),
                )
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS findings_scan_fingerprint_idx ON findings(scan_id, fingerprint) WHERE scan_id IS NOT NULL AND fingerprint IS NOT NULL")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS transformations_active_hash_idx ON transformations(file_id, operation_hash) WHERE status = 'active'")
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (6, datetime('now'))")
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 7").fetchone() is None:
            scoped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
            for row in connection.execute(
                "SELECT id, project_id, file_id, rule_type, parameters_json, signature FROM validation_rules WHERE status = 'confirmed' ORDER BY created_at"
            ).fetchall():
                parameters = json.loads(row["parameters_json"])
                if row["rule_type"] == "cross_column":
                    scope = f"{parameters.get('when_column')}->{parameters.get('then_column')}"
                else:
                    scope = str(parameters.get("column", ""))
                scoped.setdefault((row["file_id"], row["rule_type"], scope), []).append(row)
            for (_, _, scope), rows in scoped.items():
                if len({row["signature"] for row in rows}) <= 1:
                    continue
                project_id = rows[0]["project_id"]
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                connection.execute(f"UPDATE validation_rules SET status = 'disabled' WHERE id IN ({placeholders})", ids)
                connection.execute(f"UPDATE findings SET status = 'superseded', disposition = 'superseded' WHERE rule_id IN ({placeholders})", ids)
                connection.execute(
                    "INSERT INTO audit_events(id, project_id, event_type, payload_json, created_at) VALUES (?, ?, 'conflicting_rules_quarantined', ?, datetime('now'))",
                    (f"evt_{uuid.uuid4().hex}", project_id, json.dumps({"scope": scope, "rule_ids": ids})),
                )
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (7, datetime('now'))")
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 8").fetchone() is None:
            unsafe_name_rules: list[sqlite3.Row] = []
            for row in connection.execute(
                "SELECT id, project_id, parameters_json FROM validation_rules WHERE status = 'confirmed' AND rule_type = 'unique'"
            ).fetchall():
                parameters = json.loads(row["parameters_json"])
                normalized = " ".join(str(parameters.get("column", "")).replace("_", " ").replace("-", " ").casefold().split())
                if normalized in {"name", "full name", "patient name", "participant name", "respondent name"} and not str(parameters.get("override_rationale", "")).strip():
                    unsafe_name_rules.append(row)
            for row in unsafe_name_rules:
                connection.execute("UPDATE validation_rules SET status = 'disabled' WHERE id = ?", (row["id"],))
                connection.execute("UPDATE findings SET status = 'superseded', disposition = 'superseded' WHERE rule_id = ?", (row["id"],))
                connection.execute(
                    "INSERT INTO audit_events(id, project_id, event_type, payload_json, created_at) VALUES (?, ?, 'unsafe_name_uniqueness_rule_quarantined', ?, datetime('now'))",
                    (f"evt_{uuid.uuid4().hex}", row["project_id"], json.dumps({"rule_id": row["id"], "reason": "Names are not reliable participant identifiers"})),
                )
                connection.execute("UPDATE projects SET needs_rescan = 1 WHERE id = ?", (row["project_id"],))
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (8, datetime('now'))")
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 9").fetchone() is None:
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (9, datetime('now'))")


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value) if value is not None else None
    return result


def rows_dict(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [item for row in rows if (item := row_dict(row)) is not None]
