import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Suite


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "agent.sqlite3"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, config TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS suites(project_id TEXT, version INTEGER, payload TEXT,
                PRIMARY KEY(project_id,version));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT, status TEXT,
                message TEXT, created TEXT, updated TEXT);
            """)
            db.execute(
                "UPDATE jobs SET status='failed', message='Server restarted; retry this job' WHERE status IN ('queued','running')"
            )

    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def projects(self):
        with self.connect() as db:
            return [
                dict(r) | {"config": json.loads(r["config"])}
                for r in db.execute("SELECT * FROM projects ORDER BY updated DESC")
            ]

    def project(self, project_id):
        with self.connect() as db:
            r = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not r:
            raise ValueError("Project not found")
        return dict(r) | {"config": json.loads(r["config"])}

    def create_project(self, name, config):
        project_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES (?,?,?,?)", (project_id, name, json.dumps(config), now()))
        return self.project(project_id)

    def configure(self, project_id, config, name=None):
        self.project(project_id)
        with self.connect() as db:
            db.execute(
                "UPDATE projects SET config=?,updated=? WHERE id=?", (json.dumps(config), now(), project_id)
            )
            if name is not None:
                db.execute("UPDATE projects SET name=? WHERE id=?", (name, project_id))

    def latest(self, project_id):
        with self.connect() as db:
            r = db.execute(
                "SELECT payload FROM suites WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,)
            ).fetchone()
        return Suite.model_validate_json(r[0]) if r else None

    def save(self, suite: Suite):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO suites VALUES (?,?,?)",
                (suite.project_id, suite.version, suite.model_dump_json()),
            )
            db.execute("UPDATE projects SET updated=? WHERE id=?", (now(), suite.project_id))

    def job(self, job_id):
        with self.connect() as db:
            r = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            raise ValueError("Job not found")
        return dict(r)

    def new_job(self, project_id):
        self.project(project_id)
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running')", (project_id,)
            ).fetchone():
                raise ValueError("This project already has an active job")
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                (job_id, project_id, "queued", "Queued", now(), now()),
            )
        return job_id

    def job_update(self, job_id, status, message):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?, message=?, updated=? WHERE id=?", (status, message, now(), job_id)
            )

    def active(self, project_id):
        with self.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running')", (project_id,)
                ).fetchone()
            )
