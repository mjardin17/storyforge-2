"""
storyforge2/books/factory.py — main Book Factory orchestrator.

Coordinates all stages: trend scanning → brief generation → manuscript →
cover → metadata → publishing. Runs 24/7 autonomously via a scheduler or
event loop.

The BookFactory operates in DRY_RUN mode by default. Even when called with
dry_run=False (e.g. via `run_book_factory.py --live`, including from the
daily scheduled task), a cycle cannot actually publish to a real platform
unless it has been separately approved — see storyforge2/approval.py and
run_cycle()'s step 5. A full cycle takes ~30-60 minutes per book, with the
longest step being manuscript generation (Claude API).

State is tracked in books/factory_state.db (SQLite) for resumability.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from datetime import datetime
import sqlite3
from dataclasses import dataclass

from storyforge2.brief import ProjectBrief
from storyforge2.books.trends import TrendScanner, TrendOpportunity
from storyforge2.books.metadata import MetadataBuilder, BookMetadata
from storyforge2.pipeline import BookPipeline
from storyforge2.video.commercial import queue_book_commercial
from storyforge2.books.listings import queue_book_listings
from storyforge2.approval import ApprovalGate

__all__ = ["BookFactory", "BookFactoryError"]


class BookFactoryError(RuntimeError):
    pass


class MockPipelineError(RuntimeError):
    """Simplified error for mock pipeline."""
    pass


@dataclass
class BookCycle:
    """A single book generation cycle: opportunity → published book."""

    cycle_id: str  # unique identifier
    opportunity: TrendOpportunity
    brief: Optional[ProjectBrief] = None
    metadata: Optional[BookMetadata] = None
    work_dir: Optional[Path] = None
    pipeline: Optional[BookPipeline] = None
    status: str = "initialized"  # initialized → manuscript → cover → metadata → publish
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    def is_complete(self) -> bool:
        return self.status == "published"

    def is_failed(self) -> bool:
        return self.status == "failed"


class BookFactory:
    """24/7 autonomous book generation factory.

    Context manager to ensure DB connections are cleaned up properly.
    Use: with BookFactory() as factory: factory.run_cycle()

    Operates in a loop:
    1. Scan for a new trend opportunity (every 4 hours)
    2. Convert opportunity to ProjectBrief
    3. Run manuscript generation pipeline (DRY_RUN by default)
    4. Generate cover
    5. Build metadata
    6. Queue for publishing (manual approval in MVP)

    All state is persisted to disk for resumability.
    """

    def __init__(self, work_base: Path | str = "books"):
        self.work_base = Path(work_base)
        self.work_base.mkdir(parents=True, exist_ok=True)

        self.trend_scanner = TrendScanner(state_db=self.work_base / "trends_state.json")
        self.state_db = self.work_base / "factory_state.db"
        self._open_connections = []
        self._init_db()
        # Human approval gate — see storyforge2/approval.py. No cycle
        # publishes to a real platform, regardless of dry_run/--live, unless
        # it has been explicitly approved via `python -m storyforge2.approval
        # approve <cycle_id>`. This closes the gap between this docstring's
        # long-standing claim ("manual approval in MVP") and the code, which
        # previously had no such gate at all.
        self.approval = ApprovalGate(self.work_base)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._cleanup()
        return False

    def _cleanup(self):
        """Close any open database connections."""
        for conn in self._open_connections:
            try:
                conn.close()
            except Exception:
                pass
        self._open_connections.clear()

    def _init_db(self):
        """Initialize SQLite ledger for cycle tracking."""
        conn = sqlite3.connect(self.state_db)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    opportunity_niche TEXT,
                    title TEXT,
                    status TEXT,
                    work_dir TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def run_cycle(self, dry_run: bool = True) -> Optional[BookCycle]:
        """Execute one full book generation cycle.

        Returns:
            BookCycle if a book was generated, None if no opportunity was due.
        """
        print(f"\n[BOOK FACTORY] Running cycle (dry_run={dry_run})")

        # 1. Scan for opportunity
        opportunity = self.trend_scanner.scan(dry_run=dry_run)
        if not opportunity:
            print("[BOOK FACTORY] No opportunity due yet. Skipping cycle.")
            return None

        print(f"[BOOK FACTORY] Opportunity: {opportunity.title}")
        print(f"[BOOK FACTORY] Niche: {opportunity.niche}")
        print(f"[BOOK FACTORY] Pitch: {opportunity.premise}")

        # Create a cycle
        cycle = BookCycle(
            cycle_id=opportunity.scan_id,
            opportunity=opportunity,
            work_dir=self.work_base / opportunity.scan_id,
            started_at=datetime.utcnow(),
        )

        # 2. Convert opportunity to ProjectBrief
        try:
            cycle.brief = self._opportunity_to_brief(opportunity)
            print(f"[BOOK FACTORY] Brief ready: {cycle.brief.title}")
        except Exception as e:
            cycle.status = "failed"
            cycle.error = f"Brief generation failed: {e}"
            print(f"[BOOK FACTORY] ✗ {cycle.error}")
            self._save_cycle(cycle)
            return cycle

        # 3. Run manuscript generation pipeline (real, using PatersonFormula via Claude API)
        try:
            cycle.work_dir.mkdir(parents=True, exist_ok=True)

            # Call the real BookPipeline with the ProjectBrief
            # Uses Patterson formula for manuscript generation via Claude API (Anthropic)
            pipeline = BookPipeline(brief=cycle.brief, work_dir=str(cycle.work_dir))

            # Run with anthropic provider (uses ANTHROPIC_API_KEY from environment)
            # Falls back to mock if API key is not set
            credentials = self._load_credentials()
            provider_name = "anthropic" if credentials.get("anthropic_api_key") else "mock"
            # Cover art uses a SEPARATE provider from manuscript text — see
            # pipeline.py run()'s docstring for the bug this fixes (the text
            # provider_name was being threaded into the image-provider slot,
            # which has no "anthropic" entry). "waterfall" is this repo's
            # existing free-tier-first image fetcher (Pollinations, then
            # Wikimedia — see empire_render.fetch_one_scene_image), reused
            # here rather than paying for a dedicated cover-art API. Only
            # used on a real (--live) run; dry-runs keep the zero-cost mock
            # placeholder so testing stays fast and network-free.
            image_provider_name = "waterfall" if not dry_run else "mock"
            success = pipeline.run(
                provider_name=provider_name, image_provider_name=image_provider_name, dry_run=dry_run,
            )

            if not success:
                raise MockPipelineError("BookPipeline.run() failed")

            # Store pipeline instance for reuse in publishing step
            cycle.pipeline = pipeline

            cycle.status = "manuscript"
            print(f"[BOOK FACTORY] ✓ Manuscript generated via Patterson formula")
        except Exception as e:
            cycle.status = "failed"
            cycle.error = f"Manuscript generation failed: {e}"
            print(f"[BOOK FACTORY] ✗ {cycle.error}")
            self._save_cycle(cycle)
            return cycle

        # 4. Build metadata
        try:
            cycle.metadata = MetadataBuilder.build(
                title=cycle.brief.title,
                niche=opportunity.niche,
                keywords=opportunity.keywords,
                base_description=cycle.brief.premise,
                author=cycle.brief.author_name,
            )
            print(f"[BOOK FACTORY] ✓ Metadata built: ISBN {cycle.metadata.isbn}")
            cycle.status = "metadata"
        except Exception as e:
            cycle.status = "failed"
            cycle.error = f"Metadata generation failed: {e}"
            print(f"[BOOK FACTORY] ✗ {cycle.error}")
            self._save_cycle(cycle)
            return cycle

        # 4.5. Queue a social-media commercial for the book (reuses Boss
        # Listers' commercial_generator.py + render_commercial.py unchanged;
        # the already-running video_pipeline_agent.py picks this up from
        # MISSION_BOARD.json, renders it, and auto-posts to IG/TikTok/
        # YouTube Shorts/Facebook — no separate render/post step needed here)
        try:
            if pipeline.cover_dir and pipeline.cover_dir.exists():
                queue_book_commercial(
                    title=cycle.brief.title,
                    premise=cycle.brief.premise,
                    author=cycle.brief.author_name,
                    price=9.99,  # default ebook price; adjust per-book pricing later
                    cover_dir=pipeline.cover_dir,
                    mission_id=f"commercial-book-{cycle.cycle_id}",
                    mission_board_path=self.work_base.parent / "MISSION_BOARD.json",
                )
        except Exception as e:
            # Non-fatal: a missing commercial doesn't block publishing a book.
            print(f"[BOOK FACTORY] ⚠ Commercial queuing failed (non-fatal): {e}")

        # 4.6. Queue book listings to multiple platforms via MISSION_BOARD.json
        # Follows the same pattern as commercials: create missions, add to board,
        # let video_pipeline_agent handle platform-specific uploads
        try:
            listing_results = queue_book_listings(
                cycle=cycle,
                mission_board_path=self.work_base.parent / "MISSION_BOARD.json",
                platforms=["kdp", "d2d", "ingrampark", "payhip", "gumroad", "etsy"],
            )
            succeeded = sum(1 for v in listing_results.values() if v)
            print(f"[BOOK FACTORY] ✓ Queued listings to {succeeded}/{len(listing_results)} platforms")
        except Exception as e:
            # Non-fatal: listing failures don't block the cycle
            print(f"[BOOK FACTORY] ⚠ Listing queuing failed (non-fatal): {e}")

        # 5. Publish to all available platforms
        try:
            # Load credentials from environment
            credentials = self._load_credentials()

            # Define publishing platforms in priority order
            # Direct APIs first (highest automation), then manual exports
            platforms_to_try = [
                "kdp",  # Amazon KDP (Kindle Direct Publishing)
                "d2d",  # Draft2Digital (50+ retailers via one API)
                "payhip",  # Payhip (author store)
                "gumroad",  # Gumroad (creator platform)
                "etsy_digital",  # Etsy (digital products)
                "shopify",  # Shopify (if user has a store)
                "manual_export",  # Fallback: generates upload-ready packages
            ]

            # Reuse the pipeline instance created during manuscript generation
            pipeline = cycle.pipeline

            # HUMAN APPROVAL GATE — this is the check that makes --live safe
            # to run unattended (e.g. from a daily scheduled task). A cycle
            # only gets a REAL publish call if Josh already ran
            # `python -m storyforge2.approval approve <cycle_id>` for this
            # exact cycle. Anything else — including a caller that passed
            # dry_run=False — is forced back to dry_run here.
            approved = self.approval.is_approved(cycle.cycle_id)
            effective_dry_run = dry_run or not approved
            if not dry_run and not approved:
                print(f"[BOOK FACTORY] ⚠ LIVE publish requested but cycle "
                      f"{cycle.cycle_id} is not approved — running publish "
                      f"in DRY-RUN instead. Review the book in "
                      f"{cycle.work_dir}, then approve with:\n"
                      f"    python -m storyforge2.approval approve {cycle.cycle_id}")

            # Publish to all platforms (dry_run unless explicitly approved)
            publish_results = pipeline.publish(
                platforms=platforms_to_try,
                credentials=credentials,
                dry_run=effective_dry_run
            )

            # Count successes
            successes = sum(1 for r in publish_results.values() if r.get("status") == "ok")
            print(f"[BOOK FACTORY] ✓ Published to {successes}/{len(platforms_to_try)} platforms"
                  f"{' (dry-run)' if effective_dry_run else ' (LIVE)'}")

            cycle.status = "published" if not effective_dry_run else "ready_publish"
            if effective_dry_run and not dry_run:
                cycle.error = "Awaiting approval — see storyforge2/approval.py"
        except Exception as e:
            # Even if publishing fails, mark as ready_publish so user can retry later
            cycle.status = "ready_publish"
            cycle.error = f"Publishing step encountered error (book still ready): {e}"
            print(f"[BOOK FACTORY] ⚠ Publishing error: {e}")

        cycle.completed_at = datetime.utcnow()
        self._save_cycle(cycle)

        return cycle

    def _load_credentials(self) -> dict[str, str]:
        """Load publishing platform credentials from environment variables.

        Expected env vars (all optional):
        - ANTHROPIC_API_KEY: Claude API key for manuscript generation
        - KDP_EMAIL, KDP_PASSWORD: Amazon KDP credentials
        - D2D_API_KEY: Draft2Digital API key
        - PAYHIP_API_KEY: Payhip API key
        - GUMROAD_API_TOKEN: Gumroad API token
        - ETSY_ACCESS_TOKEN, ETSY_SHOP_ID, ETSY_API_KEY: Etsy credentials
        - SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP_NAME: Shopify credentials
        """
        import os
        return {
            "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
            "kdp_email": os.getenv("KDP_EMAIL", ""),
            "kdp_password": os.getenv("KDP_PASSWORD", ""),
            "d2d_api_key": os.getenv("D2D_API_KEY", ""),
            "payhip_api_key": os.getenv("PAYHIP_API_KEY", ""),
            "gumroad_api_token": os.getenv("GUMROAD_API_TOKEN", ""),
            "etsy_access_token": os.getenv("ETSY_ACCESS_TOKEN", ""),
            "etsy_shop_id": os.getenv("ETSY_SHOP_ID", ""),
            "etsy_api_key": os.getenv("ETSY_API_KEY", ""),
            "shopify_access_token": os.getenv("SHOPIFY_ACCESS_TOKEN", ""),
            "shopify_shop_name": os.getenv("SHOPIFY_SHOP_NAME", ""),
        }

    def _opportunity_to_brief(self, opportunity: TrendOpportunity) -> ProjectBrief:
        """Convert TrendOpportunity to ProjectBrief for the pipeline."""
        return ProjectBrief(
            title=opportunity.title,
            premise=opportunity.premise,
            audience=opportunity.target_audience,
            # Was hardcoded "non-fiction" — every automated cycle ignored the
            # opportunity's actual niche/genre. Now the niche rotation in
            # trends.py (which now includes fiction niches) drives genre,
            # and manuscript.py's formula_for_genre() routes it to the
            # right formula engine (Patterson for fiction, NonFictionFormula
            # for non-fiction) automatically.
            genre=opportunity.genre,
            author_name="Empire OS Publishing",
            length_chapters=12,  # 20k-40k words in 12 chapters
            platform_targets=["draft2digital", "gumroad"],  # will add more later
            marketing_platforms=[],
        )

    def _save_cycle(self, cycle: BookCycle):
        """Persist cycle to SQLite ledger."""
        conn = sqlite3.connect(self.state_db)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO cycles
                (cycle_id, opportunity_niche, title, status, work_dir, started_at, completed_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cycle.cycle_id,
                cycle.opportunity.niche,
                cycle.opportunity.title,
                cycle.status,
                str(cycle.work_dir) if cycle.work_dir else None,
                cycle.started_at.isoformat() if cycle.started_at else None,
                cycle.completed_at.isoformat() if cycle.completed_at else None,
                cycle.error,
            ))
            conn.commit()
        finally:
            conn.close()

    def get_ready_to_publish(self) -> list[BookCycle]:
        """Get all books ready to be published."""
        cycles = []
        conn = sqlite3.connect(self.state_db)
        try:
            rows = conn.execute(
                "SELECT cycle_id FROM cycles WHERE status = 'ready_publish' ORDER BY started_at DESC LIMIT 10"
            ).fetchall()
            for (cycle_id,) in rows:
                # Reconstruct from disk (simplified for MVP)
                cycles.append(BookCycle(
                    cycle_id=cycle_id,
                    opportunity=TrendOpportunity(
                        niche="unknown",
                        title="Reconstructed Book",
                        premise="",
                        keywords=[],
                        target_audience="",
                        estimated_audience_size="medium",
                    ),
                    status="ready_publish",
                ))
        finally:
            conn.close()
        return cycles

    def status_report(self) -> dict:
        """Generate a status report of the factory."""
        conn = sqlite3.connect(self.state_db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
            complete = conn.execute("SELECT COUNT(*) FROM cycles WHERE status = 'published'").fetchone()[0]
            ready = conn.execute("SELECT COUNT(*) FROM cycles WHERE status = 'ready_publish'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM cycles WHERE status = 'failed'").fetchone()[0]
        finally:
            conn.close()

        from storyforge2.books.trends import EVERGREEN_NICHES
        return {
            "total_cycles": total,
            "published": complete,
            "ready_publish": ready,
            "failed": failed,
            "trend_scanner_state": {
                "last_scan": self.trend_scanner.last_scan.isoformat() if self.trend_scanner.last_scan else None,
                "last_niche": list(EVERGREEN_NICHES.keys())[self.trend_scanner.last_niche_index] if self.trend_scanner.last_niche_index >= 0 else None,
            },
        }


# Placeholder for testing
if __name__ == "__main__":
    factory = BookFactory()
    cycle = factory.run_cycle(dry_run=True)
    if cycle:
        print(f"\n✓ Generated cycle: {cycle.cycle_id}")
        print(f"  Status: {cycle.status}")
        if cycle.metadata:
            print(f"  ISBN: {cycle.metadata.isbn}")
    print(f"\nFactory status: {json.dumps(factory.status_report(), indent=2)}")
