"""Extract text, visual evidence, embedded files and references without executing content."""

import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import pymupdf as fitz
from PIL import Image

from .config import Settings
from .connectors import Asset, checked
from .models import SourceRecord


@dataclass
class Document:
    source: SourceRecord
    text: str
    images: list[tuple[str, bytes]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    attachments: list[Asset] = field(default_factory=list)


def png(data):
    with Image.open(io.BytesIO(data)) as im:
        im.thumbnail((1600, 1600))
        out = io.BytesIO()
        im.convert("RGB").save(out, "PNG")
        return out.getvalue()


def extract(asset: Asset, settings: Settings) -> Document:
    checked(asset, settings)
    suffix = Path(asset.name).suffix.lower()
    record = SourceRecord(
        name=asset.name, locator=asset.locator, sha256=hashlib.sha256(asset.data).hexdigest()
    )
    doc = Document(record, "")
    if suffix == ".pdf":
        if not asset.data.startswith(b"%PDF"):
            raise ValueError(f"{asset.name}: file is not a valid PDF")
        with fitz.open(stream=asset.data, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise ValueError("Password-protected PDFs must be unlocked before upload")
            if len(pdf) > settings.max_pages:
                raise ValueError(
                    f"PDF exceeds the {settings.max_pages}-page limit; split it into smaller BRDs"
                )
            texts = []
            for n, page in enumerate(pdf):
                texts.append(f"[Page {n + 1}]\n" + page.get_text(sort=True))
                # Render every page: includes scans, vector diagrams and pasted screenshots.
                scale = min(1.5, 1600 / max(page.rect.width, page.rect.height))
                doc.images.append(
                    (f"Page {n + 1}", page.get_pixmap(matrix=fitz.Matrix(scale, scale)).tobytes("png"))
                )
                doc.links.extend(
                    x["uri"] for x in page.get_links() if x.get("uri", "").startswith("https://")
                )
            doc.text = "\n".join(texts)
            for name in pdf.embfile_names():
                info = pdf.embfile_info(name)
                doc.attachments.append(
                    Asset(
                        info.get("filename") or name,
                        pdf.embfile_get(name),
                        asset.locator + "#attachment=" + name,
                    )
                )
    elif suffix == ".docx":
        if not zipfile.is_zipfile(io.BytesIO(asset.data)):
            raise ValueError(f"{asset.name}: file is not a valid DOCX")
        with zipfile.ZipFile(io.BytesIO(asset.data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > settings.max_bundle_bytes:
                raise ValueError("DOCX expanded contents exceed the bundle limit")
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text_parts = []
            parts = [
                n
                for n in archive.namelist()
                if n == "word/document.xml"
                or re.fullmatch(r"word/(header\d+|footer\d+|footnotes|endnotes).xml", n)
            ]
            for part in parts:
                root = ET.fromstring(archive.read(part))
                for n, para in enumerate(root.findall(".//w:p", ns), 1):
                    text = "".join(x.text or "" for x in para.findall(".//w:t", ns))
                    if text.strip():
                        text_parts.append(f"[{Path(part).stem} paragraph {n}] {text}")
            doc.text = "\n".join(text_parts)
            for name in archive.namelist():
                if name.startswith("word/media/"):
                    try:
                        doc.images.append((name, png(archive.read(name))))
                    except Exception:
                        record.notes.append(
                            f"Unresolved image {name}: unsupported format; provide PNG/PDF reference"
                        )
                if name.endswith(".rels"):
                    root = ET.fromstring(archive.read(name))
                    doc.links.extend(
                        r.attrib["Target"]
                        for r in root
                        if r.attrib.get("TargetMode") == "External"
                        and r.attrib.get("Target", "").startswith("https://")
                    )
                if name.startswith("word/embeddings/"):
                    data = archive.read(name)
                    if name.endswith(".bin"):
                        try:
                            from oletools.oleobj import OleNativeStream, find_ole

                            found = False
                            for ole in find_ole(name, data):
                                if ole is None:
                                    continue
                                try:
                                    for stream in ole.listdir():
                                        if stream[-1] == "\x01Ole10Native":
                                            native = OleNativeStream(ole.openstream(stream))
                                            if not native.is_link:
                                                if native.actual_size > settings.max_file_bytes:
                                                    raise ValueError("Embedded reference exceeds file limit")
                                                payload = (
                                                    native.data.read(native.actual_size)
                                                    if native.data_is_stream
                                                    else native.data
                                                )
                                                doc.attachments.append(
                                                    Asset(
                                                        native.filename,
                                                        payload,
                                                        asset.locator + "#" + name,
                                                    )
                                                )
                                                found = True
                                finally:
                                    ole.close()
                            if not found:
                                record.notes.append(
                                    f"Unresolved embedded object {name}: upload the original reference separately"
                                )
                        except Exception:
                            record.notes.append(
                                f"Unresolved embedded object {name}: upload the original reference separately"
                            )
                    else:
                        doc.attachments.append(Asset(Path(name).name, data, asset.locator + "#" + name))
    elif suffix == ".doc":
        binary = shutil.which("libreoffice") or shutil.which("soffice")
        if not binary:
            mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
            binary = str(mac) if mac.exists() else None
        if not binary:
            raise ValueError(
                "Legacy .doc requires LibreOffice (included in Docker). Alternatively save as .docx."
            )
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "input.doc"
            source.write_bytes(asset.data)
            subprocess.run(
                [
                    binary,
                    "-env:UserInstallation=" + (Path(temp) / "profile").as_uri(),
                    "--headless",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    temp,
                    str(source),
                ],
                check=True,
                timeout=90,
                capture_output=True,
            )
            converted = Path(temp) / "input.docx"
            if not converted.exists():
                raise ValueError("LibreOffice could not convert the .doc file")
            doc = extract(Asset("converted.docx", converted.read_bytes(), asset.locator), settings)
            record.notes.extend(doc.source.notes)
            record.notes.append("Legacy DOC converted with LibreOffice; review conversion fidelity")
            doc.source = record
    elif suffix in {".png", ".jpg", ".jpeg"}:
        doc.images.append(("Image 1", png(asset.data)))
        doc.text = "Reference image; inspect visual content."
    elif suffix == ".txt":
        doc.text = asset.data.decode("utf-8-sig")
    else:
        raise ValueError(f"Unsupported reference {asset.name}; supported: DOC, DOCX, PDF, TXT, PNG, JPG")
    doc.links = sorted(set(doc.links + re.findall(r'https://[^\s<>"\)]+', doc.text)))
    if not doc.text.strip() and not doc.images:
        raise ValueError(f"No readable content in {asset.name}")
    return doc


def bundle(assets: list[Asset], settings: Settings, read_link=None, follow_links=False):
    documents, seen, pending = [], set(), list(assets)
    fetched_links, used_names = set(), set()
    total = 0
    while pending:
        asset = pending.pop(0)
        fingerprint = hashlib.sha256(asset.data).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        total += len(asset.data)
        if len(documents) >= settings.max_documents or total > settings.max_bundle_bytes:
            raise ValueError("Reference bundle exceeds the configured document or total-size limit")
        if asset.name in used_names:
            stem, suffix = Path(asset.name).stem, Path(asset.name).suffix
            asset.name = f"{stem}-{fingerprint[:8]}{suffix}"
        used_names.add(asset.name)
        doc = extract(asset, settings)
        documents.append(doc)
        pending.extend(doc.attachments)
        for link in doc.links:
            # Only explicit provider links or known document suffixes are references.
            from urllib.parse import urlparse

            p = urlparse(link)
            is_ref = p.hostname in {
                "drive.google.com",
                "docs.google.com",
                "dropbox.com",
                "www.dropbox.com",
            } or Path(p.path).suffix.lower() in {".pdf", ".doc", ".docx"}
            if not is_ref:
                continue
            if follow_links and read_link and link not in fetched_links:
                fetched_links.add(link)
                try:
                    pending.extend(read_link(link))
                except Exception:
                    doc.source.notes.append(
                        f"Unresolved reference: {link}. Check access or upload the reference separately."
                    )
            elif not follow_links:
                doc.source.notes.append(
                    f"Reference not fetched: {link}. Enable linked references or upload it separately."
                )
    if sum(len(d.text) for d in documents) > settings.max_text_chars:
        raise ValueError(
            "Combined document text exceeds the analysis limit; split the BRD, no text was truncated"
        )
    if sum(len(d.images) for d in documents) > settings.max_images:
        raise ValueError(
            "Combined visual pages/images exceed the analysis limit; split the BRD, no images were omitted"
        )
    # Content hash includes reference contents and unresolved-reference state, not export ZIP timestamps.
    canonical = json.dumps(
        [
            {
                "name": d.source.name,
                "locator": d.source.locator,
                "text": d.text,
                "images": [(label, hashlib.sha256(data).hexdigest()) for label, data in d.images],
                "notes": d.source.notes,
            }
            for d in documents
        ],
        sort_keys=True,
    )
    return documents, hashlib.sha256(canonical.encode()).hexdigest()
