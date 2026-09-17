"""
storyforge2/publishing/supabase_sync.py — pushes a real Payhip book listing
into the same public.products table eBay's ebay-sync Edge Function already
populates (see inventory-sync/), so it shows up on jardins-outpost.pages.dev's
"Shop The Inventory" section and the Boss Listers dashboard the same way an
eBay listing does.

Why PUSH instead of POLL (unlike ebay-sync, which polls the Trading API
every 15 minutes via pg_cron): checked Payhip's real documented API surface
before writing this (payhip.com/api-reference + the Payhip module list on
Make.com's integration page) — Payhip has no public endpoint to list or read
a seller's existing products. The only real product-related call is POST
/api/v1/product (create), which is exactly what payhip.py's
PayhipConnector.publish() already calls. There is nothing for a poll-based
sync to poll. So this module is called once, synchronously, right after a
real (non-dry-run) PayhipConnector.publish() succeeds — at that moment
pipeline.py has the one piece of real data that exists anywhere: the
response's listing_url/listing_id — and pushes it into Supabase directly,
the same one-shot-write shape lib/supabase_client.py's get_supabase_client()
already supports for other scripts in this repo (scripts/inventory_sync.py
reads from the same table via that same helper).

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_ANON_KEY) in
.env — reuses lib.supabase_client.get_supabase_client(), does not duplicate
its logic. Neither is set in Josh's .env as of 2026-09-13 (see
connector-wiring-complete.md); until they are, sync_payhip_listing() no-ops
with a clear log line rather than raising, so a book pipeline run is never
blocked by an optional inventory-sync step.

Requires the products.source check constraint to allow 'payhip'
(migrations/0017_add_payhip_source.sql) — run that migration first via
`supabase db push` from inventory-sync/, same as every other migration
in this repo.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["sync_payhip_listing"]


def sync_payhip_listing(
    *, sku: str, title: str, price: str | float, listing_url: str,
    payhip_product_id: Optional[str] = None, description: str = "",
) -> bool:
    """Upserts one book's Payhip listing into public.products.

    Returns True if the row was written, False if it was skipped (missing
    Supabase config or a client-side failure) — never raises, since this is
    a best-effort inventory-sync step, not part of the publish contract
    itself (a Supabase outage must not make publish() report failure for a
    book that really did go live on Payhip).
    """
    try:
        import sys
        from pathlib import Path

        # Same sys.path convention scripts/inventory_sync.py already uses
        # to reach lib/ from outside the storyforge2 package tree.
        repo_root = Path(__file__).resolve().parents[2]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

        from lib.supabase_client import get_supabase_client
    except Exception as exc:
        print(f"[supabase_sync] Skipping — could not import Supabase client: {exc}")
        return False

    try:
        supabase = get_supabase_client()
    except ValueError as exc:
        # SUPABASE_URL / SUPABASE_SERVICE_KEY not set yet — expected until
        # Josh adds them; this is not an error in the pipeline itself.
        print(f"[supabase_sync] Skipping — {exc}")
        return False

    row = {
        "sku": sku,
        "title": title,
        "description": description or None,
        "price": float(price),
        "quantity": 1,  # digital download — always "in stock"
        "status": "active",
        "source": "payhip",
        "payhip_product_id": payhip_product_id,
        "payhip_listing_url": listing_url,
        "synced_at": _utc_now_iso(),
    }

    try:
        supabase.table("products").upsert(row, on_conflict="sku").execute()
    except Exception as exc:
        print(f"[supabase_sync] Upsert failed for sku={sku}: {exc}")
        return False

    print(f"[supabase_sync] Synced '{title}' (sku={sku}) to public.products")
    return True


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
