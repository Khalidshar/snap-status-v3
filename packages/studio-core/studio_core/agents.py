from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any, Callable


def _hash(value: Any) -> str:
    return sha256(repr(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentRun:
    agent_id: str
    input_hash: str
    output_hash: str
    duration_ms: int
    ok: bool
    error: str | None = None


class AgentAdapter:
    """Wrap a legacy agent without changing its internal role or implementation."""

    def __init__(self, agent_id: str, runner: Callable[..., Any]):
        self.agent_id = agent_id
        self.runner = runner

    def run(self, *args: Any, **kwargs: Any) -> tuple[Any, AgentRun]:
        started = monotonic()
        input_hash = _hash((args, kwargs))
        try:
            result = self.runner(*args, **kwargs)
            record = AgentRun(self.agent_id, input_hash, _hash(result), int((monotonic()-started)*1000), True)
            return result, record
        except Exception as exc:
            record = AgentRun(self.agent_id, input_hash, "", int((monotonic()-started)*1000), False, str(exc))
            raise AgentExecutionError(record) from exc


class AgentExecutionError(RuntimeError):
    def __init__(self, run: AgentRun):
        super().__init__(run.error or "agent failed")
        self.run = run
