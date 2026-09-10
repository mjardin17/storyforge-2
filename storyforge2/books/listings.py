"""
storyforge2/books/listings.py — book listing mission generator.

Queues book listings to Draft2Digital, Payhip, Gumroad, and generates manual
listing packages for KDP, IngramSpark, and other platforms without public APIs.

Reuses BossListers' mission infrastructure (MISSION_BOARD.json handoff) exactly
like queue_book_commercial() does — no new dispatchers, no new posting code.
The agent polls missions, executes listing handlers, and tracks status.
"""

from __future__ import annotations

import sys
import json
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from dataclasses import dataclass, asdict
from datetime import datetime

from storyforge2.books.metadata import BookMetadata

if TYPE_CHECKING:
    from storyforge2.books.factory import BookCycle

__all__ = ["queue_book_listings"]


def _repo_root() -> Path:
    """Boss Listers lives at a sibling directory — resolve and add to sys.path."""
    # storyforge2/books/listings.py -> storyforge2/books -> storyforge2 -> repo root (video-bot-pipeline)
    return Path(__file__).resolve().parents[2]


def _pick_cover_images(cover_dir: Path, max_images: int = 3) -> list[str]:
    """Pick real cover files in priority order (front first, then variants)."""
    priority = [
        "cover_front.png",
        "cover_full_wrap.png",
        "cover_ebook.png",
        "cover_social_instagram.png",
        "cover_thumbnail.png",
    ]
    found: list[str] = []
    for name in priority:
        candidate = cover_dir / name
        if candidate.exists():
            found.append(str(candidate))
        if len(found) >= max_images:
            break
    return found


@dataclass
class BookListingMission:
    """A single book listing mission for one platform."""

    id: str  # mission ID
    type: str = "book_listing"
    status: str = "pending"
    title: str = ""

    # Book metadata
    book_data: dict = None

    # Platform-specific
    platform: str = ""  # kdp, d2d, ingrampark, payhip, gumroad, etsy

    # Format packages
    formats: dict = None  # { "epub": path, "mobi": path, "pdf": path, "print_pdf": path }
    cover_images: list[str] = None

    # Timestamps
    created_at: str = ""

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "title": self.title,
            "book_data": self.book_data or {},
            "platform": self.platform,
            "formats": self.formats or {},
            "cover_images": self.cover_images or [],
            "created_at": self.created_at or datetime.utcnow().isoformat(),
        }


def queue_book_listings(
    cycle: object,  # BookCycle from factory.py (avoiding circular import)
    mission_board_path: Path | str = "MISSION_BOARD.json",
    platforms: Optional[list[str]] = None,
) -> dict[str, bool]:
    """Queue book listings to multiple platforms via the mission board.

    Follows the exact pattern as queue_book_commercial(): creates missions,
    adds them to MISSION_BOARD.json, and the video_pipeline_agent picks them up.

    Args:
        cycle: Completed BookCycle with pipeline, metadata, work_dir
        mission_board_path: Path to MISSION_BOARD.json
        platforms: Which platforms to list on. Defaults to all supported.
                  Options: kdp, d2d, ingrampark, payhip, gumroad, etsy

    Returns:
        dict mapping platform name -> bool (queued successfully)
    """
    if not cycle.pipeline or not cycle.metadata:
        print(f"[book_listings] cycle incomplete (no pipeline or metadata)")
        return {}

    if not cycle.pipeline.cover_dir or not cycle.pipeline.cover_dir.exists():
        print(f"[book_listings] no cover directory for {cycle.cycle_id}")
        return {}

    # Default: all platforms
    if not platforms:
        platforms = ["kdp", "d2d", "ingrampark", "payhip", "gumroad", "etsy"]

    # Import Boss Listers helpers
    repo_root = _repo_root()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from lib.bookListingGenerator import create_listing_mission, add_to_mission_board

    cover_images = _pick_cover_images(cycle.pipeline.cover_dir)
    if not cover_images:
        print(f"[book_listings] no usable cover images for {cycle.cycle_id}")
        return {}

    results = {}
    metadata_dict = cycle.metadata.to_dict()

    for platform in platforms:
        mission_id = f"book-{platform}-{cycle.cycle_id}"

        try:
            mission = create_listing_mission(
                book_data=metadata_dict,
                platform=platform,
                cover_images=cover_images,
                formats={},  # Will be populated by pipeline or handlers
                mission_id=mission_id,
            )

            if not mission:
                print(f"[book_listings] failed to create mission for {platform}")
                results[platform] = False
                continue

            added = add_to_mission_board(mission, mission_board_path=str(mission_board_path))
            if added:
                print(f"[book_listings] queued {platform} listing for '{cycle.metadata.title}'")
                results[platform] = True
            else:
                print(f"[book_listings] {platform} listing already on board for {mission_id}")
                results[platform] = True  # It's queued, just a duplicate attempt

        except Exception as e:
            print(f"[book_listings] error queuing {platform}: {e}")
            results[platform] = False

    return results
