"""storyforge2/llm/gemini_text.py — shared Gemini text/JSON call helper.

Extracted from research_agent.py's `gemini_call`/`gemini_json` (the video
pipeline's proven, already-live pattern for calling Gemini's generateContent
endpoint and parsing JSON out of the response) so storyforge2/books/trends.py
can reuse it instead of re-implementing the same request/retry/parsing logic.
research_agent.py itself is left untouched (it's a separate top-level script,
not part of the storyforge2 package, and already works) — this module exists
for storyforge2 code that wants the same capability.

Honest scope note: this calls Gemini's TEXT generation endpoint
(`:generateContent` with `responseMimeType: text/plain`), the same one
research_agent.py already uses successfully for the gg/ml/lo video channels.
The Story Forge status doc has a separate, confirmed-real problem with
Gemini IMAGE generation specifically (AQ-format API keys returning a 0 quota
on the free image-gen tier) — that is a different quota/endpoint and does
not necessarily mean text calls are blocked too. This module still fails
closed: any failure (missing key, network, quota, bad JSON) raises, and
callers are expected to catch it and fall back rather than assume success.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# gemini-2.0-flash was retired (live 404 "no longer available", 2026-09-13).
# Live probe with this key that day: gemini-3.8-flash answers but returns
# intermittent 503 "high demand"; gemini-flash-latest is just an alias for
# 3.8-flash, so it can't serve as a fallback; gemini-2.5-flash answered in
# under 1s; Pro models return 429 (no Pro quota on this key). So the fallback
# is a *different* model, not an alias of the primary.
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_FALLBACK = "gemini-2.5-flash"
# 404 = model retired, 429 = model-specific quota, 503 = model overloaded.
_FALLBACK_HTTP_CODES = {404, 429, 503}

__all__ = ["gemini_configured", "gemini_call", "gemini_json"]


def _load_dotenv_once(_done: list = []) -> None:
    """Load .env from the repo root, once per process, without overriding
    anything already set (e.g. by a real env var or an earlier loader).
    Idempotent and safe to call even if something else already loaded .env.
    """
    if _done:
        return
    _done.append(True)
    # storyforge2/llm/gemini_text.py -> parents[2] is the repo root, matching
    # storyforge2/publishing/supabase_sync.py's existing convention.
    repo_root = Path(__file__).resolve().parents[2]
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def gemini_configured() -> bool:
    """True if a GEMINI_API_KEY is available, without making a network call."""
    _load_dotenv_once()
    return bool(os.environ.get("GEMINI_API_KEY"))


def gemini_call(
    prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    model: str = GEMINI_MODEL,
) -> str:
    """Call Gemini's generateContent endpoint. Raises RuntimeError on any
    failure (missing key, network, HTTP error, exhausted retries) — this is
    deliberate: callers must decide what "no signal from Gemini" means for
    them rather than silently getting an empty/garbage string.
    """
    _load_dotenv_once()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "text/plain",
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            # A retired model returns a plain 404 ("no longer available"),
            # which never contains "MODEL_NOT_FOUND" — so match on the status
            # code too, or the fallback can't fire when it's actually needed.
            if (e.code in _FALLBACK_HTTP_CODES or "MODEL_NOT_FOUND" in body) and model == GEMINI_MODEL:
                return gemini_call(prompt, temperature, max_tokens, model=GEMINI_FALLBACK)
            last_err = RuntimeError(f"Gemini HTTP {e.code}: {body[:200]}")
        except Exception as e:  # noqa: BLE001 - deliberately broad, re-raised below
            last_err = e
        if attempt < 3:
            time.sleep(3 * attempt)

    raise RuntimeError(f"Gemini API failed after 3 attempts: {last_err}")


def gemini_json(prompt: str, temperature: float = 0.4) -> dict | list:
    """Call Gemini and parse a JSON object/array out of the response,
    stripping markdown code fences if the model added them anyway.
    """
    text = gemini_call(prompt, temperature=temperature, max_tokens=8192)
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]+\}|\[[\s\S]+\])", text)
        if m:
            return json.loads(m.group(1))
        raise ValueError(f"Could not parse JSON from Gemini response:\n{text[:500]}")
