"""Defer handled cancellation until a partition write and its journal finish.

Modified 2026-10-17. SIGKILL/TerminateProcess/power loss cannot be handled.
"""
from contextlib import contextmanager
import os
import signal
import subprocess
import sys


class CancelRequested(KeyboardInterrupt):
    def __init__(self, signum):
        super().__init__(f"Cancellation requested ({signal.Signals(signum).name})")


class OperationSignals:
    def __init__(self):
        self.depth = 0
        self.pending = None
        self.previous = {}

    def handle(self, signum, frame):
        self.pending = signum
        if self.depth:
            print("Cancellation deferred: waiting for the active write/cleanup and journal. "
                  "Do not disconnect USB.", file=sys.stderr, flush=True)
            return
        raise CancelRequested(signum)

    def __enter__(self):
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)
        for signum in signals:
            self.previous[signum] = signal.signal(signum, self.handle)
        return self

    def __exit__(self, *args):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)
        self.previous.clear()

    @contextmanager
    def defer(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def checkpoint(self):
        if self.pending is not None and not self.depth:
            signum, self.pending = self.pending, None
            raise CancelRequested(signum)

    def start_recovery(self):
        # Consume the request which caused recovery. A new request can still
        # cancel polling, or be deferred until the recovery write completes.
        self.pending = None

    def child_options(self):
        if not self.depth:
            return {}
        # Keep terminal Ctrl+C / process-group SIGTERM away from the writer.
        # Do not create a new visible console on Windows.
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}
