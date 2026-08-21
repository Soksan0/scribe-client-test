from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import median
from typing import Any

from .profiling import is_missing_value


@dataclass(frozen=True)
class SurveyFinding:
    category: str
    severity: str
    title: str
    explanation: str
    column_name: str | None
    row_number: int
    before: Any
    affected_count: int = 1


def _plain(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _timestamp(value: Any) -> datetime | None:
    text = _plain(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _compare(left: Any, operator: str, right: Any) -> bool:
    try:
        left_value, right_value = float(left), float(right)
    except (TypeError, ValueError):
        left_value, right_value = _plain(left), _plain(right)
    return {
        "==": left_value == right_value,
        "!=": left_value != right_value,
        "<": left_value < right_value,
        "<=": left_value <= right_value,
        ">": left_value > right_value,
        ">=": left_value >= right_value,
    }.get(operator, False)


def detect_survey_quality(frame: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Run only checks explicitly configured by the researcher; never create edits."""
    findings: list[SurveyFinding] = []
    columns = {str(column) for column in frame.columns}

    participant_keys = [column for column in config.get("participant_keys", []) if column in columns]
    if participant_keys and not config.get("allowed_repeats", False):
        groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for index, row in frame.iterrows():
            key = tuple(_plain(row[column]) for column in participant_keys)
            if all(key):
                groups[key].append(int(frame.index.get_loc(index)) + 1)
        for key, rows in groups.items():
            if len(rows) > 1:
                findings.append(SurveyFinding("survey_duplicate_submission", "high", "Configured participant key repeats", f"The confirmed participant key {dict(zip(participant_keys, key, strict=True))!r} occurs on {len(rows)} rows. Confirm whether these are duplicates or permitted repeat observations.", " + ".join(participant_keys), rows[0], key, len(rows)))

    completion = config.get("completion") or {}
    required = [column for column in completion.get("required_columns", []) if column in columns]
    threshold = float(completion.get("minimum_answered_percent", 0) or 0)
    if required and threshold > 0:
        incomplete: list[tuple[int, float]] = []
        for index, row in frame.iterrows():
            answered = sum(not is_missing_value(row[column], column) for column in required)
            percent = answered / len(required) * 100
            if percent < threshold:
                incomplete.append((int(frame.index.get_loc(index)) + 1, percent))
        if incomplete:
            findings.append(SurveyFinding("survey_incomplete", "medium", "Response below configured completion threshold", f"{len(incomplete)} response(s) answered less than {threshold:g}% of the configured required items. This is a review flag, not an automatic exclusion.", None, incomplete[0][0], {"answered_percent": round(incomplete[0][1], 2), "threshold": threshold}, len(incomplete)))

    for group in config.get("item_groups", []):
        group_columns = [column for column in group.get("columns", []) if column in columns]
        if len(group_columns) < 3:
            continue
        minimum_answered = int(group.get("minimum_answered", 3))
        straightliners: list[int] = []
        for index, row in frame.iterrows():
            values = [_plain(row[column]) for column in group_columns if not is_missing_value(row[column], column)]
            if len(values) >= minimum_answered and len(set(values)) == 1:
                straightliners.append(int(frame.index.get_loc(index)) + 1)
        if straightliners:
            findings.append(SurveyFinding("survey_straightlining", "medium", f"Uniform responses in {group.get('name', 'configured item group')}", f"{len(straightliners)} response(s) selected one value across at least {minimum_answered} configured items. Uniform responses can be valid; review with timing and other quality evidence.", None, straightliners[0], {"columns": group_columns}, len(straightliners)))
        minimum, maximum = group.get("minimum"), group.get("maximum")
        if minimum is not None or maximum is not None:
            invalid: list[tuple[int, str, Any]] = []
            for index, row in frame.iterrows():
                for column in group_columns:
                    value = row[column]
                    if is_missing_value(value, column):
                        continue
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        invalid.append((int(frame.index.get_loc(index)) + 1, column, value)); continue
                    if (minimum is not None and number < float(minimum)) or (maximum is not None and number > float(maximum)):
                        invalid.append((int(frame.index.get_loc(index)) + 1, column, value))
            if invalid:
                findings.append(SurveyFinding("survey_invalid_scale", "high", "Value outside configured response scale", f"{len(invalid)} value(s) fall outside the configured scale for {group.get('name', 'this item group')}. Confirm source coding before correction.", invalid[0][1], invalid[0][0], invalid[0][2], len(invalid)))

    for check in config.get("attention_checks", []):
        column = check.get("column")
        if column not in columns:
            continue
        expected = {_plain(value) for value in check.get("expected_values", [check.get("expected")])}
        failed = [int(frame.index.get_loc(index)) + 1 for index, value in frame[column].items() if not is_missing_value(value, column) and _plain(value) not in expected]
        if failed:
            findings.append(SurveyFinding("survey_attention_check", "medium", "Configured attention check not satisfied", f"{len(failed)} response(s) do not match the configured expected answer for {column!r}. Use this with other evidence; do not infer fraud from this flag alone.", column, failed[0], frame.iloc[failed[0] - 1][column], len(failed)))

    timing = config.get("timestamp_columns") or {}
    start_column, end_column, duration_column = timing.get("start"), timing.get("end"), timing.get("duration")
    durations: list[tuple[int, float]] = []
    for position, (_, row) in enumerate(frame.iterrows(), start=1):
        duration: float | None = None
        if duration_column in columns:
            try: duration = float(row[duration_column])
            except (TypeError, ValueError): pass
        elif start_column in columns and end_column in columns:
            start, end = _timestamp(row[start_column]), _timestamp(row[end_column])
            if start and end: duration = (end - start).total_seconds()
        if duration is not None and duration >= 0:
            durations.append((position, duration))
    if len(durations) >= 100:
        median_duration = median(value for _, value in durations)
        factor = float(timing.get("speeder_factor", 0.33))
        speeders = [(row, value) for row, value in durations if value < median_duration * factor]
        if speeders:
            findings.append(SurveyFinding("survey_speeder", "medium", "Unusually short configured completion time", f"{len(speeders)} response(s) completed below {factor:g}× the median duration ({median_duration:g} seconds). Timing is a quality flag and never an automatic deletion.", duration_column or end_column, speeders[0][0], {"seconds": speeders[0][1], "median_seconds": median_duration}, len(speeders)))

    for rule in config.get("skip_rules", []):
        when_column, target_column = rule.get("when_column"), rule.get("target_column")
        if when_column not in columns or target_column not in columns:
            continue
        expected_when = {_plain(value) for value in rule.get("when_values", [])}
        violations = [position for position, (_, row) in enumerate(frame.iterrows(), start=1) if _plain(row[when_column]) in expected_when and not is_missing_value(row[target_column], target_column)]
        if violations:
            findings.append(SurveyFinding("survey_skip_logic", "high", "Configured skip logic violation", f"{len(violations)} response(s) contain {target_column!r} even though {when_column!r} indicates the item should have been skipped. Confirm export coding and questionnaire logic.", target_column, violations[0], frame.iloc[violations[0] - 1][target_column], len(violations)))

    for rule in config.get("cross_field_rules", []):
        left_column, right_column = rule.get("left_column"), rule.get("right_column")
        if left_column not in columns or (right_column and right_column not in columns):
            continue
        operator = str(rule.get("operator", "=="))
        violations: list[int] = []
        for position, (_, row) in enumerate(frame.iterrows(), start=1):
            left, right = row[left_column], row[right_column] if right_column else rule.get("value")
            if is_missing_value(left, left_column) or (right_column and is_missing_value(right, right_column)):
                continue
            if not _compare(left, operator, right):
                violations.append(position)
        if violations:
            findings.append(SurveyFinding("survey_cross_field", "high", rule.get("name") or "Configured cross-field contradiction", f"{len(violations)} row(s) violate the configured relationship {left_column} {operator} {right_column or rule.get('value')!r}.", left_column, violations[0], frame.iloc[violations[0] - 1][left_column], len(violations)))

    return [asdict(finding) for finding in findings]
