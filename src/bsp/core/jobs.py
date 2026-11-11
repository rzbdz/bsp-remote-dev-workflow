"""Background jobs: long operations (boot, flash) run in a thread and expose
progress lines + a final status, so CLI users and agents can follow them and
get an authoritative result instead of holding one HTTP request open."""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import NotFound, BspError
from .events import EventBus


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "running"            # running | succeeded | failed
    created: float = field(default_factory=time.time)
    finished: float | None = None
    lines: list[str] = field(default_factory=list)
    result: Any = None
    error: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    progress: int | None = None        # percent done, when the job's body reports it

    def view(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
            "created": self.created, "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.created, 1),
            "progress": self.progress, "lines": self.lines[since:], "next": len(self.lines),
            "result": self.result, "error": self.error, "meta": self.meta,
        }


class JobLog:
    """Handed to job bodies: ``log("...")`` appends a timestamped progress line."""

    def __init__(self, job: Job, bus: EventBus):
        self._job, self._bus = job, bus

    def __call__(self, msg: str) -> None:
        line = f"[{time.time() - self._job.created:6.1f}s] {msg}"
        self._job.lines.append(line)
        self._bus.publish("job.log", job=self._job.id, line=line)

    def progress(self, percent: float) -> None:
        """Set how far the job is (0-100); `job get` and `job list` show it."""
        pct = round(max(0.0, min(100.0, float(percent))))
        if pct != self._job.progress:
            self._job.progress = pct
            self._bus.publish("job.progress", job=self._job.id, progress=pct)


class JobManager:
    def __init__(self, bus: EventBus, keep: int = 200):
        self.bus = bus
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def start(self, kind: str, title: str, body: Callable[[JobLog], Any], **meta: Any) -> Job:
        job = Job(f"{kind}-{int(time.time())}-{next(self._seq)}", kind, title, meta=meta)
        with self._lock:
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j.created)[:-self.keep]:
                if old.status != "running":
                    del self._jobs[old.id]
        self.bus.publish("job.started", job=job.id, kind=kind, title=title, **meta)

        def run() -> None:
            log = JobLog(job, self.bus)
            try:
                job.result = body(log)
                job.status = "succeeded"
                if job.progress is not None:
                    job.progress = 100
            except BspError as e:
                job.error, job.status = e.to_dict(), "failed"
                log(f"FAILED: {e.message}" + (f" (hint: {e.hint})" if e.hint else ""))
            except Exception as e:  # pragma: no cover - defensive
                job.error = {"code": "internal_error", "message": str(e), "details": traceback.format_exc()[-2000:]}
                job.status = "failed"
                log(f"FAILED: {e}")
            finally:
                job.finished = time.time()
                self.bus.publish("job.finished", job=job.id, status=job.status, error=job.error)

        threading.Thread(target=run, name=job.id, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise NotFound(f"no job {job_id!r}", hint="job ids are printed when a job starts; `bsp job list` shows recent ones") from None

    def wait(self, job_id: str, timeout: float) -> Job:
        job = self.get(job_id)
        deadline = time.time() + timeout
        while job.status == "running" and time.time() < deadline:
            time.sleep(0.2)
        return job

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
