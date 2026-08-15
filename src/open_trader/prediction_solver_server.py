"""One runtime-owned, bounded pair of reusable native solver servers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future
from queue import Empty, Queue
from threading import BoundedSemaphore, RLock, Thread

from open_trader.prediction_solver_worker import (
    WorkerCleanupError,
    WorkerHarness,
    WorkerOutcome,
    WorkerRequest,
)


_WORKER_COUNT = 2


class SolverServerBusy(RuntimeError):
    """The bounded shared solver queue has no capacity."""


class SolverServerUnavailable(RuntimeError):
    """A worker cleanup failure makes the shared owner fail closed."""


class SolverServerOwner:
    """Dispatch canonical solver requests across exactly two serial harnesses."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        max_pending: int = 2,
        harness_factory: Callable[[Sequence[str]], WorkerHarness] = WorkerHarness,
    ) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("solver command must be non-empty")
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 0:
            raise ValueError("max_pending must be a non-negative integer")
        self._command = tuple(command)
        self._harness_factory = harness_factory
        self._queue: Queue[tuple[WorkerRequest, Future[WorkerOutcome]] | None] = Queue()
        self._capacity = BoundedSemaphore(_WORKER_COUNT + max_pending)
        self._lock = RLock()
        self._threads: list[Thread] = []
        self._harnesses: list[WorkerHarness | None] = [None] * _WORKER_COUNT
        self._closed = False
        self._fatal: BaseException | None = None
        self._start_locked()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def worker_start_counts(self) -> tuple[int, int]:
        with self._lock:
            return tuple(0 if harness is None else harness.start_count for harness in self._harnesses)  # type: ignore[return-value]

    def submit(self, request: WorkerRequest) -> Future[WorkerOutcome]:
        if not isinstance(request, WorkerRequest):
            raise ValueError("solver server accepts WorkerRequest")
        with self._lock:
            if self._closed:
                raise SolverServerUnavailable("solver server is closed")
            if self._fatal is not None:
                raise SolverServerUnavailable("solver server cleanup was not proven") from self._fatal
            self._start_locked()
            if not self._capacity.acquire(blocking=False):
                raise SolverServerBusy("solver server queue is full")
            result: Future[WorkerOutcome] = Future()
            self._queue.put((request, result))
            return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_pending_locked()
            threads = tuple(self._threads)
            for _ in threads:
                self._queue.put(None)
        for thread in threads:
            thread.join()
        with self._lock:
            if self._fatal is not None:
                raise SolverServerUnavailable("solver server cleanup was not proven") from self._fatal

    def _start_locked(self) -> None:
        if self._threads:
            return
        for index in range(_WORKER_COUNT):
            thread = Thread(target=self._run, args=(index,), name=f"prediction-solver-{index + 1}")
            thread.start()
            self._threads.append(thread)

    def _run(self, index: int) -> None:
        try:
            harness = self._harness_factory(self._command)
            harness.start()
            with self._lock:
                self._harnesses[index] = harness
            while True:
                item = self._queue.get()
                if item is None:
                    return
                request, result = item
                try:
                    outcome = harness.submit(request)
                    if not outcome.cleanup_proven:
                        self._fail_closed(WorkerCleanupError("worker cleanup was not proven"))
                    if not result.done():
                        result.set_result(outcome)
                except BaseException as exc:
                    if not result.done():
                        result.set_exception(exc)
                finally:
                    self._capacity.release()
        except BaseException as exc:
            self._fail_closed(exc)
            raise
        finally:
            harness = self._harnesses[index]
            if harness is not None:
                try:
                    harness.close()
                except WorkerCleanupError as exc:
                    self._fail_closed(exc)

    def _cancel_pending_locked(self) -> None:
        sentinels = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is None:
                sentinels += 1
                continue
            _request, result = item
            if not result.done():
                result.set_exception(SolverServerUnavailable("solver server is closing"))
            self._capacity.release()
        for _ in range(sentinels):
            self._queue.put(None)

    def _fail_closed(self, error: BaseException) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = error
                self._cancel_pending_locked()
