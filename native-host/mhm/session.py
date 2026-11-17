"""Contain core processes within one native messaging connection."""

from __future__ import annotations
import functools
import os
import signal
import threading
from .errors import HostError


class BrowserSession:
    def __init__(self):
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.processes = set()

    def require_open(self):
        if self.closed.is_set():
            raise HostError("The browser session has ended", "CONFLICT")

    def guard_spawn(self, function):
        @functools.wraps(function)
        def guarded(*args, **kwargs):
            with self.lock:
                self.require_open()
                return function(*args, **kwargs)

        return guarded

    def track(self, process):
        with self.lock:
            self.require_open()
            self.processes.add(process)

    def release(self, process):
        with self.lock:
            self.processes.discard(process)

    def close(self):
        with self.lock:
            self.closed.set()
            processes = list(self.processes)
            self.processes.clear()
        # workers can be waiting on network io or a lifecycle lock
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
        for process in processes:
            try:
                process.wait(timeout=1)
            except Exception:
                pass
