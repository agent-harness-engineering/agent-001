"""Persist agent status to memory/agents/{agent_id}.json."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class AgentStatus:
    agent_id: str
    agent_type: str
    prompt: str
    endpoint: str
    status: str          # queued | running | completed | failed | interrupted
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    output: str | None = None   # final response text
    output_chars: int = 0
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("events", None)   # events not persisted to disk (SSE only)
        return d


class StatusStore:
    def __init__(self, agents_dir: Path) -> None:
        self._dir = agents_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, agent_id: str) -> Path:
        return self._dir / f"{agent_id}.json"

    def create(self, agent_id: str, agent_type: str, prompt: str, endpoint: str) -> AgentStatus:
        status = AgentStatus(
            agent_id=agent_id,
            agent_type=agent_type,
            prompt=prompt,
            endpoint=endpoint,
            status="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._write(status)
        return status

    def update(self, agent_id: str, **kwargs) -> AgentStatus | None:
        status = self.load(agent_id)
        if status is None:
            return None
        for k, v in kwargs.items():
            if hasattr(status, k):
                setattr(status, k, v)
        self._write(status)
        return status

    def load(self, agent_id: str) -> AgentStatus | None:
        path = self._path(agent_id)
        if not path.exists():
            return None
        try:
            with self._lock:
                data = json.loads(path.read_text())
            data.setdefault("events", [])
            return AgentStatus(**data)
        except Exception:
            return None

    def list_all(self) -> list[AgentStatus]:
        results = []
        for p in sorted(self._dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                data = json.loads(p.read_text())
                data.setdefault("events", [])
                results.append(AgentStatus(**data))
            except Exception:
                continue
        return results

    def _write(self, status: AgentStatus) -> None:
        path = self._path(status.agent_id)
        with self._lock:
            path.write_text(json.dumps(status.to_dict(), indent=2))
