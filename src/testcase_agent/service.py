import fnmatch
import hashlib
import logging
from pathlib import Path

from .config import Settings
from .connectors import Asset, Sources
from .documents import bundle
from .generation import Generator, make_suite, regression
from .store import Store, now
from .workbooks import export, import_results

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.sources = Sources(settings)

    def assets(self, project):
        config = project["config"]
        assets = []
        if config.get("source_url"):
            url = config["source_url"]
            if "/folders/" in url or (url.startswith("dropbox:/") and not Path(url).suffix):
                options = self.sources.list(url)
                pattern = config.get("source_pattern") or "*"
                options = [o for o in options if fnmatch.fnmatchcase(o["name"].lower(), pattern.lower())]
                if not options:
                    raise ValueError("No BRD matches the folder filename pattern")
                options.sort(key=lambda o: (o["modified"], o["name"], o["url"]), reverse=True)
                url = options[0]["url"]
            assets.extend(self.sources.read(url))
        for item in config.get("uploads", []):
            assets.append(
                Asset(item["name"], (self.store.root / item["path"]).read_bytes(), "upload:" + item["name"])
            )
        if not assets:
            raise ValueError("Upload a BRD or provide a source URL")
        return assets

    def analyze(self, project_id, job_id, force=False):
        self.store.job_update(job_id, "running", "Reading documents and references")
        try:
            project = self.store.project(project_id)
            config = project["config"]
            docs, digest = bundle(
                self.assets(project), self.settings, self.sources.read, config.get("follow_links", False)
            )
            prior = self.store.latest(project_id)
            if prior and digest == prior.bundle_hash and not force:
                self.store.job_update(job_id, "succeeded", "No document or reference changes detected")
                return
            self.store.job_update(
                job_id, "running", f"Analyzing {len(docs)} sources and generating traceable cases"
            )
            suite = make_suite(project_id, docs, digest, Generator(self.settings), prior)
            # Validate Excel capacity before committing a new version.
            export(suite, config.get("format", "xls"))
            self.store.save(suite)
            message = (
                f"Version {suite.version}: {len(suite.cases)} cases, {len(suite.requirements)} requirements"
            )
            if config.get("auto_publish"):
                try:
                    link = self.publish(project_id)
                    message += "; published to " + link
                except Exception as exc:
                    log.exception("Automatic workbook publish failed")
                    message += "; workbook saved locally, publication failed: " + friendly(exc)
            self.store.job_update(job_id, "succeeded", message)
        except Exception as exc:
            log.exception("Generation failed")
            self.store.job_update(job_id, "failed", friendly(exc))

    def destination(self, project):
        config = project["config"]
        if config.get("destination"):
            return config["destination"]
        url = config.get("source_url", "")
        if "/folders/" in url or (url.startswith("dropbox:/") and not Path(url).suffix):
            return url
        assets = self.sources.read(url) if url else []
        parent = assets[0].parent if assets else ""
        if parent.startswith("dropbox:") or parent.startswith("https://"):
            return parent
        if parent:
            return "https://drive.google.com/drive/folders/" + parent
        raise ValueError("Choose an output folder for uploaded or direct-download documents")

    def publish(self, project_id, kind="Full"):
        project = self.store.project(project_id)
        suite = self.store.latest(project_id)
        if not suite:
            raise ValueError("Generate test cases first")
        if kind == "Regression":
            suite = regression(suite)
        fmt = project["config"].get("format", "xls")
        # Unique export snapshots avoid overwriting a tester's execution workbook.
        name = f"{kind.replace('Full', 'TestCases')}_{project_id[:8]}_v{suite.version}_{now().replace(':', '').replace('.', '')}.{fmt}"
        return self.sources.publish(self.destination(project), name, export(suite, fmt))

    def sync_execution(self, project_id):
        project = self.store.project(project_id)
        url = project["config"].get("execution_url")
        if not url:
            raise ValueError("Configure an execution workbook URL first")
        assets = self.sources.read(url)
        if len(assets) != 1:
            raise ValueError("Execution URL must reference one XLS/XLSX file")
        asset = assets[0]
        digest = hashlib.sha256(asset.data).hexdigest()
        if project["config"].get("execution_hash") == digest:
            return False
        suite = self.store.latest(project_id)
        if not suite:
            raise ValueError("Generate test cases before importing execution")
        updated = import_results(suite, asset.data, asset.name)
        self.store.save(updated)
        project["config"]["execution_hash"] = digest
        self.store.configure(project_id, project["config"])
        return True


def friendly(exc):
    # Never expose provider response bodies, bearer tokens or local credential file contents in UI.
    if isinstance(exc, ValueError):
        return str(exc)[:800]
    return f"{type(exc).__name__}: operation failed. Check server logs and connector configuration; previous results are retained."
