import io

import openpyxl
import pytest
import xlrd

from testcase_agent.generation import regression
from testcase_agent.workbooks import HEADERS, export, import_results, read_tables


@pytest.mark.parametrize("fmt", ["xls", "xlsx"])
def test_workbook_is_real_excel_and_has_rtm(sample_suite, fmt):
    data = export(sample_suite, fmt)
    assert data.startswith(b"\xd0\xcf\x11\xe0") if fmt == "xls" else data.startswith(b"PK")
    sheets = read_tables(data, "cases." + fmt)
    assert sheets["Test Cases"][0][:7] == HEADERS
    assert sheets["RTM"][1][0] == "FR-01"
    assert all(c.id in sheets["RTM"][1][2] for c in sample_suite.cases)
    assert len(sheets["Test Cases"]) == 5


def modified(suite, edits):
    book = openpyxl.load_workbook(io.BytesIO(export(suite, "xlsx")))
    edits(book)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_execution_and_regression(sample_suite):
    data = modified(sample_suite, lambda book: setattr(book["Test Cases"]["G2"], "value", "Fail"))
    updated = import_results(sample_suite, data, "execution.xlsx")
    assert updated.cases[0].status == "Fail"
    assert sample_suite.cases[0].status == "Not Run"
    reg = regression(updated)
    assert len(reg.cases) == 4
    assert reg.cases[0].change == "Failed-case retest"
    assert all(c.status == "Not Run" for c in reg.cases)
    assert updated.cases[0].status == "Fail"


def test_stale_workbook_rejected(sample_suite):
    data = export(sample_suite, "xls")
    sample_suite.version += 1
    with pytest.raises(ValueError, match="stale"):
        import_results(sample_suite, data, "old.xls")


@pytest.mark.parametrize("cell,value", [("G2", "Done"), ("E2", "changed expected result"), ("B2", "TC-9999")])
def test_invalid_execution_rejected_atomically(sample_suite, cell, value):
    data = modified(sample_suite, lambda b: setattr(b["Test Cases"][cell], "value", value))
    with pytest.raises(ValueError):
        import_results(sample_suite, data, "invalid.xlsx")
    assert all(c.status == "Not Run" for c in sample_suite.cases)


def test_missing_or_duplicate_cases_rejected(sample_suite):
    data = modified(sample_suite, lambda b: b["Test Cases"].delete_rows(2))
    with pytest.raises(ValueError, match="missing"):
        import_results(sample_suite, data, "partial.xlsx")
    data = modified(sample_suite, lambda b: b["Test Cases"].append([c.value for c in b["Test Cases"][2]]))
    with pytest.raises(ValueError, match="duplicated"):
        import_results(sample_suite, data, "duplicate.xlsx")


def test_formula_injection_is_plain_text(sample_suite):
    sample_suite.cases[0].description = '=HYPERLINK("https://example.com", "click")'
    data = export(sample_suite, "xlsx")
    book = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    assert book["Test Cases"]["C2"].data_type == "s"
    data = export(sample_suite, "xls")
    assert (
        xlrd.open_workbook(file_contents=data).sheet_by_name("Test Cases").cell(1, 2).ctype
        == xlrd.XL_CELL_TEXT
    )


def test_no_failed_cases_no_regression(sample_suite):
    with pytest.raises(ValueError, match="No failed"):
        regression(sample_suite)


def test_excel_cell_limit_not_truncated(sample_suite):
    sample_suite.cases[0].description = "x" * 32768
    with pytest.raises(ValueError, match="32,767"):
        export(sample_suite)
