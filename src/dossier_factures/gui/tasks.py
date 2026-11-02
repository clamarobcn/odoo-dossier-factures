"""Run slow work outside the interface thread and hand results back to it."""

import queue
import threading
from collections.abc import Callable
from typing import Any


class TaskRunner:
    """Run one task at a time. ``poll`` must be called regularly from the interface thread."""

    def __init__(self) -> None:
        self._events: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.cancel = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        work: Callable[[Callable[..., None], threading.Event], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
        on_progress: Callable[[str, int | None, int | None], None] = lambda *_: None,
    ) -> None:
        self.cancel = threading.Event()
        cancel = self.cancel

        def progress(message: str, current: int | None = None, total: int | None = None) -> None:
            self._events.put((on_progress, (message, current, total), False))

        def run() -> None:
            try:
                result = work(progress, cancel)
            except BaseException as error:
                self._events.put((on_error, (error,), True))
            else:
                self._events.put((on_done, (result,), True))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def poll(self) -> None:
        while True:
            try:
                handler, args, final = self._events.get_nowait()
            except queue.Empty:
                return
            if final:
                # The handler may start another task, and must not see this one as running.
                self._thread.join()
            handler(*args)

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
