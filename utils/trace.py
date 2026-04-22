"""Run-trace recorder. Every agent invocation produces a JSON trace file.

Logs every agent invocation: which tools were called, with what args, what they
returned, how long they took, and the final output. Stored as JSON so it's easy to
grep, replay, or pipe into evals. The eval harness grades runs against these traces
rather than against the final text message.

Usage:
    tracer = Tracer.start(agent="deal_checker", input="...")
    tracer.tool_call("lookup_msrp", args, result)
    tracer.final(final_text)
    tracer.save()  # writes bmw-agents/traces/{timestamp}_{agent}_{id}.json
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

TRACES_DIR = Path(__file__).parent.parent / "traces"
TRACES_DIR.mkdir(exist_ok=True)


@dataclass
class ToolEvent:
    name: str
    args: dict
    result: dict
    duration_ms: int
    ts_ms: int


@dataclass
class Trace:
    id: str
    agent: str
    started_at: float
    input: str
    model: str = ""
    tool_events: list[ToolEvent] = field(default_factory=list)
    turns: int = 0
    final_output: str = ""
    error: str | None = None
    ended_at: float | None = None

    @property
    def duration_s(self) -> float:
        end = self.ended_at or time.time()
        return round(end - self.started_at, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_s"] = self.duration_s
        return d


class Tracer:
    """Thin wrapper with context-manager semantics and auto-save."""

    def __init__(self, agent: str, input_text: str, model: str = ""):
        self.trace = Trace(
            id=uuid.uuid4().hex[:10],
            agent=agent,
            started_at=time.time(),
            input=input_text[:2000],
            model=model,
        )
        self._tool_started_at: float | None = None

    @classmethod
    def start(cls, agent: str, input: str, model: str = "") -> "Tracer":
        return cls(agent, input, model)

    def tool_call(self, name: str, args: dict, result: dict, duration_ms: int = 0) -> None:
        self.trace.tool_events.append(ToolEvent(
            name=name,
            args=_safe(args),
            result=_safe(result),
            duration_ms=duration_ms,
            ts_ms=int(time.time() * 1000),
        ))

    def turn(self) -> None:
        self.trace.turns += 1

    def final(self, text: str) -> None:
        self.trace.final_output = (text or "")[:6000]

    def error(self, err: str) -> None:
        self.trace.error = err

    def save(self) -> str:
        self.trace.ended_at = time.time()
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.trace.started_at))
        path = TRACES_DIR / f"{ts}_{self.trace.agent}_{self.trace.id}.json"
        path.write_text(json.dumps(self.trace.to_dict(), indent=2, default=str))
        return str(path)


def _safe(obj):
    """Truncate long strings so traces stay readable."""
    try:
        s = json.dumps(obj, default=str)
        if len(s) > 4000:
            return {"_truncated": True, "preview": s[:1500] + "... [truncated]"}
        return json.loads(s)
    except Exception:
        return {"_unserializable": str(type(obj))}


def list_traces(agent: str | None = None, limit: int = 50) -> list[dict]:
    """Return most-recent trace summaries. Used by the web UI."""
    files = sorted(TRACES_DIR.glob("*.json"), reverse=True)
    out: list[dict] = []
    for p in files[: limit * 2]:
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if agent and data.get("agent") != agent:
            continue
        out.append({
            "id": data.get("id"),
            "agent": data.get("agent"),
            "started_at": data.get("started_at"),
            "duration_s": data.get("duration_s"),
            "turns": data.get("turns"),
            "tool_count": len(data.get("tool_events", [])),
            "input": (data.get("input") or "")[:120],
            "error": data.get("error"),
            "path": p.name,
        })
        if len(out) >= limit:
            break
    return out


def load_trace(filename: str) -> dict | None:
    p = TRACES_DIR / os.path.basename(filename)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None
