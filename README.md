# Spec to Test — test-case generation agent

A working local prototype for turning BRD/BRS documents into reviewable Excel test cases, an RTM, and failed-case regression workbooks. Built on the repository's `feature/prototype_v1` starting point.

## Run locally

Python 3.11–3.14 (3.12 recommended):

```bash
uv sync --extra dev --no-editable
cp .env.example .env
# Set OPENAI_API_KEY in .env. Keep credentials out of Git.
uv run --no-editable testcase-agent
```

Alternatively: `python -m venv .venv`, activate it, then `pip install '.[dev]'` and `testcase-agent`.
The non-editable launch avoids macOS/iCloud marking editable-install `.pth` files hidden, which recent Python versions skip.
Open http://127.0.0.1:8000. The UI works without credentials; AI generation requires your own API key. The Codex app's connected Drive account is **not** automatically available to this standalone application.

`OPENAI_MODEL` selects a vision-capable model supporting Responses structured outputs (default `gpt-4.1`). The model can be changed without editing the code. Source documents and images are sent to the configured OpenAI API; requests use `store=False`. No synthetic fallback is presented as real AI analysis.

## Workflow

1. Create a project. Upload a BRD and optional references, or paste a configured provider link.
2. For a Google Drive folder, use **Browse** to choose a document, or retain the folder URL and enter a filename pattern such as `BRD_*.docx`. A folder source tracks the newest matching file by modification time; it does not merge every folder document.
3. Choose `.xls` (default) or `.xlsx`. Set an output folder, or leave it blank to use the remote source's parent. Local uploads and generic HTTP sources need an explicit destination to publish.
4. Choose whether to fetch linked references and automatically publish. Click **Analyze & generate**.
5. Review requirements, evidence, coverage exclusions and ambiguities. Download the workbook or publish it using the destination button.
6. Edit **Pass/Fail Status** in the workbook. Import the edited workbook, or configure its remote URL and select **Sync execution link**. Use `Not Run`, `Pass`, `Fail`, `Blocked`, or `Not Applicable`.
7. Download or publish regression. This selects failed cases plus cases sharing or directly depending on their requirements, retains their IDs, and resets regression execution to `Not Run`. It does not claim that this heuristic represents complete regression coverage.
8. Upload replacement files into the same project, or update the remote source and generate again. The new document/reference content is compared with the prior version. Unchanged cases retain IDs and results; changed/dependent cases get new IDs and `Not Run`; retired cases remain in history.

**Revisions:** keep the same project. Source-generated requirement IDs are supplied back to the model to promote stable IDs. Impact comparison is conservative; wording/evidence changes can cause extra retesting. Review the change log. A changed supporting reference is included in the bundle hash.

**Background monitoring:** set `POLL_SECONDS=300` and enable the project's monitoring checkbox. The worker runs only while the single server process is running. It checks source revisions and, if configured, execution workbook changes. Enabling automatic publication publishes updated full workbooks and regression snapshots after changed failed results. Without publication, regression is available for local download. This is polling, not an always-on cloud scheduler or webhook service.

## Supported inputs and outputs

| Requirement | Implementation |
|---|---|
| DOCX | Paragraphs, tables, headers/footnotes, pasted images, hyperlinks, embedded Office package references |
| PDF | Text plus every rendered page (including scans and vector diagrams), embedded documents, hyperlinks |
| Legacy DOC | Converts to DOCX using LibreOffice; install it locally or use the Docker image |
| Reference documents | Explicit extra uploads, PDF attachments, supported DOCX embedded files, optional linked provider documents |
| Images | Vision model receives page images/pasted images; no silent text-only mode |
| Positive/negative/boundary/exception | Structured generation validated per requirement/category; explicit reasons when a category cannot be justified |
| Workbook | Real binary `.xls` or OOXML `.xlsx`, not CSV renamed to Excel |
| Traceability | RTM in the second tab with requirement/case mappings, category coverage and execution summary |
| Revisions | SQLite version snapshots, source hashes, preserved unchanged results, retired-case log |
| Failed-case regression | Failed cases plus shared/directly related requirement cases, export in the same column format |

The first seven columns exactly follow the requested layout:
`Req. No.`, `Test Case No.`, `Test case description`, `Test steps(Not Mandatory)`, `Expected Result`, `Severity of the test case`, `Pass/Fail Status`.
Three additional columns record category, evidence, and change/selection reason. Additional sheets: Requirements, Change Log, Source Register, Review Gaps, Metadata.

The Metadata sheet identifies project and source version. Execution import rejects stale, unknown, duplicate, missing, or edited test definitions before applying any result. Do not remove Metadata or rename headers. XLSX additionally offers a status dropdown; XLS uses the same validated status values on import.

## Configure Google Drive

Enable the Drive API in your Google Cloud project. Provide a credential JSON **outside the repository** through `GOOGLE_CREDENTIALS_FILE`:

- **User OAuth:** use an authorised-user JSON containing a refresh token obtained with the `https://www.googleapis.com/auth/drive` scope. The user must have access to the source and write permission on the destination. This prototype uses server-side credentials; it does not include a multi-user Google sign-in flow.
- **Service account:** use service-account JSON and a Google **Shared Drive** where it is a member with the required access. Service accounts generally cannot create files in ordinary users' My Drive storage; use OAuth for that case.

Google Docs are exported to DOCX for analysis. Google limits native export responses to 10 MB; larger native documents must be split or uploaded as downloaded PDF/DOCX. Shared Drive listing/download/upload requests support `supportsAllDrives`. Files are published as uniquely named version snapshots, never silently overwriting a tester's workbook. Select the workbook being executed via the execution URL.

For the BRD created during this project, paste its Google Docs URL in the source field and use the accessible **AI Agent -Test Case Generation** folder URL as the destination. Ensure the application's credential identity has access to both.

## Dropbox, Jira and other links

- **Dropbox:** set `DROPBOX_ACCESS_TOKEN` with `files.metadata.read`, `files.content.read`, `files.content.write` and `sharing.read` as needed. Read a shared **file** URL or a connector path such as `dropbox:/requirements/BRD.docx`. Folder listing and destinations use `dropbox:/requirements`. Arbitrary shared folder URLs are not recursively imported; use the connected account's folder path. Long-running deployments need token refresh handling outside this prototype.
- **Jira Cloud:** set `JIRA_BASE_URL=https://your-company.atlassian.net`, `JIRA_EMAIL`, and `JIRA_API_TOKEN`. Use a `/browse/PROJECT-123` issue URL. The adapter reads issue description and supported attachments, and can attach generated workbooks to an issue. It does not create native Xray/Zephyr test entities. Referenced Jira issues are not traversed recursively.
- **Other direct links:** exact HTTPS hostnames must be explicitly configured in `ALLOWED_DOWNLOAD_HOSTS`. Only direct files are supported; HTML login pages, arbitrary file-sharing sites and redirect chains are not a universal connector. No provider credentials are sent to arbitrary hosts. Private/local resolved IPs are blocked; production deployments also need network-level egress restrictions against DNS rebinding.

Unreadable references/unsupported embedded OLE objects are surfaced as review gaps; unsupported standalone attachment formats fail explicitly. Encrypted PDFs must be unlocked. Images inside linked documents are processed once those documents are fetched. DOC conversion may change layout; review converted evidence.

## Architecture and boundaries

- `app.py`: FastAPI endpoints, local UI, background worker, access token and CSRF checks.
- `connectors.py`: Google Drive, Dropbox, Jira, allowlisted HTTPS.
- `documents.py`: text/visual/reference extraction and bounded bundles.
- `generation.py`: two-stage structured model analysis and case generation, coverage checks, version impact and regression selection.
- `workbooks.py`: Excel/RTM export and validated execution import.
- `store.py`: persisted projects, jobs, versioned suites; default `./data/`.

No external messages are sent. Publication occurs only when the user enables it or clicks Publish. Do not expose the local app to a network without `APP_TOKEN`; the CLI refuses a public bind without it. Set `ALLOWED_HOSTS` to the actual deployment hostname. Run **one application process/worker**; this prototype is not multi-tenant and has no user roles, durable distributed queue, malware scanning, or production audit service. Documents are retained locally in `data/`; delete this folder to erase the local workspace. Treat this data as sensitive. Protect logs and credential files.

Defaults: 20 MB per file, 60 MB per bundle, 12 documents, 60 PDF pages per file, 60 total visual pages/images, and 160,000 extracted characters. Limits fail clearly rather than truncating requirements. XLS has a 65,536-row limit; choose XLSX for larger outputs. Individual Excel cells are limited to 32,767 characters. Every successful AI generation is still a **draft requiring QA review**: schema and coverage validation cannot prove semantic correctness.

## Test and development

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests cover document/image/attachment extraction, reference failures, XLS/XLSX output, RTM, stale and edited workbook rejection, formula safety, changed/removed/related requirements, status preservation, regression selection, API auth and end-to-end uploads using a deterministic model double. They do **not** consume API tokens or validate live third-party credentials.

A small Azure learning BRD is provided in `examples/azure_active_customers_brd.txt`. Upload it to try the workflow with your API key. CI runs the offline suite on pushes and pull requests.

## Docker (includes legacy DOC conversion)

```bash
docker build -t testcase-agent .
# Set APP_TOKEN in .env before running; mount credential files explicitly if needed.
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  -v testcase-data:/app/data testcase-agent
```

Official API references used: [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [Google Drive downloads/exports](https://developers.google.com/workspace/drive/api/guides/manage-downloads).
