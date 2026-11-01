"""Explicit provider adapters. Never send connector credentials to arbitrary URLs."""

import io
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .config import Settings

SUPPORTED = {".doc", ".docx", ".pdf", ".txt", ".png", ".jpg", ".jpeg"}
DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass
class Asset:
    name: str
    data: bytes
    locator: str
    parent: str = ""


def drive_id(url: str) -> str:
    p = urlparse(url)
    if p.hostname not in {"drive.google.com", "docs.google.com"}:
        raise ValueError("Use a Google Drive or Google Docs URL")
    m = re.search(r"/(?:d|folders)/([\w-]+)", p.path)
    value = m.group(1) if m else parse_qs(p.query).get("id", [""])[0]
    if not re.fullmatch(r"[\w-]+", value):
        raise ValueError("Cannot find a Google Drive file/folder ID in that URL")
    return value


def checked(asset: Asset, settings: Settings):
    asset.name = Path(asset.name.replace("\\", "/")).name
    if len(asset.data) > settings.max_file_bytes:
        raise ValueError(f"{asset.name}: file exceeds {settings.max_file_bytes // 1024 // 1024} MB limit")
    if not asset.data:
        raise ValueError(f"{asset.name}: empty file")
    return asset


class GoogleDrive:
    def __init__(self, settings: Settings):
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not settings.google_credentials_file:
            raise ValueError("Google Drive is not configured. Set GOOGLE_CREDENTIALS_FILE on the server.")
        info = json.loads(Path(settings.google_credentials_file).read_text())
        scopes = ["https://www.googleapis.com/auth/drive"]
        if info.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        else:
            creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        self.api = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.settings = settings

    def metadata(self, fid):
        return (
            self.api.files()
            .get(
                fileId=fid,
                fields="id,name,mimeType,parents,modifiedTime,webViewLink,size",
                supportsAllDrives=True,
            )
            .execute()
        )

    def list(self, folder):
        results, token = [], None
        while True:
            page = (
                self.api.files()
                .list(
                    q=f"'{folder}' in parents and trashed=false",
                    pageSize=100,
                    pageToken=token,
                    fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            results.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                return results
            if len(results) > 1000:
                raise ValueError("Folder exceeds 1,000 files; use a dedicated BRD folder")

    def download(self, fid):
        from googleapiclient.http import MediaIoBaseDownload

        meta = self.metadata(fid)
        if meta["mimeType"] == FOLDER_MIME:
            raise ValueError("Select a document inside this folder first")
        if int(meta.get("size", 0)) > self.settings.max_file_bytes:
            raise ValueError("Drive document is above the configured size limit")
        name = meta["name"]
        if meta["mimeType"] == DOC_MIME:
            request = self.api.files().export_media(
                fileId=fid, mimeType="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            name += ".docx"
        else:
            request = self.api.files().get_media(fileId=fid, supportsAllDrives=True)
        buffer = io.BytesIO()
        download = MediaIoBaseDownload(buffer, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = download.next_chunk()
            if buffer.tell() > self.settings.max_file_bytes:
                raise ValueError("Drive document is above the configured size limit")
        return checked(
            Asset(
                name,
                buffer.getvalue(),
                f"https://drive.google.com/file/d/{fid}/view",
                (meta.get("parents") or [""])[0],
            ),
            self.settings,
        )

    def upload(self, folder, name, data):
        from googleapiclient.http import MediaIoBaseUpload

        if self.metadata(folder)["mimeType"] != FOLDER_MIME:
            raise ValueError("Output destination must be a Google Drive folder")
        mime = (
            "application/vnd.ms-excel"
            if name.endswith(".xls")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        result = (
            self.api.files()
            .create(
                body={"name": name, "parents": [folder]},
                media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=True),
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return result.get("webViewLink", f"https://drive.google.com/file/d/{result['id']}/view")


class Sources:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _dropbox(self, route, payload):
        if not self.settings.dropbox_access_token:
            raise ValueError("Dropbox is not configured. Set DROPBOX_ACCESS_TOKEN.")
        with httpx.Client(timeout=60) as client:
            r = client.post(
                "https://api.dropboxapi.com/2/" + route,
                headers={"Authorization": "Bearer " + self.settings.dropbox_access_token},
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    def list(self, url):
        host = urlparse(url).hostname
        if host in {"drive.google.com", "docs.google.com"}:
            drive = GoogleDrive(self.settings)
            fid = drive_id(url)
            meta = drive.metadata(fid)
            rows = drive.list(fid) if meta["mimeType"] == FOLDER_MIME else [meta]
            return [
                {
                    "name": r["name"],
                    "url": f"https://drive.google.com/file/d/{r['id']}/view",
                    "modified": r.get("modifiedTime", ""),
                }
                for r in rows
                if r["mimeType"] == DOC_MIME or Path(r["name"]).suffix.lower() in SUPPORTED
            ]
        if url.startswith("dropbox:/"):
            path = url[len("dropbox:") :].rstrip("/")
            page = self._dropbox("files/list_folder", {"path": path, "recursive": False})
            rows = page["entries"]
            while page.get("has_more"):
                page = self._dropbox("files/list_folder/continue", {"cursor": page["cursor"]})
                rows.extend(page["entries"])
                if len(rows) > 1000:
                    raise ValueError("Dropbox folder is too large; use a dedicated BRD folder")
            return [
                {
                    "name": r["name"],
                    "url": "dropbox:" + r["path_lower"],
                    "modified": r.get("server_modified", ""),
                }
                for r in rows
                if r[".tag"] == "file" and Path(r["name"]).suffix.lower() in SUPPORTED
            ]
        return [{"name": Path(urlparse(url).path).name or "Document", "url": url, "modified": ""}]

    def _stream(self, url, headers=None, auth=None, method="GET", content=None, jira_redirects=0):
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            with client.stream(method, url, headers=headers, auth=auth, content=content) as r:
                if r.is_redirect:
                    if jira_redirects:
                        target = urljoin(url, r.headers.get("location", ""))
                        p = urlparse(target)
                        host = p.hostname or ""
                        allowed = host == urlparse(self.settings.jira_base_url).hostname or host.endswith(
                            (".atlassian.net", ".atlassian.com", ".atlassian.io", ".amazonaws.com")
                        )
                        if (
                            not allowed
                            or p.scheme != "https"
                            or p.username
                            or p.password
                            or p.port not in (None, 443)
                        ):
                            raise ValueError(
                                "Jira attachment redirected outside approved HTTPS provider hosts"
                            )
                        if any(
                            not ipaddress.ip_address(a[4][0]).is_global for a in socket.getaddrinfo(host, 443)
                        ):
                            raise ValueError("Jira attachment redirected to a private address")
                        same_origin = p.netloc == urlparse(self.settings.jira_base_url).netloc
                        return self._stream(
                            target, auth=auth if same_origin else None, jira_redirects=jira_redirects - 1
                        )
                    raise ValueError(
                        "Download redirected. Use the final direct-download URL or provider connector."
                    )
                r.raise_for_status()
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > self.settings.max_file_bytes:
                        raise ValueError("Downloaded file exceeds the configured size limit")
                    chunks.append(chunk)
                return b"".join(chunks), dict(r.headers)

    def read(self, url):
        p = urlparse(url)
        if p.hostname in {"drive.google.com", "docs.google.com"}:
            return [GoogleDrive(self.settings).download(drive_id(url))]
        if url.startswith("dropbox:/") or p.hostname in {"www.dropbox.com", "dropbox.com"}:
            if not self.settings.dropbox_access_token:
                raise ValueError("Dropbox requires DROPBOX_ACCESS_TOKEN")
            args = {"path": url[len("dropbox:") :]} if url.startswith("dropbox:/") else {"url": url}
            route = "files/download" if "path" in args else "sharing/get_shared_link_file"
            data, headers = self._stream(
                "https://content.dropboxapi.com/2/" + route,
                {
                    "Authorization": "Bearer " + self.settings.dropbox_access_token,
                    "Dropbox-API-Arg": json.dumps(args),
                },
                method="POST",
            )
            meta = json.loads(headers.get("dropbox-api-result", "{}"))
            return [
                checked(
                    Asset(
                        meta.get("name", "document.docx"),
                        data,
                        url,
                        "dropbox:" + str(Path(meta["path_lower"]).parent) if meta.get("path_lower") else "",
                    ),
                    self.settings,
                )
            ]
        jira = self.settings.jira_base_url.rstrip("/")
        if jira and p.hostname == urlparse(jira).hostname and p.scheme == "https":
            return self._jira(url)
        allowed = {h.strip() for h in self.settings.allowed_download_hosts.split(",") if h.strip()}
        if (
            p.scheme != "https"
            or p.hostname not in allowed
            or p.username
            or p.password
            or p.port not in (None, 443)
        ):
            raise ValueError(
                "Unsupported link. Use Google Drive, Dropbox, configured Jira, or an HTTPS download host in ALLOWED_DOWNLOAD_HOSTS."
            )
        addresses = socket.getaddrinfo(p.hostname, 443)
        if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Private or local network download addresses are not allowed")
        data, _ = self._stream(url)
        return [checked(Asset(Path(p.path).name, data, url), self.settings)]

    def _jira(self, url):
        base = self.settings.jira_base_url.rstrip("/")
        m = re.search(r"/browse/([A-Za-z][A-Za-z0-9_]*-\d+)", urlparse(url).path)
        if not m:
            raise ValueError("Use a Jira issue URL ending in /browse/PROJECT-123")
        if not self.settings.jira_email or not self.settings.jira_api_token:
            raise ValueError("Configure JIRA_EMAIL and JIRA_API_TOKEN")
        auth = (self.settings.jira_email, self.settings.jira_api_token)
        with httpx.Client(timeout=60) as client:
            r = client.get(
                base + "/rest/api/3/issue/" + m.group(1),
                params={"fields": "summary,description,attachment"},
                auth=auth,
            )
            r.raise_for_status()
            fields = r.json()["fields"]

        def plain(node):
            if isinstance(node, str):
                return node
            if isinstance(node, dict):
                return node.get("text", "") + "\n".join(plain(x) for x in node.get("content", []))
            return ""

        assets = [
            Asset(
                m.group(1) + ".txt",
                (fields["summary"] + "\n" + plain(fields.get("description"))).encode(),
                url,
                url,
            )
        ]
        for item in fields.get("attachment", []):
            if len(assets) >= self.settings.max_documents:
                raise ValueError("Jira issue has too many attachments; narrow the scope")
            if Path(item["filename"]).suffix.lower() not in SUPPORTED:
                raise ValueError(
                    f"Unsupported Jira attachment: {item['filename']}. Convert or remove it before generation."
                )
            # Auth is only sent to the configured Jira origin. Signed CDN redirects carry no credentials.
            target = item["content"]
            if urlparse(target).netloc != urlparse(base).netloc or urlparse(target).scheme != "https":
                raise ValueError("Jira attachment URL is outside the configured Jira origin")
            data, _ = self._stream(target, auth=auth, jira_redirects=3)
            assets.append(checked(Asset(item["filename"], data, target, url), self.settings))
        return assets

    def publish(self, destination, name, data):
        if urlparse(destination).hostname in {"drive.google.com", "docs.google.com"}:
            return GoogleDrive(self.settings).upload(drive_id(destination), name, data)
        if destination.startswith("dropbox:/"):
            if not self.settings.dropbox_access_token:
                raise ValueError("Configure DROPBOX_ACCESS_TOKEN")
            path = destination[len("dropbox:") :].rstrip("/") + "/" + name
            with httpx.Client(timeout=90) as client:
                r = client.post(
                    "https://content.dropboxapi.com/2/files/upload",
                    content=data,
                    headers={
                        "Authorization": "Bearer " + self.settings.dropbox_access_token,
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": json.dumps({"path": path, "mode": "add", "autorename": True}),
                    },
                )
                r.raise_for_status()
                return "dropbox:" + r.json()["path_display"]
        jira = self.settings.jira_base_url.rstrip("/")
        if jira and urlparse(destination).netloc == urlparse(jira).netloc:
            m = re.fullmatch(r"/browse/([A-Za-z][A-Za-z0-9_]*-\d+)", urlparse(destination).path)
            if not m:
                raise ValueError("Use a Jira issue URL as the destination")
            with httpx.Client(timeout=90) as client:
                r = client.post(
                    jira + "/rest/api/3/issue/" + m.group(1) + "/attachments",
                    auth=(self.settings.jira_email, self.settings.jira_api_token),
                    headers={"X-Atlassian-Token": "no-check"},
                    files={"file": (name, data, "application/vnd.ms-excel")},
                )
                r.raise_for_status()
                return destination
        raise ValueError(
            "Output requires a Google Drive folder URL, dropbox:/folder path, or configured Jira issue URL"
        )
