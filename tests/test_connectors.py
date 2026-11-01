import httpx
import pytest

from testcase_agent.config import Settings
from testcase_agent.connectors import Asset, Sources, drive_id
from testcase_agent.service import Service
from testcase_agent.store import Store


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.google.com/document/d/abc-123/edit",
        "https://drive.google.com/drive/u/3/folders/abc-123",
        "https://drive.google.com/open?id=abc-123",
    ],
)
def test_drive_url_parser(url):
    assert drive_id(url) == "abc-123"


def test_drive_lookalike_rejected():
    with pytest.raises(ValueError):
        drive_id("https://drive.google.com.evil.test/file/d/id/view")


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/private.pdf", "https://unconfigured.test/a.pdf", "file:///etc/passwd"]
)
def test_unapproved_sources_rejected(url):
    with pytest.raises(ValueError, match="Unsupported"):
        Sources(Settings()).read(url)


def test_private_ip_rejected(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="Private"):
        Sources(Settings(allowed_download_hosts="files.test")).read("https://files.test/brd.pdf")


def test_jira_redirect_does_not_leak_credentials(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "team.atlassian.net":
            return httpx.Response(303, headers={"location": "https://api.media.atlassian.com/file/signed"})
        return httpx.Response(200, content=b"PDF bytes")

    original = httpx.Client
    monkeypatch.setattr(
        "httpx.Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: [(2, 1, 6, "", ("8.8.8.8", 443))])
    source = Sources(Settings(jira_base_url="https://team.atlassian.net"))
    data, _ = source._stream(
        "https://team.atlassian.net/attachment", auth=("user", "secret"), jira_redirects=3
    )
    assert data == b"PDF bytes"
    assert "authorization" in requests[0].headers
    assert "authorization" not in requests[1].headers


def test_newest_matching_brd_selected(tmp_path, monkeypatch):
    store = Store(tmp_path)
    p = store.create_project(
        "Test", {"source_url": "https://drive.google.com/drive/folders/id", "source_pattern": "BRD_*.docx"}
    )
    svc = Service(Settings(data_dir=tmp_path), store)
    monkeypatch.setattr(
        svc.sources,
        "list",
        lambda url: [
            {"name": "BRD_v1.docx", "modified": "2026-01-01", "url": "old"},
            {"name": "reference.pdf", "modified": "2026-03-01", "url": "reference"},
            {"name": "BRD_v2.docx", "modified": "2026-02-01", "url": "new"},
        ],
    )
    monkeypatch.setattr(svc.sources, "read", lambda url: [Asset(url, b"data", url)])
    assert svc.assets(p)[0].locator == "new"


def test_duplicate_job_is_rejected(tmp_path):
    store = Store(tmp_path)
    p = store.create_project("Test", {})
    store.new_job(p["id"])
    with pytest.raises(ValueError, match="active job"):
        store.new_job(p["id"])
