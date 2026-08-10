from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
import json
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import database as db
from backend.app.main import app, insert_finding


class ApiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_root = db.DATA_ROOT
        db.configure_storage(Path(self.temporary.name))
        db.ensure_storage()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        db.configure_storage(self.previous_root)
        self.temporary.cleanup()

    def create_project(self) -> str:
        response = self.client.post("/api/projects", json={"name": "Workflow study", "description": "API verification"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def upload_seed(self, project_id: str) -> tuple[dict, bytes]:
        content = b"participant_id,status,occupation,age\nP001,Active,Teacher  ,30\nP002,active,Nurse,999\nP002,active,Nurse,999\n"
        response = self.client.post(
            f"/api/projects/{project_id}/files?filename=participants.csv",
            content=content,
            headers={"content-type": "text/csv"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json(), content

    def test_project_upload_preview_decision_undo_and_export(self) -> None:
        project_id = self.create_project()
        uploaded, original = self.upload_seed(project_id)
        file_id = uploaded["id"]
        original_path = Path(uploaded["original_path"])
        original_hash = hashlib.sha256(original).hexdigest()

        inventory = self.client.get(f"/api/projects/{project_id}/files").json()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["sha256"], original_hash)
        processing = self.client.get(f"/api/projects/{project_id}/files/{file_id}/processing-status").json()
        self.assertEqual(processing["status"], "complete")
        preview = self.client.get(f"/api/projects/{project_id}/files/{file_id}/preview?version=original").json()
        self.assertEqual(preview["rows"][0]["values"]["occupation"], "Teacher  ")

        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        whitespace = next(item for item in findings if item["category"] == "whitespace")
        accepted = self.client.post(
            f"/api/projects/{project_id}/findings/{whitespace['id']}/decision",
            json={"decision": "accepted"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        reviewed = self.client.get(f"/api/projects/{project_id}/files/{file_id}/preview?version=reviewed").json()
        self.assertEqual(reviewed["rows"][0]["values"]["occupation"], "Teacher")
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(), original_hash)

        undone = self.client.post(f"/api/projects/{project_id}/findings/{whitespace['id']}/undo")
        self.assertEqual(undone.status_code, 200, undone.text)
        rebuilt = self.client.get(f"/api/projects/{project_id}/files/{file_id}/preview?version=reviewed").json()
        self.assertEqual(rebuilt["rows"][0]["values"]["occupation"], "Teacher  ")
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(), original_hash)

        duplicate_row = next(item for item in findings if item["category"] == "duplicate_row")
        batch = self.client.post(f"/api/projects/{project_id}/findings/batch", json={"finding_ids": [duplicate_row["id"]], "decision": "accepted"})
        self.assertEqual(batch.status_code, 422, batch.text)
        group = [duplicate_row["row_id"], *duplicate_row["operation"]["row_ids"]]
        decision = self.client.post(
            f"/api/projects/{project_id}/findings/{duplicate_row['id']}/decision",
            json={"decision": "accepted", "retained_row_id": group[0], "removed_row_ids": group[1:]},
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        deduplicated = self.client.get(f"/api/projects/{project_id}/files/{file_id}/preview?version=reviewed").json()
        self.assertEqual(deduplicated["total"], 2)

        range_rule = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": file_id, "name": "Age must be plausible", "rule_type": "range", "parameters": {"column": "age", "minimum": 0, "maximum": 120}},
        )
        self.assertEqual(range_rule.status_code, 201, range_rule.text)
        self.assertGreater(range_rule.json()["finding_count"], 0)
        duplicate_rule = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": file_id, "name": "Same rule again", "rule_type": "range", "parameters": {"column": "age", "minimum": 0, "maximum": 120}},
        )
        self.assertEqual(duplicate_rule.status_code, 201, duplicate_rule.text)
        self.assertTrue(duplicate_rule.json()["duplicate"])
        active_ranges = [rule for rule in self.client.get(f"/api/projects/{project_id}/rules").json() if rule["status"] == "confirmed" and rule["rule_type"] == "range"]
        self.assertEqual(len(active_ranges), 1)

        export = self.client.post(f"/api/projects/{project_id}/exports")
        self.assertEqual(export.status_code, 201, export.text)
        payload = export.json()
        self.assertEqual(payload["status"], "complete")
        kinds = {artifact["kind"] for artifact in payload["artifacts"]}
        self.assertTrue({"bundle", "cleaned_dataset", "r_script", "manifest", "audit", "findings_report", "readiness_report"}.issubset(kinds))
        bundle = self.client.get(f"/api/exports/{payload['id']}/download")
        self.assertEqual(bundle.status_code, 200)
        zip_path = Path(self.temporary.name) / "bundle.zip"
        zip_path.write_bytes(bundle.content)
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("decision_log.csv", names)
            self.assertIn("cleaned/participants_cleaned.csv", names)
            self.assertIn("scripts/clean_participants.R", names)
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(), original_hash)

    def test_row_lineage_survives_deletion_rescan_and_repeated_values(self) -> None:
        project_id = self.create_project()
        response = self.client.post(
            f"/api/projects/{project_id}/files?filename=lineage.csv",
            content=b"id,name,age\n1,A,forty\n1,A,forty\n2,B,forty\n",
            headers={"content-type": "text/csv"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        file_id = response.json()["id"]
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        duplicate = next(item for item in findings if item["category"] == "duplicate_row")
        group = [duplicate["row_id"], *duplicate["operation"]["row_ids"]]
        removed = self.client.post(
            f"/api/projects/{project_id}/findings/{duplicate['id']}/decision",
            json={"decision": "accepted", "retained_row_id": group[0], "removed_row_ids": group[1:]},
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/reanalyze").status_code, 200)
        refreshed = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        correction = next(item for item in refreshed if item["category"] == "invalid_type")
        self.assertEqual(correction["operation"]["row_ids"], [group[0], f"{file_id}:row:3"])
        accepted = self.client.post(
            f"/api/projects/{project_id}/findings/{correction['id']}/decision",
            json={"decision": "accepted"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        reviewed = self.client.get(f"/api/projects/{project_id}/files/{file_id}/preview?version=reviewed&limit=10").json()
        self.assertEqual([row["values"]["age"] for row in reviewed["rows"]], ["40", "40"])

    def test_project_trash_restore_and_confirmed_permanent_deletion(self) -> None:
        project_id = self.create_project()
        uploaded, original = self.upload_seed(project_id)
        original_path = Path(uploaded["original_path"])
        original_hash = hashlib.sha256(original).hexdigest()
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        whitespace = next(item for item in findings if item["category"] == "whitespace")
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/findings/{whitespace['id']}/decision", json={"decision": "accepted"}).status_code, 200)

        trashed = self.client.post(f"/api/projects/{project_id}/trash")
        self.assertEqual(trashed.status_code, 200, trashed.text)
        self.assertEqual([item["id"] for item in self.client.get("/api/projects").json()], [])
        self.assertEqual([item["id"] for item in self.client.get("/api/projects?status=trash").json()], [project_id])
        blocked = self.client.get(f"/api/projects/{project_id}/files")
        self.assertEqual(blocked.status_code, 423, blocked.text)

        restored = self.client.post(f"/api/projects/{project_id}/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(), original_hash)

        self.assertEqual(self.client.post(f"/api/projects/{project_id}/trash").status_code, 200)
        wrong = self.client.request("DELETE", f"/api/projects/{project_id}", json={"project_name": "wrong"})
        self.assertEqual(wrong.status_code, 422, wrong.text)
        deleted = self.client.request("DELETE", f"/api/projects/{project_id}", json={"project_name": "Workflow study"})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(original_path.exists())
        self.assertEqual(self.client.get(f"/api/projects/{project_id}").status_code, 404)
        with db.connect() as connection:
            tombstone = connection.execute("SELECT * FROM deletion_tombstones WHERE project_id = ?", (project_id,)).fetchone()
        self.assertIsNotNone(tombstone)
        self.assertEqual(tombstone["cleanup_status"], "complete")

    def test_permanent_deletion_restores_directory_when_database_work_fails(self) -> None:
        project_id = self.create_project()
        uploaded, _ = self.upload_seed(project_id)
        original_path = Path(uploaded["original_path"])
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/trash").status_code, 200)
        with patch("backend.app.project_lifecycle._delete_dependencies", side_effect=RuntimeError("injected deletion failure")):
            with self.assertRaises(RuntimeError):
                self.client.request("DELETE", f"/api/projects/{project_id}", json={"project_name": "Workflow study"})
        self.assertTrue(original_path.exists())
        self.assertEqual(self.client.get(f"/api/projects/{project_id}").status_code, 200)

    def test_automated_rules_and_gemini_proposals_remain_review_only(self) -> None:
        project_id = self.create_project()
        uploaded, _ = self.upload_seed(project_id)
        reanalyzed = self.client.post(f"/api/projects/{project_id}/reanalyze")
        self.assertEqual(reanalyzed.status_code, 200, reanalyzed.text)
        self.assertGreater(reanalyzed.json()["finding_count"], 0)
        suggestions = self.client.get(f"/api/projects/{project_id}/rules/suggestions").json()
        self.assertTrue(any(item.get("recommended") for item in suggestions))
        confirmed = self.client.post(f"/api/projects/{project_id}/rules/auto-confirm")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertGreater(confirmed.json()["confirmed_count"], 0)

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                answer = {"answer": "The selected row contains a category spelling that may need review.", "proposals": [{"title": "Standardize status", "explanation": "The dominant label is Active.", "file_id": uploaded["id"], "column": "status", "row_number": 2, "before": "active", "after": "Active", "operation_type": "map_category", "confidence": "medium"}]}
                return json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]}).encode()

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-secret"}, clear=False), patch("backend.app.main.urllib.request.urlopen", return_value=FakeResponse()) as gemini_request:
            status = self.client.get("/api/assistant/status").json()
            self.assertTrue(status["configured"])
            self.assertNotIn("test-secret", json.dumps(status))
            denied = self.client.post(f"/api/projects/{project_id}/assistant/propose", json={"question": "Fix status spelling", "file_id": uploaded["id"], "consent_to_send_data": False})
            self.assertEqual(denied.status_code, 422)
            response = self.client.post(f"/api/projects/{project_id}/assistant/propose", json={"question": "Fix status spelling", "file_id": uploaded["id"], "consent_to_send_data": True})
            self.assertEqual(gemini_request.call_args.kwargs["context"].verify_mode, 2)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proposal_count"], 1)
        finding = next(item for item in self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"] if item["category"] == "ai_custom")
        self.assertEqual(finding["status"], "pending")
        self.assertEqual(finding["confidence"], "needs_confirmation")

    def test_edited_grouped_correction_keeps_all_exact_source_rows(self) -> None:
        project_id = self.create_project()
        content = b"participant_id,age\nP001,forty\nP002,40\nP003,forty\nP004,nan\n"
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=written-ages.csv",
            content=content,
            headers={"content-type": "text/csv"},
        ).json()
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        conversion = next(item for item in findings if item["category"] == "invalid_type")
        self.assertEqual(conversion["affected_count"], 2)
        accepted = self.client.post(
            f"/api/projects/{project_id}/findings/{conversion['id']}/decision",
            json={"decision": "accepted", "edited_value": "40"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        preview = self.client.get(f"/api/projects/{project_id}/files/{uploaded['id']}/preview?version=reviewed&limit=10").json()
        self.assertEqual([row["values"]["age"] for row in preview["rows"][:3]], ["40", "40", "40"])
        updated = next(item for item in self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"] if item["id"] == conversion["id"])
        self.assertEqual(updated["operation"]["rows"], [1, 3])
        self.assertEqual(updated["operation"]["type"], "parse_type")

    def test_arithmetic_inference_is_reviewable_rebuilt_and_not_arbitrarily_editable(self) -> None:
        project_id = self.create_project()
        lines = ["Transaction ID,Item,Quantity,Price Per Unit,Total Spent"]
        lines.extend(f"T{index},Coffee,2,2,4" for index in range(10))
        lines.append("TARGET,Coffee,ERROR,4,20")
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=cafe.csv",
            content=("\n".join(lines) + "\n").encode(), headers={"content-type": "text/csv"},
        ).json()
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        inference = next(item for item in findings if item["category"] == "arithmetic_inference" and item["column_name"] == "Quantity")
        edited = self.client.post(
            f"/api/projects/{project_id}/findings/{inference['id']}/decision",
            json={"decision": "accepted", "edited_value": "99"},
        )
        self.assertEqual(edited.status_code, 422, edited.text)
        accepted = self.client.post(
            f"/api/projects/{project_id}/findings/{inference['id']}/decision",
            json={"decision": "accepted"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        preview = self.client.get(f"/api/projects/{project_id}/files/{uploaded['id']}/preview?version=reviewed&offset=10&limit=1").json()
        self.assertEqual(preview["rows"][0]["values"]["Quantity"], "5")
        with db.connect() as connection:
            transformation = connection.execute(
                "SELECT operation_json FROM transformations WHERE finding_id = ? AND status = 'active'", (inference["id"],)
            ).fetchone()
        self.assertIn('"formula": "Total Spent / Price Per Unit"', transformation[0])

    def test_conflicting_batch_is_atomic_and_returns_actionable_error(self) -> None:
        project_id = self.create_project()
        uploaded, _ = self.upload_seed(project_id)
        with db.connect() as connection:
            finding_ids = [
                insert_finding(
                    connection, project_id, uploaded["id"], None, "test_conflict", "medium", "high",
                    "Conflicting exact correction", "Regression fixture", "participants", "occupation", 2,
                    "Nurse", replacement,
                    {"type": "replace", "column": "occupation", "before": "Nurse", "after": replacement, "rows": [2]},
                )
                for replacement in ("Doctor", "Surgeon")
            ]
        response = self.client.post(
            f"/api/projects/{project_id}/findings/batch",
            json={"finding_ids": finding_ids, "decision": "accepted"},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("No batch decisions were saved", response.json()["detail"])
        with db.connect() as connection:
            statuses = [connection.execute("SELECT status FROM findings WHERE id = ?", (finding_id,)).fetchone()[0] for finding_id in finding_ids]
            decisions = connection.execute(
                f"SELECT COUNT(*) FROM decisions WHERE finding_id IN ({','.join('?' for _ in finding_ids)})",
                finding_ids,
            ).fetchone()[0]
        self.assertEqual(statuses, ["pending", "pending"])
        self.assertEqual(decisions, 0)

    def test_undo_cascades_corrections_that_depend_on_normalization(self) -> None:
        project_id = self.create_project()
        content = b"participant_id,name\nP001, david lee \n"
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=names.csv",
            content=content,
            headers={"content-type": "text/csv"},
        ).json()
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        whitespace = next(item for item in findings if item["category"] == "whitespace")
        self.assertEqual(self.client.post(
            f"/api/projects/{project_id}/findings/{whitespace['id']}/decision",
            json={"decision": "accepted"},
        ).status_code, 200)
        with db.connect() as connection:
            dependent_id = insert_finding(
                connection, project_id, uploaded["id"], None, "manual", "medium", "high",
                "Capitalize name", "Researcher-edited correction", "names", "name", 1,
                "david lee", "David Lee",
                {"type": "replace", "column": "name", "before": "david lee", "after": "David Lee", "rows": [1]},
            )
        self.assertEqual(self.client.post(
            f"/api/projects/{project_id}/findings/{dependent_id}/decision",
            json={"decision": "accepted"},
        ).status_code, 200)
        undone = self.client.post(f"/api/projects/{project_id}/findings/{whitespace['id']}/undo")
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(undone.json()["cascaded_finding_ids"], [dependent_id])
        with db.connect() as connection:
            statuses = [connection.execute("SELECT status FROM findings WHERE id = ?", (item,)).fetchone()[0] for item in (whitespace["id"], dependent_id)]
        self.assertEqual(statuses, ["pending", "pending"])

    def test_upload_rejects_unsafe_duplicate_empty_and_unsupported_inputs(self) -> None:
        project_id = self.create_project()
        content = b"participant_id,age\nP001,30\n"
        first = self.client.post(
            f"/api/projects/{project_id}/files?filename=safe.csv",
            content=content,
            headers={"content-type": "text/csv"},
        )
        self.assertEqual(first.status_code, 201, first.text)
        duplicate = self.client.post(
            f"/api/projects/{project_id}/files?filename=copy.csv",
            content=content,
            headers={"content-type": "text/csv"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertIn("already uploaded", duplicate.json()["detail"])
        traversal = self.client.post(
            f"/api/projects/{project_id}/files?filename=..%2F..%2Fescape.csv",
            content=b"id\n1\n",
            headers={"content-type": "text/csv"},
        )
        self.assertEqual(traversal.status_code, 400, traversal.text)
        empty = self.client.post(
            f"/api/projects/{project_id}/files?filename=empty.csv",
            content=b"",
            headers={"content-type": "text/csv"},
        )
        self.assertEqual(empty.status_code, 400, empty.text)
        unsupported = self.client.post(
            f"/api/projects/{project_id}/files?filename=notes.exe",
            content=b"not a dataset",
        )
        self.assertEqual(unsupported.status_code, 415, unsupported.text)

    def test_revalidation_does_not_duplicate_a_detector_correction(self) -> None:
        project_id = self.create_project()
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=ages.csv",
            content=b"participant_id,age\nP001,forty\n",
            headers={"content-type": "text/csv"},
        ).json()
        rule = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": uploaded["id"], "name": "Age is numeric", "rule_type": "type", "parameters": {"column": "age", "expected": "integer"}},
        ).json()
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        violation = next(item for item in findings if item["category"] == "invalid_type")
        self.assertFalse(any(item["rule_id"] == rule["id"] for item in findings))
        accepted = self.client.post(
            f"/api/projects/{project_id}/findings/{violation['id']}/decision",
            json={"decision": "accepted"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        for _ in range(2):
            response = self.client.post(f"/api/projects/{project_id}/rules/revalidate")
            self.assertEqual(response.status_code, 200, response.text)
        visible = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        matching = [item for item in visible if item["category"] == "invalid_type" and item["row_number"] == 1]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["status"], "accepted")
        self.assertFalse(any(item["rule_id"] == rule["id"] for item in visible))

    def test_rule_violation_covered_by_grouped_accepted_fix_is_not_duplicated(self) -> None:
        project_id = self.create_project()
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=ages.csv",
            content=b"participant_id,age\nP001,forty\nP002,forty\n",
            headers={"content-type": "text/csv"},
        ).json()
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        grouped = next(item for item in findings if item["category"] == "invalid_type")
        self.assertEqual(grouped["operation"]["rows"], [1, 2])
        accepted = self.client.post(
            f"/api/projects/{project_id}/findings/{grouped['id']}/decision",
            json={"decision": "accepted"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        rule = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": uploaded["id"], "name": "Age is numeric", "rule_type": "type", "parameters": {"column": "age", "expected": "integer"}},
        )
        self.assertEqual(rule.status_code, 201, rule.text)
        visible = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        self.assertFalse(any(item["category"] == "rule_type" for item in visible))

    def test_scans_checklist_dispositions_and_verified_export_gate_are_truthful(self) -> None:
        project_id = self.create_project()
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=names.csv",
            content=b"Patient Name,Age\n Nan ,forty\nAlex,nan\n",
            headers={"content-type": "text/csv"},
        ).json()
        current = self.client.get(f"/api/projects/{project_id}/scans/current")
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()[0]["status"], "complete")
        self.assertEqual(len(current.json()[0]["checks"]), 17)

        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        identity = next(item for item in findings if item["category"] == "identity_not_verifiable")
        self.assertEqual(identity["operation"], None)
        arbitrary_edit = self.client.post(
            f"/api/projects/{project_id}/findings/{identity['id']}/decision",
            json={"decision": "accepted", "edited_value": "P001"},
        )
        self.assertEqual(arbitrary_edit.status_code, 422)
        acknowledged = self.client.post(
            f"/api/projects/{project_id}/findings/{identity['id']}/disposition",
            json={"disposition": "acknowledged", "rationale": "No participant identifier was collected."},
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)

        verified = self.client.post(f"/api/projects/{project_id}/exports", json={"kind": "verified"})
        self.assertEqual(verified.status_code, 409)
        review = self.client.post(f"/api/projects/{project_id}/exports", json={"kind": "review"})
        self.assertEqual(review.status_code, 201, review.text)
        self.assertEqual(review.json()["kind"], "review")
        self.assertFalse(review.json()["validation"]["r_reproduced"])
        self.assertEqual(hashlib.sha256(Path(uploaded["original_path"]).read_bytes()).hexdigest(), uploaded["sha256"])

    def test_pattern_rule_runs_and_rule_edits_cannot_create_a_conflict(self) -> None:
        project_id = self.create_project()
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=contacts.csv",
            content=b"participant_id,email,age\nP001,valid@example.com,20\nP002,broken,30\n",
            headers={"content-type": "text/csv"},
        ).json()
        pattern = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": uploaded["id"], "name": "Valid email", "rule_type": "pattern", "parameters": {"column": "email", "pattern_type": "email"}},
        )
        self.assertEqual(pattern.status_code, 201, pattern.text)
        self.assertEqual(pattern.json()["finding_count"], 1)
        first_range = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": uploaded["id"], "name": "Age range", "rule_type": "range", "parameters": {"column": "age", "minimum": 0, "maximum": 120}},
        ).json()
        contradictory = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": uploaded["id"], "name": "Different age range", "rule_type": "range", "parameters": {"column": "age", "minimum": 18, "maximum": 80}},
        )
        self.assertEqual(contradictory.status_code, 409)
        update = self.client.patch(
            f"/api/projects/{project_id}/rules/{first_range['id']}",
            json={"name": "Adult age range", "parameters": {"column": "age", "minimum": 18, "maximum": 80}},
        )
        self.assertEqual(update.status_code, 200, update.text)

    def test_names_cannot_be_declared_unique_without_an_explicit_advanced_override(self) -> None:
        project_id = self.create_project()
        uploaded = self.client.post(
            f"/api/projects/{project_id}/files?filename=people.csv",
            content=b"Patient Name,Age\nAlex Lee,20\nAlex Lee,30\n",
            headers={"content-type": "text/csv"},
        ).json()
        blocked = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": uploaded["id"], "name": "Patient Name is unique", "rule_type": "unique", "parameters": {"column": "Patient Name"}},
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("not reliable participant identifiers", blocked.json()["detail"])
        override = self.client.post(
            f"/api/projects/{project_id}/rules",
            json={"file_id": uploaded["id"], "name": "Approved name uniqueness", "rule_type": "unique", "parameters": {"column": "Patient Name", "override_rationale": "This synthetic register guarantees legal names are unique."}},
        )
        self.assertEqual(override.status_code, 201, override.text)
        self.assertGreater(override.json()["finding_count"], 0)

    def test_legacy_migration_quarantines_accepted_changes_without_deleting_history(self) -> None:
        project_id = self.create_project()
        uploaded, _ = self.upload_seed(project_id)
        findings = self.client.get(f"/api/projects/{project_id}/findings?limit=500").json()["items"]
        whitespace = next(item for item in findings if item["category"] == "whitespace")
        self.assertEqual(self.client.post(
            f"/api/projects/{project_id}/findings/{whitespace['id']}/decision",
            json={"decision": "accepted"},
        ).status_code, 200)
        with db.connect() as connection:
            connection.execute("DELETE FROM transformations")
            connection.execute("DELETE FROM check_results")
            connection.execute("DELETE FROM scan_runs")
            connection.execute("DELETE FROM schema_migrations WHERE version IN (6, 7)")
            connection.execute("UPDATE findings SET scan_id = NULL, fingerprint = NULL, status = 'accepted' WHERE id = ?", (whitespace["id"],))
            connection.execute("UPDATE reviewed_versions SET status = 'ready'")
            connection.execute("UPDATE projects SET needs_rescan = 0 WHERE id = ?", (project_id,))
        db.ensure_storage()
        with db.connect() as connection:
            finding = connection.execute("SELECT status, disposition FROM findings WHERE id = ?", (whitespace["id"],)).fetchone()
            transformation = connection.execute("SELECT status, engine_version FROM transformations WHERE finding_id = ?", (whitespace["id"],)).fetchone()
            legacy_version = connection.execute("SELECT status FROM reviewed_versions WHERE file_id = ? ORDER BY version_number DESC LIMIT 1", (uploaded["id"],)).fetchone()
            audit_count = connection.execute("SELECT COUNT(*) FROM decisions WHERE finding_id = ?", (whitespace["id"],)).fetchone()[0]
        self.assertEqual(tuple(finding), ("superseded", "legacy_accepted"))
        self.assertEqual(tuple(transformation), ("quarantined", "legacy"))
        self.assertEqual(legacy_version[0], "legacy")
        self.assertGreaterEqual(audit_count, 1)

if __name__ == "__main__":
    unittest.main()
