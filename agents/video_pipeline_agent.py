#!/usr/bin/env python3
"""
Video Pipeline Agent for Buzz relay.
Monitors and executes render jobs from the video pipeline.
Connects to Buzz relay and posts status to #video-pipeline channel.
"""

import os
import json
import subprocess
import time
from pathlib import Path
from datetime import datetime
import sys

# Add lib paths
boss_listers_path = Path(__file__).resolve().parent.parent.parent / "BossListers"
sys.path.insert(0, str(Path(__file__).parent.parent))
if boss_listers_path.exists():
    sys.path.insert(0, str(boss_listers_path))

from lib.crosspost_bridge import queue_commercial_for_posting
try:
    from lib.bookListingGenerator import executeListingMission
except ImportError:
    executeListingMission = None

BUZZ_RELAY_URL = os.getenv("BUZZ_RELAY_URL", "ws://localhost:3000")
BUZZ_PRIVATE_KEY = os.getenv("BUZZ_PRIVATE_KEY")
MISSION_BOARD_PATH = Path(__file__).parent.parent / "MISSION_BOARD.json"
RENDER_SCRIPT = Path(__file__).parent.parent / "empire_render.py"

def run_command(cmd):
    """Execute shell command and return output."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3600)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out (1 hour)"
    except Exception as e:
        return 1, "", str(e)

def load_mission_board():
    """Load missions from MISSION_BOARD.json."""
    if not MISSION_BOARD_PATH.exists():
        return []
    try:
        with open(MISSION_BOARD_PATH) as f:
            data = json.load(f)
            return data.get("missions", [])
    except Exception as e:
        print(f"Error loading missions: {e}")
        return []

def post_to_channel(message):
    """Post message to #video-pipeline channel (fallback to stdout if unavailable)."""
    print(f"[{datetime.now().isoformat()}] #video-pipeline: {message}")

    if not BUZZ_PRIVATE_KEY:
        return

    cmd = f"""BUZZ_PRIVATE_KEY={BUZZ_PRIVATE_KEY} BUZZ_RELAY_URL={BUZZ_RELAY_URL} \
    buzz-cli message --channel video-pipeline '{message}'"""

    returncode, stdout, stderr = run_command(cmd)
    if returncode == 0:
        print(f"  ✓ Buzz posted")
    elif "not found" not in stderr.lower():
        print(f"  ⚠️ Buzz error: {stderr[:50]}")

def execute_listing(mission):
    """Execute a book listing mission."""
    mission_id = mission.get("id", "unknown")
    platform = mission.get("platform", "unknown")
    book_data = mission.get("book_data", {})
    book_title = book_data.get("title", "Untitled")

    post_to_channel(f"📚 Processing book listing: {book_title} (→ {platform.upper()})")

    if not executeListingMission:
        post_to_channel(f"⚠️ Book listing handler not available (bookListingGenerator not found)")
        return False

    try:
        env = {
            "D2D_API_KEY": os.getenv("D2D_API_KEY", ""),
            "PAYHIP_API_KEY": os.getenv("PAYHIP_API_KEY", ""),
            "GUMROAD_API_TOKEN": os.getenv("GUMROAD_API_TOKEN", ""),
            "ETSY_ACCESS_TOKEN": os.getenv("ETSY_ACCESS_TOKEN", ""),
            "ETSY_SHOP_ID": os.getenv("ETSY_SHOP_ID", ""),
        }

        result = executeListingMission(mission, env)
        status = result.get("status", "error")

        if status == "success":
            url = result.get("url", "")
            msg = f"✅ Listing posted: {book_title} → {platform.upper()}"
            if url:
                msg += f" ({url})"
            post_to_channel(msg)
            return True
        elif status == "manual_package_generated":
            post_to_channel(f"📦 Manual package generated for {platform.upper()}: {book_title}")
            post_to_channel(f"   Instructions: {result.get('message', '')}")
            return True
        else:
            error = result.get("error", "Unknown error")
            post_to_channel(f"❌ Listing failed for {platform.upper()}: {error}")
            return False

    except Exception as e:
        post_to_channel(f"❌ Error processing listing: {str(e)[:100]}")
        return False


def execute_render(mission):
    """Execute a render mission (episode or commercial)."""
    mission_id = mission.get("id", "unknown")
    mission_type = mission.get("type", "episode")
    channel = mission.get("channel", "unknown")

    if mission_type == "commercial":
        product_name = mission.get("product", {}).get("name", "Product")
        price = mission.get("product", {}).get("price", 0)
        post_to_channel(f"🎬 Rendering commercial: {product_name} (${price})")

        # Create render script for commercial
        render_script = mission.get("render_script", {})
        base_dir = Path(__file__).parent.parent
        script_path = base_dir / f".temp_commercial_{mission_id}.json"
        # output/{mission_id}.mp4 — matches the FIRST path this function's own
        # lookup below already checks, so no change needed there.
        out_path = base_dir / "output" / f"{mission_id}.mp4"

        try:
            with open(script_path, 'w') as f:
                json.dump(render_script, f)

            # FIXED 2026-08-12: was `empire_render.py --script {path}` — that
            # script requires --channel/--episode (argparse exit 2 before any
            # frame rendered) and doesn't understand commercial scene types
            # anyway (product_showcase/price_and_cta/product_loop don't exist
            # there — it renders documentary episodes). render_commercial.py
            # is the dedicated, tested entrypoint for this job; see CLAUDE.md.
            python = r"C:\Users\jjard\AppData\Local\Programs\Python\Python314\python.exe"
            cmd = (f'cd /d "{base_dir}" && "{python}" render_commercial.py '
                  f'--script "{script_path}" --out "{out_path}"')
        except Exception as e:
            post_to_channel(f"❌ Error preparing commercial: {str(e)[:100]}")
            return False
    else:
        # Episode render
        episode = mission.get("episode", "unknown")
        post_to_channel(f"🚀 Starting render: {channel} EP{episode} (mission {mission_id})")
        cmd = f"cd {Path(__file__).parent.parent} && python empire_render.py --episode {episode} --channel {channel}"

    returncode, stdout, stderr = run_command(cmd)

    if returncode == 0:
        if mission_type == "commercial":
            product_name = mission.get('product', {}).get('name', 'Product')
            product_id = mission.get('product', {}).get('id', 'unknown')
            post_to_channel(f"✅ Commercial render complete: {product_name}")

            # Queue for Crosspost publishing
            # Look for rendered file in common locations
            commercial_file = Path(__file__).parent.parent / f"output/{mission_id}.mp4"
            if not commercial_file.exists():
                commercial_file = Path(__file__).parent.parent / f"renders/{mission_id}.mp4"
            if not commercial_file.exists():
                commercial_file = Path(__file__).parent.parent / f".temp_commercial_{mission_id}.mp4"

            if commercial_file.exists():
                queue_commercial_for_posting(
                    commercial_file,
                    product_name=product_name,
                    product_id=product_id,
                    platforms=["instagram", "tiktok", "youtube_shorts", "facebook"]
                )
                post_to_channel(f"📤 Queued for Crosspost: Instagram, TikTok, YouTube Shorts, Facebook")
                post_to_channel("🚀 Crosspost will publish to all platforms automatically")
            else:
                post_to_channel(f"⚠️ Rendered file not found at expected location")
        else:
            post_to_channel(f"✅ Render complete: {channel} EP{episode}")
        return True
    else:
        if mission_type == "commercial":
            post_to_channel(f"❌ Commercial render failed\n{stderr[:500]}")
        else:
            post_to_channel(f"❌ Render failed: {channel} EP{episode}\n{stderr[:500]}")
        return False

def main():
    """Main agent loop."""
    print(f"Video Pipeline Agent starting")
    print(f"Relay: {BUZZ_RELAY_URL}")
    print(f"Agent key: {BUZZ_PRIVATE_KEY[:16] if BUZZ_PRIVATE_KEY else 'NONE'}...")

    post_to_channel("🤖 Video Pipeline Agent online - ready to render")

    processed_missions = set()

    while True:
        try:
            missions = load_mission_board()

            for mission in missions:
                mission_id = mission.get("id")
                if mission_id and mission_id not in processed_missions:
                    if mission.get("status") == "pending":
                        post_to_channel(f"📋 Found mission {mission_id}: {mission.get('title', 'Untitled')}")
                        mission_type = mission.get("type", "episode")
                        if mission_type == "book_listing":
                            execute_listing(mission)
                        else:
                            execute_render(mission)
                        processed_missions.add(mission_id)

            time.sleep(30)

        except KeyboardInterrupt:
            post_to_channel("🛑 Video Pipeline Agent stopping")
            break
        except Exception as e:
            print(f"Agent error: {e}")
            post_to_channel(f"⚠️ Agent error: {str(e)[:100]}")
            time.sleep(60)

if __name__ == "__main__":
    main()
