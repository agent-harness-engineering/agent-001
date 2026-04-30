"""Docker introspection via the host docker.sock (mounted into the container).

Returns a structured snapshot of running containers — name, image, status,
uptime, brief CPU/mem hint when available. Read-only operations only:
`list` and `stats`. We do NOT call `start`, `stop`, `restart`, `remove`,
or `exec` — even though the socket would allow it. If/when those are
needed, they belong behind an explicit operator-confirmation gate, not
inside an autonomous agent's context.

Failure mode: if the socket isn't mounted (local dev) or the docker
daemon is unreachable, returns `{"available": False, "error": "..."}`
and callers fall through gracefully — never raises.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("agent-001.docker_probe")

_DOCKER_AVAILABLE = None  # cached


def _try_import_client():
    try:
        import docker  # type: ignore
        return docker
    except ImportError:
        log.warning("docker SDK not installed — container introspection disabled")
        return None


def _list_containers_blocking() -> dict[str, Any]:
    """Run `docker ps`-equivalent. Synchronous — wrap with run_in_executor."""
    docker_mod = _try_import_client()
    if docker_mod is None:
        return {"available": False, "error": "docker SDK not installed"}
    try:
        client = docker_mod.from_env(timeout=5)
        client.ping()
    except Exception as exc:  # noqa: BLE001 — daemon may be down or socket missing
        return {"available": False, "error": f"docker daemon unreachable: {exc}"[:160]}

    try:
        containers = client.containers.list(all=False)
        out = []
        for c in containers:
            attrs = c.attrs
            state = attrs.get("State", {}) or {}
            image_tags = (attrs.get("Config", {}) or {}).get("Image", "") or ""
            started_at = state.get("StartedAt", "") or ""
            health = (state.get("Health") or {}).get("Status", "") or ""
            out.append({
                "name": c.name,
                "image": image_tags,
                "status": c.status,                  # running/exited/...
                "state": state.get("Status", ""),    # raw daemon state
                "started_at": started_at[:19],       # truncate fractional/zone
                "health": health,
                "id_short": c.short_id,
            })
        return {"available": True, "count": len(out), "containers": out}
    except Exception as exc:  # noqa: BLE001
        log.warning("docker.list failed: %s", exc)
        return {"available": False, "error": f"list failed: {exc}"[:160]}


async def list_containers() -> dict[str, Any]:
    """Async wrapper — runs blocking docker SDK in default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _list_containers_blocking)


def format_docker_table(snapshot: dict[str, Any], max_rows: int = 20) -> str:
    """Render a docker snapshot as markdown, capped at `max_rows` rows."""
    if not snapshot.get("available"):
        return f"_Docker introspection unavailable — {snapshot.get('error', 'unknown')}_"
    containers = snapshot.get("containers") or []
    if not containers:
        return "_No running containers reported by docker daemon._"
    # Stable order — alphabetical by name
    containers = sorted(containers, key=lambda c: c["name"])
    shown = containers[:max_rows]
    overflow = len(containers) - len(shown)
    lines = ["| Container | Image | State | Health |",
             "|---|---|---|---|"]
    for c in shown:
        # Tighter image trim for compact context
        image_short = c["image"].split("/")[-1][:24]
        health = c["health"] or "—"
        lines.append(f"| {c['name'][:32]} | {image_short} | {c['status']} | {health} |")
    summary = f"\n**Summary:** {snapshot['count']} running container(s)."
    if overflow > 0:
        summary += f" Showing first {len(shown)} alphabetically; +{overflow} more not shown."
    lines.append(summary)
    return "\n".join(lines)
