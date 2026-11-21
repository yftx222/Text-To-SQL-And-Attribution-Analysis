"""Process-local counters for completed background analysis runs."""

from threading import Lock
from dataagent.engines.common.observability import evaluation

_lock = Lock()
_counts = {"completed": 0, "failed": 0, "cancelled": 0}
_seconds = dict.fromkeys(_counts, 0.0)
_model_requests = {"ok": 0, "rate_limited": 0, "timeout": 0, "unavailable": 0, "other": 0}


def record(outcome: str, elapsed: float) -> None:
    with _lock:
        _counts[outcome] += 1
        _seconds[outcome] += elapsed


def record_model_request(outcome: str) -> None:
    with _lock:
        _model_requests[outcome] += 1
    evaluation.emit("model_http", outcome=outcome)


def render() -> str:
    with _lock:
        return "\n".join([
            "# TYPE dataagent_analysis_runs_total counter",
            *(f'dataagent_analysis_runs_total{{outcome="{name}"}} {count}' for name, count in _counts.items()),
            "# TYPE dataagent_analysis_run_seconds_total counter",
            *(f'dataagent_analysis_run_seconds_total{{outcome="{name}"}} {seconds}' for name, seconds in _seconds.items()),
            "# TYPE aiops_dependency_requests_total counter",
            *(f'aiops_dependency_requests_total{{dependency="llm",outcome="{name}"}} {count}'
              for name, count in _model_requests.items()),
        ]) + "\n"
