"""In-memory job queue pre async batch runy.

Pre produkčný multi-instance deploy by sa malo nahradiť Redis + RQ alebo Celery.
Pre MVP stačí in-process dict + FastAPI BackgroundTasks.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional


@dataclass
class JobRecord:
    job_id: str
    status: Literal["queued", "running", "done", "error"] = "queued"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress_pct: float = 0
    result: Optional[Any] = None
    error_message: Optional[str] = None


class JobQueue:
    """Thread-safe in-memory store."""

    def __init__(self, max_history: int = 200) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._max_history = max_history

    def create(self) -> JobRecord:
        with self._lock:
            jid = str(uuid.uuid4())
            rec = JobRecord(job_id=jid)
            self._jobs[jid] = rec
            self._order.append(jid)
            self._cleanup()
            return rec

    def update(self, job_id: str, **fields) -> Optional[JobRecord]:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return None
            for k, v in fields.items():
                setattr(rec, k, v)
            return rec

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_all(self) -> list[JobRecord]:
        with self._lock:
            return [self._jobs[j] for j in self._order]

    def _cleanup(self) -> None:
        """Drop najstaršie ak prekročí max_history."""
        while len(self._order) > self._max_history:
            old = self._order.pop(0)
            self._jobs.pop(old, None)


# Globálna singleton inštancia
_QUEUE: Optional[JobQueue] = None


def get_queue() -> JobQueue:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = JobQueue()
    return _QUEUE
