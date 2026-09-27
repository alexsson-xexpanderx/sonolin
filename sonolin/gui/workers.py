"""Keeping blocking work off the Qt thread.

Every call into SoCo is a synchronous HTTP round trip to a speaker on wifi, so
any of them can take hundreds of milliseconds and a missing speaker can take the
full timeout. Doing that on the GUI thread freezes the window, which is exactly
the problem noson-app solves in C++ with its `Future`/`Promise` pair on a
`QThreadPool`. This is the same shape in Python.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

log = logging.getLogger(__name__)


class _Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class Job(QRunnable):
    """Run `fn` on the pool; deliver the result back on the GUI thread."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            log.debug("job failed:\n%s", traceback.format_exc())
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.signals.done.emit(result)


def run(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> Job:
    """Fire `fn` in the background.

    `on_done` and `on_error` are invoked on the GUI thread, so they may touch
    widgets directly.
    """
    job = Job(fn, *args, **kwargs)
    if on_done is not None:
        job.signals.done.connect(on_done)
    if on_error is not None:
        job.signals.failed.connect(on_error)
    QThreadPool.globalInstance().start(job)
    return job
