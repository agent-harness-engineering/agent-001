"""Agent-001: Model-agnostic ecosystem orchestrator.

4M governed. Loads governance modules at startup and injects them
as system context for inference dispatch.

P0 safety layer: ToolProxy → PermissionGate → AuditStore → GovernanceGuard
wired via /v1/process, /v1/audit, and /v1/health endpoints.
"""

import asyncio
import json
import logging
import os
import ssl
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from audit import AuditStore, AuditEvent
from gates import PermissionGate, PermissionPolicy, PermissionDecision
from governance import GovernanceGuard
from proxy import ToolProxy, CanonicalToolCall, ValidationError
from runner import AgentRunner, AGENT_TYPES, StatusStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-001")

# ---------------------------------------------------------------------------
# In-process notification bus (ThinxS pattern)
# ---------------------------------------------------------------------------
_notifications: list[dict] = []          # last 50, newest at end
_notification_queues: set[asyncio.Queue] = set()


def push_notification(note: dict) -> None:
    """Broadcast a notification to all connected chat SSE clients."""
    _notifications.append(note)
    if len(_notifications) > 50:
        _notifications.pop(0)
    for q in list(_notification_queues):
        try:
            q.put_nowait(note)
        except asyncio.QueueFull:
            pass
    log.info("Notification [%s] agent=%s: %s",
             note.get("level", "info"), note.get("agent_id", ""), note.get("message", "")[:80])

BASE_DIR = Path(__file__).parent
HOST = os.getenv("AGENT001_HOST", "0.0.0.0")
PORT = int(os.getenv("AGENT001_PORT", "8095"))
MEMORY_DIR = BASE_DIR / "memory"
ENDPOINTS_FILE = MEMORY_DIR / "endpoints.json"
HISTORY_DIR = BASE_DIR / "history"
AGENTS_DIR = BASE_DIR / "memory" / "agents"

# 4M module files
GOVERNANCE_MODULES = ["mission.md", "mind.md", "morals.md", "memory_module.md"]

# P0 configuration
_AUDIT_LOG_REL = os.getenv("MAESTRO_AUDIT_LOG", "audit/maestro-audit.jsonl")
AUDIT_LOG_PATH = BASE_DIR / _AUDIT_LOG_REL
MAESTRO_DEFAULT_POLICY = os.getenv("MAESTRO_DEFAULT_POLICY", "default")


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


def load_governance_compact() -> str:
    """Read governance_compact.md for small-context endpoints (e.g. Gemma n_ctx=4096)."""
    path = BASE_DIR / "governance_compact.md"
    if path.exists():
        content = path.read_text().strip()
        log.info("Loaded compact governance: %d chars", len(content))
        return content
    log.warning("governance_compact.md not found, falling back to full governance")
    return ""


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

    # Build messages: select governance tier based on endpoint config
    governance_tier = endpoint.get("governance", "full")
    sys_prompt = state["compact_prompt"] if governance_tier == "compact" else state["system_prompt"]
    messages = [{"role": "system", "content": sys_prompt}]
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
            _log_chat(request, message, reply, endpoint_name)
            return web.json_response({"response": reply, "endpoint": endpoint_name})
    except aiohttp.ClientError as e:
        log.error("Chat connection error: %s", e)
        return web.json_response({"error": f"Connection error: {e}"}, status=502)


def _log_chat(request: web.Request, user_msg: str, assistant_reply: str, endpoint: str) -> None:
    """Append a chat turn to history/chat-YYYY-MM-DD.jsonl."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = HISTORY_DIR / f"chat-{date}.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "remote": request.remote,
            "endpoint": endpoint,
            "user": user_msg,
            "assistant": assistant_reply,
        }
        with log_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        log.warning("Failed to write chat history: %s", exc)


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
        "governance": body.get("governance", "full"),
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


async def handle_task_list(request: web.Request) -> web.Response:
    """GET /tasks — list all tasks with their current status."""
    state = request.app["state"]
    tasks = list(state["tasks"].values())
    tasks.sort(key=lambda t: t.get("submitted_at", ""), reverse=True)
    return web.json_response({"tasks": tasks})


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
    governance_tier = endpoint.get("governance", "full")
    task_sys_prompt = state["compact_prompt"] if governance_tier == "compact" else state["system_prompt"]
    try:
        result = await dispatch_to_endpoint(
            request.app["http_session"],
            endpoint,
            task_sys_prompt,
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
# P0 endpoints
# ---------------------------------------------------------------------------

async def handle_v1_process(request: web.Request) -> web.Response:
    """POST /v1/process — run model output through P0 safety pipeline.

    Request body::

        {
            "session_id": "...",
            "model_output": "..." | {...},
            "model_format": "auto" | "openai" | "anthropic" | "action_tag",
            "agent_id": "..."
        }

    Response 200::

        {
            "tool_calls": [...],   # approved CanonicalToolCall dicts
            "blocked":   [...],   # ValidationError or denied CanonicalToolCall dicts
            "decisions": [...]    # PermissionDecision dicts
        }

    Pipeline: ToolProxy → PermissionGate → AuditStore.
    GovernanceGuard is consulted before any write is allowed.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    session_id = body.get("session_id", f"sess_{uuid.uuid4().hex[:8]}")
    model_output = body.get("model_output")
    model_format = body.get("model_format", "auto")
    agent_id = body.get("agent_id", "agent-001")

    if model_output is None:
        return web.json_response({"error": "Required field: model_output"}, status=400)

    p0 = request.app["p0"]
    tool_proxy: ToolProxy = p0["tool_proxy"]
    permission_gate: PermissionGate = p0["permission_gate"]
    audit_store: AuditStore = p0["audit_store"]
    governance_guard: GovernanceGuard = p0["governance_guard"]

    # Step 1: ToolProxy — parse and validate model output
    proxy_results = tool_proxy.process(model_output, source_hint=model_format)

    tool_calls_out = []
    blocked_out = []
    decisions_out = []

    for item in proxy_results:
        if isinstance(item, ValidationError):
            # Record validation error to audit
            audit_store.record(AuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="validation_error",
                session_id=session_id,
                agent_id=agent_id,
                tool_name=None,
                tool_call_id=None,
                decision="deny",
                outcome="blocked",
                details={
                    "error_type": item.error_type,
                    "message": item.message,
                    "raw": item.raw[:512],  # truncate for log safety
                },
            ))
            blocked_out.append({
                "type": "validation_error",
                "error_type": item.error_type,
                "message": item.message,
                "timestamp": item.timestamp,
            })
            continue

        # item is a CanonicalToolCall
        call: CanonicalToolCall = item

        # Step 2: GovernanceGuard check for write-like operations
        # For write operations we do a governance guard check first
        # (the guard will audit its own denials internally)
        if call.tool_name.startswith("write") or "write" in call.tool_name.lower():
            target = call.parameters.get("path", call.parameters.get("file", ""))
            if target and not governance_guard.check_write(target, session_id=session_id):
                decision = PermissionDecision.deny(
                    tool_name=call.tool_name,
                    tool_call_id=call.id,
                    reason="governance_guard",
                    channel="system",
                    approver="system",
                )
                decisions_out.append(asdict(decision))
                blocked_out.append({
                    "type": "governance_blocked",
                    "tool_name": call.tool_name,
                    "tool_call_id": call.id,
                    "reason": "governance_guard: write to protected path denied",
                    "timestamp": decision.timestamp,
                })
                continue

        # Step 3: PermissionGate evaluation
        decision: PermissionDecision = permission_gate.evaluate(
            tool_name=call.tool_name,
            tool_call_id=call.id,
            parameters=call.parameters,
        )

        # Step 4: Record tool_call event to audit
        audit_store.record(AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="tool_call",
            session_id=session_id,
            agent_id=agent_id,
            tool_name=call.tool_name,
            tool_call_id=call.id,
            decision=decision.action,
            outcome="success" if decision.action == "allow" else "blocked",
            details={
                "source": call.source,
                "parameters": call.parameters,
                "gate_reason": decision.reason,
                "gate_channel": decision.channel,
            },
        ))

        decisions_out.append(asdict(decision))

        if decision.action == "allow":
            tool_calls_out.append({
                "id": call.id,
                "tool_name": call.tool_name,
                "parameters": call.parameters,
                "source": call.source,
                "timestamp": call.timestamp,
            })
        else:
            blocked_out.append({
                "type": "permission_denied",
                "tool_name": call.tool_name,
                "tool_call_id": call.id,
                "reason": decision.reason,
                "timestamp": decision.timestamp,
            })

    return web.json_response({
        "tool_calls": tool_calls_out,
        "blocked": blocked_out,
        "decisions": decisions_out,
    })


async def handle_v1_audit(request: web.Request) -> web.Response:
    """GET /v1/audit?session_id=X&event_type=Y&limit=50

    Query the audit log. All parameters optional.
    """
    session_id = request.rel_url.query.get("session_id") or None
    event_type = request.rel_url.query.get("event_type") or None
    try:
        limit = int(request.rel_url.query.get("limit", "50"))
    except (ValueError, TypeError):
        limit = 50

    audit_store: AuditStore = request.app["p0"]["audit_store"]
    events = audit_store.query(
        session_id=session_id,
        event_type=event_type,
        limit=limit,
    )

    return web.json_response({
        "events": [asdict(e) for e in events],
    })


async def handle_v1_health(request: web.Request) -> web.Response:
    """GET /v1/health — component-level liveness check."""
    p0 = request.app["p0"]
    components = {}

    # proxy: always available after startup
    components["proxy"] = "ok" if p0.get("tool_proxy") is not None else "unavailable"

    # gate: always available after startup
    components["gate"] = "ok" if p0.get("permission_gate") is not None else "unavailable"

    # audit: check log parent dir is writable
    audit_store: AuditStore = p0.get("audit_store")
    if audit_store is not None:
        try:
            audit_store.log_path.parent.mkdir(parents=True, exist_ok=True)
            components["audit"] = "ok"
        except Exception:
            components["audit"] = "error"
    else:
        components["audit"] = "unavailable"

    # governance: check guard is initialised
    components["governance"] = "ok" if p0.get("governance_guard") is not None else "unavailable"

    overall = "ok" if all(v == "ok" for v in components.values()) else "degraded"
    return web.json_response({
        "status": overall,
        "components": components,
    })


# ---------------------------------------------------------------------------
# Notification routes
# ---------------------------------------------------------------------------

async def handle_notify(request: web.Request) -> web.Response:
    """POST /api/notify — push a notification into all active chat sessions."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    note = {
        "id": uuid.uuid4().hex[:8],
        "agent_id": str(body.get("agent_id", "")),
        "task": str(body.get("task", "")),
        "status": str(body.get("status", "info")),
        "message": str(body.get("message", "")),
        "summary": str(body.get("summary", "")),
        "level": str(body.get("level", "info")),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    push_notification(note)
    return web.json_response({"ok": True, "id": note["id"]})


async def handle_notifications_poll(request: web.Request) -> web.Response:
    """GET /api/notifications?since=<unix_ts> — polling fallback when SSE unavailable."""
    try:
        since = float(request.rel_url.query.get("since", "0"))
    except (ValueError, TypeError):
        since = 0.0
    notes = []
    for n in _notifications:
        try:
            if datetime.fromisoformat(n["ts"]).timestamp() > since:
                notes.append(n)
        except Exception:
            pass
    return web.json_response({"notifications": notes})


async def handle_notification_stream(request: web.Request) -> web.Response:
    """GET /api/notifications/stream — SSE stream of agent completion notifications."""
    q: asyncio.Queue = asyncio.Queue(maxsize=20)
    _notification_queues.add(q)

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
    })
    await resp.prepare(request)
    log.info("Notification SSE connected: %s", request.remote)

    # Send any buffered notifications from the last minute
    cutoff = (datetime.now(timezone.utc).timestamp() - 60)
    for n in _notifications:
        try:
            ts = datetime.fromisoformat(n["ts"]).timestamp()
            if ts >= cutoff:
                await resp.write(f"data: {json.dumps(n)}\n\n".encode())
        except Exception:
            pass

    try:
        while True:
            try:
                note = await asyncio.wait_for(q.get(), timeout=25.0)
                await resp.write(f"data: {json.dumps(note)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _notification_queues.discard(q)
        await resp.write_eof()

    return resp


# ---------------------------------------------------------------------------
# Agent runner routes
# ---------------------------------------------------------------------------

async def handle_agent_spawn(request: web.Request) -> web.Response:
    """POST /api/agents/spawn — create a background agent job.

    Body: { "prompt": "...", "agent_type": "general|research|sysadmin|status", "endpoint": "name" }
    Returns: { "agent_id": "...", "status": "queued" }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    prompt = body.get("prompt", "").strip()
    if not prompt:
        return web.json_response({"error": "Required field: prompt"}, status=400)

    agent_type = body.get("agent_type", "general")
    endpoint_name = body.get("endpoint")
    runner: AgentRunner = request.app["runner"]

    try:
        agent_id = runner.spawn(prompt=prompt, agent_type=agent_type, endpoint_name=endpoint_name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=503)

    return web.json_response({"agent_id": agent_id, "status": "queued"}, status=201)


async def handle_agent_list(request: web.Request) -> web.Response:
    """GET /api/agents — list all agents (live + completed from disk)."""
    store: StatusStore = request.app["status_store"]
    agents = store.list_all()

    # Overlay live status for running agents
    result = []
    for a in agents:
        live = AgentRunner.get_live(a.agent_id)
        d = a.to_dict()
        if live:
            d["status"] = live["status"].status
        result.append(d)

    return web.json_response({"agents": result, "types": list(AGENT_TYPES.keys())})


async def handle_agent_stream(request: web.Request) -> web.Response:
    """GET /api/agents/{agent_id}/stream — SSE event stream for a live agent."""
    agent_id = request.match_info["agent_id"]
    live = AgentRunner.get_live(agent_id)

    if not live:
        # Agent finished — return stored status as a single done event
        store: StatusStore = request.app["status_store"]
        status = store.load(agent_id)
        if not status:
            return web.json_response({"error": "Agent not found"}, status=404)
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        payload = json.dumps({"status": status.status, "output": status.output or "", "event_type": "done"})
        await resp.write(f"data: {payload}\n\n".encode())
        await resp.write_eof()
        return resp

    queue: asyncio.Queue = live["queue"]
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
    })
    await resp.prepare(request)

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                # Keepalive comment
                await resp.write(b": keepalive\n\n")
                continue

            payload = json.dumps(event)
            await resp.write(f"data: {payload}\n\n".encode())

            if event.get("event_type") == "done":
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        await resp.write_eof()

    return resp


async def handle_agent_cancel(request: web.Request) -> web.Response:
    """POST /api/agents/{agent_id}/cancel — signal a running agent to stop."""
    agent_id = request.match_info["agent_id"]
    if AgentRunner.cancel(agent_id):
        return web.json_response({"status": "cancelling", "agent_id": agent_id})
    return web.json_response({"error": "Agent not found or already finished"}, status=404)


async def handle_agent_types(request: web.Request) -> web.Response:
    """GET /api/agent-types — list available agent types."""
    return web.json_response({
        name: {"description": cfg.description, "capabilities": cfg.capabilities}
        for name, cfg in AGENT_TYPES.items()
    })


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    # Load 4M governance as system prompt
    system_prompt = load_governance()
    compact_prompt = load_governance_compact() or system_prompt
    endpoints = load_endpoints()

    app["state"] = {
        "system_prompt": system_prompt,
        "compact_prompt": compact_prompt,
        "governance_loaded": bool(system_prompt),
        "endpoints": endpoints,
        "tasks": {},
        "started_at": datetime.now(timezone.utc),
    }

    # HTTP session for outbound requests to model endpoints
    app["http_session"] = aiohttp.ClientSession()

    # -----------------------------------------------------------------------
    # Initialise P0 safety components
    # -----------------------------------------------------------------------
    audit_store = AuditStore(AUDIT_LOG_PATH)

    governance_guard = GovernanceGuard(repo_root=BASE_DIR, audit_store=audit_store)

    # Tool registry: populated from endpoints.json tool definitions (if present).
    # Falls back to empty registry — every unknown call is a ValidationError.
    tool_registry: dict = {}
    tool_registry_file = MEMORY_DIR / "tool_registry.json"
    if tool_registry_file.exists():
        try:
            tool_registry = json.loads(tool_registry_file.read_text())
            log.info("Loaded tool registry: %d tools", len(tool_registry))
        except json.JSONDecodeError:
            log.warning("Corrupt tool_registry.json — starting with empty registry")

    # Permission policy: load from file if MAESTRO_DEFAULT_POLICY is a path,
    # else use the built-in default.
    if MAESTRO_DEFAULT_POLICY != "default" and Path(MAESTRO_DEFAULT_POLICY).exists():
        try:
            policy_data = json.loads(Path(MAESTRO_DEFAULT_POLICY).read_text())
            permission_policy = PermissionPolicy.from_dict(policy_data)
            log.info("Loaded permission policy from %s", MAESTRO_DEFAULT_POLICY)
        except Exception as exc:
            log.warning("Failed to load policy from %s: %s — using default", MAESTRO_DEFAULT_POLICY, exc)
            permission_policy = PermissionPolicy.default()
    else:
        permission_policy = PermissionPolicy.default()

    # Wire audit callbacks
    def gate_audit_callback(decision: PermissionDecision) -> None:
        audit_store.record(AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="permission_decision",
            session_id="system",
            agent_id="permission_gate",
            tool_name=decision.tool_name,
            tool_call_id=decision.tool_call_id,
            decision=decision.action,
            outcome="allow" if decision.action == "allow" else "blocked",
            details={
                "reason": decision.reason,
                "channel": decision.channel,
                "approver": decision.approver,
            },
        ))

    def proxy_audit_callback(result: CanonicalToolCall | ValidationError) -> None:
        if isinstance(result, CanonicalToolCall):
            audit_store.record(AuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="tool_call",
                session_id="system",
                agent_id="tool_proxy",
                tool_name=result.tool_name,
                tool_call_id=result.id,
                decision=None,
                outcome="parsed",
                details={"source": result.source},
            ))
        else:
            audit_store.record(AuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="validation_error",
                session_id="system",
                agent_id="tool_proxy",
                tool_name=None,
                tool_call_id=None,
                decision="deny",
                outcome="blocked",
                details={"error_type": result.error_type, "message": result.message},
            ))

    permission_gate = PermissionGate(
        policy=permission_policy,
        channels=[],  # no interactive TTY in server context; fail-closed on escalate
        audit_callback=gate_audit_callback,
    )

    tool_proxy = ToolProxy(
        tool_registry=tool_registry,
        audit_callback=proxy_audit_callback,
    )

    app["p0"] = {
        "audit_store": audit_store,
        "governance_guard": governance_guard,
        "tool_proxy": tool_proxy,
        "permission_gate": permission_gate,
        "permission_policy": permission_policy,
        "tool_registry": tool_registry,
    }

    log.info(
        "Agent-001 v0.2.0 started on %s:%s | governance: %d chars | endpoints: %s",
        HOST, PORT,
        len(system_prompt),
        list(endpoints.keys()) or "(none)",
    )
    log.info(
        "P0 safety layer active | audit_log: %s | tool_registry: %d tools | policy: %s",
        AUDIT_LOG_PATH,
        len(tool_registry),
        MAESTRO_DEFAULT_POLICY,
    )

    # -----------------------------------------------------------------------
    # Initialise agent runner (P1)
    # -----------------------------------------------------------------------
    status_store = StatusStore(AGENTS_DIR)

    async def _on_agent_complete(agent_id: str, status: str, summary: str) -> None:
        # 1. In-chat notification (pushes to all connected chat SSE streams)
        status_icon = {"completed": "Done", "failed": "Failed", "interrupted": "Stopped"}.get(status, status.title())
        note = {
            "id": uuid.uuid4().hex[:8],
            "agent_id": agent_id,
            "status": status,
            "message": f"**{status_icon}** `{agent_id}`" + (f": {summary[:160]}" if summary else ""),
            "summary": summary,
            "level": "success" if status == "completed" else "error",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        push_notification(note)

        # 2. Telegram (optional)
        tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            import urllib.parse
            text = f"Agent `{agent_id}` {status}\n{summary[:200]}"
            url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            params = urllib.parse.urlencode({"chat_id": tg_chat, "text": text, "parse_mode": "Markdown"})
            try:
                async with app["http_session"].post(url + "?" + params) as r:
                    pass
            except Exception as exc:
                log.warning("Telegram notify failed: %s", exc)

    agent_runner = AgentRunner(
        status_store=status_store,
        http_session=app["http_session"],
        endpoints=app["state"]["endpoints"],
        system_prompt=system_prompt,
        compact_prompt=compact_prompt,
        notify_callback=_on_agent_complete,
    )
    app["runner"] = agent_runner
    app["status_store"] = status_store
    log.info("Agent runner initialised | agents_dir: %s | types: %s",
             AGENTS_DIR, list(AGENT_TYPES.keys()))


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
    app.router.add_get("/tasks", handle_task_list)
    app.router.add_post("/task", handle_task_submit)

    # Agent runner (P1)
    app.router.add_post("/api/notify", handle_notify)
    app.router.add_get("/api/notifications", handle_notifications_poll)
    app.router.add_get("/api/notifications/stream", handle_notification_stream)
    app.router.add_post("/api/agents/spawn", handle_agent_spawn)
    app.router.add_get("/api/agents", handle_agent_list)
    app.router.add_get("/api/agents/{agent_id}/stream", handle_agent_stream)
    app.router.add_post("/api/agents/{agent_id}/cancel", handle_agent_cancel)
    app.router.add_get("/api/agent-types", handle_agent_types)

    # MCP
    app.router.add_get("/mcp/tools", handle_mcp_tools)

    # P0 safety layer
    app.router.add_post("/v1/process", handle_v1_process)
    app.router.add_get("/v1/audit", handle_v1_audit)
    app.router.add_get("/v1/health", handle_v1_health)

    # Static files (chat UI) — must be last
    static_dir = BASE_DIR / "static"
    app.router.add_static("/static/", static_dir)

    async def _index(r: web.Request) -> web.FileResponse:
        return web.FileResponse(
            static_dir / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
        )
    app.router.add_get("/", _index)

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
