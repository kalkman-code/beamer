"""What a crash leaves behind, for both apps: an exception that ends a worker thread goes to the log,
and on macOS a fatal signal writes every thread's Python stack beside it.

Without this a thread's uncaught exception went to stderr, which a bundled app has nowhere to show,
and a native crash left only the system's report, which names the C frames but not the Python
line that called into them (06-10-2026: a crash under rapid switching on an Intel Mac, with nothing
in Beamer.log to say where).
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading

# Held for the life of the process: faulthandler writes to the descriptor, not the object.
_fault_file = None


def install(directory, logger=None, fatal_signals=None) -> None:
    """`directory` is where the app's log lives; `crash.log` is written there on a fatal signal.
    `fatal_signals` defaults to macOS only: on Windows faulthandler also reports access violations
    that the code raising them goes on to handle, and would fill the file with crashes that were not."""
    global _fault_file
    logger = logger or logging.getLogger("Beamer")

    def log_thread_exception(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else "unknown"
        logger.critical("thread %s ended on an uncaught exception", name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = log_thread_exception
    if fatal_signals is None:
        fatal_signals = sys.platform == "darwin"
    if not fatal_signals:
        return
    try:
        _fault_file = open(f"{directory}/crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file, all_threads=True)
    except (OSError, ValueError, RuntimeError):
        logger.exception("could not set up crash.log")
