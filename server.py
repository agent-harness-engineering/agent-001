"""Agent-001: Model-agnostic ecosystem orchestrator.

4M governed. Loads governance modules at startup and injects them
as system context for inference dispatch.
"""

import json
import logging
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-001")

BASE_DIR = Path(__file__).parent
HOST = os.getenv("AGENT001_HOST", "0.0.0.0")
PORT = int(os.getenv("AGENT001_PORT", "8088"))
MEMORY_DIR = BASE_DIR / "memory"
ENDPOINTS_FILE = MEMORY_DIR / "endpoints.json"

# 4M module files
GOVERNANCE_MODULES = ["mission.md", "mind.md", "morals.md", "memory_module.md"]


# ---------------------------------------------------------------------------
# Governance loader
# ---------------------------------------------------------------------------

def load_governance() -> str:
    """Read all 4M modules and assemble into a single system prompt."""
    sections = []
    for filename in GOVERNANCE_MODULES:
        path = BASE_DIR / filename
        if path.exists():
            content = path.read_text().strip()
            sections.append(content)
            log.info("Loaded governance module: %s (%d chars)", filename, len(content))
        else:
            log.warning("Governance module not found: %s", filename)
    return "\n\n---\n\n".join(sections)


def load_endpoints() -> dict:
    """Load registered endpoints from disk."""
    if ENDPOINTS_FILE.exists():
        try:
            return json.loads(ENDPOINTS_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt endpoints.json, starting fresh")
    return {}


def save_endpoints(endpoints: dict) -> None:
    """Persist endpoints to disk."""
    ENDPOINTS_FILE.write_text(json.dumps(endpoints, indent=2))


# ---------------------------------------------------------------------------
# Inference dispatch
# ---------------------------------------------------------------------------

async def dispatch_to_endpoint(
    session: aiohttp.ClientSession,
    endpoint: dict,
    system_prompt: str,
    user_message: str,
) -> dict:
    """Send a chat completion request to an OpenAI-compatible endpoint."""
    url = endpoint["url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if endpoint.get("api_key"):
        headers["Authorization"] = f"Bearer {endpoint['api_key']}"

    payload = {
        "model": endpoint.get("model", endpoint["name"]),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": endpoint.get("temperature", 0.7),
        "max_tokens": endpoint.get("max_tokens", 2048),
    }

    timeout = aiohttp.ClientTimeout(
        total=endpoint.get("timeout_seconds", 300)
    )

    async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
        if resp.status != 200:
            body = await resp.text()
            return {"error": f"Endpoint returned {resp.status}: {body}"}
        data = await resp.json()
        return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def handle_chat(request: web.Request) -> web.Response:
    """Chat endpoint: accepts a message + history, dispatches to model with 4M governance."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    message = body.get("message", "").strip()
    if not message:
        return web.json_response({"error": "Required field: message"}, status=400)

    history = body.get("history", [])
    state = request.app["state"]

    # Select endpoint
    if not state["endpoints"]:
        return web.json_response({
            "error": "No model endpoints registered. Use /endpoints to register one.",
        }, status=503)

    endpoint_name = next(iter(state["endpoints"]))
    endpoint = state["endpoints"][endpoint_name]

    # Build messages: system prompt + conversation history
    messages = [{"role": "system", "content": state["system_prompt"]}]
    for entry in history:
        role = entry.get("role", "user")
        content = entry.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # If the last history entry isn't the current message, add it
    if not messages or messages[-1].get("content") != message:
        messages.append({"role": "user", "content": message})

    url = endpoint["url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if endpoint.get("api_key"):
        headers["Authorization"] = f"Bearer {endpoint['api_key']}"

    payload = {
        "model": endpoint.get("model", endpoint["name"]),
        "messages": messages,
        "temperature": endpoint.get("temperature", 0.7),
        "max_tokens": endpoint.get("max_tokens", 2048),
    }

    timeout = aiohttp.ClientTimeout(total=endpoint.get("timeout_seconds", 300))

    try:
        async with request.app["http_session"].post(
            url, json=payload, headers=headers, timeout=timeout, ssl=False
        ) as resp:
            if resp.status != 200:
                error_body = await resp.text()
                log.error("Chat dispatch failed: %s %s", resp.status, error_body)
                return web.json_response({"error": f"Model returned {resp.status}"}, status=502)
            data = await resp.json()
            choices = data.get("choices", [])
            reply = choices[0]["message"]["content"] if choices else "(no response)"
            return web.json_response({"response": reply, "endpoint": endpoint_name})
    except aiohttp.ClientError as e:
        log.error("Chat connection error: %s", e)
        return web.json_response({"error": f"Connection error: {e}"}, status=502)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({
        "status": "ok",
        "agent": "agent-001",
        "version": "0.2.0",
        "governance": "4M",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def handle_status(request: web.Request) -> web.Response:
    """System status: registered endpoints, active tasks."""
    state = request.app["state"]
    return web.json_response({
        "endpoints": {
            name: {
                "url": ep["url"],
                "model": ep.get("model", name),
                "capabilities": ep.get("capabilities", []),
            }
            for name, ep in state["endpoints"].items()
        },
        "active_tasks": len(state["tasks"]),
        "governance_loaded": state["governance_loaded"],
        "uptime_seconds": (
            datetime.now(timezone.utc) - state["started_at"]
        ).total_seconds(),
    })


async def handle_endpoints_list(request: web.Request) -> web.Response:
    """List registered endpoints."""
    return web.json_response(request.app["state"]["endpoints"])


async def handle_endpoint_register(request: web.Request) -> web.Response:
    """Register a new model endpoint."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = body.get("name")
    url = body.get("url")
    if not name or not url:
        return web.json_response(
            {"error": "Required fields: name, url"}, status=400
        )

    endpoint = {
        "name": name,
        "url": url,
        "model": body.get("model", name),
        "api_key": body.get("api_key", ""),
        "capabilities": body.get("capabilities", ["chat"]),
        "temperature": body.get("temperature", 0.7),
        "max_tokens": body.get("max_tokens", 2048),
        "timeout_seconds": body.get("timeout_seconds", 300),
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }

    state = request.app["state"]
    state["endpoints"][name] = endpoint
    save_endpoints(state["endpoints"])

    log.info("Endpoint registered: %s -> %s (model: %s)", name, url, endpoint["model"])
    return web.json_response({
        "status": "registered",
        "endpoint": name,
    }, status=201)


async def handle_endpoint_delete(request: web.Request) -> web.Response:
    """Remove a registered endpoint."""
    name = request.match_info["name"]
    state = request.app["state"]

    if name not in state["endpoints"]:
        return web.json_response({"error": f"Endpoint '{name}' not found"}, status=404)

    del state["endpoints"][name]
    save_endpoints(state["endpoints"])
    log.info("Endpoint removed: %s", name)
    return web.json_response({"status": "removed", "endpoint": name})


async def handle_task_submit(request: web.Request) -> web.Response:
    """Accept a task for orchestration. Dispatches to the best available endpoint."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    description = body.get("description")
    if not description:
        return web.json_response({"error": "Required field: description"}, status=400)

    task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    state = request.app["state"]

    # Select endpoint: prefer explicitly named, else first available
    endpoint_name = body.get("endpoint")
    if endpoint_name and endpoint_name in state["endpoints"]:
        endpoint = state["endpoints"][endpoint_name]
    elif state["endpoints"]:
        endpoint_name = next(iter(state["endpoints"]))
        endpoint = state["endpoints"][endpoint_name]
    else:
        # O4: fail loud
        log.warning("Task %s rejected: no endpoints registered", task_id)
        return web.json_response({
            "error": "No model endpoints registered. POST /endpoints to register one.",
            "task_id": task_id,
        }, status=503)

    # O2: audit trail
    task_record = {
        "id": task_id,
        "description": description,
        "endpoint": endpoint_name,
        "status": "dispatching",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    state["tasks"][task_id] = task_record
    log.info("Task %s dispatching to %s", task_id, endpoint_name)

    # Dispatch
    try:
        result = await dispatch_to_endpoint(
            request.app["http_session"],
            endpoint,
            state["system_prompt"],
            description,
        )

        if "error" in result:
            task_record["status"] = "failed"
            task_record["error"] = result["error"]
            log.error("Task %s failed: %s", task_id, result["error"])
            return web.json_response({
                "task_id": task_id,
                "status": "failed",
                "error": result["error"],
            }, status=502)

        # Extract response
        choices = result.get("choices", [])
        response_text = ""
        if choices:
            response_text = choices[0].get("message", {}).get("content", "")

        task_record["status"] = "completed"
        task_record["completed_at"] = datetime.now(timezone.utc).isoformat()
        task_record["response_length"] = len(response_text)
        log.info("Task %s completed (%d chars)", task_id, len(response_text))

        return web.json_response({
            "task_id": task_id,
            "status": "completed",
            "endpoint": endpoint_name,
            "response": response_text,
        })

    except aiohttp.ClientError as e:
        task_record["status"] = "failed"
        task_record["error"] = str(e)
        log.error("Task %s connection error: %s", task_id, e)
        return web.json_response({
            "task_id": task_id,
            "status": "failed",
            "error": f"Connection error: {e}",
        }, status=502)


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
                        "description": {
                            "type": "string",
                            "description": "Task description",
                        },
                        "endpoint": {
                            "type": "string",
                            "description": "Target endpoint name (optional)",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "normal", "high"],
                        },
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "list_endpoints",
                "description": "List registered model endpoints",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "register_endpoint",
                "description": "Register a new model endpoint",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "model": {"type": "string"},
                    },
                    "required": ["name", "url"],
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
    # Load 4M governance as system prompt
    system_prompt = load_governance()
    endpoints = load_endpoints()

    app["state"] = {
        "system_prompt": system_prompt,
        "governance_loaded": bool(system_prompt),
        "endpoints": endpoints,
        "tasks": {},
        "started_at": datetime.now(timezone.utc),
    }

    # HTTP session for outbound requests to model endpoints
    app["http_session"] = aiohttp.ClientSession()

    log.info(
        "Agent-001 v0.2.0 started on %s:%s | governance: %d chars | endpoints: %s",
        HOST, PORT,
        len(system_prompt),
        list(endpoints.keys()) or "(none)",
    )


async def on_cleanup(app: web.Application) -> None:
    await app["http_session"].close()
    log.info("Agent-001 shutting down")


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Core routes
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)

    # Chat
    app.router.add_post("/chat", handle_chat)

    # Endpoint management
    app.router.add_get("/endpoints", handle_endpoints_list)
    app.router.add_post("/endpoints", handle_endpoint_register)
    app.router.add_delete("/endpoints/{name}", handle_endpoint_delete)

    # Task dispatch
    app.router.add_post("/task", handle_task_submit)

    # MCP
    app.router.add_get("/mcp/tools", handle_mcp_tools)

    # Static files (chat UI) — must be last
    static_dir = BASE_DIR / "static"
    app.router.add_static("/static/", static_dir)
    app.router.add_get("/", lambda r: web.FileResponse(static_dir / "index.html"))

    return app


def make_ssl_context() -> ssl.SSLContext | None:
    """Load Tailscale TLS certs if available."""
    cert_dir = BASE_DIR / "certs"
    cert_file = cert_dir / "thinxai-workstation.crt"
    key_file = cert_dir / "thinxai-workstation.key"
    if cert_file.exists() and key_file.exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        log.info("TLS enabled via Tailscale certs")
        return ctx
    log.warning("No TLS certs found, running plain HTTP")
    return None


if __name__ == "__main__":
    ssl_ctx = make_ssl_context()
    web.run_app(create_app(), host=HOST, port=PORT, ssl_context=ssl_ctx)
