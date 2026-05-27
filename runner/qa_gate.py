"""qa-harness client + circuit breaker for agent-001.

POSTs subject material to qa-harness `/review`, returns the verdict, and
trips a circuit breaker after consecutive failures so a sick reviewer
does not stall agent processing forever. When the breaker is open,
review() returns a synthetic `advisory` verdict immediately.

Verdict shape (from qa-harness):
    {
      "verdict": "pass" | "fail" | "warn",
      "score": 0.0-1.0,
      "rubric": {...},
      "reason": "...",
      "hard_fails": [...],
      "reviewer_model": "...",
      "reviewer_name": "...",
      "duration_ms": float,
      "request_id": "..."
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger("agent-001.qa_gate")


class QAGate:
    """Thin client for qa-harness with a consecutive-failure circuit breaker."""

    def __init__(
        self,
        url: str,
        enabled: bool,
        timeout_seconds: float,
        circuit_breaker_threshold: int,
        cool_off_seconds: float = 300.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.enabled = enabled
        self.timeout = timeout_seconds
        self.threshold = max(1, circuit_breaker_threshold)
        self.cool_off = cool_off_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def open(self) -> bool:
        """True if breaker is open (skip the reviewer)."""
        if self._opened_at is None:
            return False
        if (time.monotonic() - self._opened_at) >= self.cool_off:
            log.info("QA breaker cool-off elapsed — half-open")
            self._opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.threshold and self._opened_at is None:
            log.warning(
                "QA breaker tripped after %d consecutive failures — entering cool-off (%.0fs)",
                self._consecutive_failures, self.cool_off,
            )
            self._opened_at = time.monotonic()

    def _record_success(self) -> None:
        if self._consecutive_failures or self._opened_at:
            log.info("QA reviewer recovered")
        self._consecutive_failures = 0
        self._opened_at = None

    async def review(
        self,
        session: aiohttp.ClientSession,
        subject_type: str,
        subject_text: str,
        context: str,
        metadata: dict[str, Any] | None = None,
        reviewer: str | None = None,
    ) -> dict:
        """Post a subject for review. Returns a verdict dict.

        Returns an `advisory` verdict (verdict='warn', advisory=True) when
        QA is disabled, the breaker is open, or the call fails — never
        raises, so the caller can decide policy uniformly.
        """
        if not self.enabled:
            return self._advisory("qa_disabled", "QA harness disabled via QA_HARNESS_ENABLED=false")
        if self.open:
            return self._advisory("qa_breaker_open", "QA breaker open — passing through advisory")

        body = {
            "subject_type": subject_type,
            "subject_text": subject_text,
            "context": context,
            "metadata": metadata or {},
        }
        if reviewer:
            body["reviewer"] = reviewer

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with session.post(f"{self.url}/review", json=body, timeout=timeout) as resp:
                data = await resp.json()
                if resp.status >= 500:
                    self._record_failure()
                    return self._advisory("qa_http_5xx", f"qa-harness HTTP {resp.status}: {data.get('reason', '')[:160]}")
                self._record_success()
                return data
        except asyncio.TimeoutError:
            self._record_failure()
            return self._advisory("qa_timeout", f"qa-harness exceeded {self.timeout}s timeout")
        except aiohttp.ClientError as e:
            self._record_failure()
            return self._advisory("qa_unreachable", f"qa-harness client error: {e}")
        except (json.JSONDecodeError, KeyError) as e:
            self._record_failure()
            return self._advisory("qa_response_invalid", f"qa-harness response unparseable: {e}")

    @staticmethod
    def _advisory(reason_code: str, message: str) -> dict:
        return {
            "verdict": "warn",
            "advisory": True,
            "reason_code": reason_code,
            "reason": message,
            "score": None,
            "rubric": None,
            "hard_fails": [],
        }


def should_block(verdict: dict, block_on_fail: bool) -> bool:
    """Apply policy: block on fail (when configured), pass otherwise."""
    if verdict.get("advisory"):
        return False  # fall-through when QA itself is unhealthy
    if block_on_fail and verdict.get("verdict") == "fail":
        return True
    return False
