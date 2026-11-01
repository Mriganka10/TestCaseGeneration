import io
from collections import defaultdict

import openpyxl
import xlrd
import xlwt
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .models import CATEGORIES, Suite

HEADERS = [
    "Req. No.",
    "Test Case No.",
    "Test case description",
    "Test steps(Not Mandatory)",
    "Expected Result",
    "Severity of the test case",
    "Pass/Fail Status",
]
EXTRA = ["Category", "Source evidence", "Change / Selection reason"]
STATUSES = {"Not Run", "Pass", "Fail", "Blocked", "Not Applicable"}


def tables(suite: Suite):
    cases = [HEADERS + EXTRA]
    by_req = defaultdict(list)
    for c in suite.cases:
        for rid in c.requirement_ids:
            by_req[rid].append(c)
        cases.append(
            [
                ", ".join(c.requirement_ids),
                c.id,
                c.description,
                "\n".join(f"{i + 1}. {s}" for i, s in enumerate(c.steps)),
                c.expected_result,
                c.severity,
                c.status,
                c.category,
                "\n".join(f"{e.source} | {e.location} | {e.excerpt}" for e in c.evidence),
                c.change,
            ]
        )
    rtm = [["Req. No.", "Requirement", "Test Case Nos.", *CATEGORIES, "Execution summary", "Coverage notes"]]
    for r in suite.requirements:
        linked = by_req[r.id]
        cats = [", ".join(c.id for c in linked if c.category == cat) or "No cases" for cat in CATEGORIES]
        counts = {s: sum(c.status == s for c in linked) for s in sorted(STATUSES)}
        notes = [e.category + ": " + e.reason for e in suite.exclusions if e.requirement_id == r.id]
        rtm.append(
            [
                r.id,
                r.description,
                ", ".join(c.id for c in linked),
                *cats,
                "; ".join(f"{s}: {n}" for s, n in counts.items() if n),
                "\n".join(notes),
            ]
        )
    return {
        "Test Cases": cases,
        "RTM": rtm,
        "Requirements": [["Req. No.", "Description", "Acceptance criteria", "Related IDs", "Ambiguities"]]
        + [
            [
                r.id,
                r.description,
                "\n".join(r.acceptance_criteria),
                ", ".join(r.related_ids),
                "\n".join(r.ambiguities),
            ]
            for r in suite.requirements
        ],
        "Change Log": [["Version", "Change"]]
        + [[str(suite.version), c] for c in suite.changes]
        + [[str(suite.version), f"{c.id}: {c.change}; previous result: {c.status}"] for c in suite.retired],
        "Source Register": [["Source", "Location", "SHA-256", "Extraction notices"]]
        + [[s.name, s.locator, s.sha256, "\n".join(s.notes)] for s in suite.sources],
        "Review Gaps": [["Review required"]] + [[g] for g in suite.gaps],
        "Metadata": [
            ["Key", "Value"],
            ["Project ID", suite.project_id],
            ["Version", str(suite.version)],
            ["Bundle hash", suite.bundle_hash],
            ["Kind", suite.kind],
            ["Generated at", suite.generated_at],
            ["Model", suite.model],
            [
                "Execution guidance",
                "Edit Pass/Fail Status only. Import into the same project/version. Not Run does not mean Pass.",
            ],
        ],
    }


def export(suite: Suite, fmt="xls") -> bytes:
    data = tables(suite)
    for name, rows in data.items():
        if fmt == "xls" and len(rows) > 65536:
            raise ValueError(f"{name} exceeds XLS row capacity; choose XLSX")
        if any(len(str(cell)) > 32767 for row in rows for cell in row):
            raise ValueError(
                f"{name} contains a cell above Excel's 32,767 character limit; reduce source detail"
            )
    output = io.BytesIO()
    if fmt == "xls":
        book = xlwt.Workbook()
        header = xlwt.easyxf(
            "font: bold on, colour white; pattern: pattern solid, fore_colour dark_blue; alignment: wrap on, vert centre"
        )
        body = xlwt.easyxf("alignment: wrap on, vert top")
        for name, rows in data.items():
            sheet = book.add_sheet(name)
            sheet.set_panes_frozen(True)
            sheet.set_horz_split_pos(1)
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    # xlwt writes plain strings; formula-like source text is never a Formula object.
                    sheet.write(i, j, str(value), header if i == 0 else body)
                    sheet.col(j).width = 256 * (18 if j < 2 else 48)
                sheet.row(i).height_mismatch = True
                sheet.row(i).height = (
                    480
                    if i == 0
                    else min(2400, 300 * max(2, max(len(str(x)) // 45 + str(x).count("\n") + 1 for x in row)))
                )
        book.save(output)
    elif fmt == "xlsx":
        book = openpyxl.Workbook()
        book.remove(book.active)
        for name, rows in data.items():
            sheet = book.create_sheet(name)
            for row in rows:
                sheet.append(row)
            for row in sheet:
                for cell in row:
                    cell.data_type = "s"  # no formula injection from documents or model output
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
                    if cell.row == 1:
                        cell.font = Font(color="FFFFFF", bold=True)
                        cell.fill = PatternFill("solid", fgColor="17324D")
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for i in range(1, sheet.max_column + 1):
                sheet.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 20 if i < 3 else 48
        validation = DataValidation(type="list", formula1='"Not Run,Pass,Fail,Blocked,Not Applicable"')
        book["Test Cases"].add_data_validation(validation)
        validation.add(f"G2:G{max(2, len(suite.cases) + 1)}")
        book.save(output)
    else:
        raise ValueError("Output format must be xls or xlsx")
    return output.getvalue()


def read_tables(data, filename):
    if filename.lower().endswith(".xls"):
        book = xlrd.open_workbook(file_contents=data)
        return {
            s.name: [[str(s.cell_value(i, j)) for j in range(s.ncols)] for i in range(s.nrows)]
            for s in book.sheets()
        }
    if filename.lower().endswith(".xlsx"):
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        return {
            s.title: [[str(x) if x is not None else "" for x in row] for row in s.iter_rows(values_only=True)]
            for s in book
        }
    raise ValueError("Execution workbook must be .xls or .xlsx")


def import_results(suite: Suite, data: bytes, filename: str):
    sheets = read_tables(data, filename)
    meta = dict(row[:2] for row in sheets.get("Metadata", [])[1:] if len(row) >= 2)
    if (
        meta.get("Project ID") != suite.project_id
        or meta.get("Version") != str(suite.version)
        or meta.get("Bundle hash") != suite.bundle_hash
    ):
        raise ValueError(
            "Workbook belongs to a different project or stale document version. Download the current workbook."
        )
    if meta.get("Kind") not in {"Full", "Regression"}:
        raise ValueError("Invalid workbook kind")
    rows = sheets.get("Test Cases", [])
    if not rows or rows[0] != HEADERS + EXTRA:
        raise ValueError("Test Cases headers have changed. Edit only the Pass/Fail Status column.")
    expected = {row[1]: row for row in tables(suite)["Test Cases"][1:]}
    updates = {}
    for row in rows[1:]:
        if not any(row):
            continue
        if len(row) != len(HEADERS + EXTRA) or row[1] not in expected or row[1] in updates:
            raise ValueError("Unknown, duplicated or malformed test case row")
        original = expected[row[1]]
        # Selection reason is legitimately different in a regression workbook.
        if any(row[i] != original[i] for i in range(len(row)) if i not in (6, 9)):
            raise ValueError(f"Test design was edited for {row[1]}; only execution status may change")
        status = {s.lower(): s for s in STATUSES}.get(row[6].strip().lower())
        if status is None:
            raise ValueError(f"Invalid status for {row[1]}: use {', '.join(sorted(STATUSES))}")
        updates[row[1]] = status
    if not updates:
        raise ValueError("Workbook has no execution rows")
    if meta["Kind"] == "Full" and set(updates) != set(expected):
        raise ValueError("Full workbook is missing cases. Import the complete workbook.")
    result = suite.model_copy(deep=True)
    for case in result.cases:
        if case.id in updates:
            case.status = updates[case.id]
    return result
