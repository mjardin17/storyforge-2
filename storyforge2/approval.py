"""
storyforge2/approval.py — human approval gate for live book publishing.

Mirrors the safety philosophy already established by the video pipeline's
council/roles/publisher.py and qa_engineer.py ("Validates upload readiness —
never uploads (Josh approves manually)") — NOT their class hierarchy. Kept
standalone (no import of the `council` package) on purpose: council/ is
video-pipeline-specific (channels, renders_dir, token files) and coupling
storyforge2 to it would be the wrong kind of reuse — same principle, decoupled
implementation, no duplicate video-shaped state.

Why this exists: BookFactory.run_cycle(dry_run=False) previously called
pipeline.publish(..., dry_run=False) unconditionally whenever the caller
passed --live — including from the "Book Factory Daily Run" scheduled task,
which runs unattended, on a cron, with no human in the loop. factory.py's own
docstring claimed "queue for publishing (manual approval in MVP)" but no such
gate existed. This module is that gate, enforced in factory.py regardless of
the --live flag: a cycle can only reach a REAL platform publish call after an
explicit, separate approve() for that exact cycle_id.

Storage: books/approvals.json — {cycle_id: {"approved_at": iso, "note": str}}.
Plain JSON, not the sqlite ledger, so Josh (or a future review UI) can read/
edit it without touching factory_state.db.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

__all__ = ["ApprovalGate"]


@dataclass
class ApprovalRecord:
    cycle_id: str
    approved_at: str
    note: str = ""


class ApprovalGate:
    """Tracks which book cycles Josh has explicitly cleared for live publishing.

    Nothing in this class ever calls a publishing API — it only answers
    "is_approved(cycle_id)". factory.py is what refuses to actually publish
    when the answer is False, no matter what dry_run/--live it was given.
    """

    def __init__(self, work_base: Path | str = "books"):
        self.work_base = Path(work_base)
        self.work_base.mkdir(parents=True, exist_ok=True)
        self.approvals_path = self.work_base / "approvals.json"

    def _load(self) -> dict[str, dict]:
        if not self.approvals_path.exists():
            return {}
        try:
            return json.loads(self.approvals_path.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt/partial approvals file must fail CLOSED (nothing
            # approved), never open — the failure mode of "can't parse the
            # approvals ledger" must not accidentally mean "publish anyway".
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        self.approvals_path.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )

    def is_approved(self, cycle_id: str) -> bool:
        return cycle_id in self._load()

    def approve(self, cycle_id: str, note: str = "") -> ApprovalRecord:
        data = self._load()
        record = ApprovalRecord(
            cycle_id=cycle_id,
            approved_at=datetime.utcnow().isoformat(),
            note=note,
        )
        data[cycle_id] = {"approved_at": record.approved_at, "note": record.note}
        self._save(data)
        return record

    def revoke(self, cycle_id: str) -> bool:
        data = self._load()
        if cycle_id in data:
            del data[cycle_id]
            self._save(data)
            return True
        return False

    def list_approved(self) -> list[str]:
        return sorted(self._load().keys())


def _list_pending(work_base: Path) -> list[tuple[str, str, str]]:
    """Cycles sitting at ready_publish/manuscript/metadata that aren't
    approved yet — read straight from factory_state.db so this stays in
    sync with BookFactory without importing it (avoids a circular import,
    since factory.py imports this module)."""
    import sqlite3

    db_path = work_base / "factory_state.db"
    if not db_path.exists():
        return []
    gate = ApprovalGate(work_base)
    approved = set(gate.list_approved())
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT cycle_id, title, status FROM cycles "
            "WHERE status IN ('ready_publish', 'manuscript', 'metadata') "
            "ORDER BY started_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [(cid, title, status) for cid, title, status in rows if cid not in approved]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Approve (or review) Book Factory cycles before they can "
        "actually publish live. Nothing publishes to a real platform without "
        "running 'approve' here first, regardless of --live."
    )
    parser.add_argument("--state-dir", type=Path, default=Path("books"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show cycles waiting on approval")

    approve_p = sub.add_parser("approve", help="approve one cycle for live publishing")
    approve_p.add_argument("cycle_id")
    approve_p.add_argument("--note", default="")

    revoke_p = sub.add_parser("revoke", help="revoke a previously approved cycle")
    revoke_p.add_argument("cycle_id")

    args = parser.parse_args()
    gate = ApprovalGate(args.state_dir)

    if args.command == "list":
        pending = _list_pending(args.state_dir)
        if not pending:
            print("No cycles waiting on approval.")
            return 0
        print(f"{'CYCLE ID':30s} {'STATUS':15s} TITLE")
        for cid, title, status in pending:
            print(f"{cid:30s} {status:15s} {title}")
        return 0

    if args.command == "approve":
        record = gate.approve(args.cycle_id, note=args.note)
        print(f"Approved {record.cycle_id} at {record.approved_at}. "
              f"Next --live run will actually publish it.")
        return 0

    if args.command == "revoke":
        ok = gate.revoke(args.cycle_id)
        print(f"Revoked {args.cycle_id}." if ok else f"{args.cycle_id} was not approved.")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
