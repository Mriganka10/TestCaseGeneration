import io
import zipfile

import pymupdf as fitz
import pytest
from docx import Document as Word
from PIL import Image

from testcase_agent.config import Settings
from testcase_agent.connectors import Asset
from testcase_agent.documents import bundle, extract


def image():
    out = io.BytesIO()
    Image.new("RGB", (40, 40), "blue").save(out, "PNG")
    return out.getvalue()


def test_docx_text_tables_images_and_reference():
    doc = Word()
    doc.add_paragraph("FR-01: status must be Active")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Maximum"
    table.cell(0, 1).text = "1000 rows"
    doc.add_picture(io.BytesIO(image()))
    doc.add_paragraph("Reference https://drive.google.com/file/d/reference/view")
    data = io.BytesIO()
    doc.save(data)
    parsed = extract(Asset("BRD.docx", data.getvalue(), "local"), Settings())
    assert "1000 rows" in parsed.text
    assert len(parsed.images) == 1
    assert len(parsed.links) == 1
    docs, _ = bundle([Asset("BRD.docx", data.getvalue(), "local")], Settings())
    assert "Reference not fetched" in docs[0].source.notes[0]


def test_pdf_scans_vector_and_attachment():
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((50, 50), "FR-01: Active customers")
    page.draw_rect((50, 100, 150, 150), color=(1, 0, 0))
    pdf.embfile_add("reference.txt", b"Limit is 1000 rows", filename="reference.txt")
    docs, _ = bundle([Asset("BRD.pdf", pdf.tobytes(), "local")], Settings())
    assert len(docs) == 2
    assert len(docs[0].images) == 1
    assert "Active customers" in docs[0].text
    assert "1000" in docs[1].text


def test_pdf_page_limit_not_silent():
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    with pytest.raises(ValueError, match="page limit"):
        extract(Asset("BRD.pdf", pdf.tobytes(), "local"), Settings(max_pages=1))


def test_image_limit_not_silent():
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    with pytest.raises(ValueError, match="images exceed"):
        bundle([Asset("BRD.pdf", pdf.tobytes(), "local")], Settings(max_images=1))


def test_cyclic_reference_is_read_once():
    asset = Asset("BRD.txt", b"FR-1 https://drive.google.com/file/d/ref/view", "local")
    calls = []

    def reader(link):
        calls.append(link)
        return [asset]

    docs, _ = bundle([asset], Settings(), reader, True)
    assert len(docs) == 1 and len(calls) == 1


def test_reference_failure_is_recorded():
    def reader(link):
        raise ValueError("not authorised")

    docs, _ = bundle(
        [Asset("BRD.txt", b"Reference https://drive.google.com/file/d/ref/view", "local")],
        Settings(),
        reader,
        True,
    )
    assert "Unresolved reference" in docs[0].source.notes[0]


def test_fake_docx_and_oversized_rejected():
    with pytest.raises(ValueError, match="valid DOCX"):
        extract(Asset("fake.docx", b"plain text", "local"), Settings())
    with pytest.raises(ValueError, match="exceeds"):
        extract(Asset("big.txt", b"123456", "local"), Settings(max_file_bytes=4))


def test_docx_container_metadata_does_not_trigger_revision():
    doc = Word()
    doc.add_paragraph("FR-01: Active only")
    data = io.BytesIO()
    doc.save(data)
    first = data.getvalue()
    rewritten = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(first)) as original, zipfile.ZipFile(rewritten, "w") as other:
        for name in original.namelist():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            other.writestr(info, original.read(name))
    _, hash1 = bundle([Asset("BRD.docx", first, "same")], Settings())
    _, hash2 = bundle([Asset("BRD.docx", rewritten.getvalue(), "same")], Settings())
    assert hash1 == hash2


def test_reference_content_change_triggers_revision():
    main = Asset("BRD.txt", b"FR-01: See reference", "main")
    _, before = bundle([main, Asset("ref.txt", b"Limit 10", "ref")], Settings())
    _, after = bundle([main, Asset("ref.txt", b"Limit 20", "ref")], Settings())
    assert before != after


def test_duplicate_filenames_are_disambiguated():
    docs, _ = bundle([Asset("BRD.txt", b"one", "a"), Asset("BRD.txt", b"two", "b")], Settings())
    assert len({d.source.name for d in docs}) == 2


def test_ole_reference_stream_is_materialized(monkeypatch):
    import struct

    payload = b"Reference requirement: maximum 10 rows"
    body = (
        struct.pack("<H", 2)
        + b"ref.txt\x00"
        + b"C:\\ref.txt\x00"
        + struct.pack("<II", 0, 0)
        + b"temp\x00"
        + struct.pack("<I", len(payload))
        + payload
    )
    native = struct.pack("<I", len(body)) + body

    class FakeOle:
        def listdir(self):
            return [["\x01Ole10Native"]]

        def openstream(self, name):
            return io.BytesIO(native)

        def close(self):
            pass

    monkeypatch.setattr("oletools.oleobj.find_ole", lambda *args: [FakeOle()])
    doc = Word()
    doc.add_paragraph("FR-01: See embedded reference")
    out = io.BytesIO()
    doc.save(out)
    with zipfile.ZipFile(out, "a") as z:
        z.writestr("word/embeddings/oleObject1.bin", b"mocked container")
    docs, _ = bundle([Asset("BRD.docx", out.getvalue(), "local")], Settings())
    assert len(docs) == 2
    assert docs[1].text == payload.decode()
