"""Background jobs for the web interface.

Launching a cluster can take minutes on a cold cache, which is far too long for a request.
The UI starts a job, gets an id back, and polls it for progress. Jobs live in memory only —
the source of truth for what exists is always the instance registry on disk.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    id: str
    kind: str
    target: str
    status: str = "running"  # running | done | failed
    events: list[str] = field(default_factory=list)
    error: str | None = None
    detail: str | None = None
    result: Any = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            # A copy: the worker thread appends to this list under the lock, and the
            # response is serialised outside it.
            "events": list(self.events),
            "error": self.error,
            "detail": self.detail,
        }


class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, target: str, work: Callable[[Callable[[str], None]], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, target=target)
        with self._lock:
            self._jobs[job.id] = job

        def record(message: str) -> None:
            with self._lock:
                if not job.events or job.events[-1] != message:
                    job.events.append(message)

        def run() -> None:
            try:
                job.result = work(record)
            except Exception as failure:  # surfaced to the browser, not swallowed
                with self._lock:
                    job.status = "failed"
                    job.error = str(failure) or failure.__class__.__name__
                    job.detail = traceback.format_exc(limit=3)
            else:
                with self._lock:
                    job.status = "done"

        threading.Thread(target=run, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def prune(self, keep: int = 50) -> None:
        with self._lock:
            if len(self._jobs) <= keep:
                return
            finished = [job for job in self._jobs.values() if job.status != "running"]
            for job in finished[: len(self._jobs) - keep]:
                self._jobs.pop(job.id, None)
