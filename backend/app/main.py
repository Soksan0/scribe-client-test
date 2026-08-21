from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import certifi
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import database as db
from .exporting import (
    apply_operations,
    apply_statistical_operations,
    apply_xlsx_operations,
    coalesce_operations,
    generate_format_r_script,
    values_equivalent,
)
from .formats import build_canonical_snapshot, load_frame, preview_records, profile_dataset
from .lineage import (
    attach_operation_row_ids,
    attach_profile_row_ids,
    original_row_ids,
    read_row_map,
    reviewed_row_ids,
    row_ordinal,
    write_row_map,
)
from .profiling import AGE_PATTERN, CATEGORICAL_PATTERN, SCALE_PATTERN, is_missing_value, is_valid_blood_pressure, is_valid_email, is_valid_phone, parse_date_with_quality, parse_number_word
from .project_lifecycle import move_to_trash, permanently_delete, restore_from_trash
from .survey_quality import detect_survey_quality
from .validation import validate_format_contract, validate_operation_result

MAX_UPLOAD_BYTES = 250 * 1024 * 1024
ENGINE_VERSION = "research-grade-1"
DETECTOR_VERSION = "2026.07"


def load_local_environment() -> None:
    if os.environ.get("SCRIBE_SKIP_DOTENV") == "1":
        return
    root = Path(__file__).resolve().parents[2]
    for filename in (".env", ".env.local"):
        path = root / filename
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
                value = value[1:-1]
            if key and not os.environ.get(key):
                os.environ[key] = value


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def persisted_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "columns": profile["columns"],
        "candidate_id_columns": profile.get("candidate_id_columns", []),
        "table_name": profile["table_name"],
        "format_metadata": profile.get("format_metadata", {}),
        "schema_fingerprint": profile.get("schema_fingerprint"),
        "encoding_confidence": profile.get("encoding_confidence"),
        "delimiter_confidence": profile.get("delimiter_confidence"),
        "parsing_config": profile.get("parsing_config", {}),
    }


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)


class ProjectUpdate(ProjectCreate):
    pass


class ProjectDeleteConfirm(BaseModel):
    project_name: str = Field(min_length=1, max_length=160)


class DecisionCreate(BaseModel):
    decision: Literal["accepted", "rejected"]
    edited_value: Any | None = None
    rationale: str = Field(default="", max_length=1000)
    retained_row_id: str | None = None
    removed_row_ids: list[str] | None = Field(default=None, max_length=500)


class BatchDecisionCreate(BaseModel):
    finding_ids: list[str] = Field(min_length=1, max_length=200)
    decision: Literal["accepted", "rejected"]


class FindingDispositionCreate(BaseModel):
    disposition: Literal["acknowledged", "false_positive", "deferred"]
    rationale: str = Field(min_length=1, max_length=2000)


class ManualOperationCreate(BaseModel):
    file_id: str
    kind: Literal["cell_correction", "exclude_row", "exclude_column"]
    source_finding_id: str | None = None
    row_id: str | None = None
    column: str | None = None
    before: Any | None = None
    after: Any | None = None
    rationale: str = Field(min_length=3, max_length=2000)
    evidence: str = Field(min_length=3, max_length=4000)


class ParsingConfigUpdate(BaseModel):
    header_row: int = Field(default=1, ge=1, le=100)
    delimiter: Literal[",", "\t", ";", "|"] | None = None
    encoding: str | None = Field(default=None, max_length=80)
    date_locale: Literal["day_first", "month_first", "year_first"] | None = None
    missing_tokens: list[str] = Field(default_factory=list, max_length=100)
    identifier_columns: list[str] = Field(default_factory=list, max_length=20)
    variable_labels: dict[str, str] = Field(default_factory=dict)


class StudyConfigUpdate(BaseModel):
    participant_keys: list[str] = Field(default_factory=list, max_length=20)
    allowed_repeats: bool = False
    item_groups: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    timestamp_columns: dict[str, str] = Field(default_factory=dict)
    completion: dict[str, Any] = Field(default_factory=dict)
    attention_checks: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    skip_rules: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    missing_codes: dict[str, list[Any]] = Field(default_factory=dict)
    cross_field_rules: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class ExportCreate(BaseModel):
    kind: Literal["review", "verified"] = "review"


class AssistantRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    file_id: str | None = None
    consent_to_send_data: bool = False


class RuleCreate(BaseModel):
    file_id: str
    name: str = Field(min_length=1, max_length=160)
    rule_type: Literal["unique", "composite_unique", "required", "missing_codes", "allowed_values", "range", "scale", "pattern", "type", "date", "cross_column"]
    parameters: dict[str, Any]


class RuleUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    parameters: dict[str, Any]


class RelationshipCreate(BaseModel):
    left_file_id: str
    left_column: str
    right_file_id: str
    right_column: str
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many"] = "many_to_one"


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_local_environment()
    db.ensure_storage()
    yield


app = FastAPI(title="Scribe local API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["content-disposition"],
)

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "privacy": "local-only", "service": "scribe", "version": "0.2.0"}


@app.get("/api/system/r-status")
def r_status() -> dict[str, Any]:
    executable = shutil.which("Rscript")
    required_packages = ["readr", "dplyr", "openxlsx", "haven"]
    if not executable:
        return {
            "available": False,
            "ready": False,
            "version": None,
            "missing_packages": required_packages,
            "message": "Rscript is not installed or is not available on PATH.",
        }
    try:
        version_result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
        version = (version_result.stdout or version_result.stderr).strip().splitlines()[0][:200]
        package_expression = "required <- c('readr','dplyr','openxlsx','haven'); cat(paste(required[!vapply(required, requireNamespace, logical(1), quietly=TRUE)], collapse=','))"
        package_result = subprocess.run([executable, "-e", package_expression], capture_output=True, text=True, timeout=30)
        if package_result.returncode != 0:
            raise RuntimeError((package_result.stderr or package_result.stdout or "R package check failed").strip())
        missing = [value for value in package_result.stdout.strip().split(",") if value]
        return {
            "available": True,
            "ready": not missing,
            "version": version,
            "missing_packages": missing,
            "message": "R is ready for verified exports." if not missing else f"Install the missing R packages: {', '.join(missing)}.",
        }
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return {"available": True, "ready": False, "version": None, "missing_packages": required_packages, "message": f"R diagnostics failed: {str(error)[:300]}"}


@app.get("/api/assistant/status")
def assistant_status() -> dict[str, Any]:
    load_local_environment()
    configured = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    return {"provider": "gemini", "configured": configured, "state": "configured_unverified" if configured else "not_configured", "model": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"), "data_leaves_device": True, "key_storage": "environment_only"}


@app.post("/api/assistant/test")
def assistant_connection_test() -> dict[str, Any]:
    load_local_environment()
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    if not api_key:
        raise HTTPException(503, "Gemini is not configured. Add GEMINI_API_KEY to .env and restart Scribe.")
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
        headers={"x-goog-api-key": api_key},
    )
    try:
        tls_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=20, context=tls_context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        state = "invalid_key" if exc.code in {401, 403} else "unavailable_model" if exc.code == 404 else "provider_error"
        raise HTTPException(502, f"Gemini connection failed ({state}, HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Gemini connection failed (network): {exc}") from exc
    actions = payload.get("supportedGenerationMethods") or payload.get("supportedActions") or []
    if actions and "generateContent" not in actions:
        raise HTTPException(422, f"Configured model {model!r} does not support generateContent")
    return {"provider": "gemini", "configured": True, "state": "ready", "model": model, "supports_generate_content": True}


@app.post("/api/projects/{project_id}/assistant/propose")
def assistant_propose(project_id: str, payload: AssistantRequest) -> dict[str, Any]:
    load_local_environment()
    if not payload.consent_to_send_data:
        raise HTTPException(422, "Confirm that selected dataset context may be sent to Google Gemini for this request")
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(503, "Gemini is not configured. Set GEMINI_API_KEY before starting Scribe.")
    ensure_project(project_id)
    with db.connect() as connection:
        if payload.file_id:
            records = [file_record(connection, project_id, payload.file_id)]
        else:
            records = db.rows_dict(connection.execute("SELECT * FROM files WHERE project_id = ? ORDER BY created_at LIMIT 3", (project_id,)).fetchall())
        if not records:
            raise HTTPException(422, "Upload a dataset before asking for custom cleaning proposals")
        context_files = []
        for item in records:
            preview = preview_records(Path(item["original_path"]), item["format"], item.get("profile"), 0, 25)
            context_files.append({"file_id": item["id"], "filename": item["filename"], "profile": item.get("profile"), "sample_rows": preview["rows"], "sample_limit": 25, "total_rows": preview["total"]})
    prompt = {
        "role": "You are Scribe's cautious research-data QA assistant. Use only the supplied uploaded-dataset context. Never invent rows, columns, or values. Do not claim a full dataset was inspected when only a sample was supplied. Propose only exact cell replacements that a researcher can review. If evidence is insufficient, explain uncertainty and return no proposal.",
        "question": payload.question,
        "datasets": context_files,
        "allowed_operation_types": ["replace", "trim", "normalize_missing", "map_category", "parse_type", "parse_date"],
    }
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "explanation": {"type": "string"},
                        "file_id": {"type": "string"},
                        "column": {"type": "string"},
                        "row_number": {"type": "integer"},
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                        "operation_type": {"type": "string", "enum": ["replace", "trim", "normalize_missing", "map_category", "parse_type", "parse_date"]},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["title", "explanation", "file_id", "column", "row_number", "before", "after", "operation_type", "confidence"],
                },
            },
        },
        "required": ["answer", "proposals"],
    }
    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    request_body = json.dumps({"contents": [{"parts": [{"text": json_dump(prompt)}]}], "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}}).encode("utf-8")
    request = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=request_body, method="POST", headers={"content-type": "application/json", "x-goog-api-key": api_key})
    try:
        tls_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=45, context=tls_context) as response:
            raw = json.loads(response.read().decode("utf-8"))
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        assistant = json.loads(text)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise HTTPException(502, f"Gemini request failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Gemini did not return a usable structured response: {exc}") from exc
    created = []
    sources = []
    with db.connect() as connection:
        for proposal in assistant.get("proposals", [])[:25]:
            try:
                item = file_record(connection, project_id, proposal["file_id"])
                frame = load_frame(Path(item["original_path"]), item["format"], item.get("profile"))
                column, row_number = proposal["column"], int(proposal["row_number"])
                if column not in frame.columns or row_number < 1 or row_number > len(frame):
                    continue
                actual = frame.iloc[row_number - 1][column]
                if str(actual) != str(proposal["before"]):
                    continue
                after: Any = proposal["after"]
                if proposal["operation_type"] == "parse_type":
                    try: after = int(after)
                    except ValueError:
                        try: after = float(after)
                        except ValueError: continue
                operation = {"type": proposal["operation_type"], "column": column, "before": actual, "after": after, "rows": [row_number]}
                finding_id = insert_finding(connection, project_id, item["id"], None, "ai_custom", "medium", "needs_confirmation", proposal["title"][:200], f"Gemini proposal: {proposal['explanation']} This was checked against the immutable original but still requires researcher review.", (item.get("profile") or {}).get("table_name", Path(item["filename"]).stem), column, row_number, actual, after, operation)
                created.append(finding_id)
                sources.append({"file_id": item["id"], "filename": item["filename"], "table": (item.get("profile") or {}).get("table_name"), "column": column, "row_number": row_number, "finding_id": finding_id})
            except (KeyError, TypeError, ValueError):
                continue
        audit(connection, project_id, "gemini_proposals_created", {"finding_ids": created, "source_count": len(sources), "model": model})
    return {"answer": assistant.get("answer", "Gemini returned no explanation."), "proposal_count": len(created), "finding_ids": created, "sources": sources, "model": model, "confidence": "external_suggestion_requires_review", "data_sent": {"files": [item["filename"] for item in records], "sample_rows_per_file": 25}}


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    project_id, timestamp = new_id("prj"), now()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO projects(id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, payload.name.strip(), payload.description.strip(), timestamp, timestamp),
        )
        audit(connection, project_id, "project_created", {"name": payload.name.strip()})
    project_root(project_id).mkdir(parents=True, exist_ok=True)
    return get_project(project_id)


@app.get("/api/projects")
def list_projects(status: Literal["active", "trash"] = "active") -> list[dict[str, Any]]:
    with db.connect() as connection:
        clause = "deleted_at IS NULL" if status == "active" else "deleted_at IS NOT NULL"
        projects = db.rows_dict(connection.execute(f"SELECT * FROM projects WHERE {clause} ORDER BY updated_at DESC").fetchall())
        for project in projects:
            project.update(project_summary(connection, project["id"]))
        return projects


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        project = db.row_dict(connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
        if project is None:
            raise HTTPException(404, "Project not found")
        project.update(project_summary(connection, project_id))
        project["recent_activity"] = db.rows_dict(
            connection.execute("SELECT * FROM audit_events WHERE project_id = ? ORDER BY created_at DESC LIMIT 10", (project_id,)).fetchall()
        )
        return project


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate) -> dict[str, Any]:
    ensure_project(project_id)
    with db.connect() as connection:
        if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise HTTPException(404, "Project not found")
        connection.execute("UPDATE projects SET name = ?, description = ?, updated_at = ? WHERE id = ?", (payload.name.strip(), payload.description.strip(), now(), project_id))
        audit(connection, project_id, "project_updated", {"name": payload.name.strip()})
    return get_project(project_id)


@app.post("/api/projects/{project_id}/trash")
def trash_project(project_id: str) -> dict[str, Any]:
    timestamp = now()
    with db.connect() as connection:
        move_to_trash(connection, project_id, timestamp)
        audit(connection, project_id, "project_moved_to_trash", {})
    return get_project(project_id)


@app.post("/api/projects/{project_id}/restore")
def restore_project(project_id: str) -> dict[str, Any]:
    timestamp = now()
    with db.connect() as connection:
        restore_from_trash(connection, project_id, timestamp)
        audit(connection, project_id, "project_restored", {})
    return get_project(project_id)


@app.delete("/api/projects/{project_id}")
def delete_project_permanently(project_id: str, payload: ProjectDeleteConfirm) -> dict[str, str]:
    return permanently_delete(project_id, payload.project_name)


@app.post("/api/projects/{project_id}/files", status_code=201)
async def upload_file(project_id: str, request: Request, filename: str = Query(min_length=1, max_length=255)) -> dict[str, Any]:
    ensure_project(project_id)
    normalized_filename = filename.replace("\\", "/")
    if normalized_filename != Path(normalized_filename).name or normalized_filename in {".", ".."} or "\x00" in normalized_filename:
        raise HTTPException(400, "The filename must not contain a directory path")
    filename = normalized_filename
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".tsv", ".xlsx", ".sav", ".dta", ".rds"}:
        raise HTTPException(415, "Supported formats are CSV, TSV, XLSX, SAV, DTA, and RDS")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Dataset exceeds Scribe's 250 MB local upload limit")
        except ValueError as error:
            raise HTTPException(400, "Invalid Content-Length header") from error
    file_id = new_id("file")
    destination = project_root(project_id) / "originals" / f"{file_id}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.stem}.partial{destination.suffix}")
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size_bytes += len(chunk)
                if size_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Dataset exceeds Scribe's 250 MB local upload limit")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if size_bytes == 0:
        temporary.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file is empty")
    sha256 = digest.hexdigest()
    with db.connect() as connection:
        duplicate = db.row_dict(connection.execute(
            "SELECT * FROM files WHERE project_id = ? AND sha256 = ? LIMIT 1",
            (project_id, sha256),
        ).fetchone())
    if duplicate:
        temporary.unlink(missing_ok=True)
        raise HTTPException(409, f"This exact dataset is already uploaded as {duplicate['filename']}")
    temporary.replace(destination)
    timestamp = now()
    job_id = new_id("job")
    with db.connect() as connection:
        connection.execute("INSERT INTO processing_jobs(id, project_id, file_id, job_type, status, progress, created_at) VALUES (?, ?, ?, 'profile', 'processing', 10, ?)", (job_id, project_id, file_id, timestamp))
    try:
        profile = profile_dataset(destination, filename)
        default_parsing = {
            "header_row": 1,
            "delimiter": profile.get("delimiter"),
            "encoding": profile.get("encoding"),
            "date_locale": None,
            "missing_tokens": ["", "N/A", "null", "missing", "."],
            "identifier_columns": profile.get("candidate_id_columns", []),
            "variable_labels": {},
        }
        profile["parsing_config"] = default_parsing
        attach_profile_row_ids(profile, original_row_ids(file_id, profile["row_count"]))
    except Exception as exc:
        destination.unlink(missing_ok=True)
        with db.connect() as connection:
            connection.execute("UPDATE processing_jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?", (str(exc), now(), job_id))
        raise HTTPException(422, f"Scribe could not read this dataset: {exc}") from exc
    canonical_path = project_root(project_id) / "canonical" / f"{file_id}.parquet"
    try:
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO files(id, project_id, filename, format, content_type, size_bytes, sha256,
                original_path, encoding, delimiter, row_count, column_count, status, profile_json,
                warnings_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_id, project_id, filename, suffix[1:], request.headers.get("content-type", "application/octet-stream"), size_bytes, sha256, str(destination), profile.get("encoding"), profile.get("delimiter"), profile["row_count"], profile["column_count"], "ready", json_dump(persisted_profile(profile)), json_dump(profile["warnings"]), timestamp),
            )
            connection.execute("UPDATE files SET original_row_count = ?, original_column_count = ? WHERE id = ?", (profile["row_count"], profile["column_count"], file_id))
            config_canonical = json.dumps(default_parsing, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            config_hash = hashlib.sha256(config_canonical.encode("utf-8")).hexdigest()
            parsing_status = "confirmed" if not profile.get("warnings") else "inferred"
            snapshot_item = db.row_dict(connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone())
            assert snapshot_item is not None
            snapshot = build_canonical_snapshot(snapshot_item, canonical_path)
            connection.execute(
                """INSERT INTO parsing_configs(file_id, project_id, version, status, config_json, config_hash,
                canonical_path, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                (file_id, project_id, parsing_status, config_canonical, config_hash, snapshot["path"], timestamp, timestamp),
            )
            scan_id = create_scan_record(connection, project_id, file_id, sha256, profile)
            persist_candidates(connection, project_id, file_id, profile, filename, scan_id)
            persist_check_results(connection, project_id, file_id, scan_id, profile)
            connection.execute("UPDATE processing_jobs SET status = 'complete', progress = 100, completed_at = ? WHERE id = ?", (now(), job_id))
            connection.execute("UPDATE projects SET updated_at = ?, needs_rescan = 0 WHERE id = ?", (timestamp, project_id))
            audit(connection, project_id, "file_uploaded", {"file_id": file_id, "filename": filename, "sha256": sha256, "finding_count": len(profile["findings"])})
    except Exception as exc:
        canonical_path.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        with db.connect() as connection:
            connection.execute("UPDATE processing_jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?", (str(exc), now(), job_id))
        raise HTTPException(422, f"Scribe could not safely finish importing this dataset: {exc}") from exc
    return get_file(project_id, file_id)


@app.get("/api/projects/{project_id}/processing-jobs")
def list_processing_jobs(project_id: str, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    ensure_project(project_id)
    with db.connect() as connection:
        return db.rows_dict(connection.execute("SELECT * FROM processing_jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?", (project_id, limit)).fetchall())


@app.get("/api/projects/{project_id}/scans")
def list_scans(project_id: str, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    ensure_project(project_id)
    with db.connect() as connection:
        return db.rows_dict(connection.execute(
            "SELECT * FROM scan_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall())


@app.get("/api/projects/{project_id}/scans/current")
def current_scans(project_id: str) -> list[dict[str, Any]]:
    ensure_project(project_id)
    with db.connect() as connection:
        scans = db.rows_dict(connection.execute(
            "SELECT * FROM scan_runs WHERE project_id = ? AND status = 'complete' ORDER BY created_at DESC",
            (project_id,),
        ).fetchall())
        for scan in scans:
            scan["checks"] = db.rows_dict(connection.execute(
                "SELECT * FROM check_results WHERE scan_id = ? ORDER BY section_number", (scan["id"],)
            ).fetchall())
        return scans


@app.get("/api/projects/{project_id}/files/{file_id}/processing-status")
def file_processing_status(project_id: str, file_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        file_record(connection, project_id, file_id)
        job = db.row_dict(connection.execute("SELECT * FROM processing_jobs WHERE project_id = ? AND file_id = ? ORDER BY created_at DESC LIMIT 1", (project_id, file_id)).fetchone())
        return job or {"file_id": file_id, "status": "ready", "progress": 100}


@app.get("/api/projects/{project_id}/files")
def list_files(project_id: str) -> list[dict[str, Any]]:
    ensure_project(project_id)
    with db.connect() as connection:
        files = db.rows_dict(connection.execute("SELECT * FROM files WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall())
        for item in files:
            version = latest_version(connection, item["id"])
            item["reviewed_version"] = version
            item["parsing"] = db.row_dict(connection.execute("SELECT * FROM parsing_configs WHERE file_id = ?", (item["id"],)).fetchone())
            item["finding_count"] = connection.execute("SELECT COUNT(*) FROM findings WHERE file_id = ? AND status != 'superseded'", (item["id"],)).fetchone()[0]
        return files


@app.get("/api/projects/{project_id}/files/{file_id}")
def get_file(project_id: str, file_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        item = file_record(connection, project_id, file_id)
        item["reviewed_version"] = latest_version(connection, file_id)
        item["parsing"] = db.row_dict(connection.execute("SELECT * FROM parsing_configs WHERE file_id = ?", (file_id,)).fetchone())
        item["finding_count"] = connection.execute("SELECT COUNT(*) FROM findings WHERE file_id = ? AND status != 'superseded'", (file_id,)).fetchone()[0]
        return item


@app.get("/api/projects/{project_id}/files/{file_id}/parsing-config")
def get_parsing_config(project_id: str, file_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        file_record(connection, project_id, file_id)
        config = db.row_dict(connection.execute("SELECT * FROM parsing_configs WHERE file_id = ?", (file_id,)).fetchone())
        if config is None:
            raise HTTPException(404, "Parsing configuration is not available for this legacy dataset")
        return config


@app.put("/api/projects/{project_id}/files/{file_id}/parsing-config")
def update_parsing_config(project_id: str, file_id: str, payload: ParsingConfigUpdate) -> dict[str, Any]:
    with db.connect() as connection:
        item = file_record(connection, project_id, file_id)
        if connection.execute("SELECT 1 FROM transformations WHERE file_id = ? AND status = 'active' LIMIT 1", (file_id,)).fetchone():
            raise HTTPException(409, "Undo accepted transformations before changing how the original file is parsed")
        config = payload.model_dump()
        profile = profile_dataset(Path(item["original_path"]), item["filename"], config)
        available_columns = {str(column["name"]) for column in profile.get("columns", [])}
        unknown_ids = sorted(set(payload.identifier_columns) - available_columns)
        unknown_labels = sorted(set(payload.variable_labels) - available_columns)
        if unknown_ids or unknown_labels:
            raise HTTPException(422, f"Parsing configuration refers to unknown columns: {unknown_ids + unknown_labels}")
        profile["candidate_id_columns"] = list(payload.identifier_columns)
        profile["parsing_config"] = config
        attach_profile_row_ids(profile, original_row_ids(file_id, profile["row_count"]))
        connection.execute("UPDATE findings SET status = 'superseded', disposition = 'superseded' WHERE file_id = ? AND status != 'superseded'", (file_id,))
        connection.execute(
            """UPDATE files SET encoding = ?, delimiter = ?, row_count = ?, column_count = ?, original_row_count = ?,
               original_column_count = ?, profile_json = ?, warnings_json = ?, status = 'ready' WHERE id = ?""",
            (profile.get("encoding"), profile.get("delimiter"), profile["row_count"], profile["column_count"], profile["row_count"], profile["column_count"], json_dump(persisted_profile(profile)), json_dump(profile["warnings"]), file_id),
        )
        canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        previous = connection.execute("SELECT version, created_at FROM parsing_configs WHERE file_id = ?", (file_id,)).fetchone()
        version = int(previous["version"]) + 1 if previous else 1
        canonical_path = project_root(project_id) / "canonical" / f"{file_id}.parquet"
        updated_item = db.row_dict(connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone())
        assert updated_item is not None
        snapshot = build_canonical_snapshot(updated_item, canonical_path)
        connection.execute(
            """INSERT INTO parsing_configs(file_id, project_id, version, status, config_json, config_hash, canonical_path, created_at, updated_at)
               VALUES (?, ?, ?, 'confirmed', ?, ?, ?, ?, ?)
               ON CONFLICT(file_id) DO UPDATE SET version=excluded.version, status='confirmed', config_json=excluded.config_json,
               config_hash=excluded.config_hash, canonical_path=excluded.canonical_path, updated_at=excluded.updated_at""",
            (file_id, project_id, version, canonical, config_hash, snapshot["path"], previous["created_at"] if previous else now(), now()),
        )
        scan_id = create_scan_record(connection, project_id, file_id, item["sha256"], profile)
        persist_candidates(connection, project_id, file_id, profile, item["filename"], scan_id)
        persist_check_results(connection, project_id, file_id, scan_id, profile)
        audit(connection, project_id, "parsing_config_confirmed", {"file_id": file_id, "version": version, "config_hash": config_hash})
        connection.execute("UPDATE projects SET needs_rescan = 0, updated_at = ? WHERE id = ?", (now(), project_id))
        return {"file_id": file_id, "version": version, "status": "confirmed", "config": config, "config_hash": config_hash, "canonical_path": snapshot["path"], "profile": persisted_profile(profile)}


@app.get("/api/projects/{project_id}/study-config")
def get_study_config(project_id: str) -> dict[str, Any]:
    ensure_project(project_id)
    with db.connect() as connection:
        config = db.row_dict(connection.execute("SELECT * FROM study_configs WHERE project_id = ?", (project_id,)).fetchone())
        return config or {"project_id": project_id, "version": 0, "status": "not_configured", "config": {}}


@app.put("/api/projects/{project_id}/study-config")
def update_study_config(project_id: str, payload: StudyConfigUpdate) -> dict[str, Any]:
    ensure_project(project_id)
    config = payload.model_dump()
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with db.connect() as connection:
        available_columns = {
            str(column["name"])
            for file_item in db.rows_dict(connection.execute("SELECT profile_json FROM files WHERE project_id = ?", (project_id,)).fetchall())
            for column in (file_item.get("profile") or {}).get("columns", [])
        }
        referenced = set(payload.participant_keys)
        referenced.update(column for group in payload.item_groups for column in group.get("columns", []))
        referenced.update(
            value
            for key, value in payload.timestamp_columns.items()
            if key in {"start", "end", "duration"} and isinstance(value, str) and value
        )
        referenced.update(payload.completion.get("required_columns", []))
        referenced.update(check.get("column") for check in payload.attention_checks if check.get("column"))
        for rule in [*payload.skip_rules, *payload.cross_field_rules]:
            referenced.update(value for key, value in rule.items() if key.endswith("_column") and value)
        unknown = sorted(referenced - available_columns)
        if unknown:
            raise HTTPException(422, f"Study configuration refers to unknown columns: {unknown}")
        invalid_operators = sorted({str(rule.get("operator")) for rule in payload.cross_field_rules if rule.get("operator") not in {"==", "!=", "<", "<=", ">", ">="}})
        if invalid_operators:
            raise HTTPException(422, f"Unsupported cross-field operators: {invalid_operators}")
        previous = connection.execute("SELECT version, created_at FROM study_configs WHERE project_id = ?", (project_id,)).fetchone()
        version = int(previous["version"]) + 1 if previous else 1
        timestamp = now()
        connection.execute(
            """INSERT INTO study_configs(project_id, version, status, config_json, config_hash, created_at, updated_at)
               VALUES (?, ?, 'confirmed', ?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET version=excluded.version, status='confirmed', config_json=excluded.config_json,
               config_hash=excluded.config_hash, updated_at=excluded.updated_at""",
            (project_id, version, canonical, digest, previous["created_at"] if previous else timestamp, timestamp),
        )
        connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (timestamp, project_id))
        audit(connection, project_id, "study_config_confirmed", {"version": version, "config_hash": digest})
    return {"project_id": project_id, "version": version, "status": "confirmed", "config": config, "config_hash": digest}


@app.get("/api/projects/{project_id}/files/{file_id}/preview")
def preview_file(project_id: str, file_id: str, version: Literal["original", "reviewed"] = "reviewed", offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    with db.connect() as connection:
        item = file_record(connection, project_id, file_id)
        path = Path(item["original_path"])
        active_version = latest_version(connection, file_id)
        if version == "reviewed" and active_version:
            path = Path(active_version["path"])
        result = preview_records(path, item["format"], item.get("profile"), offset, limit)
        result.update({"file_id": file_id, "filename": item["filename"], "version": "reviewed" if path != Path(item["original_path"]) else "original", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return result


@app.get("/api/projects/{project_id}/rules/suggestions")
def rule_suggestions(project_id: str) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    with db.connect() as connection:
        existing = {(row[0], row[1], row[2]) for row in connection.execute("SELECT file_id, rule_type, parameters_json FROM validation_rules WHERE project_id = ? AND status = 'confirmed'", (project_id,)).fetchall()}
    def add(item: dict[str, Any]) -> None:
        signature = (item["file_id"], item["rule_type"], json_dump(item["parameters"]))
        if signature not in existing:
            suggestions.append(item)
    for item in list_files(project_id):
        profile = item.get("profile") or {}
        for column in profile.get("columns", []):
            if column.get("candidate_id"):
                add({"file_id": item["id"], "filename": item["filename"], "name": f"{column['name']} must be unique", "rule_type": "unique", "parameters": {"column": column["name"]}, "reason": "The column name and values resemble an identifier.", "recommended": True})
                add({"file_id": item["id"], "filename": item["filename"], "name": f"{column['name']} is required", "rule_type": "required", "parameters": {"column": column["name"]}, "reason": "Identifiers should not be blank.", "recommended": True})
            if column.get("inferred_type") not in {"text", "empty"}:
                inferred = str(column["inferred_type"]).removeprefix("mostly_")
                add({"file_id": item["id"], "filename": item["filename"], "name": f"{column['name']} should be {inferred}", "rule_type": "date" if inferred == "date" else "type", "parameters": {"column": column["name"], "expected": inferred}, "reason": f"{round(column.get('type_confidence', 0) * 100)}% of non-missing values already match this type; exact number words are separately proposed for conversion.", "recommended": column.get("type_confidence", 0) >= 0.75 or (AGE_PATTERN.search(column["name"]) and inferred in {"integer", "number"})})
            if AGE_PATTERN.search(column["name"]):
                add({"file_id": item["id"], "filename": item["filename"], "name": f"{column['name']} should be between 0 and 120", "rule_type": "range", "parameters": {"column": column["name"], "minimum": 0, "maximum": 120}, "reason": "The column name strongly suggests human age. Review if the study uses a different population or unit.", "recommended": False})
            if SCALE_PATTERN.search(column["name"]) and column.get("minimum") is not None:
                add({"file_id": item["id"], "filename": item["filename"], "name": f"{column['name']} uses a 1–5 scale", "rule_type": "range", "parameters": {"column": column["name"], "minimum": 1, "maximum": 5}, "reason": "The name resembles a rating or Likert scale. Confirm the instrument's actual boundaries.", "recommended": False})
            codes = list((column.get("suspected_missing_codes") or {}).keys())
            if codes:
                add({"file_id": item["id"], "filename": item["filename"], "name": f"Normalize missing codes in {column['name']}", "rule_type": "missing_codes", "parameters": {"column": column["name"], "codes": codes, "replacement": ""}, "reason": f"Values {codes!r} commonly represent missing data but require study-specific confirmation.", "recommended": False})
            if column.get("inferred_type") == "text" and CATEGORICAL_PATTERN.search(column["name"]) and 1 < column.get("distinct_count", 0) <= 12:
                values = column.get("examples", [])
                add({"file_id": item["id"], "filename": item["filename"], "name": f"Allowed categories for {column['name']}", "rule_type": "allowed_values", "parameters": {"column": column["name"], "values": values}, "reason": "This low-cardinality text field looks categorical. Confirm the complete codebook before relying on it.", "recommended": False})
    return suggestions


@app.post("/api/projects/{project_id}/reanalyze")
def reanalyze_project(project_id: str) -> dict[str, Any]:
    ensure_project(project_id)
    finding_count = 0
    files = list_files(project_id)
    for item in files:
        with db.connect() as connection:
            version = latest_version(connection, item["id"])
            analysis_path = Path(version["path"]) if version else Path(item["original_path"])
            parsing_config = (item.get("profile") or {}).get("parsing_config") or {}
            scan_parsing = {**parsing_config, "header_row": 1} if version else parsing_config
            profile = profile_dataset(analysis_path, item["filename"], scan_parsing)
            profile["parsing_config"] = parsing_config
            source_row_ids = row_ids_for_source(item, version, profile["row_count"])
            attach_profile_row_ids(profile, source_row_ids)
            connection.execute(
                """UPDATE findings SET status = 'superseded', disposition =
                   CASE WHEN status = 'accepted' THEN 'resolved_by_transformation' ELSE 'superseded' END
                   WHERE file_id = ? AND status != 'superseded'""",
                (item["id"],),
            )
            connection.execute("UPDATE files SET encoding = ?, delimiter = ?, profile_json = ?, warnings_json = ?, status = 'ready' WHERE id = ?", (profile.get("encoding"), profile.get("delimiter"), json_dump(persisted_profile(profile)), json_dump(profile["warnings"]), item["id"]))
            scan_id = create_scan_record(
                connection, project_id, item["id"], hashlib.sha256(analysis_path.read_bytes()).hexdigest(), profile,
                source_version_id=version["id"] if version else None,
                source_kind="reviewed" if version else "original",
            )
            persist_candidates(connection, project_id, item["id"], profile, item["filename"], scan_id)
            study_row = db.row_dict(connection.execute("SELECT * FROM study_configs WHERE project_id = ? AND status = 'confirmed'", (project_id,)).fetchone())
            if study_row:
                analysis_frame = load_frame(analysis_path, item["format"], {**(item.get("profile") or {}), "parsing_config": scan_parsing})
                for survey_finding in detect_survey_quality(analysis_frame, study_row["config"]):
                    row_number = int(survey_finding["row_number"])
                    insert_finding(
                        connection, project_id, item["id"], None, survey_finding["category"], survey_finding["severity"],
                        "needs_confirmation", survey_finding["title"], survey_finding["explanation"], profile["table_name"],
                        survey_finding.get("column_name"), row_number, survey_finding.get("before"), None, None,
                        survey_finding.get("affected_count", 1), scan_id=scan_id,
                        evidence={"study_config_hash": study_row["config_hash"], "review_only": True},
                        row_id=source_row_ids[row_number - 1],
                    )
            rules = db.rows_dict(connection.execute("SELECT * FROM validation_rules WHERE file_id = ? AND status = 'confirmed'", (item["id"],)).fetchall())
            for rule in rules:
                finding_count += evaluate_rule(connection, project_id, {**item, "profile": {"columns": profile["columns"], "table_name": profile["table_name"], "format_metadata": profile.get("format_metadata", {})}}, rule["id"], rule["rule_type"], rule["parameters"])
            finding_count += len(profile["findings"])
            current_categories = [
                {"category": row[0]} for row in connection.execute(
                    "SELECT category FROM findings WHERE scan_id = ? AND status = 'pending'", (scan_id,)
                ).fetchall()
            ]
            persist_check_results(connection, project_id, item["id"], scan_id, {**profile, "findings": current_categories})
            connection.execute("UPDATE scan_runs SET finding_count = ? WHERE id = ?", (len(current_categories), scan_id))
            audit(connection, project_id, "file_reanalyzed", {"file_id": item["id"], "detector_findings": len(profile["findings"]), "confirmed_rules": len(rules)})
    with db.connect() as connection:
        connection.execute("UPDATE projects SET needs_rescan = 0, updated_at = ? WHERE id = ?", (now(), project_id))
    return {"file_count": len(files), "finding_count": finding_count, "readiness": readiness(project_id)}


@app.post("/api/projects/{project_id}/rules/auto-confirm")
def auto_confirm_recommended_rules(project_id: str) -> dict[str, Any]:
    recommended = [item for item in rule_suggestions(project_id) if item.get("recommended")]
    created = []
    for suggestion in recommended:
        result = create_rule(project_id, RuleCreate.model_validate(suggestion))
        created.append({"id": result["id"], "name": result["name"], "finding_count": result["finding_count"]})
    return {"confirmed_count": len(created), "finding_count": sum(item["finding_count"] for item in created), "rules": created, "readiness": readiness(project_id)}


@app.get("/api/projects/{project_id}/rules")
def list_rules(project_id: str) -> list[dict[str, Any]]:
    ensure_project(project_id)
    with db.connect() as connection:
        return db.rows_dict(connection.execute("SELECT * FROM validation_rules WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall())


@app.post("/api/projects/{project_id}/rules", status_code=201)
def create_rule(project_id: str, payload: RuleCreate) -> dict[str, Any]:
    timestamp, rule_id = now(), new_id("rule")
    with db.connect() as connection:
        item = file_record(connection, project_id, payload.file_id)
        if payload.rule_type == "unique":
            normalized_column = " ".join(str(payload.parameters.get("column", "")).replace("_", " ").replace("-", " ").casefold().split())
            unsafe_name_column = normalized_column in {"name", "full name", "patient name", "participant name", "respondent name"}
            if unsafe_name_column and not str(payload.parameters.get("override_rationale", "")).strip():
                raise HTTPException(422, "Names are not reliable participant identifiers. Use a participant ID or provide an explicit advanced uniqueness override rationale.")
        signature = db.rule_signature(payload.file_id, payload.rule_type, payload.parameters)
        duplicate = db.row_dict(connection.execute("SELECT * FROM validation_rules WHERE project_id = ? AND signature = ? AND status = 'confirmed'", (project_id, signature)).fetchone())
        if duplicate is not None:
            duplicate["finding_count"] = 0
            duplicate["duplicate"] = True
            return duplicate
        confirmed_same_type = db.rows_dict(connection.execute(
            "SELECT * FROM validation_rules WHERE project_id = ? AND file_id = ? AND rule_type = ? AND status = 'confirmed'",
            (project_id, payload.file_id, payload.rule_type),
        ).fetchall())
        requested_scope = (
            f"{payload.parameters.get('when_column')}->{payload.parameters.get('then_column')}"
            if payload.rule_type == "cross_column" else str(payload.parameters.get("column", ""))
        )
        for existing in confirmed_same_type:
            existing_scope = (
                f"{existing['parameters'].get('when_column')}->{existing['parameters'].get('then_column')}"
                if payload.rule_type == "cross_column" else str(existing["parameters"].get("column", ""))
            )
            if existing_scope == requested_scope:
                raise HTTPException(409, f"{existing['name']!r} already defines this check. Edit that rule instead of creating a contradictory rule.")
        reusable = db.row_dict(connection.execute("SELECT * FROM validation_rules WHERE project_id = ? AND signature = ? AND status = 'disabled' ORDER BY created_at LIMIT 1", (project_id, signature)).fetchone())
        if reusable is not None:
            rule_id = reusable["id"]
            connection.execute("UPDATE validation_rules SET name = ?, status = 'confirmed', source = 'user', updated_at = ? WHERE id = ?", (payload.name, timestamp, rule_id))
        else:
            connection.execute("INSERT INTO validation_rules(id, project_id, file_id, name, rule_type, parameters_json, signature, source, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'user', 'confirmed', ?, ?)", (rule_id, project_id, payload.file_id, payload.name, payload.rule_type, json_dump(payload.parameters), signature, timestamp, timestamp))
        finding_count = evaluate_rule(connection, project_id, item, rule_id, payload.rule_type, payload.parameters)
        connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (timestamp, project_id))
        audit(connection, project_id, "rule_confirmed", {"rule_id": rule_id, "type": payload.rule_type, "finding_count": finding_count})
        rule = db.row_dict(connection.execute("SELECT * FROM validation_rules WHERE id = ?", (rule_id,)).fetchone())
        rule["finding_count"] = finding_count
        return rule


@app.delete("/api/projects/{project_id}/rules/{rule_id}")
def disable_rule(project_id: str, rule_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        if connection.execute("SELECT 1 FROM validation_rules WHERE id = ? AND project_id = ?", (rule_id, project_id)).fetchone() is None:
            raise HTTPException(404, "Rule not found")
        connection.execute("UPDATE validation_rules SET status = 'disabled', updated_at = ? WHERE id = ?", (now(), rule_id))
        connection.execute("DELETE FROM findings WHERE rule_id = ? AND status = 'pending'", (rule_id,))
        connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (now(), project_id))
        audit(connection, project_id, "rule_disabled", {"rule_id": rule_id})
    return {"id": rule_id, "status": "disabled"}


@app.patch("/api/projects/{project_id}/rules/{rule_id}")
def update_rule(project_id: str, rule_id: str, payload: RuleUpdate) -> dict[str, Any]:
    with db.connect() as connection:
        rule = db.row_dict(connection.execute("SELECT * FROM validation_rules WHERE id = ? AND project_id = ?", (rule_id, project_id)).fetchone())
        if rule is None:
            raise HTTPException(404, "Rule not found")
        item = file_record(connection, project_id, rule["file_id"])
        signature = db.rule_signature(rule["file_id"], rule["rule_type"], payload.parameters)
        duplicate = connection.execute("SELECT name FROM validation_rules WHERE project_id = ? AND signature = ? AND status = 'confirmed' AND id != ?", (project_id, signature, rule_id)).fetchone()
        if duplicate is not None:
            raise HTTPException(409, f"This rule duplicates {duplicate[0]!r}. Edit that rule instead.")
        requested_scope = (
            f"{payload.parameters.get('when_column')}->{payload.parameters.get('then_column')}"
            if rule["rule_type"] == "cross_column" else str(payload.parameters.get("column", ""))
        )
        peers = db.rows_dict(connection.execute(
            "SELECT id, name, rule_type, parameters_json FROM validation_rules WHERE project_id = ? AND file_id = ? AND rule_type = ? AND status = 'confirmed' AND id != ?",
            (project_id, rule["file_id"], rule["rule_type"], rule_id),
        ).fetchall())
        for peer in peers:
            peer_scope = (
                f"{peer['parameters'].get('when_column')}->{peer['parameters'].get('then_column')}"
                if rule["rule_type"] == "cross_column" else str(peer["parameters"].get("column", ""))
            )
            if peer_scope == requested_scope:
                raise HTTPException(409, f"{peer['name']!r} already defines this check. Disable it before replacing the active rule revision.")
        connection.execute("DELETE FROM findings WHERE rule_id = ? AND status = 'pending'", (rule_id,))
        connection.execute("UPDATE validation_rules SET name = ?, parameters_json = ?, signature = ?, status = 'confirmed', updated_at = ? WHERE id = ?", (payload.name.strip(), json_dump(payload.parameters), signature, now(), rule_id))
        finding_count = evaluate_rule(connection, project_id, item, rule_id, rule["rule_type"], payload.parameters)
        connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (now(), project_id))
        audit(connection, project_id, "rule_updated", {"rule_id": rule_id, "finding_count": finding_count})
        updated = db.row_dict(connection.execute("SELECT * FROM validation_rules WHERE id = ?", (rule_id,)).fetchone())
        updated["finding_count"] = finding_count
        return updated


@app.post("/api/projects/{project_id}/rules/revalidate")
def revalidate_rules(project_id: str) -> dict[str, Any]:
    total = 0
    with db.connect() as connection:
        rules = db.rows_dict(connection.execute("SELECT * FROM validation_rules WHERE project_id = ? AND status = 'confirmed' ORDER BY created_at", (project_id,)).fetchall())
        for rule in rules:
            item = file_record(connection, project_id, rule["file_id"])
            connection.execute("DELETE FROM findings WHERE rule_id = ? AND status = 'pending'", (rule["id"],))
            total += evaluate_rule(connection, project_id, item, rule["id"], rule["rule_type"], rule["parameters"])
        audit(connection, project_id, "rules_revalidated", {"rule_count": len(rules), "finding_count": total})
        connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (now(), project_id))
    return {"rule_count": len(rules), "finding_count": total, "readiness": readiness(project_id)}


@app.get("/api/projects/{project_id}/relationships")
def list_relationships(project_id: str) -> list[dict[str, Any]]:
    with db.connect() as connection:
        return db.rows_dict(connection.execute("SELECT * FROM relationships WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall())


@app.post("/api/projects/{project_id}/relationships", status_code=201)
def create_relationship(project_id: str, payload: RelationshipCreate) -> dict[str, Any]:
    relationship_id = new_id("rel")
    with db.connect() as connection:
        left = file_record(connection, project_id, payload.left_file_id)
        right = file_record(connection, project_id, payload.right_file_id)
        duplicate = db.row_dict(connection.execute(
            """SELECT * FROM relationships
               WHERE project_id = ? AND left_file_id = ? AND left_column = ?
                 AND right_file_id = ? AND right_column = ? AND cardinality = ?
                 AND status = 'confirmed' LIMIT 1""",
            (project_id, payload.left_file_id, payload.left_column, payload.right_file_id, payload.right_column, payload.cardinality),
        ).fetchone())
        if duplicate is not None:
            duplicate["duplicate"] = True
            return duplicate
        connection.execute("INSERT INTO relationships(id, project_id, left_file_id, left_column, right_file_id, right_column, cardinality, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)", (relationship_id, project_id, payload.left_file_id, payload.left_column, payload.right_file_id, payload.right_column, payload.cardinality, now()))
        left_frame = load_frame(Path(left["original_path"]), left["format"], left.get("profile"))
        right_frame = load_frame(Path(right["original_path"]), right["format"], right.get("profile"))
        if payload.left_column not in left_frame or payload.right_column not in right_frame:
            raise HTTPException(422, "A selected relationship column does not exist")
        left_values = [str(value).strip() for value in left_frame[payload.left_column].tolist()]
        right_values = [str(value).strip() for value in right_frame[payload.right_column].tolist()]
        left_keys = {value for value in left_values if value}
        right_keys = {value for value in right_values if value}
        missing_rows = [index + 1 for index, value in enumerate(left_values) if value and value not in right_keys]
        if missing_rows:
            before = left_values[missing_rows[0] - 1]
            insert_finding(connection, project_id, payload.left_file_id, relationship_id, "cross_file", "high", "high", "Key missing from related dataset", f"{len(missing_rows)} values in {left['filename']} have no match in {right['filename']}.", (left.get("profile") or {}).get("table_name", Path(left["filename"]).stem), payload.left_column, missing_rows[0], before, None, None, len(missing_rows))
        duplicate_right = {value: count for value, count in Counter(value for value in right_values if value).items() if count > 1}
        if payload.cardinality in {"many_to_one", "one_to_one"} and duplicate_right:
            first_value = next(iter(duplicate_right))
            first_row = next(index + 1 for index, value in enumerate(right_values) if value == first_value)
            insert_finding(connection, project_id, payload.right_file_id, relationship_id, "cross_file", "high", "high", "Duplicate reference keys", f"{len(duplicate_right)} key value(s) repeat in {right['filename']}, but this relationship expects each referenced key to identify one record.", (right.get("profile") or {}).get("table_name", Path(right["filename"]).stem), payload.right_column, first_row, first_value, None, None, sum(duplicate_right.values()))
        reverse_missing_rows: list[int] = []
        if payload.cardinality in {"one_to_one", "one_to_many"}:
            reverse_missing_rows = [index + 1 for index, value in enumerate(right_values) if value and value not in left_keys]
            if reverse_missing_rows:
                before = right_values[reverse_missing_rows[0] - 1]
                insert_finding(connection, project_id, payload.right_file_id, relationship_id, "cross_file", "medium", "high", "Reference key missing from source dataset", f"{len(reverse_missing_rows)} values in {right['filename']} have no match in {left['filename']} under the confirmed relationship cardinality.", (right.get("profile") or {}).get("table_name", Path(right["filename"]).stem), payload.right_column, reverse_missing_rows[0], before, None, None, len(reverse_missing_rows))
        audit(connection, project_id, "relationship_confirmed", {"relationship_id": relationship_id, "missing_keys": len(missing_rows), "duplicate_reference_keys": len(duplicate_right), "reverse_missing_keys": len(reverse_missing_rows)})
        return db.row_dict(connection.execute("SELECT * FROM relationships WHERE id = ?", (relationship_id,)).fetchone())


@app.get("/api/projects/{project_id}/findings")
def list_findings(project_id: str, status: str | None = None, severity: str | None = None, file_id: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    clauses, parameters = ["f.project_id = ?"], [project_id]
    if status is None:
        clauses.append("f.status != 'superseded'")
    for column, value in (("f.status", status), ("f.severity", severity), ("f.file_id", file_id)):
        if value:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    where = " AND ".join(clauses)
    with db.connect() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM findings f WHERE {where}", parameters).fetchone()[0]
        rows = connection.execute(f"""SELECT f.*, files.filename FROM findings f JOIN files ON files.id = f.file_id
            WHERE {where} ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            f.row_number LIMIT ? OFFSET ?""", [*parameters, limit, offset]).fetchall()
        return {"items": db.rows_dict(rows), "total": total, "limit": limit, "offset": offset}


@app.get("/api/projects/{project_id}/findings/{finding_id}/preview")
def preview_finding(project_id: str, finding_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        finding = finding_record(connection, project_id, finding_id)
        item = file_record(connection, project_id, finding["file_id"])
        offset = max(0, (finding.get("row_number") or 1) - 3)
        original = preview_records(Path(item["original_path"]), item["format"], item.get("profile"), offset, 5)
        version = latest_version(connection, item["id"])
        reviewed = preview_records(Path(version["path"]) if version else Path(item["original_path"]), item["format"], item.get("profile"), offset, 5)
        return {"finding": finding, "columns": original["columns"], "original_rows": original["rows"], "reviewed_rows": reviewed["rows"], "total": original["total"]}


@app.post("/api/projects/{project_id}/findings/{finding_id}/disposition")
def disposition_finding(project_id: str, finding_id: str, payload: FindingDispositionCreate) -> dict[str, Any]:
    with db.connect() as connection:
        finding = finding_record(connection, project_id, finding_id)
        if finding["status"] != "pending":
            raise HTTPException(409, "Only a pending current finding can receive a review disposition")
        if payload.disposition == "acknowledged" and finding.get("operation") is not None:
            raise HTTPException(422, "Correction proposals must be accepted or rejected; acknowledgement is for review-only evidence")
        status = "deferred" if payload.disposition == "deferred" else ("acknowledged" if payload.disposition == "acknowledged" else "rejected")
        decision_id = new_id("dec")
        connection.execute(
            "INSERT INTO decisions(id, finding_id, decision, rationale, created_at) VALUES (?, ?, ?, ?, ?)",
            (decision_id, finding_id, payload.disposition, payload.rationale, now()),
        )
        connection.execute("UPDATE findings SET status = ?, disposition = ? WHERE id = ?", (status, payload.disposition, finding_id))
        section_by_category = {
            "corrupted_row": 1, "duplicate_column_name": 2, "duplicate_column": 2, "empty_column": 2,
            "missing_value": 3, "missing_code_normalization": 3, "rule_required": 3, "rule_missing_codes": 3,
            "duplicate_id": 4, "duplicate_row": 4, "near_duplicate": 4, "potential_duplicate": 4,
            "identity_not_verifiable": 4, "rule_unique": 4, "invalid_type": 5, "rule_type": 5,
            "whitespace": 6, "text_normalization": 6, "inconsistent_category": 7, "rule_allowed_values": 7,
            "impossible_value": 8, "outlier": 8, "rule_range": 8, "date_standardization": 9,
            "ambiguous_date": 9, "rule_date": 9, "invalid_scale": 10, "rule_scale": 10,
            "rule_cross_column": 11, "cross_file": 12, "constant_column": 13, "suspicious_pattern": 13,
            "survey_duplicate_submission": 4, "survey_invalid_scale": 10, "survey_skip_logic": 11,
            "survey_cross_field": 11, "survey_incomplete": 13, "survey_straightlining": 13,
            "survey_attention_check": 13, "survey_speeder": 13, "spreadsheet_formula": 14,
            "broken_text_encoding": 14,
        }
        section_number = section_by_category.get(finding["category"])
        if payload.disposition == "acknowledged" and finding.get("scan_id") and section_number:
            connection.execute(
                "UPDATE check_results SET status = 'acknowledged', reason = ? WHERE scan_id = ? AND section_number = ?",
                (payload.rationale, finding["scan_id"], section_number),
            )
        audit(connection, project_id, "finding_disposition_recorded", {"finding_id": finding_id, "disposition": payload.disposition, "rationale": payload.rationale})
    return {"id": decision_id, "finding_id": finding_id, "status": status, "disposition": payload.disposition, "readiness": readiness(project_id)}


@app.post("/api/projects/{project_id}/manual-operations", status_code=201)
def create_manual_operation(project_id: str, payload: ManualOperationCreate) -> dict[str, Any]:
    """Record one evidence-backed edit or exclusion through the normal audited plan."""
    try:
        with db.connect() as connection:
            item = file_record(connection, project_id, payload.file_id)
            version = latest_version(connection, item["id"])
            source_path = Path(version["path"]) if version else Path(item["original_path"])
            frame = load_frame(source_path, item["format"], item.get("profile"))
            source_row_ids = row_ids_for_source(item, version, len(frame))
            table_name = (item.get("profile") or {}).get("table_name", Path(item["filename"]).stem)

            if payload.kind == "cell_correction":
                if not payload.row_id or not payload.column:
                    raise HTTPException(422, "A manual cell correction requires a stable row ID and column")
                if payload.column not in frame.columns:
                    raise HTTPException(422, f"Column {payload.column!r} does not exist in the reviewed dataset")
                if payload.row_id not in source_row_ids:
                    raise HTTPException(409, "The selected row is no longer present in the current reviewed version")
                current_index = source_row_ids.index(payload.row_id)
                current = frame.iloc[current_index][payload.column]
                if not values_equivalent(current, payload.before):
                    raise HTTPException(409, f"The reviewed value changed; expected {payload.before!r}, found {current!r}")
                if values_equivalent(current, payload.after):
                    raise HTTPException(422, "The corrected value must differ from the current value")
                original_ordinal = row_ordinal(payload.row_id)
                operation = {"type": "replace", "column": payload.column, "before": payload.before, "after": payload.after, "rows": [original_ordinal], "row_ids": [payload.row_id], "manual": True, "evidence": payload.evidence}
                title = "Evidence-backed manual cell correction"
                before, proposed, row_number, row_id = payload.before, payload.after, current_index + 1, payload.row_id
            elif payload.kind == "exclude_row":
                if not payload.row_id:
                    raise HTTPException(422, "A row exclusion requires a stable row ID")
                if payload.row_id not in source_row_ids:
                    raise HTTPException(409, "The selected row is no longer present in the current reviewed version")
                current_index = source_row_ids.index(payload.row_id)
                original_ordinal = row_ordinal(payload.row_id)
                operation = {"type": "delete_rows", "rows": [original_ordinal], "row_ids": [payload.row_id], "manual": True, "evidence": payload.evidence}
                title = "Explicit research row exclusion"
                before, proposed, row_number, row_id = frame.iloc[current_index].to_dict(), None, current_index + 1, payload.row_id
            else:
                if not payload.column or payload.column not in frame.columns:
                    raise HTTPException(422, "A column exclusion requires an existing column")
                operation = {"type": "exclude_column", "column": payload.column, "manual": True, "evidence": payload.evidence}
                title = "Explicit research column exclusion"
                before, proposed, row_number, row_id = payload.column, None, None, None

            source_finding = finding_record(connection, project_id, payload.source_finding_id) if payload.source_finding_id else None
            if source_finding and (source_finding["file_id"] != item["id"] or source_finding["status"] != "pending"):
                raise HTTPException(409, "The source finding is no longer pending for this dataset")
            if source_finding:
                ensure_finding_plan_is_current(connection, source_finding)
            finding_id = insert_finding(
                connection, project_id, item["id"], None, "manual_operation", "high", "user_confirmed",
                title, f"Researcher supplied evidence: {payload.evidence}", table_name, payload.column,
                row_number, before, proposed, operation, 1, scan_id=None,
                evidence={"source": "manual", "evidence": payload.evidence, "rationale": payload.rationale, "source_finding_id": payload.source_finding_id}, row_id=row_id,
            )
            finding = finding_record(connection, project_id, finding_id)
            decision_id = new_id("dec")
            connection.execute(
                "INSERT INTO decisions(id, finding_id, decision, edited_value_json, rationale, created_at) VALUES (?, ?, 'accepted', ?, ?, ?)",
                (decision_id, finding_id, json_dump(payload.after) if payload.kind == "cell_correction" else None, payload.rationale, now()),
            )
            connection.execute("UPDATE findings SET status = 'accepted', disposition = 'change_accepted' WHERE id = ?", (finding_id,))
            if source_finding:
                source_decision_id = new_id("dec")
                connection.execute(
                    "INSERT INTO decisions(id, finding_id, decision, rationale, created_at) VALUES (?, ?, 'resolved_by_manual_operation', ?, ?)",
                    (source_decision_id, source_finding["id"], payload.rationale, now()),
                )
                connection.execute("UPDATE findings SET status = 'acknowledged', disposition = 'resolved_by_manual_operation' WHERE id = ?", (source_finding["id"],))
            register_transformation(connection, project_id, finding, decision_id, operation, payload.rationale)
            connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (now(), project_id))
            reviewed_version = create_reviewed_version(connection, project_id, item["id"])
            audit(connection, project_id, "manual_operation_accepted", {"finding_id": finding_id, "kind": payload.kind, "file_id": item["id"], "rationale": payload.rationale})
    except ValueError as error:
        raise HTTPException(409, f"Manual operation was not applied because it conflicts with the reviewed plan: {error}") from error
    return {"finding_id": finding_id, "status": "accepted", "reviewed_version": reviewed_version, "reversible": True, "readiness": readiness(project_id)}


@app.post("/api/projects/{project_id}/findings/{finding_id}/decision")
def decide_finding(project_id: str, finding_id: str, payload: DecisionCreate) -> dict[str, Any]:
    try:
        with db.connect() as connection:
            finding = finding_record(connection, project_id, finding_id)
            ensure_finding_plan_is_current(connection, finding)
            operation = finding.get("operation")
            if payload.decision == "accepted" and operation and operation.get("type") == "delete_rows":
                group_row_ids = [finding.get("row_id"), *(operation.get("row_ids") or [])]
                group_row_ids = [value for value in dict.fromkeys(group_row_ids) if value]
                removed = list(dict.fromkeys(payload.removed_row_ids or []))
                retained = payload.retained_row_id
                if not retained or retained not in group_row_ids or set(removed) != set(group_row_ids) - {retained}:
                    raise HTTPException(422, "Choose exactly one duplicate row to retain and explicitly select every other duplicate row for removal")
                operation = {
                    **operation,
                    "retained_row_id": retained,
                    "row_ids": removed,
                    "rows": [row_ordinal(row_id) for row_id in removed],
                }
                connection.execute("UPDATE findings SET operation_json = ? WHERE id = ?", (json_dump(operation), finding_id))
            if payload.decision == "accepted" and payload.edited_value is not None:
                if operation is None:
                    raise HTTPException(422, "This review-only finding cannot be converted into an unrelated cell edit. Create a separate manual correction with evidence instead.")
                if not finding.get("column_name") or not finding.get("row_number"):
                    raise HTTPException(422, "This issue cannot be corrected with a cell value")
                if operation.get("type") in {"derive_mapping", "derive_arithmetic"}:
                    raise HTTPException(422, "A proven grouped derivation cannot be replaced with one arbitrary value. Reject it or create a separate manual correction with row-level evidence.")
                edited_value = payload.edited_value
                if operation and operation.get("type") == "parse_type":
                    expected = operation.get("expected")
                    try:
                        if expected == "integer": edited_value = int(str(edited_value).strip())
                        elif expected == "number": edited_value = float(str(edited_value).strip())
                    except ValueError as error:
                        raise HTTPException(422, f"The edited correction must be a valid {expected}") from error
                if operation and operation.get("column") == finding["column_name"] and operation.get("before") == finding.get("before"):
                    operation = {**operation, "after": edited_value}
                else:
                    operation = {"type": "replace", "column": finding["column_name"], "before": finding.get("before"), "after": edited_value, "rows": [finding["row_number"]]}
                connection.execute("UPDATE findings SET proposed_json = ?, operation_json = ? WHERE id = ?", (json_dump(edited_value), json_dump(operation), finding_id))
            if payload.decision == "accepted" and operation is None:
                raise HTTPException(422, "This issue has no safe correction. Edit it with an explicit value or reject it.")
            decision_id = new_id("dec")
            connection.execute("INSERT INTO decisions(id, finding_id, decision, edited_value_json, rationale, created_at) VALUES (?, ?, ?, ?, ?, ?)", (decision_id, finding_id, payload.decision, json_dump(payload.edited_value) if payload.edited_value is not None else None, payload.rationale, now()))
            disposition = "change_accepted" if payload.decision == "accepted" else "proposal_rejected"
            connection.execute("UPDATE findings SET status = ?, disposition = ? WHERE id = ?", (payload.decision, disposition, finding_id))
            if payload.decision == "accepted" and operation is not None:
                register_transformation(connection, project_id, finding, decision_id, operation, payload.rationale)
                connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (now(), project_id))
            audit(connection, project_id, "finding_decided", {"finding_id": finding_id, "decision": payload.decision, "edited": payload.edited_value is not None})
            file_id = finding["file_id"]
            version = create_reviewed_version(connection, project_id, file_id) if payload.decision == "accepted" else latest_version(connection, file_id)
    except ValueError as error:
        raise HTTPException(409, f"Correction was not accepted because it conflicts with another reviewed change: {error}") from error
    return {"id": decision_id, "finding_id": finding_id, "status": payload.decision, "reviewed_version": version, "reversible": True, "readiness": readiness(project_id)}


@app.post("/api/projects/{project_id}/findings/batch")
def decide_findings_batch(project_id: str, payload: BatchDecisionCreate) -> dict[str, Any]:
    try:
        with db.connect() as connection:
            findings = [finding_record(connection, project_id, finding_id) for finding_id in dict.fromkeys(payload.finding_ids)]
            findings = [finding for finding in findings if finding["status"] == "pending"]
            if not findings:
                raise HTTPException(409, "No pending findings were selected")
            for finding in findings:
                ensure_finding_plan_is_current(connection, finding)
            if payload.decision == "accepted" and any(finding.get("operation") is None or finding.get("confidence") != "high" or finding["operation"].get("type") == "delete_rows" for finding in findings):
                raise HTTPException(422, "Batch acceptance is limited to high-confidence findings with exact, reproducible corrections")
            file_ids = {finding["file_id"] for finding in findings}
            if payload.decision == "accepted":
                preflight_reviewed_versions(connection, project_id, file_ids, findings)
            for finding in findings:
                decision_id = new_id("dec")
                connection.execute("INSERT INTO decisions(id, finding_id, decision, rationale, created_at) VALUES (?, ?, ?, ?, ?)", (decision_id, finding["id"], payload.decision, "Batch review", now()))
                disposition = "change_accepted" if payload.decision == "accepted" else "proposal_rejected"
                connection.execute("UPDATE findings SET status = ?, disposition = ? WHERE id = ?", (payload.decision, disposition, finding["id"]))
                if payload.decision == "accepted" and finding.get("operation"):
                    register_transformation(connection, project_id, finding, decision_id, finding["operation"], "Batch review")
            versions = [create_reviewed_version(connection, project_id, file_id) for file_id in sorted(file_ids)] if payload.decision == "accepted" else []
            if payload.decision == "accepted":
                connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (now(), project_id))
            audit(connection, project_id, "findings_batch_decided", {"finding_ids": [finding["id"] for finding in findings], "decision": payload.decision})
    except ValueError as error:
        raise HTTPException(409, f"No batch decisions were saved because accepted corrections conflict: {error}") from error
    return {"count": len(findings), "status": payload.decision, "reviewed_versions": versions, "readiness": readiness(project_id)}


@app.post("/api/projects/{project_id}/findings/{finding_id}/undo")
def undo_decision(project_id: str, finding_id: str) -> dict[str, Any]:
    try:
        with db.connect() as connection:
            finding = finding_record(connection, project_id, finding_id)
            if finding["status"] == "pending":
                raise HTTPException(409, "This finding has no decision to undo")
            previous = finding["status"]
            connection.execute("UPDATE findings SET status = 'pending', disposition = 'pending' WHERE id = ?", (finding_id,))
            connection.execute("UPDATE transformations SET status = 'reversed', updated_at = ? WHERE finding_id = ? AND status = 'active'", (now(), finding_id))
            connection.execute("INSERT INTO decisions(id, finding_id, decision, rationale, created_at) VALUES (?, ?, 'reversed', ?, ?)", (new_id("dec"), finding_id, f"Reversed previous {previous} decision", now()))
            source_finding_id = (finding.get("evidence") or {}).get("source_finding_id") if finding.get("category") == "manual_operation" else None
            if source_finding_id:
                connection.execute("UPDATE findings SET status = 'pending', disposition = 'pending' WHERE id = ? AND project_id = ? AND disposition = 'resolved_by_manual_operation'", (source_finding_id, project_id))
                connection.execute(
                    "INSERT INTO decisions(id, finding_id, decision, rationale, created_at) SELECT ?, id, 'reopened_after_manual_undo', ?, ? FROM findings WHERE id = ? AND project_id = ?",
                    (new_id("dec"), f"Reopened because manual operation {finding_id} was reversed", now(), source_finding_id, project_id),
                )
            file_id = finding["file_id"]
            cascaded = dependent_accepted_findings(connection, file_id, finding)
            for dependent in cascaded:
                connection.execute("UPDATE findings SET status = 'pending', disposition = 'pending' WHERE id = ?", (dependent["id"],))
                connection.execute("UPDATE transformations SET status = 'reversed', updated_at = ? WHERE finding_id = ? AND status = 'active'", (now(), dependent["id"]))
                connection.execute(
                    "INSERT INTO decisions(id, finding_id, decision, rationale, created_at) VALUES (?, ?, 'reversed', ?, ?)",
                    (new_id("dec"), dependent["id"], f"Reversed because it depended on {finding_id}", now()),
                )
            audit(connection, project_id, "finding_decision_reversed", {"finding_id": finding_id, "previous": previous, "cascaded_finding_ids": [item["id"] for item in cascaded]})
            version = create_reviewed_version(connection, project_id, file_id)
            connection.execute("UPDATE projects SET needs_rescan = 1, updated_at = ? WHERE id = ?", (now(), project_id))
    except ValueError as error:
        raise HTTPException(409, f"Decision could not be reversed safely: {error}") from error
    return {"finding_id": finding_id, "status": "pending", "cascaded_finding_ids": [item["id"] for item in cascaded], "reviewed_version": version, "readiness": readiness(project_id)}


@app.get("/api/projects/{project_id}/readiness")
def readiness(project_id: str) -> dict[str, Any]:
    ensure_project(project_id)
    with db.connect() as connection:
        return calculate_readiness(connection, project_id)


@app.get("/api/projects/{project_id}/exports")
def list_exports(project_id: str) -> list[dict[str, Any]]:
    with db.connect() as connection:
        exports = db.rows_dict(connection.execute("SELECT * FROM exports WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall())
        for export in exports:
            export["artifacts"] = db.rows_dict(connection.execute("SELECT id, export_id, kind, filename, size_bytes, sha256 FROM export_artifacts WHERE export_id = ? ORDER BY kind, filename", (export["id"],)).fetchall())
        return exports


@app.post("/api/projects/{project_id}/exports", status_code=201)
def create_export(project_id: str, payload: ExportCreate | None = None) -> dict[str, Any]:
    ensure_project(project_id)
    kind = (payload or ExportCreate()).kind
    with db.connect() as connection:
        if kind == "verified":
            unconfirmed_parsing = connection.execute(
                """SELECT COUNT(*) FROM files f LEFT JOIN parsing_configs pc ON pc.file_id = f.id
                   WHERE f.project_id = ? AND COALESCE(pc.status, 'missing') != 'confirmed'""",
                (project_id,),
            ).fetchone()[0]
            if unconfirmed_parsing:
                raise HTTPException(409, f"Verified export is blocked until parsing assumptions are confirmed for {unconfirmed_parsing} dataset(s)")
            pending = connection.execute("SELECT COUNT(*) FROM findings WHERE project_id = ? AND status = 'pending'", (project_id,)).fetchone()[0]
            if pending:
                raise HTTPException(409, f"Verified export is blocked until {pending} pending finding(s) are reviewed")
            blocking_checks = connection.execute(
                """SELECT COUNT(*) FROM check_results cr JOIN scan_runs sr ON sr.id = cr.scan_id
                   WHERE cr.project_id = ? AND sr.status = 'complete' AND cr.section_number < 17
                     AND cr.section_number != 15 AND cr.status IN ('attention','blocked','failed')""",
                (project_id,),
            ).fetchone()[0]
            if blocking_checks:
                raise HTTPException(409, "Verified export is blocked because applicable checklist checks have not passed or been acknowledged")
            runtime = r_status()
            if not runtime["ready"]:
                raise HTTPException(409, runtime["message"])
    export_id, timestamp = new_id("exp"), now()
    root = project_root(project_id) / "exports"
    partial = root / f".{export_id}.partial"
    final = root / export_id
    zip_path = root / f"{export_id}.zip"
    with db.connect() as connection:
        connection.execute("INSERT INTO exports(id, project_id, status, kind, validation_json, created_at) VALUES (?, ?, 'processing', ?, '{}', ?)", (export_id, project_id, kind, timestamp))
    try:
        partial.mkdir(parents=True, exist_ok=False)
        with db.connect() as connection:
            files = db.rows_dict(connection.execute("SELECT * FROM files WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall())
            if not files:
                raise ValueError("Upload at least one dataset before exporting")
            manifest: dict[str, Any] = {"export_id": export_id, "project_id": project_id, "kind": kind, "created_at": timestamp, "engine_version": ENGINE_VERSION, "files": []}
            file_validations: list[dict[str, Any]] = []
            filename_counts = Counter(item["filename"].casefold() for item in files)
            for item in files:
                operations = active_operations(connection, item["id"])
                source = Path(item["original_path"])
                actual_source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                if actual_source_hash != item["sha256"]:
                    raise ValueError(f"Original hash mismatch for {item['filename']}; export stopped")
                safe_name = f"{item['id'][:13]}_{item['filename']}"
                original_copy = partial / "originals" / safe_name
                original_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, original_copy)
                discriminator = item["id"].split("_", 1)[-1][:8] if filename_counts[item["filename"].casefold()] > 1 else None
                cleaned_name = cleaned_export_filename(item["filename"], discriminator)
                cleaned = partial / "cleaned" / cleaned_name
                output = apply_file_operations(item, source, cleaned, operations)
                change_validation = validate_operation_result(item, source, cleaned, operations)
                format_validation = validate_format_contract(item, source, cleaned, operations)
                script_discriminator = f"_{discriminator}" if discriminator else ""
                script = partial / "scripts" / f"clean_{Path(item['filename']).stem}{script_discriminator}.R"
                script.parent.mkdir(parents=True, exist_ok=True)
                profile = item.get("profile") or {}
                script_text = generate_format_r_script(safe_name, cleaned_name, operations, item["format"], item.get("delimiter"), profile.get("table_name"), profile)
                script.write_text(script_text, encoding="utf-8")
                r_reproduced, r_detail = verify_r_reproduction(item, source, cleaned, safe_name, cleaned_name, script_text, operations)
                plan_hash = hashlib.sha256("\n".join(operation_hash(operation) for operation in operations).encode("utf-8")).hexdigest()
                file_validation = {
                    "file_id": item["id"],
                    "source_hash_valid": True,
                    "output_reopened": True,
                    "shape_valid": True,
                    "change_validation": change_validation,
                    "format_contract": format_validation,
                    "r_reproduced": r_reproduced,
                    "r_detail": r_detail,
                }
                file_validations.append(file_validation)
                manifest["files"].append({"file_id": item["id"], "original": safe_name, "original_sha256": actual_source_hash, "cleaned": cleaned_name, "cleaned_sha256": output["sha256"], "r_script": script.name, "accepted_transformations": len(operations), "plan_hash": plan_hash, "validation": file_validation})
            validation = {"status": "passed" if all(item["source_hash_valid"] and item["output_reopened"] and item["shape_valid"] and item["change_validation"]["status"] == "passed" and item["format_contract"]["status"] == "passed" for item in file_validations) and (kind == "review" or all(item["r_reproduced"] for item in file_validations)) else "failed", "kind": kind, "r_reproduced": all(item["r_reproduced"] for item in file_validations), "files": file_validations}
            if kind == "verified" and validation["status"] != "passed":
                raise ValueError("R reproduction or output validation did not match the Python-reviewed data")
            manifest["validation"] = validation
            events = db.rows_dict(connection.execute("SELECT * FROM audit_events WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall())
            findings = db.rows_dict(connection.execute("SELECT f.*, files.filename FROM findings f JOIN files ON files.id = f.file_id WHERE f.project_id = ? ORDER BY f.created_at", (project_id,)).fetchall())
            decisions = db.rows_dict(connection.execute("SELECT d.*, f.project_id, f.file_id, f.title AS finding_title FROM decisions d JOIN findings f ON f.id = d.finding_id WHERE f.project_id = ? ORDER BY d.created_at", (project_id,)).fetchall())
            write_reports(partial, manifest, events, decisions, findings, calculate_readiness(connection, project_id))
        partial.replace(final)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in final.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(final))
        with db.connect() as connection:
            connection.execute("UPDATE exports SET status = 'complete', bundle_path = ?, validation_json = ?, completed_at = ? WHERE id = ?", (str(zip_path), json_dump(validation), now(), export_id))
            register_artifact(connection, export_id, "bundle", zip_path)
            for path in final.rglob("*"):
                if path.is_file():
                    register_artifact(connection, export_id, artifact_kind(path), path)
            audit(connection, project_id, "export_created", {"export_id": export_id, "artifact_count": connection.execute("SELECT COUNT(*) FROM export_artifacts WHERE export_id = ?", (export_id,)).fetchone()[0]})
    except Exception as exc:
        shutil.rmtree(partial, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        if zip_path.exists():
            zip_path.unlink()
        with db.connect() as connection:
            connection.execute("UPDATE exports SET status = 'failed', error = ?, completed_at = ? WHERE id = ?", (str(exc), now(), export_id))
        raise HTTPException(422, f"Export failed: {exc}") from exc
    return next(item for item in list_exports(project_id) if item["id"] == export_id)


def verify_r_reproduction(item: dict[str, Any], source: Path, python_output: Path, safe_name: str, cleaned_name: str, script_text: str, operations: list[dict[str, Any]]) -> tuple[bool, str]:
    rscript = shutil.which("Rscript")
    if not rscript:
        return False, "Rscript is not installed; this review package is not a verified clean export."
    with tempfile.TemporaryDirectory(prefix="scribe-r-verify-") as folder:
        root = Path(folder)
        (root / "originals").mkdir()
        (root / "cleaned").mkdir()
        shutil.copy2(source, root / "originals" / safe_name)
        script = root / "clean.R"
        script.write_text(script_text, encoding="utf-8")
        result = subprocess.run([rscript, str(script)], cwd=root, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "R script failed").strip().splitlines()[-1]
            return False, detail[:500]
        r_output = root / "cleaned" / cleaned_name
        if not r_output.exists():
            return False, "R script completed without producing the expected cleaned file."
        try:
            python_frame = load_frame(python_output, item["format"], item.get("profile"))
            r_frame = load_frame(r_output, item["format"], item.get("profile"))
            if list(python_frame.columns) != list(r_frame.columns) or len(python_frame) != len(r_frame):
                return False, "R output schema or row count differs from Python output."
            left = python_frame.fillna("").astype(str).reset_index(drop=True)
            right = r_frame.fillna("").astype(str).reset_index(drop=True)
            if not left.equals(right):
                return False, "R output values differ from Python output."
            validate_operation_result(item, source, r_output, operations)
            validate_format_contract(item, source, r_output, operations)
        except Exception as error:
            return False, f"R output comparison failed: {error}"[:500]
    return True, "R script reproduced the canonical Python-reviewed values."


def cleaned_export_filename(filename: str, discriminator: str | None = None) -> str:
    path = Path(filename)
    suffix = path.suffix
    stem = path.stem or filename
    collision_suffix = f"_{discriminator}" if discriminator else ""
    return f"{stem}_cleaned{collision_suffix}{suffix}"


@app.get("/api/exports/{export_id}/download")
def download_export(export_id: str) -> FileResponse:
    with db.connect() as connection:
        export = db.row_dict(connection.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone())
        if export is None or export["status"] != "complete" or not export.get("bundle_path"):
            raise HTTPException(404, "Completed export not found")
        path = Path(export["bundle_path"])
    return FileResponse(path, media_type="application/zip", filename=f"scribe_{export_id}.zip")


@app.get("/api/exports/{export_id}/artifacts/{artifact_id}")
def download_artifact(export_id: str, artifact_id: str) -> FileResponse:
    with db.connect() as connection:
        artifact = db.row_dict(connection.execute("SELECT * FROM export_artifacts WHERE id = ? AND export_id = ?", (artifact_id, export_id)).fetchone())
        if artifact is None:
            raise HTTPException(404, "Export artifact not found")
        path = Path(artifact["path"])
    return FileResponse(path, filename=artifact["filename"])


def ensure_project(project_id: str, *, allow_trashed: bool = False) -> None:
    with db.connect() as connection:
        project = connection.execute("SELECT deleted_at FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "Project not found")
        if project["deleted_at"] and not allow_trashed:
            raise HTTPException(423, "Project is in Trash. Restore it before continuing work.")


def project_root(project_id: str) -> Path:
    return db.DATA_ROOT / "projects" / project_id


def project_summary(connection: Any, project_id: str) -> dict[str, Any]:
    return {
        "file_count": connection.execute("SELECT COUNT(*) FROM files WHERE project_id = ?", (project_id,)).fetchone()[0],
        "rule_count": connection.execute("SELECT COUNT(*) FROM validation_rules WHERE project_id = ? AND status = 'confirmed'", (project_id,)).fetchone()[0],
        "finding_count": connection.execute("SELECT COUNT(*) FROM findings WHERE project_id = ? AND status != 'superseded'", (project_id,)).fetchone()[0],
        "pending_count": connection.execute("SELECT COUNT(*) FROM findings WHERE project_id = ? AND status = 'pending'", (project_id,)).fetchone()[0],
        "readiness": calculate_readiness(connection, project_id),
    }


def calculate_readiness(connection: Any, project_id: str) -> dict[str, Any]:
    rows = connection.execute("SELECT severity, COUNT(*) FROM findings WHERE project_id = ? AND status = 'pending' GROUP BY severity", (project_id,)).fetchall()
    counts = {row[0]: row[1] for row in rows}
    file_count = connection.execute("SELECT COUNT(*) FROM files WHERE project_id = ?", (project_id,)).fetchone()[0]
    pending_categories = {row[0] for row in connection.execute("SELECT DISTINCT category FROM findings WHERE project_id = ? AND status = 'pending'", (project_id,)).fetchall()}
    category_sections = {
        1: {"corrupted_row"}, 2: {"duplicate_column_name", "duplicate_column", "empty_column"},
        3: {"missing_value", "missing_code_normalization", "rule_required", "rule_missing_codes"},
        4: {"duplicate_id", "duplicate_row", "near_duplicate", "potential_duplicate", "identity_not_verifiable", "rule_unique", "survey_duplicate_submission"},
        5: {"invalid_type", "rule_type"}, 6: {"whitespace", "text_normalization"},
        7: {"inconsistent_category", "rule_allowed_values"}, 8: {"invalid_type", "impossible_value", "outlier", "rule_range"},
        9: {"date_standardization", "ambiguous_date", "rule_date"}, 10: {"invalid_scale", "rule_scale", "survey_invalid_scale"},
        11: {"rule_cross_column", "survey_skip_logic", "survey_cross_field"}, 12: {"cross_file"},
        13: {"constant_column", "outlier", "suspicious_pattern", "survey_incomplete", "survey_straightlining", "survey_attention_check", "survey_speeder"},
        14: {"whitespace", "text_normalization", "corrupted_row", "spreadsheet_formula", "broken_text_encoding"}, 15: set(), 16: set(), 17: pending_categories,
    }
    check_rows = db.rows_dict(connection.execute(
        """SELECT cr.* FROM check_results cr JOIN scan_runs sr ON sr.id = cr.scan_id
           WHERE cr.project_id = ? AND sr.status = 'complete' ORDER BY cr.section_number""",
        (project_id,),
    ).fetchall())
    names = {number: name for number, name in (
        (1, "File integrity"), (2, "Column structure"), (3, "Missing data"),
        (4, "Duplicate detection"), (5, "Data types"), (6, "Text standardization"),
        (7, "Categorical values"), (8, "Numeric validation"), (9, "Date validation"),
        (10, "Rating scales"), (11, "Cross-column validation"), (12, "Cross-file validation"),
        (13, "Statistical quality"), (14, "Formatting consistency"), (15, "Metadata preservation"),
        (16, "Safety & reproducibility"), (17, "Final validation"),
    )}
    priority = {"failed": 5, "blocked": 4, "attention": 3, "acknowledged": 2, "pass": 1, "not_applicable": 0}
    grouped: dict[int, list[dict[str, Any]]] = {}
    for check in check_rows:
        grouped.setdefault(check["section_number"], []).append(check)
    verified_exports = db.rows_dict(connection.execute(
        "SELECT * FROM exports WHERE project_id = ? AND kind = 'verified' AND status = 'complete' ORDER BY created_at DESC",
        (project_id,),
    ).fetchall())
    verified_export = next((item for item in verified_exports if (item.get("validation") or {}).get("status") == "passed" and (item.get("validation") or {}).get("r_reproduced") is True), None)
    checklist: list[dict[str, Any]] = []
    for number in range(1, 18):
        relevant = sorted(pending_categories & category_sections[number])
        records = grouped.get(number, [])
        if not records:
            check_status = "not_applicable" if file_count == 0 else "blocked"
            evidence: list[dict[str, Any]] = []
        else:
            applicable_records = [item for item in records if item["status"] != "not_applicable"]
            check_status = "not_applicable" if not applicable_records else max((item["status"] for item in applicable_records), key=lambda value: priority.get(value, 3))
            evidence = [item.get("evidence") or {} for item in records]
        if relevant and check_status not in {"failed", "blocked"}:
            check_status = "attention"
        if number == 17:
            check_status = "pass" if verified_export and not pending_categories else "blocked"
        if number == 16 and verified_export:
            check_status = "pass"
        if number == 15 and verified_export:
            format_contracts = [
                file_validation.get("format_contract", {}).get("status")
                for file_validation in (verified_export.get("validation") or {}).get("files", [])
            ]
            check_status = "pass" if format_contracts and all(status == "passed" for status in format_contracts) else "failed"
        checklist.append({"number": number, "name": names[number], "status": check_status, "unresolved_categories": relevant, "evidence": evidence})
    applicable = [item for item in checklist if item["status"] != "not_applicable"]
    weights = {number: 2 if number in {1, 2, 16, 17} else 1 for number in range(1, 18)}
    total_weight = sum(weights[item["number"]] for item in applicable) or 1
    completed_weight = sum(weights[item["number"]] for item in applicable if item["status"] in {"pass", "acknowledged"})
    score = round(completed_weight / total_weight * 100)
    if counts.get("critical", 0):
        score = min(score, 49)
    if any(item["status"] == "failed" for item in checklist):
        score = 0
    project_row = connection.execute("SELECT needs_rescan FROM projects WHERE id = ?", (project_id,)).fetchone()
    needs_rescan = bool(project_row and project_row[0])
    clean = bool(file_count and verified_export and not pending_categories and all(item["status"] in {"pass", "acknowledged", "not_applicable"} for item in checklist))
    if file_count == 0:
        status = "not_started"
    elif needs_rescan:
        status = "legacy_review_required"
    elif clean:
        status = "clean"
    elif not pending_categories:
        status = "review_complete_r_unverified" if not shutil.which("Rscript") else "review_complete"
    else:
        status = "provisional"
    estimated = sum(1 if item.get("operation") else 3 for item in db.rows_dict(connection.execute("SELECT operation_json FROM findings WHERE project_id = ? AND status = 'pending'", (project_id,)).fetchall()))
    return {"score": score, "status": status, "clean": clean, "needs_rescan": needs_rescan, "unresolved_by_severity": counts, "deductions": 100 - score, "estimated_review_minutes": estimated, "formula": "Weighted completion of executed applicable checks; integrity, structure, safety, and final validation count twice.", "checklist": checklist, "verified_export_id": verified_export["id"] if verified_export else None}


def file_record(connection: Any, project_id: str, file_id: str) -> dict[str, Any]:
    state = connection.execute("SELECT deleted_at FROM projects WHERE id = ?", (project_id,)).fetchone()
    if state is None:
        raise HTTPException(404, "Project not found")
    if state["deleted_at"]:
        raise HTTPException(423, "Project is in Trash. Restore it before continuing work.")
    item = db.row_dict(connection.execute("SELECT * FROM files WHERE id = ? AND project_id = ?", (file_id, project_id)).fetchone())
    if item is None:
        raise HTTPException(404, "Dataset not found")
    return item


def finding_record(connection: Any, project_id: str, finding_id: str) -> dict[str, Any]:
    state = connection.execute("SELECT deleted_at FROM projects WHERE id = ?", (project_id,)).fetchone()
    if state is None:
        raise HTTPException(404, "Project not found")
    if state["deleted_at"]:
        raise HTTPException(423, "Project is in Trash. Restore it before continuing work.")
    item = db.row_dict(connection.execute("SELECT f.*, files.filename FROM findings f JOIN files ON files.id = f.file_id WHERE f.id = ? AND f.project_id = ?", (finding_id, project_id)).fetchone())
    if item is None:
        raise HTTPException(404, "Finding not found")
    return item


def latest_version(connection: Any, file_id: str) -> dict[str, Any] | None:
    return db.row_dict(connection.execute("SELECT * FROM reviewed_versions WHERE file_id = ? AND status = 'ready' ORDER BY version_number DESC LIMIT 1", (file_id,)).fetchone())


def latest_reviewed(file_id: str) -> dict[str, Any] | None:
    with db.connect() as connection:
        return latest_version(connection, file_id)


def row_ids_for_source(item: dict[str, Any], version: dict[str, Any] | None, row_count: int) -> list[str]:
    if version:
        row_map_path = version.get("row_map_path")
        if row_map_path and Path(row_map_path).exists():
            return read_row_map(Path(row_map_path), row_count)
        # Legacy reviewed versions did not have a sidecar. Their lineage is
        # deterministically recoverable from the immutable original and active plan.
        with db.connect() as connection:
            operations = active_operations(connection, item["id"])
        recovered = reviewed_row_ids(item["id"], original_shape(item)[0], operations)
        if len(recovered) != row_count:
            raise ValueError("Legacy reviewed version row lineage could not be recovered safely")
        return recovered
    return original_row_ids(item["id"], row_count)


def original_shape(item: dict[str, Any]) -> tuple[int, int]:
    if item.get("original_row_count") is not None and item.get("original_column_count") is not None:
        return int(item["original_row_count"]), int(item["original_column_count"])
    profile = profile_dataset(Path(item["original_path"]), item["filename"])
    return int(profile["row_count"]), int(profile["column_count"])


def ensure_finding_plan_is_current(connection: Any, finding: dict[str, Any]) -> None:
    current_hash = active_plan_hash(connection, finding["file_id"])
    if finding.get("source_plan_hash") != current_hash:
        raise HTTPException(409, "This finding was produced from an older reviewed version. Run the current scan and review the refreshed evidence before deciding it.")


def apply_file_operations(item: dict[str, Any], source: Path, destination: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    profile = item.get("profile") or {}
    parsing = profile.get("parsing_config") or {}
    if item["format"] in {"csv", "tsv"}:
        return apply_operations(source, destination, operations, parsing)
    if item["format"] == "xlsx":
        return apply_xlsx_operations(source, destination, operations, profile.get("table_name", Path(item["filename"]).stem), int(parsing.get("header_row") or 1))
    return apply_statistical_operations(source, destination, operations, item["format"])


def operation_hash(operation: dict[str, Any]) -> str:
    canonical = json.dumps(operation, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_transformation(connection: Any, project_id: str, finding: dict[str, Any], decision_id: str, operation: dict[str, Any], rationale: str = "") -> dict[str, Any]:
    source = file_record(connection, project_id, finding["file_id"])
    digest = operation_hash(operation)
    existing = db.row_dict(connection.execute(
        "SELECT * FROM transformations WHERE file_id = ? AND operation_hash = ? AND status = 'active' LIMIT 1",
        (finding["file_id"], digest),
    ).fetchone())
    if existing:
        return existing
    timestamp = now()
    transformation = {
        "id": new_id("trn"), "project_id": project_id, "file_id": finding["file_id"],
        "finding_id": finding["id"], "decision_id": decision_id, "operation_json": json_dump(operation),
        "source_sha256": source["sha256"], "operation_hash": digest, "status": "active",
        "engine_version": ENGINE_VERSION, "rationale": rationale, "created_at": timestamp, "updated_at": timestamp,
    }
    connection.execute(
        """INSERT INTO transformations(id, project_id, file_id, finding_id, decision_id, operation_json,
           source_sha256, operation_hash, status, engine_version, rationale, created_at, updated_at)
           VALUES (:id, :project_id, :file_id, :finding_id, :decision_id, :operation_json,
           :source_sha256, :operation_hash, :status, :engine_version, :rationale, :created_at, :updated_at)""",
        transformation,
    )
    transformation["operation"] = operation
    return transformation


def active_operations(connection: Any, file_id: str) -> list[dict[str, Any]]:
    rows = db.rows_dict(connection.execute(
        "SELECT * FROM transformations WHERE file_id = ? AND status = 'active' ORDER BY created_at, id",
        (file_id,),
    ).fetchall())
    return coalesce_operations([row["operation"] for row in rows])


def preflight_reviewed_versions(connection: Any, project_id: str, file_ids: set[str], newly_accepted: list[dict[str, Any]]) -> None:
    candidates_by_file: dict[str, list[dict[str, Any]]] = {}
    for finding in newly_accepted:
        if finding.get("operation"):
            candidates_by_file.setdefault(finding["file_id"], []).append(finding)
    with tempfile.TemporaryDirectory(prefix="scribe-preflight-") as folder:
        root = Path(folder)
        for file_id in file_ids:
            item = file_record(connection, project_id, file_id)
            operations = coalesce_operations([*active_operations(connection, file_id), *[item["operation"] for item in candidates_by_file.get(file_id, []) if item.get("operation")]])
            destination = root / file_id / item["filename"]
            apply_file_operations(item, Path(item["original_path"]), destination, operations)
            profile_dataset(destination, item["filename"])


def create_reviewed_version(connection: Any, project_id: str, file_id: str) -> dict[str, Any]:
    item = file_record(connection, project_id, file_id)
    operations = active_operations(connection, file_id)
    version_number = connection.execute("SELECT COALESCE(MAX(version_number), 0) + 1 FROM reviewed_versions WHERE file_id = ?", (file_id,)).fetchone()[0]
    destination = project_root(project_id) / "reviewed" / file_id / f"v{version_number}" / item["filename"]
    partial = destination.with_name(f"{destination.stem}.building{destination.suffix}")
    source_path = Path(item["original_path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        output = apply_file_operations(item, source_path, partial, operations)
        validation_profile = profile_dataset(partial, item["filename"])
        change_validation = validate_operation_result(item, source_path, partial, operations)
        format_validation = validate_format_contract(item, source_path, partial, operations)
        output_row_ids = reviewed_row_ids(file_id, original_shape(item)[0], operations)
        if len(output_row_ids) != validation_profile["row_count"]:
            raise ValueError("Reviewed output row lineage does not match the rebuilt dataset")
        partial.replace(destination)
        row_map_path = destination.parent / "row-map.json"
        write_row_map(row_map_path, output_row_ids)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    plan_hash = hashlib.sha256("\n".join(operation_hash(operation) for operation in operations).encode("utf-8")).hexdigest()
    validation = {"status": "passed", "rows": validation_profile["row_count"], "columns": validation_profile["column_count"], "source_hash_valid": hashlib.sha256(source_path.read_bytes()).hexdigest() == item["sha256"], "change_validation": change_validation, "format_contract": format_validation}
    version = {"id": new_id("ver"), "project_id": project_id, "file_id": file_id, "version_number": version_number, "path": str(destination), "sha256": output["sha256"], "transformation_count": len(operations), "status": "ready", "created_at": now(), "plan_hash": plan_hash, "validation_json": json_dump(validation), "row_map_path": str(row_map_path)}
    try:
        connection.execute("""INSERT INTO reviewed_versions(id, project_id, file_id, version_number, path, sha256,
            transformation_count, status, created_at, plan_hash, validation_json, row_map_path)
            VALUES (:id, :project_id, :file_id, :version_number, :path, :sha256, :transformation_count,
            :status, :created_at, :plan_hash, :validation_json, :row_map_path)""", version)
        audit(connection, project_id, "reviewed_version_created", {"file_id": file_id, "version": version_number, "transformations": len(operations), "sha256": output["sha256"]})
    except Exception:
        destination.unlink(missing_ok=True)
        row_map_path.unlink(missing_ok=True)
        try:
            destination.parent.rmdir()
        except OSError:
            pass
        raise
    return version


def rebuild_reviewed(project_id: str, file_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        return create_reviewed_version(connection, project_id, file_id)


def unique_operations(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings:
        operation = finding.get("operation")
        if not operation:
            continue
        key = json.dumps(operation, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            operations.append(operation)
    return operations


def dependent_accepted_findings(connection: Any, file_id: str, root: dict[str, Any]) -> list[dict[str, Any]]:
    root_operation = root.get("operation")
    if not root_operation or root_operation.get("type") == "delete_rows":
        return []
    accepted = db.rows_dict(connection.execute(
        "SELECT * FROM findings WHERE file_id = ? AND status = 'accepted' AND id != ? AND operation_json IS NOT NULL ORDER BY created_at",
        (file_id, root["id"]),
    ).fetchall())
    dependencies = [root_operation]
    dependent: list[dict[str, Any]] = []
    remaining = accepted[:]
    while remaining:
        changed = False
        for finding in remaining[:]:
            operation = finding.get("operation") or {}
            rows = set(operation.get("rows") or [])
            is_dependent = any(
                operation.get("column") == prior.get("column")
                and rows.intersection(prior.get("rows") or [])
                and operation.get("before") == prior.get("after")
                for prior in dependencies
            )
            if is_dependent:
                dependent.append(finding)
                dependencies.append(operation)
                remaining.remove(finding)
                changed = True
        if not changed:
            break
    return dependent


def active_plan_hash(connection: Any, file_id: str) -> str:
    rows = connection.execute(
        "SELECT operation_hash FROM transformations WHERE file_id = ? AND status = 'active' ORDER BY operation_hash",
        (file_id,),
    ).fetchall()
    return hashlib.sha256("\n".join(row[0] for row in rows).encode("utf-8")).hexdigest()


def ruleset_hash(connection: Any, file_id: str) -> str:
    rows = connection.execute(
        "SELECT signature FROM validation_rules WHERE file_id = ? AND status = 'confirmed' ORDER BY signature",
        (file_id,),
    ).fetchall()
    study_row = connection.execute(
        "SELECT sc.config_hash FROM study_configs sc JOIN files f ON f.project_id = sc.project_id WHERE f.id = ?",
        (file_id,),
    ).fetchone()
    signatures = [row[0] or "" for row in rows]
    if study_row:
        signatures.append(f"study:{study_row[0]}")
    return hashlib.sha256("\n".join(signatures).encode("utf-8")).hexdigest()


def create_scan_record(
    connection: Any,
    project_id: str,
    file_id: str,
    source_sha256: str,
    profile: dict[str, Any],
    status: str = "complete",
    *,
    source_version_id: str | None = None,
    source_kind: str = "original",
) -> str:
    connection.execute("UPDATE scan_runs SET status = 'superseded' WHERE file_id = ? AND status = 'complete'", (file_id,))
    scan_id = new_id("scan")
    timestamp = now()
    connection.execute(
        """INSERT INTO scan_runs(id, project_id, file_id, source_sha256, reviewed_plan_hash,
           engine_version, ruleset_hash, status, finding_count, created_at, completed_at,
           source_version_id, source_kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scan_id, project_id, file_id, source_sha256, active_plan_hash(connection, file_id), ENGINE_VERSION,
         ruleset_hash(connection, file_id), status, len(profile.get("findings", [])), timestamp,
         timestamp if status == "complete" else None, source_version_id, source_kind),
    )
    return scan_id


def persist_check_results(connection: Any, project_id: str, file_id: str, scan_id: str, profile: dict[str, Any]) -> None:
    categories = {item["category"] for item in profile.get("findings", [])}
    columns = profile.get("columns", [])
    file_format_row = connection.execute("SELECT format, original_path, sha256 FROM files WHERE id = ?", (file_id,)).fetchone()
    file_format = file_format_row["format"]
    original_hash_valid = hashlib.sha256(Path(file_format_row["original_path"]).read_bytes()).hexdigest() == file_format_row["sha256"]
    rule_types = {row[0] for row in connection.execute("SELECT rule_type FROM validation_rules WHERE file_id = ? AND status = 'confirmed'", (file_id,)).fetchall()}
    project_file_count = connection.execute("SELECT COUNT(*) FROM files WHERE project_id = ?", (project_id,)).fetchone()[0]
    runtime_status = r_status()

    def status_for(relevant: set[str], *, applicable: bool = True, blocked: bool = False) -> str:
        if not applicable:
            return "not_applicable"
        if blocked:
            return "blocked"
        return "attention" if categories & relevant else "pass"

    checks = [
        (1, "file_integrity", "File integrity", status_for({"corrupted_row"}), {"encoding": profile.get("encoding"), "delimiter": profile.get("delimiter"), "warnings": profile.get("warnings", []), "source_hash_valid": original_hash_valid}),
        (2, "column_structure", "Column structure", status_for({"duplicate_column_name", "duplicate_column", "empty_column"}), {"columns": profile.get("column_count"), "schema": [item.get("name") for item in columns]}),
        (3, "missing_data", "Missing data", status_for({"missing_value", "missing_code_normalization"}), {"missing_by_column": {item["name"]: item.get("missing_count", 0) for item in columns}}),
        (4, "duplicate_detection", "Duplicate detection", status_for({"duplicate_id", "duplicate_row", "near_duplicate", "potential_duplicate", "identity_not_verifiable", "survey_duplicate_submission"}), {"candidate_id_columns": profile.get("candidate_id_columns", [])}),
        (5, "data_types", "Data types", status_for({"invalid_type"}), {"inferred_types": {item["name"]: item.get("inferred_type") for item in columns}}),
        (6, "text_standardization", "Text standardization", status_for({"whitespace", "text_normalization"}), {}),
        (7, "categorical_values", "Categorical values", status_for({"inconsistent_category", "codebook_inference"}), {}),
        (8, "numeric_validation", "Numeric validation", status_for({"invalid_type", "impossible_value", "outlier", "rule_range", "arithmetic_inference", "arithmetic_inconsistency", "arithmetic_relationship_unverified"}), {}),
        (9, "date_validation", "Date validation", status_for({"date_standardization", "ambiguous_date"}), {}),
        (10, "rating_scales", "Rating scales", status_for({"invalid_scale", "survey_invalid_scale"}, applicable=any(SCALE_PATTERN.search(item.get("name", "")) for item in columns) or "scale" in rule_types or "survey_invalid_scale" in categories), {}),
        (11, "cross_column", "Cross-column validation", status_for({"rule_cross_column", "arithmetic_inference", "arithmetic_inconsistency", "arithmetic_relationship_unverified", "survey_skip_logic", "survey_cross_field"}, applicable="cross_column" in rule_types or bool(profile.get("verified_relationships")) or bool(categories & {"survey_skip_logic", "survey_cross_field"})), {"verified_relationships": profile.get("verified_relationships", [])}),
        (12, "cross_file", "Cross-file validation", status_for({"cross_file"}, applicable=project_file_count > 1), {"project_file_count": project_file_count}),
        (13, "statistical_quality", "Statistical quality", status_for({"constant_column", "outlier", "suspicious_pattern", "survey_incomplete", "survey_straightlining", "survey_attention_check", "survey_speeder"}), {"survey_checks": "configured" if connection.execute("SELECT 1 FROM study_configs WHERE project_id = ? AND status = 'confirmed'", (project_id,)).fetchone() else "not_configured"}),
        (14, "formatting_consistency", "Formatting consistency", status_for({"whitespace", "text_normalization", "corrupted_row", "spreadsheet_formula", "broken_text_encoding"}), {"encoding": profile.get("encoding"), "delimiter": profile.get("delimiter")}),
        (15, "metadata_preservation", "Metadata preservation", "not_applicable" if file_format in {"csv", "tsv"} else "blocked", {"format": file_format, "metadata": profile.get("format_metadata", {})}),
        (16, "safety_reproducibility", "Safety & reproducibility", "failed" if not original_hash_valid else ("pass" if runtime_status["ready"] else "attention"), {"source_hash_valid": original_hash_valid, "r_runtime_available": runtime_status["available"], "r_runtime_ready": runtime_status["ready"], "r_runtime_message": runtime_status["message"]}),
        (17, "final_validation", "Final validation", "blocked", {"reason": "A verified export has not passed all validation gates."}),
    ]
    timestamp = now()
    for section, key, name, check_status, evidence in checks:
        applicability = "not_applicable" if check_status == "not_applicable" else "applicable"
        connection.execute(
            """INSERT OR REPLACE INTO check_results(id, scan_id, project_id, file_id, section_number,
               check_key, name, status, applicability, evidence_json, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id("chk"), scan_id, project_id, file_id, section, key, name, check_status, applicability,
             json_dump(evidence), "", timestamp),
        )


def persist_candidates(connection: Any, project_id: str, file_id: str, profile: dict[str, Any], filename: str, scan_id: str | None = None) -> None:
    for candidate in profile["findings"]:
        operation = candidate["operation"]
        proposed = candidate["proposed"]
        insert_finding(connection, project_id, file_id, None, candidate["category"], candidate["severity"], candidate["confidence"], candidate["title"], candidate["explanation"], profile["table_name"], candidate["column_name"], candidate["row_number"], candidate["before"], proposed, operation, candidate["affected_count"], candidate.get("record_key"), scan_id=scan_id, evidence={"detector": candidate["category"], "affected_count": candidate["affected_count"]}, row_id=candidate.get("row_id"))


def insert_finding(connection: Any, project_id: str, file_id: str, rule_id: str | None, category: str, severity: str, confidence: str, title: str, explanation: str, table_name: str, column_name: str | None, row_number: int | None, before: Any, proposed: Any, operation: dict[str, Any] | None, affected_count: int = 1, record_key: str | None = None, scan_id: str | None = None, evidence: dict[str, Any] | None = None, row_id: str | None = None) -> str:
    if scan_id is None:
        row = connection.execute(
            "SELECT id FROM scan_runs WHERE file_id = ? AND status = 'complete' ORDER BY created_at DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        scan_id = row[0] if row else None
    fingerprint_payload = {
        "rule_id": rule_id,
        "category": category,
        "column": column_name,
        "row": row_number,
        "before": before,
        "proposed": proposed,
        "operation": operation,
        "title": title,
    }
    fingerprint = hashlib.sha256(json_dump(fingerprint_payload).encode("utf-8")).hexdigest()
    if scan_id:
        existing = connection.execute(
            "SELECT id FROM findings WHERE scan_id = ? AND fingerprint = ? LIMIT 1",
            (scan_id, fingerprint),
        ).fetchone()
        if existing:
            return existing[0]
    finding_id = new_id("iss")
    source_plan_hash = active_plan_hash(connection, file_id)
    connection.execute("""INSERT INTO findings(id, project_id, file_id, rule_id, category, severity, confidence, title, explanation, table_name, column_name, row_number, record_key, before_json, proposed_json, operation_json, affected_count, status, created_at, scan_id, fingerprint, detector_version, disposition, evidence_json, applicability, row_id, source_plan_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 'pending', ?, 'applicable', ?, ?)""", (finding_id, project_id, file_id, rule_id, category, severity, confidence, title, explanation, table_name, column_name, row_number, record_key, json_dump(before), json_dump(proposed), json_dump(operation) if operation else None, affected_count, now(), scan_id, fingerprint, DETECTOR_VERSION, json_dump(evidence or {}), row_id, source_plan_hash))
    return finding_id


def evaluate_rule(connection: Any, project_id: str, item: dict[str, Any], rule_id: str, rule_type: str, parameters: dict[str, Any]) -> int:
    connection.execute("DELETE FROM findings WHERE rule_id = ? AND status = 'pending'", (rule_id,))
    version = latest_version(connection, item["id"])
    data_path = Path(version["path"]) if version else Path(item["original_path"])
    frame = load_frame(data_path, item["format"], item.get("profile"))
    source_row_ids = row_ids_for_source(item, version, len(frame))
    table = (item.get("profile") or {}).get("table_name", Path(item["filename"]).stem)
    column = parameters.get("column")
    if column and column not in frame.columns:
        raise HTTPException(422, f"Column {column!r} does not exist")
    violations: list[tuple[int, Any, Any, dict[str, Any] | None, str, int]] = []
    values = frame[column].tolist() if column else []
    if rule_type == "composite_unique":
        key_columns = parameters.get("columns") or []
        if len(key_columns) < 2 or any(name not in frame.columns for name in key_columns):
            raise HTTPException(422, "Composite uniqueness requires at least two existing columns")
        seen_keys: dict[tuple[str, ...], list[int]] = {}
        for index, row in frame.iterrows():
            key = tuple(str(row[name]).strip() for name in key_columns)
            if all(key):
                seen_keys.setdefault(key, []).append(int(frame.index.get_loc(index)) + 1)
        for key, rows in seen_keys.items():
            if len(rows) > 1:
                violations.append((rows[0], dict(zip(key_columns, key)), None, None, f"Composite key occurs on {len(rows)} rows; no row will be removed automatically.", len(rows)))
        column = " + ".join(key_columns)
    elif rule_type == "unique":
        seen: dict[str, list[int]] = {}
        for index, value in enumerate(values, start=1):
            key = str(value).strip()
            if key and not is_missing_value(value, column):
                seen.setdefault(key, []).append(index)
        for value, rows in seen.items():
            if len(rows) > 1:
                violations.append((rows[0], value, None, None, f"Value occurs on {len(rows)} rows; Scribe will not choose a record to delete.", len(rows)))
    elif rule_type == "required":
        rows = [index for index, value in enumerate(values, start=1) if is_missing_value(value, column)]
        if rows:
            violations.append((rows[0], values[rows[0] - 1], None, None, f"{len(rows)} required values are missing", len(rows)))
    elif rule_type == "missing_codes":
        codes = {str(value) for value in parameters.get("codes", [])}
        replacement = parameters.get("replacement", "")
        for code in codes:
            rows = [index + 1 for index, value in enumerate(values) if str(value).strip() == code]
            if rows:
                violations.append((rows[0], code, replacement, {"type": "normalize_missing", "column": column, "before": code, "after": replacement, "rows": rows}, f"{len(rows)} values use the confirmed missing code {code!r}", len(rows)))
    elif rule_type == "allowed_values":
        allowed = {str(value) for value in parameters.get("values", [])}
        mappings = {str(key): value for key, value in parameters.get("mappings", {}).items()}
        for value in sorted({str(value) for value in values if not is_missing_value(value, column) and str(value).strip() not in allowed}):
            rows = [index + 1 for index, current in enumerate(values) if str(current) == value]
            proposed = mappings.get(value)
            operation = {"type": "map_category", "column": column, "before": value, "after": proposed, "rows": rows} if proposed is not None else None
            violations.append((rows[0], value, proposed, operation, f"{len(rows)} values are outside the confirmed category set", len(rows)))
    elif rule_type in {"range", "scale"}:
        minimum, maximum = parameters.get("minimum"), parameters.get("maximum")
        for index, value in enumerate(values, start=1):
            if is_missing_value(value, column):
                continue
            try:
                number = float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                # Type validation owns this observation. Repeating it as a range
                # violation creates two review tasks for the same cell.
                continue
            if (minimum is not None and number < float(minimum)) or (maximum is not None and number > float(maximum)):
                violations.append((index, value, None, None, f"Value is outside the confirmed range {minimum} to {maximum}", 1))
    elif rule_type == "pattern":
        pattern_type = parameters.get("pattern_type", "regex")
        regex = parameters.get("regex")
        compiled = None
        if pattern_type == "regex":
            if not regex or len(str(regex)) > 500:
                raise HTTPException(422, "A regex pattern of at most 500 characters is required")
            try:
                compiled = re.compile(str(regex))
            except re.error as error:
                raise HTTPException(422, f"Invalid regex: {error}") from error
        validators = {"email": is_valid_email, "phone": is_valid_phone, "blood_pressure": is_valid_blood_pressure}
        validator = validators.get(pattern_type)
        grouped_invalid: dict[str, list[int]] = {}
        for index, value in enumerate(values, start=1):
            if is_missing_value(value, column):
                continue
            valid = bool(compiled.fullmatch(str(value))) if compiled else bool(validator and validator(str(value)))
            if not valid:
                grouped_invalid.setdefault(str(value), []).append(index)
        for value, rows in grouped_invalid.items():
            violations.append((rows[0], value, None, None, f"{len(rows)} value(s) do not match the confirmed {pattern_type.replace('_', ' ')} pattern", len(rows)))
    elif rule_type in {"type", "date"}:
        expected = parameters.get("expected", "date" if rule_type == "date" else "number")
        grouped_values: dict[str, list[int]] = {}
        originals: dict[str, Any] = {}
        for index, value in enumerate(values, start=1):
            if not is_missing_value(value, column):
                key = str(value)
                grouped_values.setdefault(key, []).append(index)
                originals[key] = value
        for key, rows in grouped_values.items():
            value = originals[key]
            try:
                if expected == "integer":
                    number = float(str(value).replace(",", ""))
                    if not number.is_integer(): raise ValueError(value)
                elif expected == "number": float(str(value).replace(",", ""))
                elif expected == "date":
                    _, ambiguous = parse_date_with_quality(str(value))
                    if ambiguous:
                        raise ValueError(value)
                elif expected == "boolean" and str(value).strip().casefold() not in {"true", "false", "yes", "no", "y", "n", "0", "1"}: raise ValueError(value)
            except (ValueError, TypeError):
                proposed = None
                operation = None
                if expected in {"integer", "number"}:
                    try:
                        proposed = parse_number_word(str(value))
                        if expected == "integer": proposed = int(proposed)
                        operation = {"type": "parse_type", "column": column, "before": value, "after": proposed, "rows": rows, "expected": expected}
                    except ValueError:
                        pass
                explanation = f"Value is not a valid {expected}" if proposed is None else f"Number word can be deterministically converted to {proposed}"
                if expected == "date":
                    explanation = "Date is invalid or ambiguous; confirm the study date convention before standardizing"
                violations.append((rows[0], value, proposed, operation, explanation, len(rows)))
    elif rule_type == "cross_column":
        when_column, when_equals = parameters.get("when_column"), str(parameters.get("when_equals"))
        then_column, then_equals = parameters.get("then_column"), parameters.get("then_equals")
        if when_column not in frame or then_column not in frame:
            raise HTTPException(422, "A selected cross-column field does not exist")
        for index, row in frame.iterrows():
            if str(row[when_column]) == when_equals and ((then_equals is None and is_missing_value(row[then_column], then_column)) or (then_equals is not None and str(row[then_column]) != str(then_equals))):
                violations.append((int(frame.index.get_loc(index)) + 1, row[then_column], None, None, f"When {when_column} is {when_equals!r}, {then_column} must be {then_equals!r}", 1))
        column = then_column
    inserted_count = 0
    for row, before, proposed, operation, explanation, affected_count in violations[:2000]:
        operation = attach_operation_row_ids(operation, source_row_ids)
        if operation and operation_is_covered(connection, item["id"], operation):
            continue
        already_decided = connection.execute(
            """SELECT 1 FROM findings
               WHERE rule_id = ? AND category = ? AND COALESCE(column_name, '') = COALESCE(?, '')
                 AND COALESCE(row_number, -1) = COALESCE(?, -1) AND before_json = ?
                 AND COALESCE(proposed_json, 'null') = COALESCE(?, 'null')
                 AND COALESCE(operation_json, 'null') = COALESCE(?, 'null')
                 AND status IN ('accepted', 'rejected') LIMIT 1""",
            (rule_id, f"rule_{rule_type}", column, row, json_dump(before), json_dump(proposed), json_dump(operation) if operation else None),
        ).fetchone()
        if already_decided:
            continue
        insert_finding(connection, project_id, item["id"], rule_id, f"rule_{rule_type}", "high" if rule_type in {"unique", "composite_unique", "range", "scale", "cross_column"} else "medium", "high" if operation else "needs_confirmation", f"{rule_type.replace('_', ' ').title()} rule violation", explanation, table, column, row, before, proposed, operation, affected_count, row_id=source_row_ids[row - 1] if row else None)
        inserted_count += 1
    return inserted_count


def operation_is_covered(connection: Any, file_id: str, candidate: dict[str, Any]) -> bool:
    candidate_targets = candidate.get("row_ids") or candidate.get("rows") or []
    if candidate.get("type") == "delete_rows" or not candidate_targets:
        return False
    covered_rows: set[Any] = set()
    findings = db.rows_dict(connection.execute(
        "SELECT operation_json FROM findings WHERE file_id = ? AND status IN ('pending', 'accepted') AND operation_json IS NOT NULL",
        (file_id,),
    ).fetchall())
    for finding in findings:
        operation = finding.get("operation") or {}
        if (
            operation.get("type") == candidate.get("type")
            and operation.get("column") == candidate.get("column")
            and operation.get("before") == candidate.get("before")
            and operation.get("after") == candidate.get("after")
            and operation.get("expected") == candidate.get("expected")
        ):
            covered_rows.update(operation.get("row_ids") or operation.get("rows") or [])
    return set(candidate_targets).issubset(covered_rows)


def write_reports(root: Path, manifest: dict[str, Any], events: list[dict[str, Any]], decisions: list[dict[str, Any]], findings: list[dict[str, Any]], readiness_report: dict[str, Any]) -> None:
    (root / "manifest.json").write_text(json_dump(manifest), encoding="utf-8")
    (root / "audit.json").write_text(json_dump({"events": events, "decisions": decisions, "findings": findings}), encoding="utf-8")
    (root / "readiness.json").write_text(json_dump(readiness_report), encoding="utf-8")
    with (root / "audit_log.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "event_type", "created_at", "payload"])
        writer.writeheader()
        for event in events:
            writer.writerow({key: safe_report_cell(value) for key, value in {"id": event["id"], "event_type": event["event_type"], "created_at": event["created_at"], "payload": json_dump(event.get("payload"))}.items()})
    with (root / "decision_log.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ["id", "finding_id", "finding_title", "decision", "edited_value", "rationale", "created_at"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for decision in decisions:
            writer.writerow({key: safe_report_cell(json_dump(decision.get(key)) if key == "edited_value" else decision.get(key)) for key in fields})
    fields = ["id", "filename", "severity", "category", "title", "table_name", "column_name", "row_number", "before", "proposed", "status", "affected_count"]
    with (root / "findings.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for finding in findings:
            writer.writerow({key: safe_report_cell(json_dump(finding.get(key)) if key in {"before", "proposed"} else finding.get(key)) for key in fields})


def safe_report_cell(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def artifact_kind(path: Path) -> str:
    if "cleaned" in path.parts: return "cleaned_dataset"
    if "scripts" in path.parts: return "r_script"
    if path.name.startswith("audit") or path.name.startswith("decision"): return "audit"
    if path.name.startswith("findings"): return "findings_report"
    if path.name.startswith("readiness"): return "readiness_report"
    if path.name.startswith("manifest"): return "manifest"
    return "original_copy"


def register_artifact(connection: Any, export_id: str, kind: str, path: Path) -> None:
    content = path.read_bytes()
    connection.execute("INSERT INTO export_artifacts(id, export_id, kind, filename, path, size_bytes, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id("art"), export_id, kind, path.name, str(path), len(content), hashlib.sha256(content).hexdigest()))


def audit(connection: Any, project_id: str, event_type: str, payload: dict[str, Any]) -> None:
    connection.execute("INSERT INTO audit_events(id, project_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)", (new_id("evt"), project_id, event_type, json_dump(payload), now()))
