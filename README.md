# ELMS Helper

A Windows command-line assistant for STI ELMS that scans course assignments,
reads instructions and PDF attachments, prepares answer PDFs—including requested
SVG diagrams—and can stage drafts for review. It never submits assignments.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- Microsoft Edge
- An STI ELMS account
- A Gemini API key, or an OpenRouter API key

The first launch creates a local virtual environment and installs Python
dependencies. No administrator access is required.

## Clone and run

```powershell
git clone https://github.com/Koni-hub/ELMS-Helper.git
cd ELMS-Helper
```

Double-click `ELMS-Helper.bat`. On first launch it runs `setup.ps1` to create
`_system\.venv` and install the packages in `requirements.txt`. Then use the
menu:

1. **Setup** — add a Gemini or OpenRouter API key and your display name.
2. **Log in to ELMS** — sign in in the helper's Microsoft Edge window.
3. **Scan assignments** — read-only scan of enrolled courses and assignment
   submission status.
4. **Run helper** — process eligible assignments, save PDFs, and stage answers.
5. **Doctor** — check the local setup.

You can also start from PowerShell:

```powershell
.\ELMS-Helper.bat
```

Or run a command directly:

```powershell
.\ELMS-Helper.bat setup
.\ELMS-Helper.bat login
.\ELMS-Helper.bat scan
.\ELMS-Helper.bat run --limit 3
.\ELMS-Helper.bat doctor
```

The first run asks you to sign into ELMS yourself. The session is saved locally
for later runs. The API key and session are not included in this repository.

## What option 4 does

- Scans assignment rows under each enrolled course's `#mainContent`.
- Skips submitted assignments, past-due rows, divider rows, Teacher Evaluation,
  and Student Grade Monitoring groups.
- Reads assignment instructions and attached PDFs, then asks the configured AI
  provider to prepare the response. Requested diagrams and wireframes are
  requested as inline SVG graphics in the generated PDF.
- Saves files under `output/<Course>/<Assignment>/`.
- Attempts to stage the answer on ELMS when an answer field or upload control is
  available.
- Never clicks Submit. Review the generated answer and submit it yourself if
  appropriate.

If ELMS has no detectable answer field, the PDF is still saved and the report
will say that manual upload is needed. AI output can be incomplete or incorrect;
review it before using it.

## Generated and local files

These are created on your computer and intentionally excluded from Git:

- `_system/config.json` — API keys and local settings
- `_system/.browser-profile/` — ELMS login session
- `_system/logs/` — diagnostic logs and run reports
- `output/` — generated PDFs and screenshots
- `_system/.venv/` — installed Python environment

Never commit API keys, browser sessions, assignment material, generated student
work, or personal logs.

## PDF notes

Selectable-text PDFs can be read using `pypdf`. Optional Poppler tools can be
placed in `_system/tools/poppler/` to extract text from more PDFs and convert
scanned pages to images for AI reading. Without Poppler, scanned-PDF image
extraction is unavailable; the rest of the helper can still run.

The answer PDF is rendered with Microsoft Edge and uses the bundled HTML template
at `_system/templates/answer.html`.

## Privacy

Assignment instructions and attachment content are sent to the AI provider you
configure. Use this only for material you are permitted to share with that
provider. ELMS credentials are entered by you in Edge; the helper does not ask
for or store your password. Local configuration and browser data are ignored by
Git.

## License

MIT. See [LICENSE](LICENSE).
