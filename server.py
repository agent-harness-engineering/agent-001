"""Agent-001: Model-agnostic ecosystem orchestrator.

Minimal aiohttp shell. 4M governed.
"""

import json
import logging
import os
from datetime import datetime, timezone

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-001")

HOST = os.getenv("AGENT001_HOST", "0.0.0.0")
PORT = int(os.getenv("AGENT001_PORT", "8088"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({
        "status": "ok",
        "agent": "agent-001",
        "version": "0.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def handle_status(request: web.Request) -> web.Response:
    """System status: registered endpoints, active tasks."""
    state = request.app["state"]
    return web.json_response({
        "endpoints": list(state["endpoints"].keys()),
        "active_tasks": len(state["tasks"]),
        "uptime_seconds": (datetime.now(timezone.utc) - state["started_at"]).total_seconds(),
    })


async def handle_task_submit(request: web.Request) -> web.Response:
    """Accept a task for orchestration. Stub: logs and acknowledges."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    log.info("Task submitted: %s — %s", task_id, body.get("description", "(no description)"))

    # O2: audit trail
    state = request.app["state"]
    state["tasks"][task_id] = {
        "id": task_id,
        "description": body.get("description"),
        "status": "received",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    return web.json_response({
        "task_id": task_id,
        "status": "received",
        "message": "Task accepted. Orchestration not yet implemented (v0.1 shell).",
    }, status=202)


# ---------------------------------------------------------------------------
# MCP stub
# ---------------------------------------------------------------------------

async def handle_mcp_tools(request: web.Request) -> web.Response:
    """List available MCP tools. Stub for future integration."""
    return web.json_response({
        "tools": [
            {
                "name": "submit_task",
                "description": "Submit a task for orchestration by Agent-001",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string", "description": "Task description"},
                        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "get_status",
                "description": "Get Agent-001 system status",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
    })


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    app["state"] = {
        "endpoints": {},
        "tasks": {},
        "started_at": datetime.now(timezone.utc),
    }
    log.info("Agent-001 v0.1.0 started on %s:%s", HOST, PORT)


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)

    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/task", handle_task_submit)
    app.router.add_get("/mcp/tools", handle_mcp_tools)

    return app


if __name__ == "__main__":
    web.run_app(create_app(), host=HOST, port=PORT)
