"""AgentRunner — asyncio background agent dispatcher.

Model-agnostic equivalent of ThinxS's agent_runner.py.
Dispatches to any registered OpenAI-compatible endpoint,
streams events via asyncio.Queue, persists status to disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from .agent_types import AGENT_TYPES, AgentTypeConfig
from .status_store import AgentStatus, StatusStore

log = logging.getLogger("agent-001.runner")

# In-memory agent registry — maps agent_id → live agent dict while running.
# Completed agents are readable from StatusStore (disk).
_LIVE_AGENTS: dict[str, dict] = {}


def _gen_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"agent_{ts}_{uuid.uuid4().hex[:6]}"


class AgentRunner:
    def __init__(
        self,
        status_store: StatusStore,
        http_session: aiohttp.ClientSession,
        endpoints: dict,
        system_prompt: str,
        notify_callback=None,
    ) -> None:
        self._store = status_store
        self._http = http_session
        self._endpoints = endpoints
        self._system_prompt = system_prompt
        self._notify = notify_callback  # async fn(agent_id, status, summary)

    def spawn(
        self,
        prompt: str,
        agent_type: str = "general",
        endpoint_name: str | None = None,
    ) -> str:
        """Create a background agent and schedule it. Returns agent_id immediately."""
        agent_id = _gen_id()

        # Resolve endpoint
        ep_name = endpoint_name or next(iter(self._endpoints), None)
        if ep_name is None or ep_name not in self._endpoints:
            raise ValueError("No model endpoint registered — POST /endpoints first")

        # Resolve agent type config
        type_cfg: AgentTypeConfig = AGENT_TYPES.get(agent_type, AGENT_TYPES["general"])

        # Persist initial status
        status = self._store.create(
            agent_id=agent_id,
            agent_type=agent_type,
            prompt=prompt,
            endpoint=ep_name,
        )

        # Live entry with event queue for SSE
        agent = {
            "id": agent_id,
            "status": status,
            "queue": asyncio.Queue(),
            "cancel": asyncio.Event(),
        }
        _LIVE_AGENTS[agent_id] = agent

        # Schedule background task
        asyncio.ensure_future(self._run(agent, type_cfg, ep_name))
        log.info("Spawned agent %s (type=%s endpoint=%s)", agent_id, agent_type, ep_name)
        return agent_id

    async def _run(self, agent: dict, type_cfg: AgentTypeConfig, ep_name: str) -> None:
        agent_id = agent["id"]
        queue: asyncio.Queue = agent["queue"]

        async def emit(event_type: str, data: dict) -> None:
            data["timestamp"] = datetime.now(timezone.utc).isoformat()
            data["event_type"] = event_type
            await queue.put(data)

        await emit("status", {"status": "running"})
        self._store.update(agent_id, status="running",
                           started_at=datetime.now(timezone.utc).isoformat())

        ep = self._endpoints.get(ep_name)
        if not ep:
            await emit("error", {"message": f"Endpoint '{ep_name}' no longer registered"})
            self._store.update(agent_id, status="failed",
                               error=f"Endpoint '{ep_name}' not found",
                               completed_at=datetime.now(timezone.utc).isoformat())
            await emit("done", {"status": "failed"})
            return

        # Build prompt with type prefix + governance
        full_prompt = type_cfg.prompt_prefix + agent["status"].prompt

        url = ep["url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if ep.get("api_key"):
            headers["Authorization"] = f"Bearer {ep['api_key']}"

        payload = {
            "model": ep.get("model", ep_name),
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": full_prompt},
            ],
            "temperature": ep.get("temperature", 0.7),
            "max_tokens": ep.get("max_tokens", 4096),
        }

        try:
            timeout = aiohttp.ClientTimeout(total=ep.get("timeout_seconds", 300))
            async with self._http.post(
                url, json=payload, headers=headers, timeout=timeout, ssl=False
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Endpoint returned {resp.status}: {body[:200]}")

                data = await resp.json()
                choices = data.get("choices", [])
                output = choices[0]["message"]["content"] if choices else "(no response)"

            await emit("output", {"text": output})
            self._store.update(
                agent_id,
                status="completed",
                output=output,
                output_chars=len(output),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            await emit("done", {"status": "completed", "output_chars": len(output)})
            log.info("Agent %s completed (%d chars)", agent_id, len(output))

            if self._notify:
                try:
                    await self._notify(agent_id, "completed", output[:200])
                except Exception as e:
                    log.warning("Notify callback failed: %s", e)

        except asyncio.CancelledError:
            self._store.update(agent_id, status="interrupted",
                               completed_at=datetime.now(timezone.utc).isoformat())
            await emit("done", {"status": "interrupted"})

        except Exception as exc:
            log.error("Agent %s failed: %s", agent_id, exc)
            self._store.update(
                agent_id,
                status="failed",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            await emit("error", {"message": str(exc)})
            await emit("done", {"status": "failed"})

        finally:
            # Remove from live registry after a short delay (allows last SSE reads)
            await asyncio.sleep(30)
            _LIVE_AGENTS.pop(agent_id, None)

    @staticmethod
    def get_live(agent_id: str) -> dict | None:
        return _LIVE_AGENTS.get(agent_id)

    @staticmethod
    def cancel(agent_id: str) -> bool:
        agent = _LIVE_AGENTS.get(agent_id)
        if agent:
            agent["cancel"].set()
            return True
        return False
