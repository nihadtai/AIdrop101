"""A local, dependency-free context brain for fast, observable LLM calls.

Telemetry intentionally records metrics and hashes, never raw prompts or model responses.
Recent responses live only in the in-memory exact-request cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any, Optional


@dataclass
class ContextEvent:
    timestamp: float
    agent: str
    operation: str
    model: str
    input_tokens_before: int
    input_tokens_sent: int
    output_tokens_estimate: int
    latency_ms: int
    cache_hit: bool
    context_trimmed: bool
    status: str
    error: Optional[str] = None


class AIContextBrain:
    """Deduplicates repeated calls, bounds context, and tracks every LLM interaction."""

    def __init__(
        self,
        input_char_budget: int = 24_000,
        cache_ttl_seconds: int = 300,
        max_events: int = 500,
        cache_size: int = 128,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.input_char_budget = max(1_000, input_char_budget)
        self.cache_ttl_seconds = max(0, cache_ttl_seconds)
        self.max_events = max(10, max_events)
        self.cache_size = max(1, cache_size)
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._events: deque[ContextEvent] = deque(maxlen=self.max_events)
        self._lock = Lock()

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Provider-independent conservative estimate suitable for dashboards.
        return max(1, (len(text or "") + 3) // 4)

    def prepare(self, prompt: str, system_instruction: str) -> tuple[str, str, bool]:
        """Normalize whitespace and retain the beginning and end of oversized context."""
        prompt = re.sub(r"[ \t]+\n", "\n", (prompt or "").strip())
        system_instruction = re.sub(r"[ \t]+\n", "\n", (system_instruction or "").strip())
        if not self.enabled:
            return prompt, system_instruction, False
        total = len(prompt) + len(system_instruction)
        if total <= self.input_char_budget:
            return prompt, system_instruction, False

        # Preserve system contract, then the task opening and freshest details at the end.
        system_budget = min(len(system_instruction), self.input_char_budget // 3)
        compact_system = system_instruction[:system_budget]
        prompt_budget = self.input_char_budget - len(compact_system)
        omission_notice = "\n\n[Earlier context omitted by AI Context Brain for token efficiency]\n\n"
        prompt_budget = max(1, prompt_budget - len(omission_notice))
        head = int(prompt_budget * 0.45)
        tail = prompt_budget - head
        compact_prompt = (
            prompt[:head]
            + omission_notice
            + prompt[-tail:]
        )
        return compact_prompt, compact_system, True

    @staticmethod
    def request_key(model: str, prompt: str, system_instruction: str, extra: Any = None) -> str:
        payload = json.dumps(
            {"model": model, "prompt": prompt, "system": system_instruction, "extra": extra},
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get_cached(self, key: str) -> Optional[str]:
        if not self.enabled or not self.cache_ttl_seconds:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._cache.get(key)
            if not item or now - item[0] > self.cache_ttl_seconds:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return item[1]

    def cache_response(self, key: str, response: str) -> None:
        if not self.enabled or not self.cache_ttl_seconds or not response:
            return
        with self._lock:
            self._cache[key] = (time.monotonic(), response)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def record(self, event: ContextEvent) -> None:
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = [asdict(event) for event in reversed(self._events)]
            cache_entries = len(self._cache)
        total_before = sum(event["input_tokens_before"] for event in events)
        total_sent = sum(event["input_tokens_sent"] for event in events)
        cache_hits = sum(1 for event in events if event["cache_hit"])
        return {
            "summary": {
                "calls": len(events),
                "cache_hits": cache_hits,
                "cache_hit_rate": round(cache_hits / len(events) * 100, 1) if events else 0,
                "estimated_input_tokens_saved": max(0, total_before - total_sent),
                "cache_entries": cache_entries,
                "input_char_budget": self.input_char_budget,
                "cache_ttl_seconds": self.cache_ttl_seconds,
                "enabled": self.enabled,
            },
            "events": events,
        }

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._events.clear()


_brain: Optional[AIContextBrain] = None


def get_context_brain() -> AIContextBrain:
    global _brain
    if _brain is None:
        _brain = AIContextBrain(
            input_char_budget=int(os.getenv("AI_CONTEXT_INPUT_CHAR_BUDGET", "24000")),
            cache_ttl_seconds=int(os.getenv("AI_CONTEXT_CACHE_TTL_SECONDS", "300")),
            max_events=int(os.getenv("AI_CONTEXT_MAX_EVENTS", "500")),
            enabled=os.getenv("AI_CONTEXT_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        )
    return _brain
