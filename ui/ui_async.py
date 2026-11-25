"""Thread-to-Tk marshaling helper for background tasks.

Worker threads never touch Tk widgets directly; they push results and progress
callbacks into a ``queue.SimpleQueue`` drained on the Tk main thread via
``widget.after()``. Polling runs only while work or callbacks are pending, and
all callbacks are silently dropped once the widget is destroyed or ``shutdown``
is called.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

_logger = logging.getLogger("anqicms.ui_async")


class BackgroundRunner:
    """Runs blocking callables on daemon worker threads and delivers UI callbacks safely."""

    def __init__(self, widget: tk.Misc, poll_ms: int = 50) -> None:
        self._widget = widget
        self._poll_ms = max(1, int(poll_ms))
        self._ui_thread = threading.current_thread()
        self._queue: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._in_flight = 0
        self._poll_job: str | None = None
        self._closed = False
        try:
            self._widget.bind("<Destroy>", self._on_widget_destroy, add="+")
        except (tk.TclError, AttributeError):
            pass

    @property
    def busy(self) -> bool:
        with self._lock:
            return (not self._closed) and self._in_flight > 0

    def _widget_exists(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self._widget.winfo_exists())
        except (tk.TclError, RuntimeError):
            return False

    def _on_widget_destroy(self, event) -> None:
        if getattr(event, "widget", None) is self._widget:
            self.shutdown()

    def _ensure_polling(self) -> None:
        with self._lock:
            if self._closed or self._poll_job is not None:
                return
            if not self._widget_exists():
                self._closed = True
                return
            try:
                self._poll_job = self._widget.after(self._poll_ms, self._drain)
            except (tk.TclError, RuntimeError):
                self._closed = True
                self._poll_job = None

    def post(self, callback: Callable[[], None]) -> None:
        """Schedule *callback* to run on the Tk thread; silently dropped if closed/destroyed."""
        if callback is None:
            return
        with self._lock:
            if self._closed:
                return
            self._queue.put(("post", callback))
            needs_poll = self._poll_job is None
        if needs_poll:
            if threading.current_thread() is self._ui_thread:
                self._ensure_polling()
            else:
                # Best-effort wakeup if post() was invoked from an external thread while idle.
                try:
                    self._ensure_polling()
                except Exception:
                    pass

    def make_progress(
        self,
        ui_cb: Callable[..., None] | None,
    ) -> Callable[..., None]:
        """Wrap *ui_cb* into a thread-safe progress callback that marshals to the Tk thread."""
        if ui_cb is None:
            return lambda *args, **kwargs: None

        def _progress(*args: Any, **kwargs: Any) -> None:
            if self._closed:
                return
            self.post(lambda: ui_cb(*args, **kwargs))

        return _progress

    def submit(
        self,
        func: Callable[[], T],
        on_success: Callable[[T], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> threading.Thread | None:
        """Execute *func* on a daemon worker thread and invoke *on_success*/*on_error* on Tk."""
        with self._lock:
            if self._closed or not self._widget_exists():
                self._closed = True
                return None
            self._in_flight += 1

        self._ensure_polling()

        def _worker() -> None:
            try:
                result = func()
            except Exception as exc:  # noqa: BLE001
                self._enqueue_completion(False, exc, on_success, on_error)
            else:
                self._enqueue_completion(True, result, on_success, on_error)

        thread = threading.Thread(target=_worker, name="sitegroup-ui-worker", daemon=True)
        thread.start()
        return thread

    def _enqueue_completion(
        self,
        ok: bool,
        payload: Any,
        on_success: Callable[[Any], None] | None,
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        with self._lock:
            if self._closed:
                self._in_flight = max(0, self._in_flight - 1)
                return
            self._queue.put(("done", ok, payload, on_success, on_error))

    def _drain_discard(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _drain(self) -> None:
        with self._lock:
            self._poll_job = None
            if self._closed or not self._widget_exists():
                self._closed = True
                self._in_flight = 0
                self._drain_discard()
                return

        while True:
            if self._closed or not self._widget_exists():
                with self._lock:
                    self._closed = True
                    self._in_flight = 0
                    self._drain_discard()
                return
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "post":
                callback = item[1]
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    _logger.exception("Unhandled exception in BackgroundRunner.post callback")
            elif kind == "done":
                _, ok, payload, on_success, on_error = item
                with self._lock:
                    self._in_flight = max(0, self._in_flight - 1)
                if ok:
                    if on_success is not None:
                        try:
                            on_success(payload)
                        except Exception:  # noqa: BLE001
                            _logger.exception(
                                "Unhandled exception in BackgroundRunner on_success callback"
                            )
                else:
                    if on_error is not None:
                        try:
                            on_error(payload)
                        except Exception:  # noqa: BLE001
                            _logger.exception(
                                "Unhandled exception in BackgroundRunner on_error callback"
                            )
                    else:
                        _logger.error("Unhandled background task error: %s", payload)

        with self._lock:
            still_pending = (not self._closed) and (
                self._in_flight > 0 or not self._queue.empty()
            )
        if still_pending and self._widget_exists():
            self._ensure_polling()

    def shutdown(self) -> None:
        """Stop polling and drop all queued/future callbacks."""
        with self._lock:
            self._closed = True
            self._in_flight = 0
            job = self._poll_job
            self._poll_job = None
            self._drain_discard()
        if job is not None:
            try:
                if self._widget.winfo_exists():
                    self._widget.after_cancel(job)
            except (tk.TclError, RuntimeError):
                pass
