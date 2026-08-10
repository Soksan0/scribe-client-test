# Scribe Project Handoff

**Prepared:** August 4, 2026  
**Workspace:** `/Applications/Scribe_GPT`  
**Current status:** Functional dataset-cleaning prototype with substantial QA work completed, but it is not release-ready. Project deletion and Settings are planned but not implemented.

## 1. Initial Goal and Product Context

Scribe is intended to be a trustworthy, AI-assisted research quality-assurance application for researchers with limited technical knowledge.

The original long-term vision is to help a researcher review an entire study or dissertation project before submission. That includes:

- Cleaning and validating large datasets while preserving data integrity.
- Detecting duplicate IDs and rows, missing data, inconsistent coding, invalid scales, whitespace, malformed values, and cross-file inconsistencies.
- Comparing proposals, surveys, consent forms, manuscripts, transcripts, and related documents.
- Finding missing citations, unsupported claims, repeated text, and contradictions, with evidence for every finding.
- Answering questions only from uploaded materials and citing the exact source.
- Producing a transparent readiness score and effort estimate.
- Exporting reviewed copies, audit records, reports, source-linked summaries, and reproducible R cleaning scripts.

The current development scope is intentionally narrower: make dataset cleaning reliable before expanding into document and methodology review.

### Current MVP purpose

Scribe should:

1. Create one project per study.
2. Preserve every uploaded original unchanged.
3. Profile uploaded datasets.
4. Detect data-quality problems automatically.
5. Explain what was found, why it matters, and where it occurs.
6. Propose corrections without applying them automatically.
7. Let the researcher accept, edit, reject, acknowledge, defer, or undo findings.
8. Rebuild a separate reviewed copy from the immutable original and accepted transformations.
9. Generate an audit trail and reproducible R script.
10. Export cleaned or provisional files without overwriting the original.

Scribe's primary job is data cleaning, not statistical analysis. Its outputs should be ready for the researcher to analyze elsewhere.

### Product principles

- Correctness and reproducibility before flashy AI features.
- No hidden or automatic changes to research data.
- Explicit uncertainty when a correction cannot be justified.
- Evidence, source locations, confidence, and assumptions for every finding.
- Local and open-source technologies wherever possible.
- Python backend and a simple, modern researcher-friendly frontend.
- No authentication, subscriptions, or cloud infrastructure for the MVP.
- Modular design so later document and methodology checks can reuse the evidence, audit, and readiness architecture.
- Simple architecture that a Computer Science student can understand and maintain.

## 2. Current Architecture

### Frontend

- React 19 with a Next.js-compatible App Router surface, currently built and served through Vinext/Vite.
- The main UI is concentrated in `app/ScribeClient.tsx`.
- Global styling is concentrated in `app/globals.css`.
- Refreshable project routes currently include:
  - `/projects/{id}/overview`
  - `/projects/{id}/files`
  - `/projects/{id}/rules`
  - `/projects/{id}/issues`
  - `/projects/{id}/exports`
- The home screen lists and creates projects.

### Backend

- FastAPI and Python.
- Most API and workflow logic is concentrated in `backend/app/main.py`.
- Dataset profiling and detection logic is primarily in `backend/app/profiling.py`.
- Reviewed-copy and R-script behavior is primarily in `backend/app/exporting.py`.
- Format handling supports CSV, TSV, XLSX, SPSS SAV, Stata DTA, and tabular RDS.

### Persistence and local storage

- SQLite database configured in `backend/app/database.py`.
- Runtime state is stored under `.scribe_data/` unless `SCRIBE_DATA_DIR` is set.
- Project files are stored under `.scribe_data/projects/{project_id}`.
- Originals, reviewed versions, reports, scripts, manifests, and export bundles are stored separately.
- SQLite foreign keys are enabled, but existing project-child relationships use restrictive `NO ACTION` behavior rather than cascading deletion.

### Optional AI

- Gemini is optional and is not authoritative.
- Deterministic local checks should remain the source of truth.
- The environment supports `GEMINI_API_KEY` and `GEMINI_MODEL`.
- Gemini should propose reviewable rules or corrections only after explicit consent to send samples.
- Gemini must never silently apply changes or mark a quality check as passed.

## 3. Work Completed So Far

### Startup and navigation

- Restored the missing global stylesheet path that previously prevented the app from loading.
- Added real URL-based navigation for the five current workflow pages.
- Added handling for backend-unavailable states instead of substituting demo data.
- Added local launcher behavior for selecting usable ports and passing the API address to the frontend.
- Removed hard-coded demo findings, fake counts, and static preview rows.

### Dataset ingestion and preservation

- Added immutable original-file storage and SHA-256 hashes.
- Added streamed uploads with a 250 MB limit rather than reading the whole request into memory.
- Added filename and path validation.
- Added duplicate-upload, empty-file, unsupported-file, and unsafe-name tests.
- Added ingestion for CSV, TSV, XLSX, SAV, DTA, and RDS.
- Added profile information, schema fingerprints, row and column counts, warnings, encoding, delimiter, and format metadata.

### Detection and cleaning improvements

- Added or strengthened detection for:
  - Missing values and contextual missing tokens.
  - Written numbers such as `forty` in confidently numeric columns.
  - Invalid types and mixed-type columns.
  - Impossible ages and range violations.
  - Exact duplicate rows.
  - Duplicate IDs and composite values.
  - Near-duplicate records.
  - Duplicate and normalized-duplicate columns.
  - Leading, trailing, repeated, invisible, and non-breaking whitespace.
  - Category capitalization and coding inconsistencies.
  - Dates, ambiguous date formats, Boolean variants, patterns, scales, and outliers.
  - Cross-column arithmetic relationships.
- Added contextual handling so text such as a person's name `Nan` or a state code `NA` is not always erased as missing.
- Added safeguards so semantic identifiers, telephone numbers, postal codes, and similar values preserve leading zeros.
- Added arithmetic inference when exactly one value is missing and the other values prove it. Example: `price = 4`, `total = 20`, and missing `quantity` can produce a reviewable proposal of `quantity = 5`.
- Arithmetic inference checks all source preconditions and avoids ambiguous reverse calculations.
- Added grouped transformations so repeated changes such as many `forty -> 40` cells can be reviewed as one logical operation.
- Added conflict detection and coalescing to reduce duplicate operations on the same cell.
- Prevented names such as `Patient Name` from being automatically declared unique identifiers.

### Rules and findings

- Added automatic generic checks after upload so researchers do not need to create every basic rule manually.
- Added suggested and advanced rules for uniqueness, required fields, missing codes, allowed values, ranges, scales, types, dates, patterns, cross-column conditions, and cross-file relationships.
- Added signatures to prevent identical confirmed rules from being inserted repeatedly.
- Added conflict checks for incompatible active rules.
- Added grouped findings, evidence, confidence, detector metadata, lifecycle fields, dispositions, and scan associations.
- Added accepted, rejected, acknowledged, false-positive, deferred, superseded, and undo behavior.
- Added atomic batch-decision preflight so conflicting transformations should fail before partial decisions are saved.

### Reviewed versions and reproducibility

- Reviewed copies are rebuilt from immutable originals and active accepted transformations.
- Transformations include source preconditions and operation hashes.
- Undo rebuilds from the original and remaining accepted transformations.
- Added reviewed-version history, reviewed hashes, transformation counts, and validation metadata.
- Added R scripts generated from the same transformation records used by Python.
- Added same-format reviewed exports for supported formats.
- Added ZIP bundles and individual cleaned files, scripts, audit records, findings, readiness reports, and hash manifests.
- Added provisional review exports and gating logic for verified exports.

### Readiness and checklist

- Added scan runs and a 17-section data-cleaning checklist.
- Added evidence-linked check results and backend-derived readiness.
- Added readiness caps and verified-export gates intended to prevent unresolved work from being labelled clean.
- Added acknowledgement/disposition behavior for limitations that cannot be automatically verified.

### Gemini

- Added environment-variable support in `.env.example` and backend environment loading.
- Added Gemini status and connection-test endpoints.
- Added model validation through the Gemini Models endpoint.
- Added explicit consent before sending samples.
- Restricted Gemini output to pending proposals that still require researcher review.

### UI and UX improvements

- Added file inventory, Original/Reviewed previews, hashes, scan state, schema fingerprint, and integrity panels.
- Added automatic selection of uploaded files for preview.
- Added grouped issue review, batch-preview controls, evidence panels, filters, paging, decisions, dispositions, and undo.
- Improved correction-button visibility and disabled states.
- Added loading, success, and error messages in the main workflows.
- Added export status, artifact downloads, and verified/provisional messaging.

## 4. Real Fixtures and Acceptance Work

### `healthcare_messy_data.csv`

The healthcare fixture was used to drive improvements for:

- `nan` handling in the Age column.
- Written ages such as `forty`.
- Missingness summaries.
- Name whitespace without unwanted capitalization.
- Date standardization.
- Duplicate and identity limitations.
- Rule conflicts and duplicate transformations.
- Reopening reviewed output with blank-or-integer ages and standardized dates.

### `dirty_cafe_sales.csv`

The cafe fixture was used to drive arithmetic cleaning:

- Deriving quantity, price, or total only when two valid values prove the third.
- Verifying arithmetic preconditions before applying a transformation.
- Rejecting ambiguous or contradictory calculations.
- Recording formulas and source values in the R script and audit trail.

## 5. Current Test Evidence

As of August 4, 2026:

- Backend automated suite: **37 tests passed**.
- Frontend build: **passed**.
- Rendered-route/control suite: **2 tests passed**.
- The build reports that Vinext cannot statically classify every route; this is informational, not currently a build failure.
- The backend reports a Starlette/TestClient deprecation warning involving `httpx`; tests still pass, but this dependency should be updated later.

Important: these passing tests do not prove the live application is release-ready. Earlier live-browser sessions exposed failures that unit tests missed. A complete real-browser journey must remain a release requirement.

## 6. Problems Still Present

### Highest priority: projects cannot be deleted

This is confirmed in the current code:

- The backend has create, list, get, and update project endpoints but no project delete endpoint.
- The home page renders projects only as links and has no delete control.
- There is no project Settings route.
- Deleting only the project row would fail because child foreign keys do not cascade.
- Deleting only the row would also leave original files, reviewed versions, reports, scripts, and exports on disk.

The deletion implementation was planned but **not implemented** before this handoff.

The approved design is:

- Add a recoverable Trash state using `projects.deleted_at`.
- Allow Move to Trash from Home and Settings.
- Add Active and Trash project views.
- Allow complete restoration without changing files, hashes, decisions, or audit history.
- Require an already-trashed project and exact project-name confirmation before permanent deletion.
- Block normal operations while a project is trashed.
- Reject deletion while processing jobs are active.
- Use a path-safe quarantine plus dependency-ordered database transaction for permanent deletion.
- Restore the directory if the database transaction fails.
- Retain only a minimal deletion tombstone after permanent deletion.
- Add `/projects/{id}/settings` for editing the project and accessing the danger zone.

### Live-browser confidence is still insufficient

- The default browser runner currently executes the general journey and cafe journey when the external cafe fixture exists.
- The healthcare browser journey exists but is not clearly included in the default runner.
- Project trash, restore, permanent deletion, Settings, invalid routes, and deletion rollback have no tests because the feature does not exist yet.
- The complete release suite should verify no console errors, failed requests, inaccessible controls, stale state, or dead actions.

### Passing tests previously missed real failures

Past user testing found:

- `Failed to fetch` caused by CORS/API-port mismatches.
- Batch correction requests returning HTTP 500.
- Gemini appearing configured but not working.
- Invisible or unclear correction controls.
- Rules being added repeatedly.
- Findings that were duplicated or semantically incorrect.
- Values being reformatted without genuinely solving the data-quality issue.
- Exports claiming verification before all gates were satisfied.

Many related fixes now exist, but each should be rechecked against the running app rather than assumed fixed from unit tests.

### Data-quality and research-trust gaps to keep reviewing

- Every checklist item must distinguish pass, attention, acknowledged, not applicable, blocked, and failed. Absence of findings must never imply a pass.
- Missing tokens must remain contextual. `Nan` and `NA` cannot be globally erased.
- Ambiguous dates require a confirmed locale.
- Category fuzzy matching and near-duplicate survivorship require human review.
- Duplicate removal must require selecting retained and removed records.
- Participant uniqueness must be reported as unverifiable when no trustworthy ID exists.
- Cross-file integrity must be evaluated on reviewed versions and confirmed relationships.
- Verified exports must remain blocked until hashes, output reopening, audit counts, transformation counts, and R reproduction agree.
- If R is unavailable, the app must say reproduction was not verified and must not label the dataset Clean.
- Metadata preservation for SAV, DTA, RDS, and complex XLSX files requires continued round-trip verification.

### Maintainability concerns

- `app/ScribeClient.tsx` is very large and contains most frontend screens and behavior.
- `backend/app/main.py` is very large and contains API models, endpoints, Gemini behavior, scans, decisions, and exports.
- These files should be divided gradually by feature, without rewriting the whole application at once.
- The workspace is not currently recognized as a Git repository, so normal status, diff, commit, and rollback workflows are unavailable. Confirm whether `.git` was omitted or whether this is an exported working copy before making broad changes.
- Both `.scribe_data/scribe.db` and `.scribe_data/scribe.sqlite3` exist. The configured application database is `scribe.sqlite3`; the older file should be investigated before any cleanup and must not be deleted blindly.
- The README's Gemini setup code block is malformed and should be corrected.

### Scope divergence that the next engineer must understand

The latest development prompt says R scripts, Gemini, and multi-file projects were future features, but the current code already contains all three. Do not delete these capabilities merely to match the older prompt. Stabilize them if they remain in the visible product.

The broader document-comparison, citation-review, methodology-review, and uploaded-material Q&A features remain paused until dataset cleaning passes the release gates.

## 7. Data Safety Context

- `.scribe_data/` currently occupies approximately **543 MB**.
- The project currently open in the browser, `prj_e3595e117d01445d9cb5e1d970b99351`, occupies approximately **355 MB**.
- Do not use this real project for destructive testing.
- Use a temporary `SCRIBE_DATA_DIR` and test projects for trash and permanent-deletion testing.
- Never overwrite originals or manually remove project directories as a substitute for implementing lifecycle APIs.

## 8. Recommended Next Milestones

### Milestone 1: Project Trash and Settings

- Add a safe database migration for `deleted_at` and deletion status.
- Add active/trashed list filtering, soft delete, restore, and confirmed permanent-delete APIs.
- Add Settings, Home project actions, Trash view, accessible confirmation dialogs, and clear error states.
- Add full backend, component, and browser tests using temporary data.
- Verify restore preserves every original hash and permanent deletion affects only the exact test project.

### Milestone 2: Full browser regression

- Run project creation, upload, scan, rule confirmation, issue review, batch preview, accept, edit, reject, acknowledge, undo, reviewed preview, export, download, trash, restore, and permanent deletion.
- Run the healthcare and cafe fixtures through the real frontend and backend.
- Check browser refresh, back/forward, repeated clicks, network failures, invalid URLs, and console output.
- Fix every discovered defect before describing the flow as complete.

### Milestone 3: Reproducibility release gate

- Reopen every exported artifact and compare schema, values, order, missingness, sheets, labels, and supported metadata.
- Execute generated R scripts when R is available and compare canonical results with Python-reviewed data.
- Verify original hash, reviewed hash, manifest, audit, and transformation counts.
- Keep verified export disabled until every applicable gate passes.

### Milestone 4: Maintainability cleanup

- Split frontend screens and backend route groups incrementally.
- Preserve API behavior and run the regression suite after each extraction.
- Repair documentation and clarify which SQLite file is authoritative.
- Restore or initialize version-control history only with the project owner's approval.

### Milestone 5: Broader research QA

Only after the dataset-cleaning release gate passes:

- Add document inventory and parsing.
- Add proposal/survey/consent/manuscript comparison.
- Add citation and unsupported-claim checks.
- Add qualitative retrieval and source-only answers with exact citations.
- Reuse the existing evidence, decision, audit, version, and readiness concepts.

## 9. Definition of Done

Do not call Scribe's dataset output Clean merely because the page builds or a CSV looks neater.

A dataset is Clean only when:

- The original hash still matches the upload.
- The latest scan corresponds to the latest reviewed transformation plan.
- Every applicable check passed or was explicitly acknowledged.
- No unresolved critical or pending finding remains.
- Every applied transformation has evidence, a decision, source preconditions, and audit lineage.
- The reviewed output reopens successfully and preserves required schema, values, row order, missingness, and metadata.
- The R script reproduces the reviewed data, or the app truthfully states that R verification is unavailable and does not grant Clean status.
- Export validation, hashes, audit counts, and transformation counts agree.
- The readiness report records the final status and limitations.

## 10. Immediate Handoff Note

The next concrete task is to implement the approved Project Trash and Settings repair. The investigation is already complete: deletion is absent at both the API and UI layers, and safe deletion requires coordinated database and filesystem handling. No project-deletion code had been applied at the time this document was created.
