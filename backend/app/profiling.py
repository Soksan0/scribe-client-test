from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from statistics import quantiles
from typing import Any, Iterable

MISSING_MARKERS = {"", "na", "n/a", "nan", "null", "nil", "missing", "."}
CORE_MISSING_MARKERS = {"", "n/a", "nan", "null", "nil", "missing", "."}
CONTEXTUAL_NA_COLUMNS = re.compile(r"state|province|territory|region|country|code|abbrev|timezone", re.I)
ID_PATTERN = re.compile(r"(^id$|[ _-]id$|^id[ _-]|identifier|participant.*id|subject.*id|transaction.*id|record.*id|case.*id)", re.I)
SEMANTIC_TEXT_PATTERN = re.compile(r"(^id$|[ _-]id$|^id[ _-]|identifier|participant.*id|subject.*id|transaction.*id|record.*id|case.*id|phone|mobile|telephone|zip|postal|postcode|code|mrn|medical[ _-]*record|ssn|account|barcode)", re.I)
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d", "%Y.%m.%d", "%B %d, %Y", "%b %d, %Y")
AGE_PATTERN = re.compile(r"(^|_)(age|years_old|age_years)($|_)", re.I)
ENTITY_NAME_PATTERN = re.compile(r"patient[ _-]*name|participant[ _-]*name|respondent[ _-]*name|subject[ _-]*name", re.I)
FREE_TEXT_PATTERN = re.compile(r"name|comment|note|description|narrative|address|response[ _-]*text|transcript", re.I)
SCALE_PATTERN = re.compile(r"likert|rating|satisfaction|agreement|score", re.I)
CATEGORICAL_PATTERN = re.compile(r"gender|sex|status|category|type|group|condition|medication|consent|response|country|region|marital|education|occupation", re.I)
EMAIL_PATTERN = re.compile(r"email|e-mail", re.I)
PHONE_PATTERN = re.compile(r"phone|mobile|telephone|tel$", re.I)
BP_PATTERN = re.compile(r"blood[ _-]*pressure|^bp$|systolic[ _/-]*diastolic", re.I)
ITEM_PATTERN = re.compile(r"(^|[ _-])(item|product|service)([ _-]|$)", re.I)
QUANTITY_PATTERN = re.compile(r"(^|[ _-])(quantity|qty|units?)([ _-]|$)", re.I)
UNIT_PRICE_PATTERN = re.compile(r"price[ _-]*per[ _-]*(unit|item)|unit[ _-]*price", re.I)
TOTAL_PATTERN = re.compile(r"(^|[ _-])(total([ _-]*(spent|amount|cost|price))?|line[ _-]*total|extended[ _-]*(price|amount))([ _-]|$)", re.I)
SYSTEM_MISSING_MARKERS = {"ERROR", "UNKNOWN"}
US_STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
NUMBER_WORD_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
NUMBER_WORD_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


@dataclass(frozen=True)
class FindingCandidate:
    category: str
    severity: str
    confidence: str
    title: str
    explanation: str
    column_name: str | None
    row_number: int | None
    record_key: str | None
    before: Any
    proposed: Any
    operation: dict[str, Any] | None
    affected_count: int = 1


def decode_bytes(content: bytes) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return content.decode(encoding), encoding, warnings
        except UnicodeDecodeError:
            pass
    warnings.append("The file is not UTF-8; it was decoded as Windows-1252.")
    return content.decode("cp1252"), "cp1252", warnings


def detect_delimiter(text: str, filename: str) -> str:
    if filename.lower().endswith(".tsv"):
        return "\t"
    try:
        return csv.Sniffer().sniff(text[:65536], delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def is_missing_value(value: Any, column_name: str | None = None) -> bool:
    text = str(value).strip()
    if text in SYSTEM_MISSING_MARKERS:
        return True
    lowered = text.casefold()
    if lowered in {"", "n/a", "null", "nil", "missing", "."}:
        return True
    if lowered not in {"na", "nan"}:
        return False
    if column_name:
        if CONTEXTUAL_NA_COLUMNS.search(column_name) and US_STATE_PATTERN.fullmatch(text):
            return False
        if FREE_TEXT_PATTERN.search(column_name):
            return False
    return True


def parse_decimal(value: Any, column_name: str | None = None) -> Decimal | None:
    if is_missing_value(value, column_name):
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def plain_decimal(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value.normalize())


def detect_relational_inferences(
    headers: list[str], columns: dict[str, list[str]], primary_id: str | None
) -> tuple[list[FindingCandidate], set[tuple[int, str]], bool]:
    """Find only relationships strongly demonstrated by complete rows in this dataset."""
    findings: list[FindingCandidate] = []
    covered: set[tuple[int, str]] = set()
    row_count = len(next(iter(columns.values()), []))
    item_column = next((name for name in headers if ITEM_PATTERN.search(name)), None)
    quantity_column = next((name for name in headers if QUANTITY_PATTERN.search(name)), None)
    price_column = next((name for name in headers if UNIT_PRICE_PATTERN.search(name)), None)
    total_column = next((name for name in headers if TOTAL_PATTERN.search(name) and name != price_column), None)
    if not all((quantity_column, price_column, total_column)):
        return findings, covered, False

    assert quantity_column and price_column and total_column
    complete: list[tuple[int, Decimal, Decimal, Decimal]] = []
    for index in range(row_count):
        quantity = parse_decimal(columns[quantity_column][index], quantity_column)
        price = parse_decimal(columns[price_column][index], price_column)
        total = parse_decimal(columns[total_column][index], total_column)
        if quantity is not None and price is not None and total is not None:
            complete.append((index + 1, quantity, price, total))
    minimum_support = max(5, min(50, row_count // 20))
    consistent = [record for record in complete if record[1] * record[2] == record[3]]
    if len(complete) < minimum_support:
        if complete:
            findings.append(FindingCandidate(
                "arithmetic_relationship_unverified", "high", "needs_confirmation",
                "Arithmetic relationship could not be verified",
                f"Only {len(complete)} complete rows are available to test {quantity_column} × {price_column} = {total_column}; at least {minimum_support} are required. Scribe will not derive missing values from insufficient evidence.",
                None, complete[0][0], None, {"consistent": len(consistent), "complete": len(complete)}, None, None, len(complete) - len(consistent),
            ))
        return findings, covered, False

    contradictory = [record for record in complete if record[1] * record[2] != record[3]]
    if contradictory:
        findings.append(FindingCandidate(
            "arithmetic_inconsistency", "high", "needs_confirmation", "Arithmetic totals do not agree",
            f"{len(contradictory)} complete row(s) do not satisfy {quantity_column} × {price_column} = {total_column}. Existing values are flagged but never overwritten automatically.",
            total_column, contradictory[0][0], columns[primary_id][contradictory[0][0] - 1] if primary_id else None,
            {"quantity": plain_decimal(contradictory[0][1]), "unit_price": plain_decimal(contradictory[0][2]), "total": plain_decimal(contradictory[0][3])}, None, None, len(contradictory),
        ))
        return findings, covered, False

    working = [{header: columns[header][index] for header in headers} for index in range(row_count)]
    operations: dict[tuple[str, str], dict[str, Any]] = {}

    def add_change(kind: str, target: str, formula: str, row_number: int, after: Any, sources: list[str], evidence: dict[str, Any], phase: int) -> None:
        before = working[row_number - 1][target]
        key = (kind, f"{formula}::phase-{phase}")
        operation = operations.setdefault(key, {
            "type": kind, "column": target, "formula": formula, "source_columns": sources,
            "rows": [], "changes": [], "evidence": evidence, "phase": phase,
        })
        operation["rows"].append(row_number)
        operation["changes"].append({
            "row": row_number, "before": before, "after": after,
            "inputs": {source: working[row_number - 1][source] for source in sources},
        })
        working[row_number - 1][target] = after
        covered.add((row_number, target))

    item_to_price: dict[str, Decimal] = {}
    canonical_items: dict[str, str] = {}
    price_to_items: dict[Decimal, set[str]] = defaultdict(set)
    mapping_support: dict[str, int] = {}
    if item_column:
        observed: dict[str, Counter[Decimal]] = defaultdict(Counter)
        item_spellings: dict[str, Counter[str]] = defaultdict(Counter)
        for index in range(row_count):
            item = columns[item_column][index]
            price = parse_decimal(columns[price_column][index], price_column)
            if is_missing_value(item, item_column) or price is None:
                continue
            key = normalize_text(item).casefold()
            observed[key][price] += 1
            item_spellings[key][normalize_text(item)] += 1
        mapping_minimum = max(5, min(20, row_count // 100))
        for key, prices in observed.items():
            support = sum(prices.values())
            if support >= mapping_minimum and len(prices) == 1:
                price = next(iter(prices))
                item_to_price[key] = price
                canonical_items[key] = item_spellings[key].most_common(1)[0][0]
                mapping_support[key] = support
                price_to_items[price].add(key)

    # Iterate because an item-price inference can make an arithmetic inference possible,
    # and a derived price can in turn identify an item when the reverse mapping is unique.
    changed = True
    phase = 0
    while changed:
        phase += 1
        changed = False
        for row_number, row in enumerate(working, start=1):
            if item_column:
                item = row[item_column]
                price = parse_decimal(row[price_column], price_column)
                item_key = normalize_text(str(item)).casefold() if not is_missing_value(item, item_column) else ""
                if price is None and item_key in item_to_price and (row_number, price_column) not in covered:
                    mapped = item_to_price[item_key]
                    add_change("derive_mapping", price_column, f"{price_column} from {item_column}", row_number, plain_decimal(mapped), [item_column], {
                        "mapping_support": mapping_support[item_key], "mapping_confidence": 1.0,
                    }, phase)
                    changed = True
                    price = mapped
                if is_missing_value(row[item_column], item_column) and price is not None and len(price_to_items.get(price, set())) == 1 and (row_number, item_column) not in covered:
                    key = next(iter(price_to_items[price]))
                    add_change("derive_mapping", item_column, f"{item_column} from unique {price_column}", row_number, canonical_items[key], [price_column], {
                        "mapping_support": mapping_support[key], "mapping_confidence": 1.0,
                    }, phase)
                    changed = True

            quantity = parse_decimal(row[quantity_column], quantity_column)
            price = parse_decimal(row[price_column], price_column)
            total = parse_decimal(row[total_column], total_column)
            missing = [quantity is None, price is None, total is None]
            if sum(missing) != 1:
                continue
            if quantity is None and price not in {None, Decimal(0)} and total is not None:
                result = total / price
                if result > 0 and result == result.to_integral_value():
                    add_change("derive_arithmetic", quantity_column, f"{total_column} / {price_column}", row_number, int(result), [total_column, price_column], {"validated_rows": len(complete), "agreement": len(consistent) / len(complete)}, phase)
                    changed = True
            elif price is None and quantity not in {None, Decimal(0)} and total is not None:
                result = total / quantity
                if result >= 0 and result.as_tuple().exponent >= -4:
                    add_change("derive_arithmetic", price_column, f"{total_column} / {quantity_column}", row_number, plain_decimal(result), [total_column, quantity_column], {"validated_rows": len(complete), "agreement": len(consistent) / len(complete)}, phase)
                    changed = True
            elif total is None and quantity is not None and price is not None:
                result = quantity * price
                if result >= 0:
                    add_change("derive_arithmetic", total_column, f"{quantity_column} * {price_column}", row_number, plain_decimal(result), [quantity_column, price_column], {"validated_rows": len(complete), "agreement": len(consistent) / len(complete)}, phase)
                    changed = True

    for operation in operations.values():
        changes = operation["changes"]
        first = changes[0]
        category = "arithmetic_inference" if operation["type"] == "derive_arithmetic" else "codebook_inference"
        title = "Derive missing value from verified arithmetic" if category == "arithmetic_inference" else "Derive missing value from stable item-price mapping"
        if category == "arithmetic_inference":
            why = f"Across {len(complete)} complete rows, {len(consistent)} ({len(consistent) / len(complete):.1%}) satisfy {quantity_column} × {price_column} = {total_column}."
        else:
            why = "The source-to-target mapping is one-to-one in all supporting complete rows; ambiguous reverse mappings are excluded."
        findings.append(FindingCandidate(
            category, "medium", "high", title,
            f"{why} Scribe can apply {operation['formula']} to {len(changes)} exact row(s). Each source value and result is recorded; this proposal still requires approval.",
            operation["column"], first["row"], columns[primary_id][first["row"] - 1] if primary_id else None,
            first["before"], first["after"], operation, len(changes),
        ))
    return findings, covered, True


def parse_date(value: str) -> datetime:
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue
    raise ValueError(value)


def parse_date_with_quality(value: str) -> tuple[datetime, bool]:
    stripped = value.strip()
    slash_or_dash = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", stripped)
    ambiguous = False
    if slash_or_dash:
        first, second = int(slash_or_dash.group(1)), int(slash_or_dash.group(2))
        ambiguous = first <= 12 and second <= 12 and first != second
    return parse_date(stripped), ambiguous


def parse_number_word(value: str) -> int | float:
    cleaned = re.sub(r"[-\s]+", " ", value.strip().casefold())
    if cleaned in NUMBER_WORD_ONES:
        return NUMBER_WORD_ONES[cleaned]
    if cleaned in NUMBER_WORD_TENS:
        return NUMBER_WORD_TENS[cleaned]
    parts = cleaned.split()
    if len(parts) == 2 and parts[0] in NUMBER_WORD_TENS and parts[1] in NUMBER_WORD_ONES and NUMBER_WORD_ONES[parts[1]] < 10:
        return NUMBER_WORD_TENS[parts[0]] + NUMBER_WORD_ONES[parts[1]]
    raise ValueError(value)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def infer_type(values: Iterable[str], column_name: str | None = None) -> tuple[str, float]:
    non_missing = [value.strip() for value in values if not is_missing_value(value, column_name)]
    if not non_missing:
        return "empty", 1.0
    if column_name and SEMANTIC_TEXT_PATTERN.search(column_name):
        return "text", 1.0
    checks: list[tuple[str, Any]] = [
        ("integer", lambda value: int(value.replace(",", ""))),
        ("number", lambda value: float(value.replace(",", ""))),
        ("date", parse_date),
    ]
    for type_name, parser in checks:
        passed = 0
        for value in non_missing:
            try:
                parser(value)
                passed += 1
            except (ValueError, TypeError, OverflowError):
                continue
        ratio = passed / len(non_missing)
        if ratio == 1:
            return type_name, ratio
        if ratio >= 0.75:
            return f"mostly_{type_name}", ratio
    lowered = {value.lower() for value in non_missing}
    if lowered <= {"true", "false", "yes", "no", "y", "n", "0", "1"}:
        return "boolean", 1.0
    return "text", 1.0


def boolean_canonical(value: str) -> str | None:
    lowered = value.strip().casefold()
    if lowered in {"true", "yes", "y", "1"}:
        return "Yes"
    if lowered in {"false", "no", "n", "0"}:
        return "No"
    return None


def is_valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def is_valid_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 7 <= len(digits) <= 15


def is_valid_blood_pressure(value: str) -> bool:
    match = re.fullmatch(r"\s*(\d{2,3})\s*/\s*(\d{2,3})\s*", value)
    if not match:
        return False
    systolic, diastolic = int(match.group(1)), int(match.group(2))
    return 40 <= diastolic < systolic <= 260


def profile_delimited(path: Path, filename: str, parsing_config: dict[str, Any] | None = None) -> dict[str, Any]:
    content = path.read_bytes()
    config = parsing_config or {}
    requested_encoding = config.get("encoding")
    if requested_encoding:
        try:
            text = content.decode(str(requested_encoding))
        except (LookupError, UnicodeDecodeError) as error:
            raise ValueError(f"The file could not be decoded as {requested_encoding}") from error
        encoding, warnings = str(requested_encoding), []
    else:
        text, encoding, warnings = decode_bytes(content)
    delimiter = str(config.get("delimiter") or detect_delimiter(text, filename))
    if delimiter not in {",", "\t", ";", "|"}:
        raise ValueError("Delimiter must be comma, tab, semicolon, or pipe")
    header_row = int(config.get("header_row") or 1)
    if header_row < 1:
        raise ValueError("Header row must be at least 1")
    if header_row > 1:
        try:
            raw_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
        except csv.Error as error:
            raise ValueError(f"Malformed CSV quoting: {error}") from error
        if header_row > len(raw_rows):
            raise ValueError(f"Header row {header_row} is outside the file")
        staged = io.StringIO()
        writer = csv.writer(staged, delimiter=delimiter, lineterminator="\n")
        writer.writerows(raw_rows[header_row - 1 :])
        text = staged.getvalue()
        warnings.append(f"Rows 1-{header_row - 1} were treated as metadata and excluded from the analysis table.")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
    headers = reader.fieldnames or []
    if not headers:
        raise ValueError("The dataset does not contain a header row.")
    if len(headers) != len(set(headers)):
        warnings.append("Duplicate column names were detected; columns must be renamed before cleaning.")
        raw_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        row_count = max(0, len(raw_rows) - 1)
        duplicate_header_names = [name for name, count in Counter(headers).items() if count > 1]
        finding = FindingCandidate(
            "duplicate_column_name",
            "critical",
            "high",
            "Duplicate column names",
            f"Column names must be unique before Scribe can safely target cell-level corrections: {duplicate_header_names!r}. Rename duplicate headers, then re-upload or reanalyze.",
            None,
            None,
            None,
            duplicate_header_names,
            None,
            None,
            len(duplicate_header_names),
        )
        profiles = [
            {
                "name": header,
                "inferred_type": "unknown",
                "type_confidence": 0,
                "missing_count": None,
                "missing_percent": None,
                "missing_markers": {},
                "suspected_missing_codes": {},
                "distinct_count": None,
                "examples": [],
                "candidate_id": bool(ID_PATTERN.search(header.strip())),
                "minimum": None,
                "maximum": None,
                "constant": False,
            }
            for header in headers
        ]
        return {"sha256": hashlib.sha256(content).hexdigest(), "encoding": encoding, "encoding_confidence": "high" if encoding.startswith("utf-8") else "medium", "delimiter": delimiter, "delimiter_confidence": "low", "schema_fingerprint": hashlib.sha256(json_schema(headers).encode("utf-8")).hexdigest(), "row_count": row_count, "column_count": len(headers), "columns": profiles, "candidate_id_columns": [], "warnings": warnings, "findings": [asdict(finding)], "parsing_config": config}
    columns: dict[str, list[str]] = {header: [] for header in headers}
    row_hashes: dict[tuple[str, ...], list[int]] = defaultdict(list)
    whitespace: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    text_normalization: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    explicit_missing_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    findings: list[FindingCandidate] = []
    row_count = 0
    try:
        for row_count, row in enumerate(reader, start=1):
            if None in row or any(row.get(header) is None for header in headers):
                findings.append(FindingCandidate("corrupted_row", "critical", "high", "Malformed or truncated row", "This row has more or fewer fields than the header. Review the source file before cleaning values.", None, row_count, None, row.get(None), None, None))
            values: list[str] = []
            for header in headers:
                value = row.get(header) or ""
                columns[header].append(value)
                values.append(value)
                if value != value.strip():
                    whitespace[(header, value, value.strip())].append(row_count)
                normalized = normalize_text(value)
                if normalized != value and value == value.strip():
                    text_normalization[(header, value, normalized)].append(row_count)
            row_hashes[tuple(values)].append(row_count)
    except csv.Error as error:
        raise ValueError(f"Malformed CSV quoting near row {reader.line_num}: {error}") from error

    profiles: list[dict[str, Any]] = []
    configured_ids = [str(value) for value in config.get("identifier_columns", [])]
    unknown_ids = [value for value in configured_ids if value not in headers]
    if unknown_ids:
        raise ValueError(f"Configured identifier columns do not exist: {unknown_ids}")
    id_columns = configured_ids or [header for header in headers if ID_PATTERN.search(header.strip())]
    primary_id = id_columns[0] if id_columns else None
    duplicate_header_names = [name for name, count in Counter(headers).items() if count > 1]
    if duplicate_header_names:
        findings.append(FindingCandidate("duplicate_column_name", "critical", "high", "Duplicate column names", f"Column names must be unique. Rename duplicate headers before cell-level cleaning: {duplicate_header_names!r}.", None, None, None, duplicate_header_names, None, None, len(duplicate_header_names)))
    for header, values in columns.items():
        inferred_type, confidence = infer_type(values, header)
        missing_count = sum(is_missing_value(value, header) for value in values)
        missing_markers_found = Counter(value.strip() for value in values if is_missing_value(value, header) and value.strip())
        suspected_missing_codes = Counter(value.strip() for value in values if value.strip().casefold() in {"-1", "-9", "-99", "9999", "dk", "don't know", "dont know", "refused"})
        counts = Counter(value.strip() for value in values if not is_missing_value(value, header))
        if AGE_PATTERN.search(header):
            non_missing = [value.strip() for value in values if not is_missing_value(value, header)]
            direct_numeric = 0
            convertible = 0
            for value in non_missing:
                try: int(value.replace(",", "")); direct_numeric += 1; convertible += 1
                except ValueError:
                    try: parse_number_word(value); convertible += 1
                    except ValueError: pass
            if non_missing and convertible == len(non_missing) and direct_numeric < len(non_missing):
                inferred_type, confidence = "mostly_integer", direct_numeric / len(non_missing)
        numeric_values: list[float] = []
        for value in counts:
            try: numeric_values.append(float(value.replace(",", "")))
            except ValueError: pass
        profiles.append({"name": header, "inferred_type": inferred_type, "type_confidence": round(confidence, 3), "missing_count": missing_count, "missing_percent": round((missing_count / row_count * 100) if row_count else 0, 2), "missing_markers": dict(missing_markers_found), "suspected_missing_codes": dict(suspected_missing_codes), "distinct_count": len(counts), "examples": [value for value, _ in counts.most_common(5)], "candidate_id": header in id_columns, "minimum": min(numeric_values) if numeric_values else None, "maximum": max(numeric_values) if numeric_values else None, "constant": len(counts) == 1 and row_count > 1})
        if missing_count == row_count and row_count:
            findings.append(FindingCandidate("empty_column", "medium", "high", "Empty column", f"Every row in {header!r} is missing. Confirm whether this column is intentional.", header, 1, columns[primary_id][0] if primary_id else None, None, None, None, row_count))
        elif len(counts) == 1 and row_count >= 3 and header not in id_columns:
            findings.append(FindingCandidate("constant_column", "low", "high", "Constant-value column", f"All non-missing rows in {header!r} contain the same value. This may be intentional or may indicate a collection problem.", header, 1, columns[primary_id][0] if primary_id else None, next(iter(counts)), None, None, sum(counts.values())))
        formula_rows = [
            index for index, value in enumerate(values, start=1)
            if value.lstrip().startswith(("=", "+", "@")) or (value.lstrip().startswith("-") and parse_decimal(value, header) is None)
        ]
        if formula_rows:
            example = values[formula_rows[0] - 1]
            findings.append(FindingCandidate("spreadsheet_formula", "high", "needs_confirmation", "Value may execute as a spreadsheet formula", f"{len(formula_rows)} value(s) in {header!r} begin with a spreadsheet formula character. Scribe preserves the research value but flags it before CSV/XLSX use; confirm whether it is intentional text.", header, formula_rows[0], columns[primary_id][formula_rows[0] - 1] if primary_id else None, example, None, None, len(formula_rows)))
        control_rows = [index for index, value in enumerate(values, start=1) if "\ufffd" in value or any(ord(character) < 32 and character not in "\t\n\r" for character in value)]
        if control_rows:
            findings.append(FindingCandidate("broken_text_encoding", "high", "needs_confirmation", "Broken Unicode or control characters", f"{len(control_rows)} value(s) in {header!r} contain replacement or control characters. Preserve them until the source encoding or intended text is confirmed.", header, control_rows[0], columns[primary_id][control_rows[0] - 1] if primary_id else None, values[control_rows[0] - 1], None, None, len(control_rows)))
        if AGE_PATTERN.search(header):
            for index, value in enumerate(values, start=1):
                if is_missing_value(value, header):
                    continue
                try: number = float(value.replace(",", ""))
                except ValueError: continue
                if number < 0 or number > 120:
                    findings.append(FindingCandidate("impossible_value", "high", "high", "Implausible age", f"{value!r} is outside Scribe's suggested human-age range of 0–120. Confirm the study population before correcting or rejecting this finding.", header, index, columns[primary_id][index - 1] if primary_id else None, value, None, None))
        if inferred_type.removeprefix("mostly_") == "date":
            date_variants: dict[tuple[str, str], list[int]] = defaultdict(list)
            ambiguous_dates: dict[str, list[int]] = defaultdict(list)
            for index, value in enumerate(values, start=1):
                if is_missing_value(value, header):
                    continue
                try:
                    parsed, ambiguous = parse_date_with_quality(value.strip())
                except ValueError:
                    continue
                if ambiguous:
                    ambiguous_dates[value].append(index)
                    continue
                standardized = parsed.strftime("%Y-%m-%d")
                if value != standardized:
                    date_variants[(value, standardized)].append(index)
            for (before, after), rows in date_variants.items():
                findings.append(FindingCandidate("date_standardization", "medium", "high", "Standardize mixed date format", f"{before!r} is a valid date written in a different format. Scribe can standardize these exact values to ISO 8601 ({after}) for reliable sorting and analysis.", header, rows[0], columns[primary_id][rows[0] - 1] if primary_id else None, before, after, {"type": "parse_date", "column": header, "before": before, "after": after, "rows": rows}, len(rows)))
            for before, rows in ambiguous_dates.items():
                findings.append(FindingCandidate("ambiguous_date", "high", "needs_confirmation", "Ambiguous date format", f"{before!r} can be interpreted as either month/day/year or day/month/year. Confirm the study's date convention before standardizing.", header, rows[0], columns[primary_id][rows[0] - 1] if primary_id else None, before, None, None, len(rows)))
        numeric_pairs: list[tuple[int, str, float]] = []
        for index, value in enumerate(values, start=1):
            if is_missing_value(value, header):
                continue
            try: numeric_pairs.append((index, value, float(value.replace(",", ""))))
            except ValueError: pass
        if SCALE_PATTERN.search(header):
            scale_numbers = [(index, value, number) for index, value, number in numeric_pairs if float(number).is_integer()]
            if len(scale_numbers) >= 5:
                in_range = [item for item in scale_numbers if 1 <= item[2] <= 5]
                outside = [item for item in scale_numbers if item[2] < 1 or item[2] > 5]
                if outside and len(in_range) / max(1, len(scale_numbers)) >= 0.65:
                    for index, value, _ in outside[:500]:
                        findings.append(FindingCandidate("invalid_scale", "high", "needs_confirmation", "Value outside likely rating scale", f"Most numeric values in {header!r} fall within 1-5, but {value!r} does not. Confirm the instrument scale before correcting.", header, index, columns[primary_id][index - 1] if primary_id else None, value, None, None))
        if len(numeric_pairs) >= 8 and inferred_type.removeprefix("mostly_") in {"integer", "number"}:
            q1, _, q3 = quantiles([item[2] for item in numeric_pairs], n=4, method="inclusive")
            iqr = q3 - q1
            if iqr > 0:
                lower, upper = q1 - 3 * iqr, q3 + 3 * iqr
                for index, value, number in numeric_pairs:
                    if number < lower or number > upper:
                        findings.append(FindingCandidate("outlier", "medium", "needs_confirmation", "Extreme numeric outlier", f"{value!r} lies beyond three interquartile ranges ({lower:g} to {upper:g}) for {header!r}. Outliers are never changed automatically.", header, index, columns[primary_id][index - 1] if primary_id else None, value, None, None))
        if missing_count:
            missing_rows = [index + 1 for index, value in enumerate(values) if is_missing_value(value, header)]
            findings.append(FindingCandidate("missing_value", "medium", "high", "Missing values", f"{missing_count} of {row_count} rows are missing a value in {header!r}. Scribe will not impute values automatically.", header, missing_rows[0], columns[primary_id][missing_rows[0] - 1] if primary_id else None, values[missing_rows[0] - 1], None, None, missing_count))
            for marker in sorted({value for value in values if value.strip() and is_missing_value(value, header)}):
                explicit_missing_groups[(header, marker)].extend(index + 1 for index, value in enumerate(values) if value == marker)
        if inferred_type == "boolean":
            boolean_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
            for index, value in enumerate(values, start=1):
                if is_missing_value(value, header):
                    continue
                canonical = boolean_canonical(value)
                if canonical is not None and value != canonical:
                    boolean_groups[(value, canonical)].append(index)
            for (before, after), rows in boolean_groups.items():
                findings.append(FindingCandidate("boolean_standardization", "medium", "needs_confirmation", "Standardize boolean coding", f"{before!r} is a boolean-like value. Scribe can map it to {after!r} after you confirm this column's coding scheme.", header, rows[0], columns[primary_id][rows[0] - 1] if primary_id else None, before, after, {"type": "map_category", "column": header, "before": before, "after": after, "rows": rows}, len(rows)))
        if EMAIL_PATTERN.search(header):
            for index, value in enumerate(values, start=1):
                if not is_missing_value(value, header) and not is_valid_email(value):
                    findings.append(FindingCandidate("invalid_pattern", "medium", "needs_confirmation", "Invalid email pattern", f"{value!r} does not match a basic email address pattern. Scribe will not invent a correction.", header, index, columns[primary_id][index - 1] if primary_id else None, value, None, None))
        if PHONE_PATTERN.search(header):
            for index, value in enumerate(values, start=1):
                if not is_missing_value(value, header) and not is_valid_phone(value):
                    findings.append(FindingCandidate("invalid_pattern", "medium", "needs_confirmation", "Invalid phone pattern", f"{value!r} does not contain a plausible phone number length. Scribe will not invent a correction.", header, index, columns[primary_id][index - 1] if primary_id else None, value, None, None))
        if BP_PATTERN.search(header):
            for index, value in enumerate(values, start=1):
                if not is_missing_value(value, header) and not is_valid_blood_pressure(value):
                    findings.append(FindingCandidate("invalid_pattern", "high", "needs_confirmation", "Invalid blood pressure pattern", f"{value!r} does not look like a plausible systolic/diastolic reading. Confirm units and source entry before correcting.", header, index, columns[primary_id][index - 1] if primary_id else None, value, None, None))
        if inferred_type == "text" and 1 < len(counts) <= 100:
            case_groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for value, count in counts.items():
                case_groups[value.casefold()].append((value, count))
            for variants in case_groups.values():
                if len(variants) < 2:
                    continue
                ordered = sorted(variants, key=lambda item: (-item[1], item[0]))
                canonical = ordered[0][0]
                for variant, count in ordered[1:]:
                    rows = [index + 1 for index, value in enumerate(values) if value.strip() == variant]
                    findings.append(FindingCandidate("inconsistent_category", "medium", "needs_confirmation", "Inconsistent category capitalization", f"Values {', '.join(repr(value) for value, _ in ordered)} differ only by capitalization. The most frequent spelling is proposed, but you must confirm it is the study's canonical label.", header, rows[0], columns[primary_id][rows[0] - 1] if primary_id else None, variant, canonical, {"type": "map_category", "column": header, "before": variant, "after": canonical, "rows": rows}, count))
        if header in id_columns:
            for value, count in list({value: count for value, count in counts.items() if count > 1}.items())[:500]:
                row_numbers = [index + 1 for index, item in enumerate(values) if item.strip() == value]
                findings.append(FindingCandidate("duplicate_id", "high", "high", "Duplicate identifier", f"The identifier {value!r} occurs on {count} rows. Scribe cannot choose the correct record automatically.", header, row_numbers[0], value, value, None, None, count))
        if inferred_type.startswith("mostly_"):
            expected = inferred_type.removeprefix("mostly_")
            parser = {"integer": lambda value: int(value.replace(",", "")), "number": lambda value: float(value.replace(",", "")), "date": parse_date}[expected]
            invalid_groups: dict[tuple[str, Any], list[int]] = defaultdict(list)
            for index, value in enumerate(values, start=1):
                if is_missing_value(value, header):
                    continue
                try:
                    parser(value.strip())
                except (ValueError, TypeError):
                    proposed = None
                    operation = None
                    if expected in {"integer", "number"}:
                        try:
                            proposed = parse_number_word(value)
                            if expected == "integer": proposed = int(proposed)
                        except ValueError:
                            pass
                    invalid_groups[(value, proposed)].append(index)
            for (value, proposed), rows in invalid_groups.items():
                    operation = {"type": "parse_type", "column": header, "before": value, "after": proposed, "rows": rows, "expected": expected} if proposed is not None else None
                    explanation = f"Most values in {header!r} are {expected}s, but this value could not be parsed."
                    if proposed is not None:
                        explanation += f" The number word has an exact deterministic conversion to {proposed!r} on {len(rows)} row(s); review once before applying the grouped correction."
                    else:
                        explanation += " Confirm the intended column type before correcting it."
                    findings.append(FindingCandidate("invalid_type", "high", "high" if proposed is not None else "needs_confirmation", f"Value does not match mostly {expected} column", explanation, header, rows[0], columns[primary_id][rows[0] - 1] if primary_id else None, value, proposed, operation, len(rows)))
    relational_findings, relational_cells, arithmetic_relationship_verified = detect_relational_inferences(headers, columns, primary_id)
    findings.extend(relational_findings)
    for configured_marker in {str(value) for value in config.get("missing_tokens", []) if str(value) != ""}:
        for header, values in columns.items():
            rows = [index + 1 for index, value in enumerate(values) if value == configured_marker]
            if rows and not is_missing_value(configured_marker, header):
                findings.append(FindingCandidate(
                    "missing_code_normalization", "medium", "high", "Normalize confirmed missing code",
                    f"{configured_marker!r} was explicitly confirmed as a missing token in the parsing configuration. Scribe can normalize these {len(rows)} exact value(s) without imputing data.",
                    header, rows[0], columns[primary_id][rows[0] - 1] if primary_id else None,
                    configured_marker, "", {"type": "normalize_missing", "column": header, "before": configured_marker, "after": "", "rows": rows}, len(rows),
                ))
    for (header, marker), marker_rows in explicit_missing_groups.items():
        remaining_rows = [row for row in marker_rows if (row, header) not in relational_cells]
        if remaining_rows:
            findings.append(FindingCandidate("missing_code_normalization", "medium", "high", "Standardize explicit missing marker", f"{marker!r} is an explicit missing token. Scribe can convert the {len(remaining_rows)} value(s) that could not be deterministically recovered to the format's standard missing value without imputing data.", header, remaining_rows[0], columns[primary_id][remaining_rows[0] - 1] if primary_id else None, marker, "", {"type": "normalize_missing", "column": header, "before": marker, "after": "", "rows": remaining_rows}, len(remaining_rows)))
    for (header, before, after), row_numbers in whitespace.items():
        findings.append(FindingCandidate("whitespace", "medium", "high", "Leading or trailing whitespace", "Invisible surrounding whitespace can create duplicate categories, break joins, and affect validation.", header, row_numbers[0], columns[primary_id][row_numbers[0] - 1] if primary_id else None, before, after, {"type": "trim", "column": header, "before": before, "after": after, "rows": row_numbers}, len(row_numbers)))
    for (header, before, after), row_numbers in text_normalization.items():
        findings.append(FindingCandidate("text_normalization", "medium", "high", "Inconsistent or invisible spacing", "Repeated spaces, non-breaking spaces, or Unicode formatting can split categories and break joins.", header, row_numbers[0], columns[primary_id][row_numbers[0] - 1] if primary_id else None, before, after, {"type": "replace", "column": header, "before": before, "after": after, "rows": row_numbers}, len(row_numbers)))
    column_signatures: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for header, values in columns.items():
        column_signatures[tuple(normalize_text(value).casefold() for value in values)].append(header)
    for duplicate_columns in column_signatures.values():
        if len(duplicate_columns) > 1:
            findings.append(FindingCandidate("duplicate_column", "medium", "high", "Duplicate column contents", f"Columns {duplicate_columns!r} contain the same normalized values on every row. Confirm whether both variables are required.", None, None, None, duplicate_columns, None, None, len(duplicate_columns)))
    normalized_rows: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for row_index in range(row_count):
        signature = tuple(normalize_text(columns[header][row_index]).casefold() for header in headers)
        normalized_rows[signature].append(row_index + 1)
    for row_numbers in normalized_rows.values():
        raw_rows = {tuple(columns[header][row - 1] for header in headers) for row in row_numbers}
        if len(row_numbers) > 1 and len(raw_rows) > 1:
            findings.append(FindingCandidate("near_duplicate", "medium", "needs_confirmation", "Near-duplicate records", f"Rows {row_numbers[:10]} become identical after safe text normalization. Review them before deciding whether they represent the same record.", None, row_numbers[0], None, {"rows": row_numbers[:10]}, None, None, len(row_numbers)))
    for values, row_numbers in row_hashes.items():
        if len(row_numbers) > 1:
            operation = {"type": "delete_rows", "rows": row_numbers[1:]}
            findings.append(FindingCandidate("duplicate_row", "high", "high", "Duplicate rows", f"These {len(row_numbers)} rows contain identical values. The first occurrence will be retained if you accept removal.", None, row_numbers[0], None, {header: value for header, value in zip(headers, values)}, {"remove_duplicate_rows": row_numbers[1:]}, operation, len(row_numbers)))
    entity_columns = [header for header in headers if ENTITY_NAME_PATTERN.search(header)]
    date_columns = [profile["name"] for profile in profiles if profile["inferred_type"].removeprefix("mostly_") == "date"]
    if entity_columns:
        entity_column = entity_columns[0]
        repeated = Counter(normalize_text(value).casefold() for value in columns[entity_column] if normalize_text(value))
        repeated = Counter({value: count for value, count in repeated.items() if count > 1})
        if repeated:
            first_value = next(iter(repeated))
            first_row = next(index + 1 for index, value in enumerate(columns[entity_column]) if normalize_text(value).casefold() == first_value)
            findings.append(FindingCandidate("potential_duplicate", "medium", "needs_confirmation", "Repeated potential participant keys", f"{len(repeated)} normalized {entity_column!r} values repeat across {sum(repeated.values())} rows. Repeated people may represent valid visits, so Scribe flags them without deleting anything.", entity_column, first_row, normalize_text(columns[entity_column][first_row - 1]), dict(repeated.most_common(10)), None, None, sum(repeated.values())))
        if date_columns:
            date_column = date_columns[0]
            groups: dict[tuple[str, str], list[int]] = defaultdict(list)
            for index, (entity, date) in enumerate(zip(columns[entity_column], columns[date_column]), start=1):
                groups[(normalize_text(entity).casefold(), normalize_text(date).casefold())].append(index)
            duplicate_groups = [rows for rows in groups.values() if len(rows) > 1]
            if duplicate_groups:
                affected = sum(len(rows) for rows in duplicate_groups)
                examples = [{"rows": rows[:10], "count": len(rows)} for rows in duplicate_groups[:10]]
                findings.append(FindingCandidate("potential_duplicate", "high", "needs_confirmation", "Repeated participant and visit-date combinations", f"{len(duplicate_groups)} combinations of {entity_column!r} and {date_column!r} occur more than once, affecting {affected} rows. These may be duplicate encounters or valid repeated records; compare the remaining fields before removal.", None, duplicate_groups[0][0], None, examples, None, None, affected))
        if not id_columns:
            findings.append(FindingCandidate("identity_not_verifiable", "high", "needs_confirmation", "Participant identity cannot be verified", f"{entity_column!r} resembles a participant name, but no reliable identifier column was detected. Names are not assumed unique; acknowledge this limitation or identify a primary/composite key.", entity_column, None, None, None, None, None, row_count))
    findings.sort(key=lambda item: ({"critical": 0, "high": 1, "medium": 2, "low": 3}[item.severity], item.row_number or 0))
    return {"sha256": hashlib.sha256(content).hexdigest(), "encoding": encoding, "encoding_confidence": "high" if encoding.startswith("utf-8") else "medium", "delimiter": delimiter, "delimiter_confidence": "high" if len(headers) > 1 else "low", "schema_fingerprint": hashlib.sha256(json_schema(headers).encode("utf-8")).hexdigest(), "row_count": row_count, "column_count": len(headers), "columns": profiles, "candidate_id_columns": id_columns, "warnings": warnings, "verified_relationships": ([{"kind": "arithmetic", "equation": "quantity * unit_price = total"}] if arithmetic_relationship_verified else []), "findings": [asdict(item) for item in findings], "parsing_config": config}


def json_schema(headers: list[str]) -> str:
    return "\u001f".join(headers)
