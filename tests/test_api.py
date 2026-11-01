import time

from conftest import FakeGenerator
from fastapi.testclient import TestClient

from testcase_agent.app import create_app
from testcase_agent.config import Settings


def test_complete_upload_generate_download_and_no_change(tmp_path, monkeypatch):
    monkeypatch.setattr("testcase_agent.service.Generator", lambda settings: FakeGenerator())
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        headers = {"X-CSRF-Token": cfg["csrf"]}
        assert client.post("/api/projects", json={"name": "test"}).status_code == 403
        p = client.post("/api/projects", json={"name": "Azure test"}, headers=headers).json()
        pid = p["id"]
        r = client.post(
            f"/api/projects/{pid}/uploads",
            files=[("files", ("BRD.txt", b"FR-01: Active only", "text/plain"))],
            headers=headers,
        )
        assert r.status_code == 200
        for expected in ["Version 1", "No document"]:
            r = client.post(f"/api/projects/{pid}/generate", headers=headers)
            assert r.status_code == 200
            jid = r.json()["job_id"]
            for _ in range(100):
                job = client.get("/api/jobs/" + jid).json()
                if job["status"] in ("failed", "succeeded"):
                    break
                time.sleep(0.02)
            assert job["status"] == "succeeded", job
            assert expected in job["message"]
        suite = client.get(f"/api/projects/{pid}/suite").json()
        assert len(suite["cases"]) == 4
        r = client.get(f"/api/projects/{pid}/download")
        assert r.status_code == 200 and r.content.startswith(b"\xd0\xcf")
        r = client.post(
            f"/api/projects/{pid}/execution", files={"file": ("execution.xls", r.content)}, headers=headers
        )
        assert r.status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200


def test_api_authentication(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, app_token="secret"))) as client:
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/config", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_failed_generation_keeps_previous(tmp_path, monkeypatch):
    from testcase_agent.generation import make_suite
    from testcase_agent.service import Service
    from testcase_agent.store import Store

    store = Store(tmp_path)
    p = store.create_project("Example", {})
    prior = make_suite(p["id"], [], "old", FakeGenerator())
    store.save(prior)
    jid = store.new_job(p["id"])
    Service(Settings(data_dir=tmp_path), store).analyze(p["id"], jid)
    assert store.job(jid)["status"] == "failed"
    assert store.latest(p["id"]).bundle_hash == "old"
