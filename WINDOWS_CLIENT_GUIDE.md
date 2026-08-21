# Scribe Windows client testing guide

This guide is for testers who want to run Scribe on their own Windows computer.

Scribe runs locally. That means:

- your browser opens Scribe at a private loopback address, normally `http://localhost:3000`;
- uploaded datasets stay on your computer;
- GitHub is only used to download the app code;
- datasets are not uploaded to GitHub unless you manually add them there.

## 1. Install the two required apps

Install these first:

1. Python 3.11 or newer  
   https://www.python.org/downloads/

   During install, check **Add python.exe to PATH** if Windows shows that option.

2. Node.js 22 or newer  
   https://nodejs.org/

After installing both, close and reopen PowerShell.

## 2. Download Scribe

Option A: download ZIP

1. Open the GitHub repo.
2. Click **Code**.
3. Click **Download ZIP**.
4. Unzip it.
5. Open the unzipped folder.

Option B: clone with Git

```powershell
git clone https://github.com/Soksan0/scribe-client-test.git
cd scribe-client-test
```

## 3. Start Scribe

The easiest option is to double-click:

```text
Start-Scribe-Windows.bat
```

If Windows blocks it, right-click the file, choose **Properties**, check **Unblock** if present, then try again.

If you prefer PowerShell, open PowerShell in the Scribe folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Start-Scribe-Windows.ps1
```

The first run can take a few minutes because it installs local project packages.

## 4. Open the app

The starter should open Scribe automatically. If it does not, open:

```text
http://localhost:3000
```

If port `3000` is already busy, Scribe may choose another local address. Use the address printed in the terminal window.

Leave the starter window open while using Scribe. Closing it stops the local data service, so uploads will no longer work.

## 5. Stop Scribe

Go back to the terminal window and press:

```text
Ctrl + C
```

## 6. What to test

Please try this with a sample or copied dataset first:

1. Create a project.
2. Upload a dataset by choosing a file or dragging it onto the Files screen. Scribe accepts CSV, TSV, XLSX, SAV, DTA, and RDS files up to 250 MB.
3. Review the Issues page.
4. Try accepting a safe correction.
5. Try rejecting, deferring, or marking a finding as a false positive.
6. Export the reviewed/cleaned package.
7. If R is installed, try a verified clean export.
8. Try Settings, Trash, Restore, and permanent delete on a test project only.

## 7. Privacy notes

- Do not upload real client data to GitHub.
- Do not email the `.scribe_data` folder unless intentionally sharing local test data.
- Scribe stores local projects in `.scribe_data` inside the project folder by default.
- `.scribe_data`, `.env`, `.venv`, and `node_modules` are intentionally ignored by Git.

## 8. If something goes wrong

Please send:

- a screenshot of the Scribe page;
- a screenshot of the terminal window;
- what file type you uploaded, such as CSV or XLSX;
- what you expected to happen;
- what actually happened.
