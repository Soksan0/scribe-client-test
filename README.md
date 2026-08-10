# Scribe

Scribe is a privacy-first research dataset quality-assurance application. It profiles datasets, presents evidence-backed findings for review, and creates cleaned copies and reproducible R scripts without changing original uploads.

## Current working capabilities

- Local projects and immutable uploaded originals with SHA-256 fingerprints
- Stable internal row identities so corrections remain attached to the approved records after row deletion
- Recoverable project Trash, restoration, and exact-name-confirmed permanent deletion
- CSV, TSV, XLSX, SPSS SAV, Stata DTA, and tabular RDS ingestion
- Column profiling and candidate identifier detection
- Duplicate identifier/row/column, normalized near-duplicate, `nan`/missing token, malformed row, whitespace, category, type/date, number-word, impossible value, outlier, range, scale, cross-column, and confirmed cross-file findings
- Suggestion-first rule setup with one-click confirmation of high-confidence identifier and type rules
- A 17-section clean-data checklist included in backend-derived readiness
- High-confidence deterministic corrections only; ambiguous findings remain review-only
- Accept, edit, reject, guarded batch review, and reversible decisions in the Guided Review interface
- Immediate reviewed-version rebuilds from immutable originals and active accepted transformations
- Same-format reviewed copies for all six supported formats
- R cleaning scripts generated from the same transformation records as Python exports
- Downloadable ZIP and individual cleaned files, R scripts, decision/audit records, findings report, readiness report, and hash manifest
- Local SQLite project state; no authentication or cloud storage, with Gemini disabled unless explicitly configured

## Optional Gemini assistant

Core profiling and cleaning remain local. Gemini is optional and is used only for custom requests that deterministic checks cannot express. It can create pending, evidence-linked proposals but cannot accept or apply them.

Set the key only in the backend environment before starting Scribe:

Copy `.env.example` to `.env`, then add the key locally:

```dotenv
GEMINI_API_KEY=your-restricted-key
GEMINI_MODEL=gemini-3.6-flash
```

The launcher reads `.env` and `.env.local` automatically. Restart Scribe after changing either file:

```bash
python3 scripts/start_scribe.py
```

The key is never stored in the browser, SQLite, exports, or source code. Each Gemini request also requires explicit in-app consent because dataset profiles and up to 25 selected sample rows are sent to Google. Use a Gemini-restricted key and review Google's current retention and usage terms before sending sensitive research data.

## Run locally

Requirements: Python 3.11 or newer, Node.js 22.13 or newer, and npm.

### macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
npm ci
python3 scripts/start_scribe.py
```

### Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
npm ci
.venv\Scripts\python scripts\start_scribe.py
```

The launcher prints and opens the exact local address it selected. It reuses a healthy Scribe instance and chooses new loopback ports when the defaults are occupied.

## Verification

```bash
.venv/bin/pytest
npm run lint
npm run build
```

The repeatable live-browser journey is in `tests/browser-journey.mjs`. It accepts `SCRIBE_UI_URL`, `SCRIBE_CHROME_PATH`, and optionally `SCRIBE_PLAYWRIGHT_MODULE` when Playwright is supplied by a shared local runtime.

The benchmark suite includes 10,000-row, hundreds-of-column validation. Format fixtures verify that original files remain unchanged and that supported output data and workbook sheets survive reviewed exports.

## Privacy and data layout

Runtime project data is stored under `.scribe_data/` unless `SCRIBE_DATA_DIR` is set. Each project has separate immutable `originals/` and generated `exports/` areas. `.scribe_data/` is ignored by source control.

Scribe does not execute spreadsheet macros, formulas, uploaded scripts, or serialized code. Excel formulas are flagged and are never rewritten automatically.

## Current boundaries

Scribe's MVP is a dataset-cleaning tool. Document review, methodological QA, citation checking, manuscript review, OCR, transcription, authentication, subscriptions, and cloud infrastructure are intentionally excluded.

Range rules, scale rules, missing-value code mappings, and cross-file relationships require confirmation before they can produce accepted transformations. Advanced Excel objects and format-specific metadata are preserved where the open-source libraries safely support them; any known limitation must be shown before export.
