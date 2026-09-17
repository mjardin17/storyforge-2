"""
storyforge2/publishing/inventory.py — cross-book, cross-platform publishing
inventory ledger.

Why this exists: Josh asked whether the pipeline "inventory syncs" across
selling platforms. It didn't — verified by reading every file under
storyforge2/publishing/ (registry.py, connectors/payhip.py,
connectors/etsy_digital.py, connectors/manual_export.py) plus pipeline.py's
publish() method: nothing anywhere records which book is already listed on
which platform. Two concrete consequences of that gap, both real:

1. pipeline.py's publish() has no re-publish guard. Calling it twice for the
   same book+platform fires the connector twice. For Payhip that means a
   second, duplicate product. For Etsy that would eventually mean a second
   draft listing (draft creation is free, but it's still clutter that has to
   be manually reconciled before anything real ships).
2. PublishingConnectorResult.listing_url / .metadata (e.g. Payhip's real
   product link, Etsy's listing_id) were computed by the connector and then
   thrown away — pipeline.py's publish() only kept `status`/`message` in the
   dict it returned and logged to state.py. There was no durable record of
   the actual listing URL anywhere. Fixed alongside this module in
   pipeline.py's publish().

Storage: books/publishing_inventory.json — same convention as
approval.py's books/approvals.json (plain JSON, not the per-project SQLite
state ledger, so it's readable across every book/cycle and by a human
without a DB tool). Shape:

    {
      "<book_slug>": {
        "<platform_id>": {
          "status": "success" | "failed" | "dry_run",
          "listing_id": str | null,
          "listing_url": str | null,
          "message": str,
          "recorded_at": iso8601 str
        }
      }
    }

A dry-run result is recorded (so you can see it was exercised) but never
counts as "published" — is_published() only returns True for a real,
successful publish. Fails closed on a corrupt file, same reasoning as
approval.py: a ledger Claude can't parse must never silently read as
"nothing published yet, go ahead and republish everything" being treated as
safe by a caller that assumes the opposite; every read site here treats
"can't parse" as "don't know" and the pipeline caller decides to skip out of
caution, not to plow ahead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = ["PublishingInventory", "InventoryRecord"]


@dataclass
class InventoryRecord:
    book_slug: str
    platform_id: str
    status: str
    listing_id: Optional[str] = None
    listing_url: Optional[str] = None
    message: str = ""
    recorded_at: str = ""


class PublishingInventory:
    """Tracks, per book, which platforms it has actually been published to.

    Read-only for connectors — only pipeline.py's publish() writes to this,
    right after a connector call returns, using that call's real result.
    Nothing in here ever calls a platform API itself.
    """

    def __init__(self, work_base: Path | str = "books"):
        self.work_base = Path(work_base)
        self.work_base.mkdir(parents=True, exist_ok=True)
        self.inventory_path = self.work_base / "publishing_inventory.json"

    def _load(self) -> dict[str, dict]:
        if not self.inventory_path.exists():
            return {}
        try:
            return json.loads(self.inventory_path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt/partial file -> fail closed: report as "unknown", not
            # "empty". See module docstring — an empty read here must never
            # be indistinguishable from "verified nothing published yet".
            return {"__corrupt__": True}

    def _save(self, data: dict[str, dict]) -> None:
        self.inventory_path.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )

    def is_corrupt(self) -> bool:
        return bool(self._load().get("__corrupt__"))

    def is_published(self, book_slug: str, platform_id: str) -> bool:
        """True only for a real (non-dry-run), successful publish. A
        dry_run or failed attempt never blocks a real retry."""
        data = self._load()
        if data.get("__corrupt__"):
            return False  # fail closed -> caller should treat as "unknown", not skip
        entry = data.get(book_slug, {}).get(platform_id)
        return bool(entry and entry.get("status") == "success")

    def get(self, book_slug: str, platform_id: str) -> Optional[dict]:
        return self._load().get(book_slug, {}).get(platform_id)

    def record(
        self, book_slug: str, platform_id: str, *, status: str,
        listing_id: Optional[str] = None, listing_url: Optional[str] = None,
        message: str = "",
    ) -> InventoryRecord:
        data = self._load()
        data.pop("__corrupt__", None)
        data.setdefault(book_slug, {})
        record = InventoryRecord(
            book_slug=book_slug, platform_id=platform_id, status=status,
            listing_id=listing_id, listing_url=listing_url, message=message,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        data[book_slug][platform_id] = {
            "status": record.status, "listing_id": record.listing_id,
            "listing_url": record.listing_url, "message": record.message,
            "recorded_at": record.recorded_at,
        }
        self._save(data)
        return record

    def list_book(self, book_slug: str) -> dict[str, dict]:
        return self._load().get(book_slug, {})

    def list_all(self) -> dict[str, dict]:
        data = self._load()
        data.pop("__corrupt__", None)
        return data


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect the cross-platform publishing inventory ledger "
        "(books/publishing_inventory.json) — what's actually live where."
    )
    parser.add_argument("--state-dir", type=Path, default=Path("books"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every book and its per-platform status")

    show_p = sub.add_parser("show", help="show one book's per-platform status")
    show_p.add_argument("book_slug")

    clear_p = sub.add_parser("clear", help="remove one book/platform record (forces a fresh publish)")
    clear_p.add_argument("book_slug")
    clear_p.add_argument("platform_id")

    args = parser.parse_args()
    inv = PublishingInventory(args.state_dir)

    if inv.is_corrupt():
        print(f"[WARN] {inv.inventory_path} could not be parsed — "
              f"treating as unknown, not empty. Fix or delete it by hand.")
        return 1

    if args.command == "list":
        data = inv.list_all()
        if not data:
            print("No publishing records yet.")
            return 0
        for slug, platforms in sorted(data.items()):
            print(f"\n{slug}")
            for platform_id, entry in sorted(platforms.items()):
                url = entry.get("listing_url") or "-"
                print(f"  {platform_id:15s} {entry['status']:10s} {url}")
        return 0

    if args.command == "show":
        entries = inv.list_book(args.book_slug)
        if not entries:
            print(f"No publishing records for '{args.book_slug}'.")
            return 0
        for platform_id, entry in sorted(entries.items()):
            print(f"{platform_id:15s} {entry['status']:10s} "
                  f"{entry.get('listing_url') or '-'}  ({entry.get('recorded_at', '')})")
        return 0

    if args.command == "clear":
        data = inv._load()
        data.pop("__corrupt__", None)
        removed = data.get(args.book_slug, {}).pop(args.platform_id, None)
        inv._save(data)
        print(f"Cleared {args.book_slug}/{args.platform_id}." if removed
              else f"No record for {args.book_slug}/{args.platform_id}.")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
