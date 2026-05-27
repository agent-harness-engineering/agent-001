"""HTTP health probe for declared ecosystem services.

Hits each `(name, url)` for HTTP status + round-trip latency in parallel.
Used by status/sysadmin agents to ground their reports in real readings
instead of inventing UNKNOWN cells. Public-side (Cloudflare-fronted) URLs
only — no creds, no auth, just GET / and read the response code.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import aiohttp

log = logging.getLogger("agent-001.health_probe")

# Declared ecosystem service surface — public-side only. Source of truth for
# this list is mission.md's tier table; if you add or rename a service there,
# update here too. Probes are HEAD/GET on `/` — auth-protected services like
# Keycloak return 200/302/303, all considered UP.
DEFAULT_TARGETS: list[tuple[str, str]] = [
    # Chatbot tier
    ("chatbot", "https://chatbot.telogos.ai/"),
    ("devbot", "https://devbot.telogos.ai/"),
    ("ng", "https://ng.telogos.ai/"),
    ("claudedev", "https://claudedev.telogos.ai/"),
    # Office stack
    ("auth", "https://auth.telogos.ai/"),
    ("chat", "https://chat.telogos.ai/"),
    ("files", "https://files.telogos.ai/"),
    ("collabora", "https://collabora.telogos.ai/"),
    ("git", "https://git.telogos.ai/"),
    ("blog", "https://blog.telogos.ai/"),
    ("www", "https://www.telogos.ai/"),
    ("portal", "https://portal.telogos.ai/"),
    ("ologosai", "https://ologosai.telogos.ai/"),
]


async def _probe_one(
    session: aiohttp.ClientSession,
    name: str,
    url: str,
    timeout: float = 5.0,
) -> dict:
    start = time.monotonic()
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
            ssl=True,
        ) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            # 2xx and 3xx (auth redirects on Keycloak/Nextcloud) all count as UP
            up = 200 <= resp.status < 400
            return {
                "name": name,
                "url": url,
                "up": up,
                "status": resp.status,
                "latency_ms": elapsed_ms,
                "error": None,
            }
    except asyncio.TimeoutError:
        return {
            "name": name, "url": url, "up": False, "status": None,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": "timeout",
        }
    except aiohttp.ClientError as exc:
        return {
            "name": name, "url": url, "up": False, "status": None,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:120],
        }


async def probe_http(
    session: aiohttp.ClientSession,
    targets: Iterable[tuple[str, str]] | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Probe each target in parallel. Always returns a result per target."""
    targets = list(targets) if targets else DEFAULT_TARGETS
    return await asyncio.gather(
        *(_probe_one(session, name, url, timeout=timeout) for name, url in targets)
    )


def format_health_table(results: list[dict]) -> str:
    """Render probe results as a markdown table."""
    if not results:
        return "_(no health probe results)_"
    lines = ["| Service | Status | HTTP | Latency | Notes |",
             "|---|---|---|---|---|"]
    for r in results:
        verdict = "UP" if r["up"] else "DOWN"
        http = str(r["status"]) if r["status"] is not None else "—"
        latency = f"{r['latency_ms']} ms"
        notes = r["error"] or ""
        lines.append(f"| {r['name']} | **{verdict}** | {http} | {latency} | {notes} |")
    up_count = sum(1 for r in results if r["up"])
    lines.append(f"\n**Summary:** {up_count}/{len(results)} UP.")
    return "\n".join(lines)
