"""Bounded uploads; only the coordinator updates caches and result counters."""

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Callable

from rich.progress import ProgressColumn
from rich.text import Text


@dataclass
class UploadOutcome:
    path: Path
    result: dict | None = None
    error: Exception | None = None


class UploadStats:
    def __init__(self) -> None:
        self.lock = Lock()
        self.started: float | None = None
        self.sent = 0
        self.active = 0
        self.request_seconds = 0.0
        self.transfer_seconds = 0.0
        self.response_seconds = 0.0
        self.measured = 0
        self.server_timings: dict[str, tuple[float, int]] = {}

    def progress(self, delta: int) -> None:
        with self.lock:
            self.sent += delta

    def timing(self, transfer: float, response: float, server: dict | None = None) -> None:
        with self.lock:
            self.transfer_seconds += transfer
            self.response_seconds += response
            self.measured += 1
            for name, seconds in (server or {}).items():
                total, count = self.server_timings.get(name, (0.0, 0))
                self.server_timings[name] = (total + seconds, count + 1)

    def display(self) -> str:
        with self.lock:
            elapsed = max(monotonic() - self.started, 0.001) if self.started else 0.001
            mib = self.sent / (1024 * 1024)
            return f"{self.active} active | sent {mib:.1f} MiB | avg {mib / elapsed:.2f} MiB/s"


class UploadRateColumn(ProgressColumn):
    def __init__(self, stats: UploadStats) -> None:
        super().__init__()
        self.stats = stats

    def render(self, task) -> Text:
        return Text(self.stats.display() if self.stats.started else "")


class UploadPipeline:
    def __init__(self, upload: Callable, workers: int, stats: UploadStats) -> None:
        self.upload = upload
        self.workers = workers
        self.stats = stats
        self.pending: dict[Future, Path] = {}
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nv-upload")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        # Let in-flight requests finish, including on interruption. Never leave
        # background uploads running after the command has returned.
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _upload(self, path: Path, kwargs: dict) -> dict:
        previous = 0
        started = monotonic()
        with self.stats.lock:
            if self.stats.started is None:
                self.stats.started = started
            self.stats.active += 1

        def progress(current: int, _total: int) -> None:
            nonlocal previous
            self.stats.progress(max(current - previous, 0))
            previous = current

        try:
            return self.upload(
                path, **kwargs, progress_callback=progress, timing_callback=self.stats.timing
            )
        finally:
            with self.stats.lock:
                self.stats.active -= 1
                self.stats.request_seconds += monotonic() - started

    def collect(self, *, block: bool = False) -> list[UploadOutcome]:
        if not self.pending:
            return []
        done, _ = wait(self.pending, timeout=None if block else 0, return_when=FIRST_COMPLETED)
        outcomes = []
        for future in done:
            path = self.pending.pop(future)
            try:
                outcomes.append(UploadOutcome(path, result=future.result()))
            except (RuntimeError, OSError, ValueError) as exc:
                outcomes.append(UploadOutcome(path, error=exc))
        return outcomes

    def submit(self, path: Path, **kwargs) -> list[UploadOutcome]:
        outcomes = self.collect(block=len(self.pending) >= self.workers)
        self.pending[self.executor.submit(self._upload, path, kwargs)] = path
        return outcomes

    def drain(self):
        while self.pending:
            yield from self.collect(block=True)
