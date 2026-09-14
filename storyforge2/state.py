"""
storyforge2/state.py — idempotent, resumable pipeline state ledger.

Same design as the SQLite stage ledger proven working in this session's
earlier empire-os work (AutomationStudio): every stage attempt records
its inputs, outputs, provider, status, timestamps, and error, so a run
is retryable without duplicating completed work, and any single asset
can be regenerated without rebuilding the whole project.

One project = one book. Stages run in a fixed order (STAGES below) but
each stage's *history* is append-only — regenerating a stage creates a
new StageRun row (higher attempt_number) rather than overwriting the
old one, so nothing is silently lost.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Pipeline stage order, per the mission brief:
# brief -> outline -> manuscript -> illustrations -> book layout -> cover ->
# export -> storyboard -> narration -> master video -> shorts/commercials ->
# marketing package -> approval -> publish/export -> results log
STAGES: tuple[str, ...] = (
    "brief",
    "outline",
    "manuscript",
    "illustrations",
    "layout",
    "cover",
    "export",
    "storyboard",
    "narration",
    "master_video",
    "commercials",
    "marketing",
    "approval",
    "publish",
    "results",
)

STAGE_STATUSES = {"pending", "running", "completed", "failed", "skipped"}

# Approval gates a project can require before certain stages proceed.
# Matches the mission's 10-step user workflow (review outline, approve
# manuscript, approve book+cover, approve publication, ...).
APPROVAL_GATES = {"manuscript", "book_and_cover", "video_and_commercials", "publish"}


class StateError(ValueError):
    pass


@dataclass
class StageRun:
    id: str
    project_id: str
    stage_name: str
    attempt_number: int
    status: str
    provider: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: Optional[float] = None


@dataclass
class Project:
    id: str
    brief: dict[str, Any]
    status: str = "active"
    created_at: float = 0.0
    updated_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    brief TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_name TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    provider TEXT NOT NULL DEFAULT '',
    inputs TEXT NOT NULL DEFAULT '{}',
    outputs TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_stage_runs_project ON stage_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_stage_runs_project_stage ON stage_runs(project_id, stage_name);

CREATE TABLE IF NOT EXISTS approvals (
    project_id TEXT NOT NULL,
    gate TEXT NOT NULL,
    approved_at REAL NOT NULL,
    approved_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, gate)
);
"""


def _row_to_stage_run(row: sqlite3.Row) -> StageRun:
    return StageRun(
        id=row["id"], project_id=row["project_id"], stage_name=row["stage_name"],
        attempt_number=row["attempt_number"], status=row["status"], provider=row["provider"],
        inputs=json.loads(row["inputs"]), outputs=json.loads(row["outputs"]),
        error=row["error"], started_at=row["started_at"], finished_at=row["finished_at"],
    )


class StateStore:
    """SQLite-backed pipeline ledger. One DB file can hold many projects."""

    def __init__(self, db_path: str = "storyforge2_state.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True) if Path(db_path).parent != Path(".") else None
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- projects ---------------------------------------------------

    def create_project(self, brief: dict[str, Any], project_id: Optional[str] = None) -> str:
        pid = project_id or str(uuid.uuid4())
        now = time.time()
        # INSERT OR IGNORE makes this idempotent: reconstructing a BookPipeline
        # against a work_dir whose project row already exists (e.g. re-running
        # publish() on an already-approved book) must reuse that row, not crash.
        # Fixed 2026-09-13 after a real sqlite3.IntegrityError: UNIQUE constraint
        # failed: projects.id, root-caused to books/08e3e63d/pipeline_state.db
        # already containing this project's row from the original factory run
        # (created_at matched the db file's own ctime -- proof it predated this
        # session's tests, not a duplicate insert by the tests themselves).
        self._conn.execute(
            "INSERT OR IGNORE INTO projects (id, brief, status, created_at, updated_at) VALUES (?, ?, 'active', ?, ?)",
            (pid, json.dumps(brief), now, now),
        )
        self._conn.commit()
        return pid

    def get_project(self, project_id: str) -> Optional[Project]:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        return Project(id=row["id"], brief=json.loads(row["brief"]), status=row["status"],
                        created_at=row["created_at"], updated_at=row["updated_at"])

    def touch_project(self, project_id: str) -> None:
        self._conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (time.time(), project_id))
        self._conn.commit()

    # -- stages -------------------------------------------------------

    def start_stage(self, project_id: str, stage_name: str, inputs: Optional[dict[str, Any]] = None,
                     provider: str = "") -> StageRun:
        if stage_name not in STAGES:
            raise StateError(f"Unknown stage '{stage_name}'. Valid stages: {STAGES}")
        prior = self._conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) AS n FROM stage_runs WHERE project_id = ? AND stage_name = ?",
            (project_id, stage_name),
        ).fetchone()
        attempt = (prior["n"] or 0) + 1
        run_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            """INSERT INTO stage_runs (id, project_id, stage_name, attempt_number, status, provider, inputs, started_at)
               VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
            (run_id, project_id, stage_name, attempt, provider, json.dumps(inputs or {}), now),
        )
        self._conn.commit()
        self.touch_project(project_id)
        return StageRun(id=run_id, project_id=project_id, stage_name=stage_name, attempt_number=attempt,
                         status="running", provider=provider, inputs=inputs or {}, started_at=now)

    def complete_stage(self, run_id: str, outputs: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE stage_runs SET status = 'completed', outputs = ?, finished_at = ? WHERE id = ?",
            (json.dumps(outputs), time.time(), run_id),
        )
        self._conn.commit()

    def fail_stage(self, run_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE stage_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (error, time.time(), run_id),
        )
        self._conn.commit()

    def skip_stage(self, run_id: str, reason: str) -> None:
        self._conn.execute(
            "UPDATE stage_runs SET status = 'skipped', error = ?, finished_at = ? WHERE id = ?",
            (reason, time.time(), run_id),
        )
        self._conn.commit()

    def latest_stage_run(self, project_id: str, stage_name: str) -> Optional[StageRun]:
        row = self._conn.execute(
            """SELECT * FROM stage_runs WHERE project_id = ? AND stage_name = ?
               ORDER BY attempt_number DESC LIMIT 1""",
            (project_id, stage_name),
        ).fetchone()
        return _row_to_stage_run(row) if row else None

    def is_stage_complete(self, project_id: str, stage_name: str) -> bool:
        run = self.latest_stage_run(project_id, stage_name)
        return run is not None and run.status == "completed"

    def all_stage_runs(self, project_id: str) -> list[StageRun]:
        rows = self._conn.execute(
            "SELECT * FROM stage_runs WHERE project_id = ? ORDER BY started_at ASC", (project_id,)
        ).fetchall()
        return [_row_to_stage_run(r) for r in rows]

    def latest_stage_runs(self, project_id: str) -> dict[str, StageRun]:
        """One entry per stage_name — its most recent attempt, regardless
        of status. This is what a resumable pipeline run should check
        before deciding whether to (re)run each stage."""
        out: dict[str, StageRun] = {}
        for run in self.all_stage_runs(project_id):
            out[run.stage_name] = run  # later rows overwrite earlier -> latest wins
        return out

    # -- approvals ------------------------------------------------------

    def approve(self, project_id: str, gate: str, approved_by: str = "") -> None:
        if gate not in APPROVAL_GATES:
            raise StateError(f"Unknown approval gate '{gate}'. Valid gates: {APPROVAL_GATES}")
        self._conn.execute(
            "INSERT OR REPLACE INTO approvals (project_id, gate, approved_at, approved_by) VALUES (?, ?, ?, ?)",
            (project_id, gate, time.time(), approved_by),
        )
        self._conn.commit()

    def is_approved(self, project_id: str, gate: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM approvals WHERE project_id = ? AND gate = ?", (project_id, gate)
        ).fetchone()
        return row is not None

    def revoke_approval(self, project_id: str, gate: str) -> None:
        self._conn.execute("DELETE FROM approvals WHERE project_id = ? AND gate = ?", (project_id, gate))
        self._conn.commit()
