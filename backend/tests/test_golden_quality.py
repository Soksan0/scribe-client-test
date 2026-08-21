from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.app.exporting import apply_operations, coalesce_operations
from backend.app.profiling import profile_delimited
from backend.app.validation import validate_operation_result


class GoldenQualityTests(unittest.TestCase):
    def test_golden_surveys_detect_expected_problems_without_forbidden_changes(self) -> None:
        catalog_path = Path(__file__).parent / "fixtures" / "golden_surveys.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        scorecards = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for fixture in catalog:
                with self.subTest(fixture=fixture["name"]):
                    source = root / f"{fixture['name']}.csv"
                    output = root / f"{fixture['name']}_cleaned.csv"
                    source.write_text(fixture["content"], encoding="utf-8")
                    profile = profile_delimited(source, source.name)
                    categories = {finding["category"] for finding in profile["findings"]}
                    expected = set(fixture["expected_categories"])
                    missed = sorted(expected - categories)
                    self.assertEqual(missed, [])
                    operations = coalesce_operations([
                        finding["operation"] for finding in profile["findings"]
                        if finding.get("operation") and finding.get("confidence") == "high"
                    ])
                    operation_types = {operation["type"] for operation in operations}
                    self.assertFalse(operation_types & set(fixture["forbidden_operations"]))
                    apply_operations(source, output, operations)
                    item = {"id": f"file_{fixture['name']}", "format": "csv", "profile": profile}
                    validation = validate_operation_result(item, source, output, operations)
                    self.assertEqual(validation["unexpected_changed_cells"], 0)
                    scorecards.append({
                        "fixture": fixture["name"],
                        "expected_categories": len(expected),
                        "detected_expected_categories": len(expected & categories),
                        "category_recall": 1.0 if not expected else len(expected & categories) / len(expected),
                        "unexpected_changed_cells": validation["unexpected_changed_cells"],
                    })
        self.assertTrue(all(card["category_recall"] == 1.0 for card in scorecards))
        self.assertTrue(all(card["unexpected_changed_cells"] == 0 for card in scorecards))


if __name__ == "__main__":
    unittest.main()
