import asyncio
import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .generation import regression
from .service import Service, friendly
from .store import Store
from .workbooks import export, import_results


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_url: str = ""
    source_pattern: str = "*"
    destination: str = ""
    format: str = "xls"
    follow_links: bool = False
    auto_publish: bool = False
    watch: bool = False
    execution_url: str = ""


class LinkInput(BaseModel):
    url: str


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings.data_dir)
    service = Service(settings, store)
    tasks = set()
    # A per-process CSRF header prevents web pages from driving an unauthenticated localhost service.
    csrf = secrets.token_urlsafe(32)

    def submit(project_id, force=False):
        job_id = store.new_job(project_id)
        task = asyncio.create_task(asyncio.to_thread(service.analyze, project_id, job_id, force))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return job_id

    async def monitor():
        while True:
            await asyncio.sleep(max(settings.poll_seconds, 30))
            for p in store.projects():
                if not p["config"].get("watch") or store.active(p["id"]):
                    continue
                try:
                    # Import results before source refresh; stale workbooks are rejected after a revision.
                    if p["config"].get("execution_url") and store.latest(p["id"]):
                        changed = await asyncio.to_thread(service.sync_execution, p["id"])
                        if changed and any(c.status == "Fail" for c in store.latest(p["id"]).cases):
                            if p["config"].get("auto_publish"):
                                await asyncio.to_thread(service.publish, p["id"], "Regression")
                    if p["config"].get("source_url"):
                        submit(p["id"])
                except Exception as exc:
                    logging.exception("Monitor failed for project %s", p["id"])
                    # Record visible failures without blocking future source updates on a stale execution file.
                    jid = store.new_job(p["id"])
                    store.job_update(jid, "failed", "Monitor: " + friendly(exc))
                    if p["config"].get("source_url"):
                        submit(p["id"])

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(monitor()) if settings.poll_seconds > 0 else None
        yield
        if task:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Spec to Test", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts.split(","))
    app.state.store, app.state.service = store, service

    @app.middleware("http")
    async def access(request: Request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > settings.max_bundle_bytes + 1024 * 1024:
            return JSONResponse({"detail": "Request exceeds the total upload limit"}, status_code=413)
        if request.url.path.startswith("/api/"):
            if settings.app_token and not hmac.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + settings.app_token
            ):
                return JSONResponse({"detail": "Enter the configured app access token"}, status_code=401)
            if request.method not in {"GET", "HEAD"} and request.headers.get("x-csrf-token") != csrf:
                return JSONResponse({"detail": "Refresh the application before submitting"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def other_error(request, exc):
        logging.exception("Request failed")
        return JSONResponse({"detail": friendly(exc)}, status_code=500)

    @app.get("/api/config")
    def config():
        return {
            "csrf": csrf,
            "model": settings.openai_model,
            "ai_ready": bool(settings.openai_api_key),
            "google_ready": bool(settings.google_credentials_file),
            "dropbox_ready": bool(settings.dropbox_access_token),
            "jira_ready": bool(settings.jira_base_url and settings.jira_api_token and settings.jira_email),
            "poll_seconds": settings.poll_seconds,
            "max_file_mb": settings.max_file_bytes // 1024 // 1024,
        }

    @app.get("/api/projects")
    def projects():
        result = []
        for p in store.projects():
            suite = store.latest(p["id"])
            with store.connect() as db:
                jobs = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 5", (p["id"],)
                    )
                ]
            result.append(
                p
                | {
                    "version": suite.version if suite else 0,
                    "case_count": len(suite.cases) if suite else 0,
                    "jobs": jobs,
                }
            )
        return result

    @app.post("/api/projects")
    def create(data: ProjectInput):
        if data.format not in {"xls", "xlsx"}:
            raise ValueError("Choose XLS or XLSX")
        return store.create_project(data.name, data.model_dump(exclude={"name"}))

    @app.put("/api/projects/{pid}")
    def update(pid: str, data: ProjectInput):
        if store.active(pid):
            raise ValueError("Wait for the current job before editing the project")
        if data.format not in {"xls", "xlsx"}:
            raise ValueError("Choose XLS or XLSX")
        old = store.project(pid)
        config = old["config"] | data.model_dump(exclude={"name"})
        store.configure(pid, config, data.name)
        return store.project(pid)

    @app.post("/api/sources/list")
    def list_sources(data: LinkInput):
        return service.sources.list(data.url)

    async def bytes_limited(file):
        value = await file.read(settings.max_file_bytes + 1)
        if len(value) > settings.max_file_bytes:
            raise ValueError("Upload exceeds file-size limit")
        if not value:
            raise ValueError("Uploaded file is empty")
        return value

    @app.post("/api/projects/{pid}/uploads")
    async def uploads(pid: str, files: list[UploadFile] = File(...), replace: bool = Form(True)):
        if store.active(pid):
            raise ValueError("Wait for the active job before replacing sources")
        p = store.project(pid)
        if len(files) > settings.max_documents:
            raise ValueError("Too many uploaded documents")
        entries = []
        root = store.root / "uploads" / pid
        root.mkdir(parents=True, exist_ok=True)
        for file in files:
            name = Path((file.filename or "document").replace("\\", "/")).name
            if Path(name).suffix.lower() not in {".doc", ".docx", ".pdf", ".txt", ".png", ".jpg", ".jpeg"}:
                raise ValueError("Unsupported upload type")
            data = await bytes_limited(file)
            path = root / (hashlib.sha256(data).hexdigest() + Path(name).suffix.lower())
            path.write_bytes(data)
            entries.append({"name": name, "path": str(path.relative_to(store.root))})
        p["config"]["uploads"] = entries if replace else p["config"].get("uploads", []) + entries
        store.configure(pid, p["config"])
        return {"files": entries}

    @app.post("/api/projects/{pid}/generate")
    async def generate(pid: str):
        return {"job_id": submit(pid)}

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        return store.job(jid)

    @app.get("/api/projects/{pid}/suite")
    def suite(pid: str):
        result = store.latest(pid)
        if not result:
            raise HTTPException(404, "No generated workbook yet")
        return result.model_dump()

    @app.get("/api/projects/{pid}/download")
    def download(pid: str, format: str = "xls", kind: str = "Full"):
        if kind not in {"Full", "Regression"}:
            raise ValueError("Invalid workbook kind")
        suite = store.latest(pid)
        if not suite:
            raise ValueError("Generate test cases first")
        if kind == "Regression":
            suite = regression(suite)
        data = export(suite, format)
        mime = (
            "application/vnd.ms-excel"
            if format == "xls"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return Response(
            data,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{kind}_v{suite.version}.{format}"'},
        )

    @app.post("/api/projects/{pid}/publish")
    def publish(pid: str, kind: str = "Full"):
        if kind not in {"Full", "Regression"}:
            raise ValueError("Invalid workbook kind")
        if store.active(pid):
            raise ValueError("Wait for the current generation before publishing")
        return {"url": service.publish(pid, kind)}

    @app.post("/api/projects/{pid}/execution")
    async def execution(pid: str, file: UploadFile = File(...)):
        if store.active(pid):
            raise ValueError("Wait for generation before importing execution results")
        suite = store.latest(pid)
        if not suite:
            raise ValueError("Generate test cases first")
        updated = import_results(suite, await bytes_limited(file), file.filename or "")
        store.save(updated)
        return {
            "failed": sum(c.status == "Fail" for c in updated.cases),
            "message": "Results imported. Regression workbook is ready if failures exist.",
        }

    @app.post("/api/projects/{pid}/sync-execution")
    def sync_execution(pid: str):
        if store.active(pid):
            raise ValueError("Wait for generation before syncing execution")
        return {"changed": service.sync_execution(pid)}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
