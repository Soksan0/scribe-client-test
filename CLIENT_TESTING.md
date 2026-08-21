# Scribe client testing guide

Scribe is a local research-data cleaning app. For testing, run it on your own computer so uploaded datasets stay on that machine.

## Requirements

- Python 3.11 or newer
- Node.js 22.13 or newer
- npm
- Optional: R with `Rscript` on your PATH if you want verified clean exports. Without R, Scribe can still create provisional review packages.

## Start Scribe

Windows easiest option:

1. Install Python 3.11+ and Node.js 22+.
2. Download or clone the repo.
3. Double-click `Start-Scribe-Windows.bat`.
4. Open the local address printed in the terminal, normally `http://localhost:3000`.

For more detail, see `WINDOWS_CLIENT_GUIDE.md`.

macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
npm ci
python3 scripts/start_scribe.py
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
npm ci
.venv\Scripts\python scripts\start_scribe.py
```

The launcher prints and opens the local address, usually `http://localhost:3000`. Keep that terminal window open while using Scribe.

## GitHub handoff option

Use a private GitHub repository for source code only. Do not upload `.scribe_data/`, `.env`, `.venv/`, or `node_modules/`.

From the Scribe folder:

```bash
git init
git add .
git status
git commit -m "Prepare Scribe client testing build"
git branch -M main
git remote add origin https://github.com/YOUR_ORG_OR_USERNAME/scribe-client-test.git
git push -u origin main
```

Before inviting the client, confirm GitHub shows only source files and no project data. Then invite them to the private repository as a collaborator or send them GitHub's generated ZIP download link.

After cloning:

```bash
git clone https://github.com/YOUR_ORG_OR_USERNAME/scribe-client-test.git
cd scribe-client-test
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
npm ci
python3 scripts/start_scribe.py
```

## What to test

1. Create a project.
2. Upload a CSV, TSV, XLSX, SAV, DTA, or RDS dataset.
3. Review the file inventory and original/reviewed previews.
4. Open Issues and try:
   - accepting safe deterministic corrections;
   - editing a proposed correction before accepting;
   - using the Needs confirmation rationale options;
   - rejecting or deferring findings that need study context.
5. Run the current scan if Scribe asks for it after changes.
6. Generate a review package from Exports.
7. If R is installed, try the verified clean export gate.
8. Try Settings, Trash, Restore, and exact-name permanent deletion on a test project only.

## Important privacy notes

- Do not put real research data into GitHub.
- Runtime data is stored in `.scribe_data/`, which is intentionally ignored by source control.
- Local secrets belong in `.env` or `.env.local`, which are also ignored.
- Gemini is optional and disabled unless configured. If enabled, every Gemini request still requires in-app consent before sample data is sent out.

## Feedback requested

Please note:

- any issue explanation that is unclear;
- any cleaning suggestion that feels too aggressive or too conservative;
- any missing cleaning strategy your workflow needs;
- upload/export formats that fail;
- whether the review package is understandable enough for audit or handoff.
